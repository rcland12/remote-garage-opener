"""Remote garage door controller: WiFi, HTTP API and the main service loop.

Design notes:
  - One cooperative loop. The listening socket is polled with a short timeout,
    so servicing the relay and the watchdog never waits on a client, and a
    client never waits on a 500 ms relay pulse.
  - The relay is idle at boot and after any reset, crash or watchdog reboot.
  - Everything that changes the door requires the shared secret token.
  - There is no door sensor. This device pulses the opener's pushbutton and
    reports nothing about the door's position, because it cannot know it.
    POST /toggle is therefore the only door command, matching the single
    button on the wall. Look at the garage camera before using it.
"""

import gc
import json
import select
import socket
import sys
import time

import config
import machine
import network
from garage import EventLog, Relay

try:
    import secrets
except ImportError:
    print(
        "FATAL: secrets.py is missing. Copy secrets.example.py and fill it in."
    )
    raise SystemExit


# --- Monotonic clock --------------------------------------------------------
# time.ticks_ms() wraps roughly every 12 days on MicroPython. Accumulate the
# deltas so uptime and every deadline in the program keep working past that.

_last_tick = time.ticks_ms()
_uptime_ms = 0


def uptime_ms():
    global _last_tick, _uptime_ms
    now = time.ticks_ms()
    _uptime_ms += time.ticks_diff(now, _last_tick)
    _last_tick = now
    return _uptime_ms


# --- Globals ----------------------------------------------------------------

log = EventLog(config.LOG_SIZE)
relay = Relay(log)
wdt = None
wlan = None
_moving_until = 0
_next_wifi_check = 0


# --- WiFi -------------------------------------------------------------------

# Built with getattr so a port that is missing one of these constants gives a
# vaguer log line rather than an ImportError, which here would be a boot loop.
RESET_CAUSES = {}
for _name, _label in (
    ("PWRON_RESET", "power-on"),
    ("HARD_RESET", "hard"),
    ("WDT_RESET", "watchdog"),
    ("SOFT_RESET", "soft"),
    ("DEEPSLEEP_RESET", "deepsleep"),
):
    _code = getattr(machine, _name, None)
    if _code is not None:
        RESET_CAUSES[_code] = _label


def wifi_start():
    global wlan
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        # Keep the radio out of power save so the API stays responsive.
        wlan.config(pm=network.WLAN.PM_NONE)
    except (AttributeError, ValueError, OSError):
        pass
    static = getattr(secrets, "STATIC_IP", None)
    if static:
        wlan.ifconfig(static)
    return wlan


def wifi_connect(timeout_ms=30000):
    """Attempt to associate. Returns True on success. Feeds the watchdog."""
    if wlan.isconnected():
        return True
    log.add("wifi_connect", secrets.WIFI_SSID, uptime_ms())
    try:
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
    except OSError as exc:
        log.add("wifi_error", str(exc), uptime_ms())
        return False

    deadline = uptime_ms() + timeout_ms
    while uptime_ms() < deadline:
        feed()
        if wlan.isconnected():
            log.add("wifi_up", wlan.ifconfig()[0], uptime_ms())
            return True
        time.sleep_ms(200)
    log.add("wifi_timeout", None, uptime_ms())
    return False


def wifi_poll(now):
    """Re-associate after a drop, without blocking the loop for long."""
    global _next_wifi_check
    if now < _next_wifi_check:
        return
    _next_wifi_check = now + config.WIFI_RETRY_MS
    if not wlan.isconnected():
        log.add("wifi_down", None, now)
        wifi_connect(timeout_ms=8000)


def rssi():
    try:
        return wlan.status("rssi")
    except (OSError, ValueError, TypeError):
        try:
            return wlan.config("rssi")
        except Exception:
            return None


def ip_address():
    try:
        return wlan.ifconfig()[0]
    except Exception:
        return None


# --- Watchdog ---------------------------------------------------------------


def feed():
    if wdt is not None:
        wdt.feed()


# --- HTTP -------------------------------------------------------------------

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
}


def send_json(conn, code, payload):
    body = json.dumps(payload).encode()
    head = (
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
        % (code, STATUS_TEXT.get(code, "OK"), len(body))
    )
    conn.send(head.encode())
    conn.send(body)


