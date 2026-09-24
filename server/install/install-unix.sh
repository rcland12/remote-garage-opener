#!/usr/bin/env sh
# Install the garage API as a native background service.
#
#   sudo ./install-unix.sh              Linux -> systemd, macOS -> launchd
#   sudo ./install-unix.sh --uninstall
#
# One script for both because the two differ in about six lines. It installs
# app.py and run.py to /opt/garage-api, the env file where the platform wants
# it, and registers the unit. It never starts anything before you have filled
# the env file in.
#
# POSIX sh on purpose: macOS ships bash 3.2 and Debian's /bin/sh is dash.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"
INSTALL_DIR=/opt/garage-api
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    -h | --help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }

case "$(uname -s)" in
  Linux)  PLATFORM=linux ;;
  Darwin) PLATFORM=macos ;;
  *) echo "unsupported: $(uname -s). Use Docker, or run run.py by hand." >&2
     exit 1 ;;
esac

# --- Uninstall --------------------------------------------------------------

if [ "$UNINSTALL" -eq 1 ]; then
  if [ "$PLATFORM" = linux ]; then
    systemctl disable --now garage-api 2>/dev/null || true
    rm -f /etc/systemd/system/garage-api.service
    systemctl daemon-reload
    echo "Removed the systemd unit. /etc/garage-api.env was left in place."
  else
    launchctl unload -w /Library/LaunchDaemons/com.garage.api.plist 2>/dev/null || true
    rm -f /Library/LaunchDaemons/com.garage.api.plist
    echo "Removed the launchd job. $INSTALL_DIR was left in place."
  fi
  exit 0
fi

# --- Python -----------------------------------------------------------------

PYTHON="${PYTHON:-$(command -v python3 || true)}"
[ -n "$PYTHON" ] || {
  echo "no python3 on PATH. Install it, or set PYTHON=/path/to/python3." >&2
  exit 1
}
echo "python:  $PYTHON"

# --- Files ------------------------------------------------------------------

mkdir -p "$INSTALL_DIR"
cp "$SRC/app.py" "$SRC/run.py" "$INSTALL_DIR/"
echo "files:   $INSTALL_DIR"

if [ "$PLATFORM" = linux ]; then
  ENV_FILE=/etc/garage-api.env
else
  ENV_FILE="$INSTALL_DIR/garage-api.env"
fi

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$SRC/garage-api.env" ]; then
    cp "$SRC/garage-api.env" "$ENV_FILE"
    echo "env:     $ENV_FILE (copied from $SRC/garage-api.env)"
  else
    cp "$SRC/garage-api.env.example" "$ENV_FILE"
    echo "env:     $ENV_FILE (from the example - FILL IT IN)"
    NEEDS_EDIT=1
  fi
else
  echo "env:     $ENV_FILE (kept, not overwritten)"
fi

# --- Service account and permissions ----------------------------------------
#
# The env file holds GARAGE_TOKEN, which is the key to the door. Nothing else
# on the machine has any business reading it.

if [ "$PLATFORM" = linux ]; then
  if ! id garage >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin garage
    echo "user:    created system user 'garage'"
  fi
  chown garage:garage "$ENV_FILE"
  RUN_USER=garage
else
  chown nobody:nobody "$ENV_FILE"
  RUN_USER=nobody
fi
chmod 600 "$ENV_FILE"

# --- PyJWT, only for access mode --------------------------------------------

if grep -Eq '^[[:space:]]*AUTH_MODE[[:space:]]*=[[:space:]]*access' "$ENV_FILE"; then
  if ! "$PYTHON" -c 'import jwt' 2>/dev/null; then
    echo "pyjwt:   installing (AUTH_MODE=access needs it)"
    "$PYTHON" -m pip install --quiet "PyJWT[crypto]==2.10.1" 2>/dev/null ||
      "$PYTHON" -m pip install --quiet --break-system-packages "PyJWT[crypto]==2.10.1" 2>/dev/null || {
        echo "pyjwt:   pip failed. Install it by hand, or use Docker." >&2
        echo "         On Debian/Ubuntu: apt install python3-jwt python3-cryptography" >&2
      }
  else
    echo "pyjwt:   already present"
  fi
fi

# --- Register ---------------------------------------------------------------

if [ "$PLATFORM" = linux ]; then
  sed "s|/usr/bin/python3|$PYTHON|" "$HERE/garage-api.service" \
    > /etc/systemd/system/garage-api.service
  systemctl daemon-reload
  echo "unit:    /etc/systemd/system/garage-api.service"
  START="sudo systemctl enable --now garage-api"
  LOGS="journalctl -u garage-api -f"
else
  sed -e "s|/usr/bin/python3|$PYTHON|" \
      -e "s|/opt/garage-api|$INSTALL_DIR|g" \
      "$HERE/com.garage.api.plist" > /Library/LaunchDaemons/com.garage.api.plist
  chown root:wheel /Library/LaunchDaemons/com.garage.api.plist
  chmod 644 /Library/LaunchDaemons/com.garage.api.plist
  echo "job:     /Library/LaunchDaemons/com.garage.api.plist"
  START="sudo launchctl load -w /Library/LaunchDaemons/com.garage.api.plist"
  LOGS="tail -f /var/log/garage-api.log"
fi

chown -R "$RUN_USER" "$INSTALL_DIR"

echo
if [ "${NEEDS_EDIT:-0}" -eq 1 ]; then
  echo "NEXT: edit $ENV_FILE. It will refuse to start without API_SCOPES,"
  echo "      and /garage/toggle will refuse without GARAGE_TOKEN."
  echo
fi
echo "  check    sudo -u $RUN_USER $PYTHON $INSTALL_DIR/run.py --env $ENV_FILE --check"
echo "  start    $START"
echo "  logs     $LOGS"
