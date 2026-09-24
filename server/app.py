"""Garage door API - the piece that stands between the internet and the ESP32.

    phone / laptop
      -> https://api.example.com/garage/toggle
         Cloudflare Access service token, or a bearer key over a private VPN
      -> Cloudflare Access                    service token policy, at the edge
      -> cloudflared -> this process            on a machine on your LAN
      -> http://<esp32>/toggle                  X-Auth-Token, LAN only
      -> the relay closes for 500 ms

The controller does not talk to the internet and must never be made to. It has
no TLS, no rate limiting and no update path, and its single shared token is
the whole of its authentication. This process is what makes that posture safe:
it is the only thing that ever calls the device, it holds the device token, and
it authenticates every caller before it will make that call.

It is deliberately small and dependency-light so it can live on whatever
machine is already running in the house - a NAS, a Pi, a laptop with the lid
shut. Standard library only, unless you use Cloudflare Access, which needs
PyJWT to check the signature on the assertion.

What guards the relay, in order:

  1. The transport. Cloudflare Access at the edge, or a private network
     (Tailscale, WireGuard) that the caller has to be on at all.
  2. This process authenticating the caller: verifying the signed
     Cf-Access-Jwt-Assertion against Cloudflare's public keys - checking
     issuer and audience, not merely that the header is present - or comparing
     a bearer key in constant time. Something that reaches this port by
     another route cannot forge either one.
  3. API_SCOPES, a deny-by-default map from identity to permitted actions.
     Empty is a fatal config error, not a permissive default.
  4. A per-identity rate limit, and a toggle cooldown on top of it.
  5. The firmware's own 2 second cooldown, underneath all of the above.

There is no /open and no /close, here or on the device. The controller has no
door sensor, so it cannot know which way a pulse will send the door, and a
/close that guessed wrong would open the garage and leave it open.
"""

import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Configuration ----------------------------------------------------------


def _env(name, default=None):
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"fatal: {name} must be an integer")


LISTEN_ADDR = _env("LISTEN_ADDR", "0.0.0.0")
LISTEN_PORT = _env_int("LISTEN_PORT", 8080)

# --- How callers authenticate ---
#
# access  Cloudflare Access. The edge policy decides who gets through, and
#         this process verifies the assertion it forwards. Use this when the
#         endpoint has a public hostname.
#
# token   A bearer key per client, compared here. Use this ONLY when the
#         transport is already private - Tailscale, WireGuard, an SSH forward.
#         A token-mode listener on a public address is one guessed key away
#         from an open garage.
AUTH_MODE = (_env("AUTH_MODE", "access") or "").lower()
if AUTH_MODE not in ("access", "token"):
    sys.exit("fatal: AUTH_MODE must be 'access' or 'token'")

ACCESS_TEAM_DOMAIN = _env("ACCESS_TEAM_DOMAIN", "")
ACCESS_AUD = _env("ACCESS_AUD", "")
ACCESS_ISSUER = f"https://{ACCESS_TEAM_DOMAIN}"
ACCESS_CERTS_URL = f"{ACCESS_ISSUER}/cdn-cgi/access/certs"

if AUTH_MODE == "access" and not (ACCESS_TEAM_DOMAIN and ACCESS_AUD):
    sys.exit(
        "fatal: ACCESS_TEAM_DOMAIN and ACCESS_AUD are required in access "
        "mode.\n"
        "       Both come from the Access application in the Zero Trust\n"
        "       dashboard. If you are not using Cloudflare, set\n"
        "       AUTH_MODE=token and put this behind a private network."
    )

# --- The garage controller ---
#
# The ESP32-S3 on the LAN. Three firmware facts shape device_call below, all
# of them confirmed against the hardware:
#
#   * MAX_REQUEST_BYTES in firmware/config.py is 2048. A tunneled request
#     carrying Cf-Access-Jwt-Assertion plus the CF-* set is larger than that,
#     which is why nothing may proxy_pass straight to the device. The request
#     built below is hand-made and carries exactly one header.
#   * The device serves one connection at a time and closes it after each
#     response, so calls are serialised here where the error is better.
#   * It enforces its own 2 s relay cooldown underneath TOGGLE_COOLDOWN_S.
#
# An IP rather than a hostname is one less thing between a phone and the door,
# but a DHCP reservation or an mDNS name works too.
GARAGE_HOST = _env("GARAGE_HOST", "192.168.1.50")
GARAGE_TOKEN = _env("GARAGE_TOKEN", "")
DEVICE_TIMEOUT_S = _env_int("DEVICE_TIMEOUT_S", 8)

