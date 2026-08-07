#!/usr/bin/env python3
"""Copy the firmware to the ESP32-S3 over USB. Works on Linux, macOS, Windows.

This is the cross-platform equivalent of deploy.sh. Same behaviour, same
checks, but it runs in PowerShell as well as a POSIX shell, and it can tell
the board's two USB ports apart by vendor id rather than by device name.

    pip install mpremote
    python tools/deploy.py                # auto-detect the port
    python tools/deploy.py --list         # show candidate ports and exit
    python tools/deploy.py --port COM5    # or name it
    python tools/deploy.py --port /dev/ttyACM0
    python tools/deploy.py --reset        # deploy, reboot, open the console

On Windows the ports are COM3, COM4 and so on, and there is no dialout group
to join. On Linux your user needs serial access once:

    sudo usermod -aG dialout $USER        # log out and back in
"""

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE = os.path.join(REPO, "firmware")
FILES = ("config.py", "garage.py", "main.py", "secrets.py")
PLACEHOLDER = "replace-me-with-a-long-random-string"

# Dev boards with two USB-C sockets expose two different things, and the
# device name does not tell you which is which: a CDC-class bridge like the
# CH343 enumerates as /dev/ttyACM0, exactly like the native USB peripheral
# would. The USB vendor id does tell you. Flash over the bridge: its DTR and
# RTS lines drive EN and GPIO 0, so esptool can enter the ROM bootloader on
# its own, and the port survives whatever the firmware does afterwards.
BRIDGE_VIDS = {
    0x1A86: "QinHeng/WCH (CH340, CH343)",
    0x10C4: "Silicon Labs CP210x",
    0x0403: "FTDI",
}
NATIVE_VIDS = {
    0x303A: "Espressif native USB",
}


def list_ports(usb_only=True):
    """Return [(device, vid, pid, description)], bridge ports first.

    Ports with no USB vendor id are filtered out by default. They are the
    machine's legacy serial hardware: a typical Linux box advertises 32 of
    them as /dev/ttyS0 through /dev/ttyS31, and a Windows box often has a
    motherboard COM1. None of them can ever be the board, and listing them
    buries the one port that matters.

    pyserial is a dependency of mpremote, so it is already installed. Guard
    the import anyway: a missing pyserial should degrade to "name the port
    yourself", not crash.
    """
    try:
        from serial.tools import list_ports as pyserial_ports
    except ImportError:
        return None

    found = []
    for port in pyserial_ports.comports():
        if usb_only and port.vid is None:
            continue
        found.append((port.device, port.vid, port.pid, port.description or ""))
    found.sort(key=lambda p: (p[1] not in BRIDGE_VIDS, p[0]))
    return found


def describe(vid):
    if vid in BRIDGE_VIDS:
        return "UART bridge, %s -- use this one" % BRIDGE_VIDS[vid]
    if vid in NATIVE_VIDS:
        return "%s -- no auto-reset, avoid" % NATIVE_VIDS[vid]
    return "unrecognised"


def print_ports(ports):
    if ports is None:
        print("pyserial not available, cannot enumerate ports.")
        print("Name the port yourself with --port.")
        return
    if not ports:
        print("No USB serial ports found.")
        print("If the board is plugged in, the cable may be charge-only:")
        print("plenty of USB-C cables carry power and no data. Swap it before")
        print("suspecting the board.")
        return
    for device, vid, pid, description in ports:
        ident = "%04x:%04x" % (vid, pid) if vid is not None else "unknown"
        print("  %-16s %-10s %s" % (device, ident, describe(vid)))
        if description and description.lower() not in ("n/a", device.lower()):
            print("  %-16s %-10s %s" % ("", "", description))


def choose_port(ports):
    """Pick the UART bridge port, or explain why we cannot."""
    if not ports:
        return None
    bridges = [p for p in ports if p[1] in BRIDGE_VIDS]
    if len(bridges) == 1:
        return bridges[0][0]
    if len(bridges) > 1:
        print("More than one UART bridge is plugged in:")
        print_ports(bridges)
        raise SystemExit("error: name the port with --port")
    return None


def mpremote(port, *args):
    """Run mpremote as a module so it works without the scripts dir on PATH.

    A pip install on Windows frequently leaves Scripts\\ off PATH, which makes
    the bare 'mpremote' command fail in a way that looks like the package is
    missing. Going through the interpreter that is already running sidesteps
    it entirely.
    """
    command = [sys.executable, "-m", "mpremote", "connect", port or "auto"]
    command.extend(args)
    return subprocess.call(command)


def check_secrets():
    secrets = os.path.join(FIRMWARE, "secrets.py")
    if not os.path.isfile(secrets):
        raise SystemExit(
            "error: %s does not exist.\n"
            "       Copy secrets.example.py to secrets.py and fill it in."
            % secrets
        )
    with open(secrets, "r", encoding="utf-8") as handle:
        if PLACEHOLDER in handle.read():
            raise SystemExit(
                "error: secrets.py still contains the placeholder API_TOKEN.\n"
                "       Generate one:\n"
                "       python -c \"import secrets;"
                " print(secrets.token_urlsafe(32))\""
            )


def dry_run_is_on():
    config = os.path.join(FIRMWARE, "config.py")
    with open(config, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip().replace(" ", "")
            if stripped.startswith("DRY_RUN=True"):
                return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyACM0")
    parser.add_argument(
        "--list", action="store_true", help="list candidate ports and exit"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reboot the board and open the serial console afterwards",
    )
    args = parser.parse_args(argv)

    ports = list_ports()

    if args.list:
        print_ports(ports)
        return 0

    try:
        import mpremote  # noqa: F401
    except ImportError:
        raise SystemExit("error: mpremote not found. pip install mpremote")

    check_secrets()

    port = args.port or (choose_port(ports) if ports is not None else None)
    if port is None and ports:
        print("No UART bridge port recognised. Candidates:")
        print_ports(ports)
        print("Falling back to mpremote's own auto-detection.")

    print("deploying to %s" % (port or "auto-detected port"))
    for name in FILES:
        print("  -> %s" % name)
        source = os.path.join(FIRMWARE, name)
        if mpremote(port, "fs", "cp", source, ":%s" % name) != 0:
            raise SystemExit("error: copying %s failed" % name)

    if dry_run_is_on():
        print()
        print("note: DRY_RUN is True. The relay pin will not be driven.")
        print("      Set DRY_RUN = False in firmware/config.py once the")
        print("      wiring is verified through step 4 of docs/hardware.md.")

    if args.reset:
        print()
        print("resetting board, Ctrl-] to exit the console")
        mpremote(port, "reset")
        mpremote(port, "repl")

    return 0


if __name__ == "__main__":
    sys.exit(main())
