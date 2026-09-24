#!/usr/bin/env python3
"""Command line client for the garage controller. Standard library only.

Meant to be run from the local server over SSH.

    export GARAGE_HOST=garage.home.arpa
    export GARAGE_TOKEN=...          # same value as API_TOKEN in secrets.py

    ./garage.py status
    ./garage.py toggle
    ./garage.py watch

It can also drive the server API in `server/`, which is the same commands one
hop further out and namespaces every path under /garage. Point it at the
server and give it that server's credentials instead of the device token; the
server holds the device token.

    # AUTH_MODE=access, through a Cloudflare Tunnel
    export GARAGE_HOST=https://api.example.com
    export CF_ACCESS_CLIENT_ID=...
    export CF_ACCESS_CLIENT_SECRET=...

    # AUTH_MODE=token, over Tailscale, WireGuard or an SSH forward
    export GARAGE_HOST=http://laptop.your-tailnet.ts.net:8080
    export GARAGE_API_KEY=...

    ./garage.py status
    ./garage.py audit             # who pressed the button, server only
    ./garage.py actions           # what these credentials may call

Use selftest to check the remote path works without the door moving. It takes
the same credentials as toggle and travels the same route as far as the
controller, so it is what to build a phone shortcut against:

    ./garage.py selftest

It does not prove the device token is correct. Nothing can, short of a real
toggle: the controller's token-checked endpoint is the one that moves the
door. See docs/remote-access.md.

There is no open or close command. The device has no door sensor, so it can
only do what the wall button does: toggle. Check the garage camera first.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 8


class ApiError(Exception):
    pass


def via_server(args):
    """True when we are calling the server API rather than the device.

    An https URL is always the server; the device has no TLS. Otherwise it is
    the presence of server credentials that decides, which is what makes a
    token-mode server on a plain http tailnet address work.
    """
    return (
        args.host.startswith("https://")
        or bool(args.api_key)
        or bool(args.access_id and args.access_secret)
    )


def route(args, command):
    """The path for a command, which differs between the two APIs.

    The device serves four flat routes. The server namespaces everything by
    area, so that /status can mean the door on one path and something else on
    another later. /health is the exception: the server's liveness route is
    unauthenticated and lives under /service.
    """
    if not via_server(args):
        return "/%s" % command
    if command == "health":
        return "/service/health"
    if command == "actions":
        return "/service/actions"
    return "/garage/%s" % command


def call(
    host,
    path,
    method="GET",
    token=None,
    access=None,
    api_key=None,
    timeout=DEFAULT_TIMEOUT,
):
    url = host if "://" in host else "http://%s" % host
    url = "%s%s" % (url.rstrip("/"), path)
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("X-Auth-Token", token)
    if access:
        # Cloudflare Access service token. Consumed at the edge; neither the
        # server nor the device ever sees these.
        request.add_header("CF-Access-Client-Id", access[0])
        request.add_header("CF-Access-Client-Secret", access[1])
    if api_key:
        # An AUTH_MODE=token server. Checked by the server, never forwarded.
        request.add_header("X-Api-Key", api_key)
    if method == "POST":
        request.data = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"ok": False, "error": body.strip()}
    except urllib.error.URLError as exc:
        raise ApiError("cannot reach %s: %s" % (url, exc.reason))
    except (TimeoutError, OSError) as exc:
        raise ApiError("cannot reach %s: %s" % (url, exc))


def require_token(args):
    """The device token, needed only when talking to the device directly.

    Through the server it supplies its own, which is the whole point: the
    token stays on the server and clients carry a revocable credential
    instead.
    """
    if via_server(args):
        return None
    if not args.token:
        raise ApiError(
            "%s needs a token: set GARAGE_TOKEN or pass --token" % args.command
        )
    return args.token


def access_creds(args):
    """Cloudflare Access credentials, for an AUTH_MODE=access server."""
    if not via_server(args) or args.api_key:
        return None
    if not (args.access_id and args.access_secret):
        raise ApiError(
            "%s needs Access credentials: set CF_ACCESS_CLIENT_ID and "
            "CF_ACCESS_CLIENT_SECRET, or GARAGE_API_KEY for a token-mode "
            "server" % args.host
        )
    return args.access_id, args.access_secret


def api_key(args):
    """The bearer key for an AUTH_MODE=token server."""
    return args.api_key if via_server(args) else None


def fmt_status(payload):
    bits = ["moving" if payload.get("moving") else "idle"]
    for key in ("uptime_s", "rssi", "ip", "dry_run"):
        if key in payload and payload[key] is not None:
            bits.append("%s=%s" % (key, payload[key]))
    return "  ".join(bits)


def cmd_watch(args):
    """Poll /status until interrupted, printing only on change."""
    previous = None
    access = access_creds(args)
    key = api_key(args)
    path = route(args, "status")
    while True:
        try:
            _, payload = call(
                args.host,
                path,
                access=access,
                api_key=key,
                timeout=args.timeout,
            )
            line = fmt_status(payload)
        except ApiError as exc:
            line = "unreachable: %s" % exc
        if line != previous:
            print("%s  %s" % (time.strftime("%H:%M:%S"), line))
            previous = line
        time.sleep(args.interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=[
            "health",
            "status",
            "log",
            "audit",
            "actions",
            "selftest",
            "toggle",
            "watch",
        ],
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("GARAGE_HOST", "garage.home.arpa"),
        help="device host or ip, or the server's url "
        "(env GARAGE_HOST)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GARAGE_TOKEN"),
        help="device shared secret, LAN only (env GARAGE_TOKEN)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GARAGE_API_KEY"),
        help="bearer key for an AUTH_MODE=token server (env GARAGE_API_KEY)",
    )
    parser.add_argument(
        "--access-id",
        default=os.environ.get("CF_ACCESS_CLIENT_ID"),
        help="Cloudflare Access service token client id "
        "(env CF_ACCESS_CLIENT_ID)",
    )
    parser.add_argument(
        "--access-secret",
        default=os.environ.get("CF_ACCESS_CLIENT_SECRET"),
        help="Cloudflare Access service token secret "
        "(env CF_ACCESS_CLIENT_SECRET)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="watch poll interval, seconds",
    )
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args(argv)

    try:
        if args.command == "watch":
            cmd_watch(args)
            return 0

        # These three belong to the server, not the device. The controller has
        # none of those routes, so asking it directly would just 404.
        for command, hint in (
            ("audit", "It is the server's log, not the device's. Try 'log'."),
            (
                "selftest",
                "It is the server's check, not the device's. On the LAN, "
                "'status' tells you the device is reachable and smoke_test.sh "
                "exercises the rest.",
            ),
            ("actions", "It describes server credentials, which the device "
             "does not have."),
        ):
            if args.command == command and not via_server(args):
                raise ApiError(
                    "%s only exists on the server API in server/. %s"
                    % (command, hint)
                )

        # selftest is a POST and takes the same credentials as toggle, so the
        # two are interchangeable in a client by URL alone. It is the safe one:
        # same path, no pulse.
        if args.command in ("toggle", "selftest"):
            code, payload = call(
                args.host,
                route(args, args.command),
                method="POST",
                token=require_token(args),
                access=access_creds(args),
                api_key=api_key(args),
                timeout=args.timeout,
            )
        else:
            code, payload = call(
                args.host,
                route(args, args.command),
                access=access_creds(args),
                api_key=api_key(args),
                timeout=args.timeout,
            )
    except ApiError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.command == "log":
        for entry in payload.get("events", []):
            print(
                "%4d  %6ds  %-12s %s"
                % (
                    entry.get("seq", 0),
                    entry.get("t", 0),
                    entry.get("event", ""),
                    entry.get("detail", ""),
                )
            )
    elif args.command == "audit":
        # The server's records are wall-clock and carry an identity, unlike
        # the device's, which only knows milliseconds since its last boot.
        for entry in payload.get("events", []):
            rest = " ".join(
                "%s=%s" % (k, v)
                for k, v in sorted(entry.items())
                if k not in ("t", "event")
            )
            print(
                "%-25s %-14s %s"
                % (entry.get("t", ""), entry.get("event", ""), rest)
            )
    elif args.command == "actions":
        for action in payload.get("allowed", []):
            print(action)
    elif args.command == "status":
        print(fmt_status(payload))
    else:
        print(json.dumps(payload))

    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
