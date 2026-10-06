"""Add (or with --uninstall, remove) the desk buddy hooks in ~/.claude/settings.json.

Existing hooks are kept; a timestamped backup is written before any change.
Running it twice leaves a single copy of each buddy hook.
"""
import json
import os
import shutil
import sys
import time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(HERE, ".venv", "bin", "python")
HOOK = os.path.join(HERE, "buddy_hook.py")
# Observational events never block Claude Code.
ASYNC_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
                "PostToolUse", "PostToolUseFailure", "Stop"]
# Covers buddyd's --wait plus margin; buddy_hook gives up first.
PERMISSION_TIMEOUT_S = 75


def is_buddy(hook):
    return hook.get("args") == [HOOK]


def strip(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        for group in hooks[event]:
            group["hooks"] = [h for h in group.get("hooks", []) if not is_buddy(h)]
        hooks[event] = [g for g in hooks[event] if g["hooks"]]
        if not hooks[event]:
            del hooks[event]


def main():
    uninstall = "--uninstall" in sys.argv
    with open(SETTINGS) as f:
        settings = json.load(f)
    backup = f"{SETTINGS}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(SETTINGS, backup)
    strip(settings)
    if not uninstall:
        hooks = settings.setdefault("hooks", {})
        base = {"type": "command", "command": PYTHON, "args": [HOOK]}
        for event in ASYNC_EVENTS:
            hooks.setdefault(event, []).append({"hooks": [{**base, "async": True, "timeout": 5}]})
        hooks.setdefault("PermissionRequest", []).append({"hooks": [{**base, "timeout": PERMISSION_TIMEOUT_S}]})
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    print(f"{'removed' if uninstall else 'installed'} buddy hooks; backup at {backup}")


if __name__ == "__main__":
    main()
