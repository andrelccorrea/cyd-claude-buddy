"""Bridge between Claude Code (terminal) hooks and a Hardware Buddy device.

Hook events arrive from buddy_hook.py over a Unix socket. buddyd keeps the
state of every open Claude Code session and sends it to the device as the
Hardware Buddy heartbeat snapshot (newline-delimited JSON, see
anthropics/claude-desktop-buddy REFERENCE.md) over the board's USB serial
port, or over Wi-Fi with --wifi. With --approvals, a pending
PermissionRequest is shown on the device and the tapped decision goes back
to the waiting hook.
"""
import argparse
import collections
import datetime
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import unicodedata

import serial

HEARTBEAT_S = 10
RECONNECT_S = 2
STALE_SESSION_S = 12 * 3600
# The firmware reads lines into a 1024-byte buffer and drops longer ones.
MAX_LINE_BYTES = 1000
# Field widths of the firmware's TamaState (src/data.h).
MSG_CHARS = 23
TOOL_CHARS = 19
HINT_CHARS = 43
ENTRY_CHARS = 80
MAX_ENTRIES = 6
PROMPT_ID_CHARS = 39
TURN_TEXT_CHARS = 600
NET_PORT = 7777
WIFI_CONFIG = os.path.expanduser("~/.config/cyd-buddy/wifi.json")
PORT_GLOBS = ("/dev/cu.usbserial-*", "/dev/cu.wchusbserial*")

# key=value or "Bearer xyz" secrets inside commands shown on the screen.
SECRET_RE = re.compile(
    r"((?:password|passwd|pwd|token|secret|api[_-]?key|authorization)[\"']?\s*[=:]\s*[\"']?|bearer\s+)\S+",
    re.IGNORECASE,
)


