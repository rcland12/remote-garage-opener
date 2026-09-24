#!/usr/bin/env bash
# Verify the Cloudflare Access path to the garage controller.
#
#   ./verify_remote.sh
#   CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... ./verify_remote.sh
#
# Run it after the Zero Trust dashboard steps in server/README.md. Without
# service token credentials it checks that the
# hostname exists and that Access is actually guarding it, which is the part
# that matters most. With credentials it also checks that the whole path works
# end to end.
#
# It never sends POST /garage/toggle. The door does not move.

set -u

# Credentials come from the environment, or from tools/access.env if you would
# rather not retype them. That file is gitignored. Do not put them in this
# script: it is committed, and a service token in git is a garage key in git.
#
#   cat > tools/access.env <<'EOF'
#   CF_ACCESS_CLIENT_ID=xxxxxxxx.access
#   CF_ACCESS_CLIENT_SECRET=xxxxxxxx
#   EOF
#   chmod 600 tools/access.env
ACCESS_ENV="$(dirname "$0")/access.env"
if [ -f "$ACCESS_ENV" ]; then
  # shellcheck disable=SC1090
  . "$ACCESS_ENV"
fi

HOST="${GARAGE_PUBLIC_HOST:-}"
TEAM="${CF_ACCESS_TEAM:-}"
ID="${CF_ACCESS_CLIENT_ID:-}"
SECRET="${CF_ACCESS_CLIENT_SECRET:-}"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf 'ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; }
note() { printf '      %s\n' "$1"; }

echo "target: https://$HOST"
echo "team:   $TEAM"
echo

# --- 0. Credential shape ----------------------------------------------------
#
# Cloudflare shows these two values beside the header names they belong in, so
# it is easy to copy "CF-Access-Client-Id: abc.access" when only "abc.access"
# is the value. Access answers that with a bare 403 and no explanation, so it
# is worth catching here where the message can be useful.

check_shape() {
  local label="$1" name="$2" value="$3" suffix="$4" want_len="$5"
  case "$value" in
    *:*)
      bad "$label contains a colon"
      note "You have probably pasted the header name along with the value."
      note "The value is only the part after '$name:'."
      return ;;
  esac
  case "$value" in
    *[[:space:]]*)
      bad "$label contains whitespace"
      note "Copy it again; there is a stray space or newline in it."
      return ;;
  esac
  if [ -n "$suffix" ]; then
    case "$value" in
      *"$suffix") : ;;
      *)
        bad "$label does not end in '$suffix'"
        note "That suffix is part of the value, not decoration."
        return ;;
    esac
  fi
  if [ "${#value}" -ne "$want_len" ]; then
    note "note: $label is ${#value} characters, expected $want_len."
    note "Not fatal - Cloudflare may have changed the format - but check it."
  fi
  ok "$label looks well formed"
}

if [ -n "$ID" ]; then
  check_shape "client id" "CF-Access-Client-Id" "$ID" ".access" 39
fi
if [ -n "$SECRET" ]; then
  check_shape "client secret" "CF-Access-Client-Secret" "$SECRET" "" 64
fi
if [ -n "$ID" ] || [ -n "$SECRET" ]; then
  echo
fi

# --- 1. DNS -----------------------------------------------------------------

addrs=$(dig +short "$HOST" 2>/dev/null | grep -E '^[0-9]+\.' | tr '\n' ' ')
if [ -z "$addrs" ]; then
  bad "DNS: $HOST does not resolve"
  note "Add a CNAME: $HOST -> d01d3247-a662-4a48-9582-adfde3d64154.cfargotunnel.com"
  note "Proxy status must be Proxied (orange cloud)."
  echo
  echo "passed: $PASS  failed: $FAIL"
  exit 1
fi
ok "DNS: resolves to $addrs"

# A proxied record answers as Cloudflare anycast space, never as a home IP.
# If this shows a residential address the orange cloud is off and the tunnel
# is being bypassed entirely.
case "$addrs" in
  104.*|172.6[4-9].*|172.7[0-1].*|188.114.*|190.93.*|197.234.*|198.41.*|162.15[89].*|173.245.*)
    ok "DNS: address is in Cloudflare anycast space (record is proxied)" ;;
  *)
    bad "DNS: $addrs does not look like Cloudflare"
    note "The record is probably set to DNS only. Turn the orange cloud on." ;;
esac

# --- 2. Access is actually in front -----------------------------------------

hdrs=$(curl -sS -m 20 -o /dev/null -D- "https://$HOST/garage/status" 2>/dev/null)
code=$(printf '%s' "$hdrs" | awk '/^HTTP/{c=$2} END{print c}')
location=$(printf '%s' "$hdrs" | awk 'BEGIN{IGNORECASE=1} /^location:/{print $2}' | tr -d '\r')