# Refuse a toggle landing within this many seconds of the previous one. Longer
# than the firmware's own window because the failure it prevents is different:
# a phone on a bad connection retries a request it thinks timed out, and a
# second pulse reverses a door that had already started moving.
TOGGLE_COOLDOWN_S = _env_int("TOGGLE_COOLDOWN_S", 5)

# Requests per minute per identity, across every route. A human presses this
# a few times a day; anything faster is a retry storm or a bug. This is the
# job a reverse proxy would otherwise do, done here so one is not required.
RATE_LIMIT_PER_MIN = _env_int("RATE_LIMIT_PER_MIN", 60)

AUDIT_SIZE = _env_int("AUDIT_SIZE", 200)

START_TIME = time.monotonic()


# --- Authorisation ----------------------------------------------------------

# API_SCOPES=identity=action,action;identity=action
#
# In access mode an identity is a service token Client ID (the full value,
# ending in .access) or the email address of an interactive login. In token
# mode it is a client name from API_CLIENTS. An action pattern is an exact
# action name, a prefix wildcard like garage.*, or a bare * for all.


def _parse_scopes(raw):
    scopes = {}
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            sys.exit(
                f"fatal: API_SCOPES entry {chunk!r} is not identity=actions"
            )
        identity, actions = chunk.split("=", 1)
        patterns = [a.strip() for a in actions.split(",") if a.strip()]
        if not patterns:
            sys.exit(
                f"fatal: API_SCOPES entry for {identity!r} lists no actions"
            )
        scopes[identity.strip().lower()] = patterns
    return scopes


API_SCOPES = _parse_scopes(_env("API_SCOPES", ""))

if not API_SCOPES:
    sys.exit(
        "fatal: API_SCOPES is required and must not be empty.\n"
        "       There is no safe permissive default for an endpoint that can\n"
        "       open a garage door. Grant explicitly:\n"
        "         API_SCOPES=<token-id>.access=garage.*;you@example.com=*\n"
        "       Deny-by-default is the point: a phone that only needs to\n"
        "       press the button must not also be able to read the audit log."
    )


def _parse_clients(raw):
    """API_CLIENTS=name:key;name:key - token mode only."""
    clients = {}
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            sys.exit(f"fatal: API_CLIENTS entry {chunk!r} is not name:key")
        name, key = chunk.split(":", 1)
        name, key = name.strip(), key.strip()
        if not name or not key:
            sys.exit(f"fatal: API_CLIENTS entry {chunk!r} is incomplete")
        if len(key) < 32:
            sys.exit(
                f"fatal: the key for API_CLIENTS entry {name!r} is "
                f"{len(key)} characters.\n"
                "       32 is the minimum. Generate one with:\n"
                "         python3 -c \"import secrets; "
                'print(secrets.token_urlsafe(32))"'
            )
        clients[name] = key
    return clients


API_CLIENTS = _parse_clients(_env("API_CLIENTS", ""))

if AUTH_MODE == "token" and not API_CLIENTS:
    sys.exit(
        "fatal: AUTH_MODE=token needs at least one API_CLIENTS entry.\n"
        "         API_CLIENTS=phone:<key>;laptop:<other key>"
    )


def scope_allows(identity, action):
    for pattern in API_SCOPES.get((identity or "").lower(), []):
        if pattern == "*" or pattern == action:
            return True
        if pattern.endswith(".*") and action.startswith(pattern[:-1]):
            return True
    return False


# --- Logging and audit ------------------------------------------------------

_audit = deque(maxlen=AUDIT_SIZE)
_audit_lock = threading.Lock()


def log(event, **fields):
    """One JSON line per event to stdout, and a copy in the ring buffer.

    Cloudflare keeps its own Access log, but that one is not on this machine
    and does not survive a lapsed subscription. Every action is recorded here
    with the identity that asked for it. The ring buffer dies with the
    process; stdout - the journal, the container log - is the durable copy.
    """
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
    entry.update(fields)
    with _audit_lock:
        _audit.append(entry)
    sys.stdout.write(json.dumps(entry, default=str) + "\n")
    sys.stdout.flush()


# --- Authenticating the caller ----------------------------------------------


class AuthDenied(Exception):
    pass


_jwk_client = None