def ascii_line(text, limit):
    """Single-line ASCII text: the device font has no accents."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(text.split())[:limit]


def redact(text):
    return SECRET_RE.sub(lambda m: m.group(1) + "***", text or "")


def short_tool(tool):
    # mcp__plugin_linear_linear__save_issue -> save_issue
    return tool.rsplit("__", 1)[-1] if tool.startswith("mcp__") else tool


class Pending:
    def __init__(self, session_id, tool, hint):
        self.session_id = session_id
        self.tool = tool
        self.hint = hint
        self.decision = None
        self.created = time.time()
        self.done = threading.Event()


class Bridge:
    def __init__(self, approvals, wait_s):
        self.approvals = approvals
        self.wait_s = wait_s
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.sessions = {}  # session_id -> {"project", "state", "last"}
        self.entries = collections.deque(maxlen=MAX_ENTRIES)
        self.pending = collections.OrderedDict()  # prompt id -> Pending
        self.completed = False
        self.tokens = 0
        self.tokens_today = 0
        self.today = datetime.date.today()
        self.transcripts = {}  # path -> (offset, set of counted message ids)
        self.turn = None
        self.link = None
        self.last_error = None

    # ---- hook events -----------------------------------------------------

    def handle_event(self, ev):
        """Apply one hook event. Returns (prompt id, Pending) to wait on, or None."""
        sid = ev.get("session_id") or "?"
        name = ev.get("event")
        with self.lock:
            if name == "SessionEnd":
                self._drop_pending(sid)
                self.sessions.pop(sid, None)
                self.changed.set()
                return None
            s = self.sessions.setdefault(sid, {"project": "", "state": "idle", "last": 0})
            s["project"] = os.path.basename(ev.get("cwd") or "") or s["project"]
            s["last"] = time.time()
            # Async hooks arrive out of order, so only events that settle a
            # prompt clear it: its own tool finishing, a tool finishing well
            # after it was asked (it was denied in the terminal), or the turn
            # ending.
            if name in ("UserPromptSubmit", "Stop"):
                self._drop_pending(sid)
            elif name in ("PostToolUse", "PostToolUseFailure"):
                self._drop_pending(sid, tool_use_id=ev.get("tool_use_id", ""), older_than_s=2)
            if name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"):
                s["state"] = "waiting" if self._has_pending(sid) else "running"
            if name == "PreToolUse":
                self._add_entry(s["project"], ev.get("tool", ""), ev.get("hint", ""))
            elif name == "Stop":
                s["state"] = "idle"
                self.completed = True
                self._count_tokens(ev.get("transcript_path", ""))
                if ev.get("last_message"):
                    self.turn = ev["last_message"]
            elif name == "PermissionRequest":
                s["state"] = "waiting"
                pid = (ev.get("tool_use_id") or f"req-{time.time_ns()}")[:PROMPT_ID_CHARS]
                p = Pending(sid, short_tool(ev.get("tool", "")), redact(ev.get("hint", "")))
                self.pending[pid] = p
                self.changed.set()
                return (pid, p) if self.approvals else None
            self.changed.set()
        return None

    def _has_pending(self, sid):
        return any(p.session_id == sid for p in self.pending.values())

    def _drop_pending(self, sid, tool_use_id=None, older_than_s=None):
        now = time.time()
        for pid, p in list(self.pending.items()):
            if p.session_id != sid:
                continue
            if tool_use_id is not None and pid != tool_use_id[:PROMPT_ID_CHARS] and now - p.created < older_than_s:
                continue
            self.pending.pop(pid).done.set()
        s = self.sessions.get(sid)
        if s and s["state"] == "waiting" and not self._has_pending(sid):
            s["state"] = "running"

    def _add_entry(self, project, tool, hint):
        line = f"{time.strftime('%H:%M')} {project}: {short_tool(tool)} {redact(hint)}"
        self.entries.appendleft(ascii_line(line, ENTRY_CHARS))

    def _count_tokens(self, path):
        """Add output tokens of assistant messages appended since the last read."""
        if not path or not os.path.exists(path):
            return
        offset, seen = self.transcripts.get(path, (0, set()))
        today_str = datetime.date.today().isoformat()
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # partially written line; read it next time
                offset += len(raw)
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                m = o.get("message") or {}
                usage = m.get("usage")
                if o.get("type") != "assistant" or not usage or m.get("id") in seen:
                    continue
                # Each content block is its own line, repeating the message usage.
                seen.add(m.get("id"))
                out = usage.get("output_tokens", 0)
                self.tokens += out
                ts = o.get("timestamp", "")
                local_day = (
                    datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
                    if ts else today_str
                )
                if local_day == today_str:
                    self.tokens_today += out
        self.transcripts[path] = (offset, seen)

    def decide(self, pid, decision):
        with self.lock:
            p = self.pending.pop(pid, None)
            if not p:
                return
            p.decision = {"once": "allow", "deny": "deny"}.get(decision)
            s = self.sessions.get(p.session_id)
            if s:
                s["state"] = "running"
            p.done.set()
            self.changed.set()

    def link_error(self, err):
        """Log each distinct connection failure once, not every retry."""
        if err != self.last_error:
            print(f"device unavailable: {err}", flush=True)
            self.last_error = err

    def expire(self, pid):
        """The hook stopped waiting: hand the prompt back to the terminal."""
        with self.lock:
            if self.pending.pop(pid, None):
                self.changed.set()

    # ---- device output ---------------------------------------------------

    def snapshot(self):
        with self.lock:
            now = time.time()
            for sid in [k for k, s in self.sessions.items() if now - s["last"] > STALE_SESSION_S]:
                self.sessions.pop(sid)
            if datetime.date.today() != self.today:
                self.today, self.tokens_today = datetime.date.today(), 0
            states = [s["state"] for s in self.sessions.values()]
            snap = {
                "total": len(states),
                "running": states.count("running"),
                "waiting": states.count("waiting"),
                "completed": self.completed,
                "tokens": self.tokens,
                "tokens_today": self.tokens_today,
                "entries": list(self.entries),
            }
            self.completed = False
            first = next(iter(self.pending.values()), None)
            if first:
                snap["msg"] = ascii_line(f"approve: {first.tool}", MSG_CHARS)
                if self.approvals:
                    pid = next(iter(self.pending))
                    snap["prompt"] = {
                        "id": pid,
                        "tool": ascii_line(first.tool, TOOL_CHARS),
                        "hint": ascii_line(first.hint, HINT_CHARS),
                    }
            elif snap["running"]:
                snap["msg"] = ascii_line(f"{snap['running']} running", MSG_CHARS)
            else:
                snap["msg"] = "idle" if states else "no sessions"
            turn, self.turn = self.turn, None
        line = json.dumps(snap)
        while len(line.encode()) > MAX_LINE_BYTES and snap["entries"]:
            snap["entries"].pop()
            line = json.dumps(snap)
        lines = [line]
        if turn:
            text = ascii_line(redact(turn), TURN_TEXT_CHARS)
            lines.append(json.dumps({"evt": "turn", "role": "assistant", "content": [{"type": "text", "text": text}]}))
        return lines


def find_port(explicit):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for pattern in PORT_GLOBS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def open_serial(path):
    ser = serial.Serial()
    ser.port = path
    ser.baudrate = 115200
    ser.timeout = 0.5
    # Keep IO0 released so the board never lands in its bootloader. macOS
    # still pulses EN when the port opens, so the board restarts once here.
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


class SerialLink:
    """The board's USB serial port: plain JSON lines."""

    def __init__(self, port_arg):
        path = find_port(port_arg)
        if not path:
            raise OSError("no serial port")
        self.ser = open_serial(path)
        self.name = path

    def readline(self):
        raw = self.ser.readline()
        return raw if raw.startswith(b"{") else None

    def write(self, line):
        self.ser.write(line.encode() + b"\n")

    def close(self):
        self.ser.close()