def read_request(conn):
    """Return (method, path, headers). Raises ValueError on a bad request."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(256)
        if not chunk:
            raise ValueError("connection closed early")
        buf += chunk
        if len(buf) > config.MAX_REQUEST_BYTES:
            raise ValueError("request too large")

    lines = buf.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    parts = lines[0].decode().split(" ")
    if len(parts) < 2:
        raise ValueError("malformed request line")
    method, path = parts[0].upper(), parts[1]
    path = path.split("?", 1)[0]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.decode().strip().lower()] = value.decode().strip()
    return method, path, headers


def token_ok(headers):
    """Compare in constant time so the token cannot be guessed byte by byte."""
    supplied = headers.get(config.AUTH_HEADER, "")
    expected = secrets.API_TOKEN
    if len(supplied) != len(expected):
        return False
    mismatch = 0
    for a, b in zip(supplied, expected):
        mismatch |= ord(a) ^ ord(b)
    return mismatch == 0


# --- Route handlers ---------------------------------------------------------


def door_snapshot(now):
    # No "door" key: there is no sensor, so any value here would be a guess
    # presented as a fact. "moving" is an honest timer, not a measurement.
    return {
        "moving": now < _moving_until,
        "uptime_s": now // 1000,
    }


def handle_health(now, headers):
    return 200, {"ok": True, "uptime_s": now // 1000}


def handle_status(now, headers):
    payload = door_snapshot(now)
    payload.update(
        {
            "ok": True,
            "rssi": rssi(),
            "ip": ip_address(),
            "ssid": secrets.WIFI_SSID,
            "dry_run": config.DRY_RUN,
            "relay_ready": relay.ready(now),
            "reset_cause": RESET_CAUSES.get(machine.reset_cause(), "unknown"),
            "free_mem": gc.mem_free(),
        }
    )
    return 200, payload


def handle_log(now, headers):
    return 200, {"ok": True, "events": log.entries()}


def handle_toggle(now, headers):
    """Pulse the relay. The opener decides what that means.

    There is deliberately no /open or /close. Without a door sensor this
    device cannot tell an open door from a closed one, so a "close" command
    would be a coin flip that opens the garage half the time. One button, the
    same one that is on the wall.
    """
    global _moving_until
    if not relay.pulse(now, "toggle"):
        return 429, {"ok": False, "error": "relay busy, try again shortly"}
    _moving_until = now + config.TRAVEL_MS
    payload = door_snapshot(now)
    payload.update({"ok": True, "action": "pulsed", "dry_run": config.DRY_RUN})
    return 200, payload


# path -> (method, handler, auth_required)
ROUTES = {
    "/health": ("GET", handle_health, False),
    "/status": ("GET", handle_status, False),
    "/log": ("GET", handle_log, False),
    "/toggle": ("POST", handle_toggle, True),
}


def dispatch(conn, addr):
    now = uptime_ms()
    try:
        method, path, headers = read_request(conn)
    except ValueError as exc:
        send_json(conn, 400, {"ok": False, "error": str(exc)})
        return
    except OSError:
        return  # client vanished mid-request

    route = ROUTES.get(path)
    if route is None:
        send_json(
            conn, 404, {"ok": False, "error": "no such endpoint", "path": path}
        )
        return

    want_method, handler, auth_required = route
    if method != want_method:
        send_json(
            conn,
            405,
            {"ok": False, "error": "use %s %s" % (want_method, path)},
        )
        return

    if auth_required and not token_ok(headers):
        log.add("auth_fail", "%s %s from %s" % (method, path, addr[0]), now)
        send_json(
            conn, 401, {"ok": False, "error": "bad or missing auth token"}
        )
        return

    code, payload = handler(now, headers)
    send_json(conn, code, payload)


def serve_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", config.HTTP_PORT))
    sock.listen(2)
    sock.setblocking(False)
    return sock


# --- Main loop --------------------------------------------------------------


def main():
    global wdt

    log.add(
        "boot",
        "reset=%s dry_run=%s"
        % (RESET_CAUSES.get(machine.reset_cause(), "?"), config.DRY_RUN),
        uptime_ms(),
    )

    if config.WDT_ENABLED:
        # Started only after the relay pin is already idle, and only after
        # secrets loaded, so a config mistake does not become a boot loop.
        wdt = machine.WDT(0, config.WDT_TIMEOUT_MS)

    wifi_start()
    wifi_connect()

    sock = serve_socket()
    poller = select.poll()
    poller.register(sock, select.POLLIN)
    log.add("http_up", "port %d" % config.HTTP_PORT, uptime_ms())

    next_gc = 0
    while True:
        now = uptime_ms()
        feed()
        relay.poll(now)
        wifi_poll(now)

        # 20 ms is short enough to release the relay within a millisecond or
        # two of the 500 ms mark, and long enough that the loop is not a busy
        # spin.
        for _sock, _event in poller.poll(20):
            conn = None
            try:
                conn, addr = sock.accept()
                conn.settimeout(config.CLIENT_TIMEOUT_S)
                dispatch(conn, addr)
            except OSError:
                pass
            except Exception as exc:  # never let one bad request kill the loop
                log.add("request_error", repr(exc), uptime_ms())
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass

        if now >= next_gc:
            next_gc = now + 5000
            gc.collect()


try:
    main()
except Exception as exc:
    # Anything unexpected: drop the contact, record why, and let the board
    # restart clean rather than sitting there wedged with the API down.
    relay.safe_off()
    sys.print_exception(exc)
    print("unhandled exception, resetting in 5s")
    time.sleep(5)
    machine.reset()