case "$code" in
  302|301)
    case "$location" in
      *cloudflareaccess.com*)
        ok "Access: unauthenticated request is redirected to the login page" ;;
      *)
        bad "Access: redirected somewhere unexpected: $location" ;;
    esac
    ;;
  403)
    # An application whose only policy is Service Auth has no interactive
    # login to redirect to, so Access denies outright instead of bouncing to
    # a login page. This is the stricter of the two outcomes, and for an
    # endpoint only ever called by a service token it is the better one.
    ok "Access: unauthenticated request is denied (403, no interactive policy)"
    note "No login redirect to read the AUD tag from. Copy it from"
    note "Zero Trust > Access > Applications > garage > Overview." ;;
  200)
    bad "ACCESS IS NOT PROTECTING THIS HOSTNAME - it answered 200 with no credentials"
    note "Anyone on the internet can reach the shim right now."
    note "Create the Access application for $HOST before going further, or"
    note "remove the ingress entry from cloudflared/config.yml until you have."
    ;;
  404)
    bad "Access: 404 - nothing is serving this hostname yet"
    note "Access let the request through but the origin has no route for it." ;;
  502|503)
    bad "Access: $code - reached nginx, but the garage shim is not answering"
    note "docker compose logs garage" ;;
  530|"")
    bad "Access: $code - the hostname is not routing through the tunnel"
    note "Check the ingress entry in cloudflared/config.yml, then restart cloudflared." ;;
  *)
    bad "Access: unexpected status $code" ;;
esac

# --- 3. Pull the AUD tag out of the login redirect ---------------------------
#
# The redirect carries a signed meta JWT whose aud claim is the application's
# Audience tag. Reading it here saves copying it out of the dashboard, and
# proves the value you put in garage-api.env belongs to this hostname.

if [ -n "$location" ]; then
  aud=$(printf '%s' "$location" | python3 -c '
import sys, base64, json, urllib.parse
url = sys.stdin.read().strip()
qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
meta = qs.get("meta", [""])[0]
try:
    payload = meta.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    print(claims.get("aud", ""))
except Exception:
    print("")
' 2>/dev/null)
  if [ -n "$aud" ]; then
    ok "Access: application audience (AUD) tag for $HOST is"
    note "$aud"
    note "That is the value for ACCESS_AUD in server/garage-api.env."
  fi
fi

# --- 4. The service token ----------------------------------------------------

if [ -z "$ID" ] || [ -z "$SECRET" ]; then
  echo
  note "CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET unset;"
  note "skipping the authenticated checks."
  echo
  echo "passed: $PASS  failed: $FAIL"
  [ "$FAIL" -eq 0 ]
  exit
fi

auth=(-H "CF-Access-Client-Id: $ID" -H "CF-Access-Client-Secret: $SECRET")

out=$(curl -sS -m 20 -w '\n%{http_code}' "${auth[@]}" "https://$HOST/garage/status" 2>&1)
code=$(printf '%s' "$out" | tail -n1)
body=$(printf '%s' "$out" | sed '$d')
case "$code" in
  200) ok "service token: GET /garage/status -> 200"; note "$body" ;;
  302)
    bad "service token: still bounced to the login page"
    note "The policy action is almost certainly Allow. It must be Service Auth."
    note "Zero Trust > Access > Applications > your app > Policies: create a"
    note "policy with action Service Auth and include Service Token > your token." ;;
  403)
    bad "service token: 403 - the token is not permitted by any policy"
    note "Check the token is the one named in the Service Auth policy, and that"
    note "the Client Id you sent ends in .access" ;;
  404)
    # The token got past Access - that part works. The 404 came from further
    # in. An empty body is cloudflared's http_status:404 catch-all, meaning
    # the tunnel has no ingress rule for this hostname yet. A JSON body means
    # nginx answered, so the vhost is live and only the path is unknown.
    if [ -z "$body" ]; then
      bad "service token: authenticated, but the tunnel has no route for $HOST"
      note "The service token works. The origin side is not deployed yet:"
      note "  cd server && docker compose --profile tunnel up -d --build"
      note "cloudflared only reads config.yml at startup, so restart it even"
      note "when the ingress entry is already in the file."
    else
      bad "service token: 404 from the origin"
      note "$body"
      note "Every path is namespaced: /garage/status, not /status."
    fi ;;
  *)  bad "service token: GET /garage/status -> $code"; note "$body" ;;
esac

# Full dress rehearsal, still without moving the door.
out=$(curl -sS -m 20 -w '\n%{http_code}' -X POST "${auth[@]}" "https://$HOST/garage/selftest" 2>&1)
code=$(printf '%s' "$out" | tail -n1)
body=$(printf '%s' "$out" | sed '$d')
case "$code" in
  200)
    ok "service token: POST /garage/selftest -> 200 (whole path good, door untouched)"
    note "$body" ;;
  502)
    bad "the server could not reach the controller on the LAN"
    note "cd server && docker compose logs garage-api - or journalctl -u"
    note "garage-api. Check the device is powered and on wifi, and that"
    note "GARAGE_HOST in garage-api.env is its current address." ;;
  *)  bad "service token: POST /garage/selftest -> $code"; note "$body" ;;
esac

echo
echo "passed: $PASS  failed: $FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo
  echo "The path is good, as far as it can be checked without moving the door."
  echo "GARAGE_TOKEN is the one link not exercised: the controller's only"
  echo "token-checked endpoint is the one that pulses the relay. If you have"
  echo "just rotated it, the first /toggle is the test."
  echo
  echo "A POST to /garage/toggle would now open or close the door. Camera first."
fi
[ "$FAIL" -eq 0 ]