def hmac_hex(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


class NetLink:
    """The board over Wi-Fi: mutual HMAC handshake, then every line is
    prefixed with HMAC(session key, dir|seq|json) (firmware net_bridge.h)."""

    def __init__(self, cfg):
        secret = bytes.fromhex(cfg["secret"])
        last = None
        for host in (cfg.get("host"), cfg.get("ip")):
            if not host:
                continue
            try:
                self.sock = socket.create_connection((host, cfg.get("port", NET_PORT)), timeout=3)
                break
            except OSError as e:
                last = e
        else:
            raise last or OSError("no host configured")
        self.name = f"{host}:{cfg.get('port', NET_PORT)}"
        self.buf = b""
        nh = secrets.token_hex(16)
        self.sock.sendall(json.dumps({"hello": nh}, separators=(",", ":")).encode() + b"\n")
        reply = json.loads(self._recv_line() or b"{}")
        nb = reply.get("hello", "")
        if not hmac.compare_digest(reply.get("mac", ""), hmac_hex(secret, f"board|{nh}|{nb}")):
            self.sock.close()
            raise OSError("board failed authentication")
        self.sock.sendall(json.dumps({"mac": hmac_hex(secret, f"host|{nh}|{nb}")}, separators=(",", ":")).encode() + b"\n")
        self.key = bytes.fromhex(hmac_hex(secret, f"key|{nh}|{nb}"))
        self.tx = self.rx = 0
        self.lock = threading.Lock()
        self.sock.settimeout(0.5)

    def _recv_line(self):
        """One line, None on timeout. A socket file object can't be used
        after a timeout, so lines are cut from a buffer here."""
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except TimeoutError:
                return None
            if not chunk:
                raise OSError("closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.rstrip(b"\r")

    def readline(self):
        raw = self._recv_line()
        if raw is None:
            return None
        mac, _, body = raw.partition(b" ")
        want = hmac_hex(self.key, f"b|{self.rx}|{body.decode(errors='replace')}")[:32]
        if not hmac.compare_digest(mac.decode(errors="replace"), want):
            raise OSError("line failed authentication")
        self.rx += 1
        return body

    def write(self, line):
        with self.lock:
            mac = hmac_hex(self.key, f"h|{self.tx}|{line}")[:32]
            self.tx += 1
            self.sock.sendall(f"{mac} {line}\n".encode())

    def close(self):
        self.sock.close()


def link_loop(bridge, open_link):
    while True:
        try:
            link = open_link()
        except (OSError, ValueError, serial.SerialException) as e:
            bridge.link_error(str(e))
            time.sleep(RECONNECT_S)
            continue
        print(f"device connected on {link.name}", flush=True)
        bridge.link = link
        now = time.time()
        write_lines(bridge, [json.dumps({"time": [int(now), time.localtime(now).tm_gmtoff]})])
        bridge.changed.set()
        try:
            while True:
                raw = link.readline()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("cmd") == "permission" and bridge.approvals:
                    bridge.decide(msg.get("id", ""), msg.get("decision"))
        except (serial.SerialException, OSError) as e:
            print(f"device disconnected: {e}", flush=True)
        bridge.link = None
        try:
            link.close()
        except OSError:
            pass
        time.sleep(RECONNECT_S)


def write_lines(bridge, lines):
    link = bridge.link
    if not link:
        return
    try:
        for line in lines:
            link.write(line)
    except (serial.SerialException, OSError):
        pass  # link_loop notices the drop and reconnects


def heartbeat_loop(bridge):
    while True:
        bridge.changed.wait(HEARTBEAT_S)
        bridge.changed.clear()
        write_lines(bridge, bridge.snapshot())


def handle_client(bridge, conn):
    with conn:
        conn.settimeout(2)
        try:
            ev = json.loads(conn.makefile().readline() or "{}")
        except (OSError, ValueError):
            return
        waiting = bridge.handle_event(ev)
        if not waiting:
            return
        pid, pending = waiting
        if not pending.done.wait(bridge.wait_s):
            bridge.expire(pid)
        try:
            conn.sendall((json.dumps({"decision": pending.decision}) + "\n").encode())
        except OSError:
            pass


def socket_loop(bridge, sock_path):
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_client, args=(bridge, conn), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port (default: first /dev/cu.usbserial-*)")
    ap.add_argument("--sock", default=os.path.expanduser(os.environ.get("CYD_BUDDY_SOCK", "~/.cache/cyd-buddy/buddy.sock")))
    ap.add_argument("--wifi", action="store_true", help=f"reach the board over Wi-Fi ({WIFI_CONFIG}, see wifi_setup.py)")
    ap.add_argument("--approvals", action="store_true", help="show Approve/Deny on the device")
    ap.add_argument("--wait", type=float, default=25, help="seconds to wait for a tap before the terminal prompt appears")
    args = ap.parse_args()
    bridge = Bridge(args.approvals, args.wait)
    if args.wifi:
        with open(WIFI_CONFIG) as f:
            cfg = json.load(f)
        open_link = lambda: NetLink(cfg)
    else:
        open_link = lambda: SerialLink(args.port)
    threading.Thread(target=link_loop, args=(bridge, open_link), daemon=True).start()
    threading.Thread(target=heartbeat_loop, args=(bridge,), daemon=True).start()
    print(f"buddyd listening on {args.sock} (approvals {'on' if args.approvals else 'off'})", flush=True)
    socket_loop(bridge, args.sock)


if __name__ == "__main__":
    main()