def _jwks():
    """Import PyJWT lazily so token mode stays standard library only."""
    global _jwk_client
    if _jwk_client is None:
        try:
            from jwt import PyJWKClient
        except ImportError:
            sys.exit(
                "fatal: AUTH_MODE=access needs PyJWT to verify the Access\n"
                "       assertion. Install it with:\n"
                "         pip install 'PyJWT[crypto]==2.10.1'\n"
                "       or use the Docker image, which already has it."
            )
        _jwk_client = PyJWKClient(ACCESS_CERTS_URL, cache_keys=True)
    return _jwk_client


def _access_identity(headers):
    import jwt

    token = headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        raise AuthDenied("no Access assertion on request")
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=ACCESS_AUD,
            issuer=ACCESS_ISSUER,
        )
    except Exception as exc:
        raise AuthDenied(f"invalid Access assertion: {exc}")

    # Service tokens identify by common_name; interactive logins by email.
    identity = claims.get("common_name") or claims.get("email") or ""
    if not identity:
        raise AuthDenied("Access assertion carries no identity")
    return identity


def _token_identity(headers):
    """Match a bearer key against API_CLIENTS in constant time.

    Every configured key is compared even after a match, so the time taken
    does not reveal which client - or how many - are configured.
    """
    presented = headers.get("X-Api-Key", "")
    if not presented:
        authorization = headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
    if not presented:
        raise AuthDenied("no X-Api-Key or bearer token on request")

    matched = None
    for name, key in API_CLIENTS.items():
        if hmac.compare_digest(presented, key):
            matched = name
    if matched is None:
        raise AuthDenied("unrecognised API key")
    return matched


def caller_identity(headers):
    if AUTH_MODE == "access":
        return _access_identity(headers)
    return _token_identity(headers)


# --- Rate limiting ----------------------------------------------------------
#
# A fixed window per identity. Crude, and correct for the shape of the
# traffic: a handful of requests a day from a phone, and a burst only when
# something is retrying in a loop, which is exactly what should be cut off.

_rate = {}
_rate_lock = threading.Lock()


def rate_ok(identity):
    if RATE_LIMIT_PER_MIN <= 0:
        return True
    window = int(time.monotonic() // 60)
    with _rate_lock:
        seen_window, count = _rate.get(identity, (window, 0))
        if seen_window != window:
            seen_window, count = window, 0
        count += 1
        _rate[identity] = (seen_window, count)
        # Drop stale identities so a churn of keys cannot grow this forever.
        if len(_rate) > 256:
            for key in [k for k, v in _rate.items() if v[0] != window]:
                del _rate[key]
        return count <= RATE_LIMIT_PER_MIN


# --- Talking to the controller ----------------------------------------------


class UpstreamError(Exception):
    pass


# The firmware serves one connection at a time and closes it after each
# response. Two concurrent requests would queue at the socket and one would
# time out, so serialise here where the error message is better.
_device_lock = threading.Lock()


def device_call(path, method="GET", authenticate=False):
    """Call the controller and return (status, parsed json).

    Hand-built rather than routed through a helper: the request that reaches
    the ESP32 has to stay under MAX_REQUEST_BYTES, so it carries exactly one
    header and nothing else.
    """
    if authenticate and not GARAGE_TOKEN:
        raise UpstreamError(
            "GARAGE_TOKEN is not configured; refusing to operate the door"
        )

    request = urllib.request.Request(
        f"http://{GARAGE_HOST}{path}", method=method
    )
    if authenticate:
        request.add_header("X-Auth-Token", GARAGE_TOKEN)
    if method == "POST":
        request.data = b""

    if not _device_lock.acquire(timeout=DEVICE_TIMEOUT_S):
        raise UpstreamError("another request is already talking to the device")
    try:
        with urllib.request.urlopen(
            request, timeout=DEVICE_TIMEOUT_S
        ) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"ok": False, "error": body.strip()[:500]}
    except ValueError as exc:
        raise UpstreamError(f"controller returned malformed JSON: {exc}")
    except Exception as exc:
        raise UpstreamError(
            f"cannot reach the controller at {GARAGE_HOST}: {exc}"
        )
    finally:
        _device_lock.release()


# --- Handlers ---------------------------------------------------------------

_last_toggle = 0.0
_toggle_lock = threading.Lock()


def claim_toggle():
    """Reserve the right to pulse, or return the seconds left to wait."""
    global _last_toggle
    with _toggle_lock:
        now = time.monotonic()
        remaining = TOGGLE_COOLDOWN_S - (now - _last_toggle)
        if remaining > 0:
            return remaining
        _last_toggle = now
        return 0


