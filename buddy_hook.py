"""Claude Code hook client for the CYD desk buddy.

Claude Code runs this for each hook event with the event JSON on stdin. It
forwards a compact summary to buddyd over a Unix socket and exits. For
PermissionRequest it waits for the decision tapped on the device; with no
decision (approvals off, timeout, daemon down) it prints nothing, so the
normal terminal prompt appears. It never approves on its own and never
fails the hook: every error path exits 0 silently.

With --statusline it instead forwards the status line JSON (model, context,
rate limits) and returns at once; the status line script calls it that way.
"""
import json
import os
import socket
import subprocess
import sys
from urllib.parse import urlparse

SOCK_PATH = os.path.expanduser(os.environ.get("CYD_BUDDY_SOCK", "~/.cache/cyd-buddy/buddy.sock"))
CONNECT_TIMEOUT_S = 0.3
# Must stay below the hook's `timeout` in settings.json; buddyd answers first.
DECISION_TIMEOUT_S = 60
LAST_MESSAGE_CHARS = 600


def tool_hint(tool, tool_input):
    """One short line describing what the tool is about to touch."""
    if not isinstance(tool_input, dict):
        return ""
    if tool == "Bash":
        return tool_input.get("command", "")
    for key in ("file_path", "notebook_path"):
        if tool_input.get(key):
            return os.path.basename(tool_input[key])
    if tool_input.get("url"):
        u = urlparse(tool_input["url"])
        return u.netloc + u.path
    for key in ("pattern", "query", "description", "prompt"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key]
    for value in tool_input.values():
        if isinstance(value, str):
            return value
    return ""


def git_place(cwd):
    """(repo name, branch) the way the terminal status line shows them."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    def git(*args):
        try:
            out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=1, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""
    top = git("rev-parse", "--show-toplevel")
    return os.path.basename(top or cwd), git("symbolic-ref", "--short", "HEAD")


def status_message(data):
    """Summary of the JSON Claude Code passes to the status line command."""
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or ""
    repo, branch = git_place(cwd) if cwd else ("", "")
    ctx = data.get("context_window") or {}
    limits = data.get("rate_limits") or {}
    return {
        "event": "StatusLine",
        "session_id": data.get("session_id", ""),
        "cwd": cwd,
        "name": data.get("session_name") or repo,
        "branch": branch,
        "model": (data.get("model") or {}).get("display_name", ""),
        "effort": (data.get("effort") or {}).get("level", ""),
        "ctx_pct": ctx.get("used_percentage"),
        "ctx_tokens": ctx.get("total_input_tokens"),
        "ctx_size": ctx.get("context_window_size"),
        "duration_ms": (data.get("cost") or {}).get("total_duration_ms"),
        "rl_5h": (limits.get("five_hour") or {}).get("used_percentage"),
        "rl_7d": (limits.get("seven_day") or {}).get("used_percentage"),
    }


def send(msg):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    sock.connect(SOCK_PATH)
    sock.sendall((json.dumps(msg) + "\n").encode())
    return sock


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if "--statusline" in sys.argv:
        # Claude Code kills an in-flight status line script when a newer
        # update arrives; detach so the forward finishes regardless.
        if os.fork():
            return
        os.setsid()
        try:
            send(status_message(event)).close()
        except OSError:
            pass
        return
    name = event.get("hook_event_name", "")
    tool = event.get("tool_name", "")
    msg = {
        "event": name,
        "session_id": event.get("session_id", ""),
        "cwd": event.get("cwd", ""),
        "transcript_path": event.get("transcript_path", ""),
        "tool": tool,
        "hint": tool_hint(tool, event.get("tool_input")),
        "tool_use_id": event.get("tool_use_id", ""),
        "last_message": (event.get("last_assistant_message") or "")[:LAST_MESSAGE_CHARS],
    }
    try:
        sock = send(msg)
        if name != "PermissionRequest":
            return
        sock.settimeout(DECISION_TIMEOUT_S)
        reply = json.loads(sock.makefile().readline() or "{}")
    except (OSError, ValueError):
        return
    decision = reply.get("decision")
    if decision == "allow":
        out = {"behavior": "allow"}
    elif decision == "deny":
        out = {"behavior": "deny", "message": "Denied from the desk buddy."}
    else:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}))


if __name__ == "__main__":
    main()
