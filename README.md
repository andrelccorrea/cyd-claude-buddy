# cyd-claude-buddy

Shows your terminal Claude Code sessions on an ESP32-2432S028R ("Cheap Yellow
Display") running the [claude-desktop-buddy-esp32](https://github.com/alinke/claude-desktop-buddy-esp32)
firmware, and optionally lets you approve or deny permission prompts by tapping it.

```
Claude Code hooks ──► buddy_hook.py ──(Unix socket)──► buddyd.py ──(USB serial, JSON lines)──► CYD
```

buddyd speaks the Hardware Buddy protocol
([REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md))
over the board's USB serial port, which the firmware reads alongside BLE. USB is used
on purpose: that firmware's BLE link is unencrypted.

## Setup

1. Flash the patched firmware (see Firmware below). The stock web-flasher build shows a
   mirrored, transposed image on this panel and has no Wi-Fi.
2. `python3 -m venv .venv && .venv/bin/pip install pyserial`
3. `.venv/bin/python install_hooks.py` adds the hooks to `~/.claude/settings.json`
   (keeps existing hooks, writes a timestamped backup). Undo with `--uninstall`.
4. Run the daemon: `./buddyctl start [--approvals] [--wifi]` (`stop`, `restart`, `status`, `log`).

Hooks exit silently when buddyd is not running, so Claude Code is unaffected.

## Firmware

`firmware/` is the fork (branch `tpm408-panel-and-wifi`, upstream PR alinke/claude-desktop-buddy-esp32#1) with two changes: the panel config for the TPM408-2.8
glass this board shipped with (env `cyd-tpm408`, `board_configs/esp32-2432s028r-tpm408/`: 320x240,
`offset_rotation = 7`, RGB) and a Wi-Fi bridge (`src/net_bridge.*`). Build and flash:

```
export PLATFORMIO_CORE_DIR=$PWD/.platformio
cd firmware && ../.venv/bin/pio run -e cyd-tpm408
# first install (blank board): the full image
../.venv/bin/esptool --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write-flash 0x0 .pio/build/cyd-tpm408/firmware.factory.bin
# updates: the app only; the full image blanks NVS (Wi-Fi setup, touch calibration)
../.venv/bin/esptool --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write-flash 0x10000 .pio/build/cyd-tpm408/firmware.bin
```

Plug the board straight into the Mac: behind a USB hub it browns out when the radio starts.

## Wi-Fi

`./buddyctl wifi-setup --ssid "Network"` (board on USB) reads the Wi-Fi password from the
macOS Keychain, generates a 32-byte shared secret and sends both over USB, the only channel
the firmware accepts them from. The board then joins the network, turns BLE off and listens
on port 7777 as `claude-buddy.local`; the host, IP and secret are saved to
`~/.config/cyd-buddy/wifi.json` (mode 600). Start with `./buddyctl start --wifi --approvals`.
`./buddyctl wifi-forget` undoes it.

Both ends prove they know the secret (HMAC challenge-response) and every line carries an
HMAC under a per-connection key, so nobody on the LAN can inject or alter a decision. The
traffic is not encrypted: someone on the same network can read the commands shown on screen.

## Approvals

`buddyd.py --approvals [--wait 25]` puts Approve/Deny on the screen for each
PermissionRequest. The terminal prompt appears only after the hook returns, so it shows
up after `--wait` seconds without a tap (keep it under 60). A missing tap, a
disconnected board or a stopped daemon never approves anything.

## Status line data

Model, effort, context use, session time and the 5h/7d rate limits only reach the status
line command, not hooks. To mirror them on the board (home screen limits and the SESSIONS
info page), add this right after the status line script reads its input:

```bash
input=$(cat)
buddy="$HOME/path/to/cyd-claude-buddy"
if [ -S "$HOME/.cache/cyd-buddy/buddy.sock" ]; then
  printf '%s' "$input" | "$buddy/.venv/bin/python" "$buddy/buddy_hook.py" --statusline >/dev/null 2>&1 &
fi
```

The forwarder detaches itself, so Claude Code cancelling a stale status line run doesn't
lose the update. Cost is deliberately not sent.

## What is shown

| Device field | Source |
|---|---|
| sessions / running / waiting | SessionStart, UserPromptSubmit, Stop, SessionEnd, PermissionRequest |
| recent entries | PreToolUse (secrets like `password=` are masked) |
| last reply | `last_assistant_message` from Stop |
| per-session repo, branch, model, effort, context, time; 5h/7d limits | status line JSON |
| tokens / today | output tokens read from the session transcripts |