def release_toggle():
    """Undo a claim when the pulse never actually happened."""
    global _last_toggle
    with _toggle_lock:
        _last_toggle = 0.0


def h_toggle(ctx):
    """The only state-changing door route, deliberately.

    Check the garage camera before you press. Do not put this on a schedule
    or a geofence: it cannot know which way the door will go, and the failure
    mode is an open garage you find out about when you get home.
    """
    wait = claim_toggle()
    if wait:
        log(
            "cooldown",
            who=ctx.identity,
            ip=ctx.client_ip,
            wait_s=round(wait, 1),
        )
        return 429, {
            "ok": False,
            "error": "too soon after the last pulse",
            "retry_after_s": round(wait, 1),
        }

    try:
        code, payload = device_call("/toggle", "POST", authenticate=True)
    except UpstreamError:
        release_toggle()
        raise

    if code != 200:
        # The device refused (busy relay, bad token). Nothing moved, so do
        # not hold the caller in cooldown for it.
        release_toggle()
        log("toggle_refused", who=ctx.identity, ip=ctx.client_ip, code=code)

    return code, payload


def h_selftest(ctx):
    """Dress rehearsal for /garage/toggle, with no pulse at the end of it.

    It exercises the transport, the auth, the scopes, this process and the
    LAN hop to the controller. It does NOT exercise GARAGE_TOKEN, and says so
    in its own response rather than letting a green result imply more than it
    checked.

    The controller has no unauthenticated-but-token-checking endpoint to aim
    at: /health, /status and /log need no token, and /toggle needs one but
    moves the door. Closing that gap would mean physically retrieving the
    ESP32 to reflash it, which is not worth it for a check that only fails
    when the token is rotated - a moment when you already know to re-test.

    Deliberately a GET to a read-only device route. There is no code path
    from here to the relay.
    """
    code, device = device_call("/status")
    return (200 if code == 200 else 502), {
        "ok": code == 200,
        "action": "garage.selftest",
        "pulsed": False,
        "checked": ["transport", "auth", "scopes", "api", "device_reachable"],
        "device_token_verified": False,
        "note": "GARAGE_TOKEN is not exercised by this check; only a real "
        "toggle does that",
        "cooldown_remaining_s": round(
            max(0.0, TOGGLE_COOLDOWN_S - (time.monotonic() - _last_toggle)), 1
        ),
        "device": device,
    }


def h_status(ctx):
    return device_call("/status")


def h_log(ctx):
    return device_call("/log")


def h_audit(ctx):
    """Who has pressed the button.

    In memory and bounded by AUDIT_SIZE, so it does not survive a restart.
    The process's stdout is the durable copy: `docker compose logs garage-api`,
    `journalctl -u garage-api`, or the log file the launchd and Windows
    installs write.
    """
    with _audit_lock:
        events = [
            e
            for e in _audit
            if str(e.get("action", "")).startswith("garage.")
            or e.get("event") in ("toggle_refused", "cooldown")
        ]
    return 200, {"ok": True, "action": "garage.audit", "events": events}


# --- Route table ------------------------------------------------------------
#
# Every path is namespaced by area, including this service's own
# introspection, which lives under /service rather than at the root. Nothing
# sits at a bare /status or /log.
#
# The action name mirrors the path with dots for slashes, which is what makes
# an API_SCOPES pattern like `garage.*` line up with `/garage/*`.

ROUTES = {
    ("POST", "/garage/toggle"): ("garage.toggle", h_toggle),
    ("POST", "/garage/selftest"): ("garage.selftest", h_selftest),
    ("GET", "/garage/status"): ("garage.status", h_status),
    ("GET", "/garage/log"): ("garage.log", h_log),
    ("GET", "/garage/audit"): ("garage.audit", h_audit),
}

# service.audit is handled inline rather than through the table - it reads the
# ring buffer rather than calling anything - but it is a scopeable action like
# any other. service.actions and service.health are not scopeable: the first
# only ever describes the caller to itself, and the second is unauthenticated
# for health checks and must not be published by the tunnel.
ALL_ACTIONS = sorted(
    {action for action, _ in ROUTES.values()} | {"service.audit"}
)


# --- HTTP surface -----------------------------------------------------------


class Context:
    def __init__(self, headers, query, identity, client_ip):
        self.headers = headers
        self.query = query
        self.identity = identity
        self.client_ip = client_ip


