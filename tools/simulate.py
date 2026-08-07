#!/usr/bin/env python3
"""Run the real firmware on a desktop against fake hardware.

This imports firmware/main.py unmodified, with stub machine/network/secrets
modules and the MicroPython-only time helpers patched in. It lets you exercise
every endpoint, the auth check and the relay pulse timing before anything is
wired up.

    ./tools/simulate.py                  # serves on http://127.0.0.1:8080
    GARAGE_HOST=127.0.0.1:8080 GARAGE_TOKEN=simulator-token ./tools/smoke_test.sh --pulse

Every relay pin transition is printed with a timestamp, so pulse width can be
confirmed without a scope.
"""

import argparse
import gc
import os
import sys
import time
import types

FIRMWARE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "firmware"
)


# --- MicroPython time helpers ----------------------------------------------

_START = time.monotonic()
time.ticks_ms = lambda: int((time.monotonic() - _START) * 1000)
time.ticks_diff = lambda a, b: a - b
time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)

gc.mem_free = lambda: 120000
sys.print_exception = lambda exc: __import__("traceback").print_exception(
    type(exc), exc, exc.__traceback__
)


# --- Fake machine -----------------------------------------------------------


class Pin:
    OUT = "OUT"
    IN = "IN"
    PULL_UP = "PULL_UP"

    def __init__(self, number, mode=None, pull=None, value=None):
        self.number = number
        self.mode = mode
        self._value = 1 if value is None else value
        if mode == Pin.OUT:
            print(
                "[sim] pin %d configured OUT, initial level %d"
                % (number, self._value)
            )

    def value(self, level=None):
        if level is None:
            return self._value
        if level != self._value:
            print(
                "[sim] %8.3fs pin %d -> %d%s"
                % (
                    time.monotonic() - _START,
                    self.number,
                    level,
                    "   RELAY CLOSED (door would move)"
                    if level == 0
                    else "   relay open",
                )
            )
        self._value = level


class WDT:
    def __init__(self, id=0, timeout=5000):
        self.timeout = timeout
        self._last = time.monotonic()
        print("[sim] watchdog armed, timeout %d ms" % timeout)

    def feed(self):
        now = time.monotonic()
        if (now - self._last) * 1000 > self.timeout:
            print("[sim] WATCHDOG WOULD HAVE RESET THE BOARD")
        self._last = now


machine = types.ModuleType("machine")
machine.Pin = Pin
machine.WDT = WDT
machine.reset = lambda: print("[sim] machine.reset() called") or sys.exit(1)
machine.reset_cause = lambda: 1
machine.PWRON_RESET = 1
machine.HARD_RESET = 2
machine.WDT_RESET = 3
machine.SOFT_RESET = 4
machine.DEEPSLEEP_RESET = 5
sys.modules["machine"] = machine


# --- Fake network -----------------------------------------------------------


class WLAN:
    PM_NONE = 0

    def __init__(self, interface):
        self._active = False

    def active(self, state=None):
        if state is not None:
            self._active = state
        return self._active

    def config(self, *args, **kwargs):
        if args and args[0] == "rssi":
            return -47
        return None

    def status(self, what=None):
        if what == "rssi":
            return -47
        return 1010

    def isconnected(self):
        return True

    def connect(self, ssid, password):
        print("[sim] wifi connect %s" % ssid)

    def ifconfig(self, cfg=None):
        return ("127.0.0.1", "255.255.255.0", "127.0.0.1", "127.0.0.1")


network = types.ModuleType("network")
network.WLAN = WLAN
network.STA_IF = 0
network.STAT_GOT_IP = 1010
sys.modules["network"] = network


# --- Fake secrets -----------------------------------------------------------

secrets = types.ModuleType("secrets")
secrets.WIFI_SSID = "simulator"
secrets.WIFI_PASSWORD = "simulator"
secrets.API_TOKEN = os.environ.get("GARAGE_SIM_TOKEN", "simulator-token")
secrets.STATIC_IP = None
sys.modules["secrets"] = secrets


def run():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the DRY_RUN path instead of driving the fake pin",
    )
    args = parser.parse_args()

    sys.path.insert(0, FIRMWARE)
    import config

    config.HTTP_PORT = args.port
    config.DRY_RUN = args.dry_run

    print("[sim] token:   %s" % secrets.API_TOKEN)
    print("[sim] serving: http://127.0.0.1:%d" % args.port)
    print()

    main_path = os.path.join(FIRMWARE, "main.py")
    with open(main_path) as handle:
        source = handle.read()
    exec(compile(source, main_path, "exec"), {"__name__": "__main__"})


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n[sim] stopped")
