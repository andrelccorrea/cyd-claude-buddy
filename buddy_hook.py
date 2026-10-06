"""Claude Code hook client for the CYD desk buddy.

Claude Code runs this for each hook event with the event JSON on stdin. It
forwards a compact summary to buddyd over a Unix socket and exits. For
PermissionRequest it waits for the decision tapped on the device; with no
decision (approvals off, timeout, daemon down) it prints nothing, so the
normal terminal prompt appears. It never approves on its own and never
fails the hook: every error path exits 0 silently.
"""
import json
import os
import socket
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


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
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
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(SOCK_PATH)
        sock.sendall((json.dumps(msg) + "\n").encode())
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
