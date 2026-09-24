#!/usr/bin/env python3
"""Start the garage API from an env file. Works the same on Linux, macOS and
Windows, which is the only reason it exists - `set -a; . garage-api.env` is
one line on a Unix shell and nothing at all on PowerShell.

    python3 run.py                          # reads ./garage-api.env
    python3 run.py --env /etc/garage-api.env
    python3 run.py --check                  # validate config and exit

Standard library only. Everything it reads is documented in
garage-api.env.example.
"""

import argparse
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE / "garage-api.env"


def load_env(path):
    """Parse a KEY=value file the way docker compose's env_file does.

    No shell, no expansion, no line continuation. A trailing backslash is part
    of the value, which is worth knowing because API_SCOPES is long and the
    temptation to wrap it is real.
    """
    loaded = 0
    for number, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            sys.exit(f"{path}:{number}: not KEY=value: {raw!r}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        # Strip one layer of matching quotes, as compose does.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # Anything already in the real environment wins, so a one-off
        # override does not need the file edited.
        os.environ.setdefault(key, value)
        loaded += 1
    return loaded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env",
        type=Path,
        default=DEFAULT_ENV,
        help="env file to read (default: %(default)s)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="load the config, report what it would do, and exit",
    )
    args = parser.parse_args()

    if args.env.exists():
        count = load_env(args.env)
        print(f"loaded {count} settings from {args.env}", file=sys.stderr)
    elif args.env != DEFAULT_ENV:
        sys.exit(f"fatal: no such env file: {args.env}")
    else:
        print(
            f"note: {args.env} not found; using the environment as it is.\n"
            f"      Copy garage-api.env.example to garage-api.env to start.",
            file=sys.stderr,
        )

    if args.check:
        # Importing app.py runs its configuration block, which exits with a
        # readable message on anything missing. Nothing binds a socket until
        # main() is called, which --check does not do.
        sys.path.insert(0, str(HERE))
        import app

        print("config ok")
        print(f"  auth mode   {app.AUTH_MODE}")
        print(f"  listen      {app.LISTEN_ADDR}:{app.LISTEN_PORT}")
        print(f"  device      {app.GARAGE_HOST}")
        print(
            "  device token"
            f"{' set' if app.GARAGE_TOKEN else ' MISSING - /toggle will 502'}"
        )
        print(f"  identities  {', '.join(sorted(app.API_SCOPES)) or 'none'}")
        return

    sys.path.insert(0, str(HERE))
    runpy.run_path(str(HERE / "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
