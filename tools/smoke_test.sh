#!/usr/bin/env bash
# Exercise every endpoint on the garage controller.
#
#   GARAGE_HOST=garage.home.arpa GARAGE_TOKEN=... ./smoke_test.sh
#
# Read-only by default: it checks the API surface and auth without moving the
# door. Pass --pulse to also fire POST /toggle, which WILL move the door
# unless the firmware is running with DRY_RUN = True.

set -u

HOST="${GARAGE_HOST:-garage.home.arpa}"
TOKEN="${GARAGE_TOKEN:-}"
PULSE=0
[ "${1:-}" = "--pulse" ] && PULSE=1

case "$HOST" in
  http://*|https://*) BASE="$HOST" ;;
  *) BASE="http://$HOST" ;;
esac

PASS=0
FAIL=0

# check <label> <expected-status> <curl args...>
check() {
  local label="$1" expect="$2"; shift 2
  local out code body
  out=$(curl -sS --max-time 8 -w '\n%{http_code}' "$@" 2>&1)
  code=$(printf '%s' "$out" | tail -n1)
  body=$(printf '%s' "$out" | sed '$d')
  if [ "$code" = "$expect" ]; then
    PASS=$((PASS + 1))
    printf 'ok    %-34s %s\n' "$label" "$body"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL  %-34s expected %s got %s: %s\n' "$label" "$expect" "$code" "$body"
  fi
}

echo "target: $BASE"
if [ -z "$TOKEN" ]; then
  echo "warning: GARAGE_TOKEN is unset, authenticated checks will be skipped"
fi
echo

check "GET /health"                200 "$BASE/health"
check "GET /status"                200 "$BASE/status"
check "GET /log"                   200 "$BASE/log"
check "GET /nope -> 404"           404 "$BASE/nope"
check "GET /toggle -> 405"         405 "$BASE/toggle"
check "POST /toggle no token"      401 -X POST "$BASE/toggle"
check "POST /toggle bad token"     401 -X POST -H "X-Auth-Token: wrong" "$BASE/toggle"

# This device has no door sensor, so it cannot honour "open" or "close" and
# must not pretend to. They are gone, not aliased to toggle: a /close that
# actually toggled would open the garage half the time.
check "POST /open gone -> 404"     404 -X POST -H "X-Auth-Token: $TOKEN" "$BASE/open"
check "POST /close gone -> 404"    404 -X POST -H "X-Auth-Token: $TOKEN" "$BASE/close"

if [ -n "$TOKEN" ]; then
  if [ "$PULSE" = "1" ]; then
    check "POST /toggle authed"    200 -X POST -H "X-Auth-Token: $TOKEN" "$BASE/toggle"
    # A second pulse inside the cooldown must be refused, so a retrying
    # client cannot immediately reverse a door that just started moving.
    check "POST /toggle cooldown"  429 -X POST -H "X-Auth-Token: $TOKEN" "$BASE/toggle"
    echo
    echo "status for the next 20s (moving clears when TRAVEL_MS elapses):"
    for _ in $(seq 1 10); do
      sleep 2
      curl -sS --max-time 8 "$BASE/status" | tr -d '\n'; echo
    done
  else
    echo "skipping authenticated POST checks (pass --pulse to run them)"
  fi
fi

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
