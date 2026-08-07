#!/usr/bin/env python3
"""Exercise every endpoint on the garage controller. Standard library only.

Cross-platform equivalent of smoke_test.sh, for Windows hosts and anywhere
curl is not installed.

    GARAGE_HOST=192.168.1.50 GARAGE_TOKEN=... python tools/smoke_test.py

    # PowerShell
    $env:GARAGE_HOST="192.168.1.50"; $env:GARAGE_TOKEN="..."
    python tools\\smoke_test.py

Read-only by default: it checks the API surface and the auth path without
moving the door. Pass --pulse to also fire POST /toggle, which WILL move the
door unless the firmware is running with DRY_RUN = True.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 8


def request(base, path, method="GET", token=None):
    """Return (status, body). Never raises for an HTTP error status."""
    url = "%s%s" % (base.rstrip("/"), path)
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("X-Auth-Token", token)
    if method == "POST":
        req.data = b""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, str(exc)


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, label, expect, base, path, method="GET", token=None):
        status, body = request(base, path, method=method, token=token)
        body = " ".join(body.split())[:120]
        if status == expect:
            self.passed += 1
            print("ok    %-34s %s" % (label, body))
        else:
            self.failed += 1
            print(
                "FAIL  %-34s expected %s got %s: %s"
                % (label, expect, status, body)
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        # Matches smoke_test.sh. Note this is a name your router has to
        # resolve, via a DHCP reservation or a static lease; the firmware
        # runs no mDNS responder, so a .local name would never resolve.
        # Passing the IP directly always works.
        default=os.environ.get("GARAGE_HOST", "garage.home.arpa"),
        help="device host or ip (env GARAGE_HOST)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GARAGE_TOKEN", ""),
        help="device shared secret (env GARAGE_TOKEN)",
    )
    parser.add_argument(
        "--pulse",
        action="store_true",
        help="also fire POST /toggle. This moves the door unless DRY_RUN",
    )
    args = parser.parse_args(argv)

    host = args.host
    base = host if "://" in host else "http://%s" % host
    token = args.token

    print("target: %s" % base)
    if not token:
        print("warning: GARAGE_TOKEN is unset, authenticated checks skipped")
    print()

    r = Results()
    r.check("GET /health", 200, base, "/health")
    r.check("GET /status", 200, base, "/status")
    r.check("GET /log", 200, base, "/log")
    r.check("GET /nope -> 404", 404, base, "/nope")
    r.check("GET /toggle -> 405", 405, base, "/toggle")
    r.check("POST /toggle no token", 401, base, "/toggle", "POST")
    r.check("POST /toggle bad token", 401, base, "/toggle", "POST", "wrong")

    # This device has no door sensor, so it cannot honour "open" or "close"
    # and must not pretend to. They are gone, not aliased to toggle: a /close
    # that actually toggled would open the garage half the time.
    r.check("POST /open gone -> 404", 404, base, "/open", "POST", token)
    r.check("POST /close gone -> 404", 404, base, "/close", "POST", token)

    if token and args.pulse:
        r.check("POST /toggle authed", 200, base, "/toggle", "POST", token)
        # A second pulse inside the cooldown must be refused, so a retrying
        # client cannot immediately reverse a door that just started moving.
        r.check("POST /toggle cooldown", 429, base, "/toggle", "POST", token)
        print()
        print("status for the next 20s (moving clears when TRAVEL_MS elapses):")
        for _ in range(10):
            time.sleep(2)
            _, body = request(base, "/status")
            print("  %s" % " ".join(body.split()))
    elif token:
        print("skipping authenticated POST checks (pass --pulse to run them)")

    print()
    print("passed: %d  failed: %d" % (r.passed, r.failed))
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
