"""Put the board on Wi-Fi (or take it off with --forget), over USB.

    wifi_setup.py --ssid "My Network"     password read from the macOS Keychain
    wifi_setup.py --forget                back to USB/BLE only

Generates a fresh 32-byte shared secret, sends it with the credentials over
the USB serial port (the only channel the firmware accepts them from), waits
for the board to report its address, and saves host, ip and secret to
~/.config/cyd-buddy/wifi.json (mode 600) for `buddyd --wifi`. Stop buddyd
first: it holds the serial port.
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time

import buddyd

BOOT_WAIT_S = 30


def keychain_password(ssid):
    # macOS asks the user to allow access; the password never touches a prompt here.
    out = subprocess.run(
        ["security", "find-generic-password", "-D", "AirPort network password", "-a", ssid, "-w"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"no Keychain password for Wi-Fi network '{ssid}' (or access was denied)")
    return out.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ssid")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--port", help="serial port (default: first /dev/cu.usbserial-*)")
    args = ap.parse_args()
    if not args.forget and not args.ssid:
        ap.error("pass --ssid or --forget")

    path = buddyd.find_port(args.port)
    if not path:
        sys.exit("board not found on USB")
    if args.forget:
        cmd, secret = {"cmd": "wifi", "ssid": "", "pass": "", "secret": ""}, None
    else:
        secret = secrets.token_hex(32)
        cmd = {"cmd": "wifi", "ssid": args.ssid, "pass": keychain_password(args.ssid), "secret": secret}

    ser = buddyd.open_serial(path)
    time.sleep(3)  # opening the port restarted the board; let it boot
    ser.reset_input_buffer()
    ser.write(json.dumps(cmd).encode() + b"\n")
    deadline, acked, ip = time.time() + BOOT_WAIT_S, False, None
    while time.time() < deadline and not (acked and (ip or args.forget)):
        line = ser.readline().decode(errors="replace").strip()
        acked = acked or line.startswith('{"ack":"wifi"')
        m = re.match(r"\[net\] ip (\S+)", line)
        if m:
            ip = m.group(1)
    ser.close()
    if not acked:
        sys.exit("the board did not acknowledge; is it running the cyd-claude-buddy firmware?")

    cfg_path = buddyd.WIFI_CONFIG
    if args.forget:
        if os.path.exists(cfg_path):
            os.remove(cfg_path)
        print("board is back on USB/BLE; Wi-Fi config removed")
        return
    if not ip:
        sys.exit(f"credentials saved on the board, but it did not join '{args.ssid}' within {BOOT_WAIT_S}s")
    os.makedirs(os.path.dirname(cfg_path), mode=0o700, exist_ok=True)
    fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"host": "claude-buddy.local", "ip": ip, "port": buddyd.NET_PORT, "secret": secret}, f)
    print(f"board joined '{args.ssid}' at {ip}; run: buddyctl start --wifi --approvals")


if __name__ == "__main__":
    main()
