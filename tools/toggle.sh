#!/usr/bin/env bash
# Pulse the garage opener once, the same as pressing the wall button.
#
#   GARAGE_TOKEN=... ./toggle.sh
#   GARAGE_HOST=192.168.1.50 GARAGE_TOKEN=... ./toggle.sh   # bypass DNS
#
# There is no open or close, only toggle: the device has no door sensor and
# cannot tell an open door from a closed one. Whether this opens or closes
# depends on what the door is doing right now, so check the garage camera
# first. Run ./toggle.sh again to reverse it.

set -eu

HOST="${GARAGE_HOST:-garage.home.arpa}"
TOKEN="${GARAGE_TOKEN:-}"

case "$HOST" in
  http://*|https://*) BASE="$HOST" ;;
  *) BASE="http://$HOST" ;;
esac

if [ -z "$TOKEN" ]; then
  echo "error: GARAGE_TOKEN is unset (same value as API_TOKEN in secrets.py)" >&2
  exit 2
fi

out=$(curl -sS --max-time 8 -w '\n%{http_code}' \
  -X POST -H "X-Auth-Token: $TOKEN" "$BASE/toggle") || {
  echo "error: cannot reach $BASE" >&2
  exit 2
}

code=$(printf '%s' "$out" | tail -n1)
body=$(printf '%s' "$out" | sed '$d')

case "$code" in
  200) echo "pulsed: $body" ;;
  401) echo "rejected: bad or missing token" >&2; exit 1 ;;
  # The firmware refuses a second pulse inside COOLDOWN_MS so a double-tap
  # cannot immediately reverse a door that just started moving.
  429) echo "busy: pulsed too recently, wait a couple of seconds" >&2; exit 1 ;;
  *)   echo "unexpected $code: $body" >&2; exit 1 ;;
esac