class Handler(BaseHTTPRequestHandler):
    server_version = "garage-api"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # log() covers what matters, in a form something can parse

    def respond(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self):
        return (
            self.headers.get("CF-Connecting-IP")
            or self.headers.get("X-Real-IP")
            or self.client_address[0]
        )

    def drain_body(self):
        """Read and discard any body. No route here takes one.

        Left unread, it stays in the socket buffer and desynchronises the next
        request on a keep-alive connection.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= 65536:
            self.rfile.read(length)

    def handle_request(self, method):
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path.rstrip("/") or "/"
        query = {
            k: v[-1]
            for k, v in urllib.parse.parse_qs(
                raw_query, keep_blank_values=True
            ).items()
        }
        self.drain_body()

        # Liveness only, and deliberately unauthenticated: a container or
        # service health check hits it on 127.0.0.1 and it reveals nothing
        # beyond "the process is up". The tunnel must not publish it.
        if method == "GET" and path == "/service/health":
            return self.respond(
                200,
                {
                    "ok": True,
                    "uptime_s": int(time.monotonic() - START_TIME),
                    "auth_mode": AUTH_MODE,
                },
            )

        try:
            identity = caller_identity(self.headers)
        except AuthDenied as exc:
            log("auth_fail", path=path, ip=self.client_ip(), reason=str(exc))
            return self.respond(403, {"ok": False, "error": "forbidden"})

        if not rate_ok(identity):
            log("rate_limited", who=identity, path=path, ip=self.client_ip())
            return self.respond(
                429, {"ok": False, "error": "too many requests"}
            )

        ctx = Context(self.headers, query, identity, self.client_ip())

        # What this caller may do. Needs no scope of its own - it tells an
        # identity about itself and nothing about anyone else.
        if method == "GET" and path == "/service/actions":
            return self.respond(
                200,
                {
                    "ok": True,
                    "identity": identity,
                    "allowed": [
                        a for a in ALL_ACTIONS if scope_allows(identity, a)
                    ],
                },
            )

        if method == "GET" and path == "/service/audit":
            if not scope_allows(identity, "service.audit"):
                return self.deny(identity, "service.audit", path)
            with _audit_lock:
                return self.respond(200, {"ok": True, "events": list(_audit)})

        route = ROUTES.get((method, path))
        if route is None:
            return self.respond(
                404, {"ok": False, "error": "no such endpoint"}
            )

        action, handler = route
        if not scope_allows(identity, action):
            return self.deny(identity, action, path)

        try:
            status, payload = handler(ctx)
        except UpstreamError as exc:
            log("upstream_error", action=action, who=identity, reason=str(exc))
            return self.respond(502, {"ok": False, "error": str(exc)})
        except Exception as exc:
            log("error", action=action, who=identity, reason=repr(exc))
            return self.respond(500, {"ok": False, "error": "internal error"})

        log(
            "action",
            action=action,
            who=identity,
            ip=ctx.client_ip,
            status=status,
        )
        return self.respond(status, payload)

    def deny(self, identity, action, path):
        log(
            "scope_denied",
            action=action,
            who=identity,
            path=path,
            ip=self.client_ip(),
        )
        return self.respond(
            403,
            {"ok": False, "error": f"identity is not scoped for {action}"},
        )

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")


def main():
    log(
        "boot",
        addr=LISTEN_ADDR,
        port=LISTEN_PORT,
        auth_mode=AUTH_MODE,
        identities=len(API_SCOPES),
        clients=len(API_CLIENTS) or None,
        actions=len(ALL_ACTIONS),
        device=GARAGE_HOST if GARAGE_TOKEN else "no token",
    )
    if not GARAGE_TOKEN:
        # Not fatal on purpose. Failing closed for a door means refusing to
        # move it, which device_call does with a clear 502 - it does not mean
        # refusing to start and taking the read-only routes down with it.
        log(
            "warning",
            message="GARAGE_TOKEN is unset; /garage/toggle will refuse. "
            "Read-only door routes still work.",
        )
    if AUTH_MODE == "token" and LISTEN_ADDR in ("0.0.0.0", "::", ""):
        log(
            "warning",
            message="token mode on a wildcard address. This must not be "
            "reachable from the internet - keep it on a LAN or a private "
            "VPN, or use AUTH_MODE=access.",
        )

    server = ThreadingHTTPServer((LISTEN_ADDR, LISTEN_PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
