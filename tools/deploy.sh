#!/usr/bin/env bash
# Copy the firmware to the ESP32-S3 over USB using mpremote.
#
#   pip install mpremote
#   ./tools/deploy.sh                 # auto-detect the port
#   ./tools/deploy.sh /dev/ttyACM0    # or name it
#
# Add --reset to reboot the board and watch the serial console afterwards.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW="$REPO/firmware"
PORT=""
RESET=0

for arg in "$@"; do
  case "$arg" in
    --reset) RESET=1 ;;
    /dev/*) PORT="$arg" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

command -v mpremote >/dev/null || { echo "mpremote not found: pip install mpremote" >&2; exit 1; }

if [ ! -f "$FW/secrets.py" ]; then
  echo "error: $FW/secrets.py does not exist." >&2
  echo "       cp $FW/secrets.example.py $FW/secrets.py  and fill it in." >&2
  exit 1
fi

if grep -q "replace-me-with-a-long-random-string" "$FW/secrets.py"; then
  echo "error: secrets.py still contains the placeholder API_TOKEN." >&2
  echo "       generate one: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'" >&2
  exit 1
fi

CONNECT=(connect "${PORT:-auto}")

echo "deploying to ${PORT:-auto-detected port}"
for file in config.py garage.py main.py secrets.py; do
  echo "  -> $file"
  mpremote "${CONNECT[@]}" fs cp "$FW/$file" ":$file"
done

if grep -qE '^DRY_RUN\s*=\s*True' "$FW/config.py"; then
  echo
  echo "note: DRY_RUN is True. The relay pin will not be driven."
  echo "      Set DRY_RUN = False in firmware/config.py once the wiring is verified."
fi

if [ "$RESET" = "1" ]; then
  echo
  echo "resetting board, Ctrl-] to exit the console"
  mpremote "${CONNECT[@]}" reset
  sleep 1
  mpremote "${CONNECT[@]}" repl
fi
