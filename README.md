# Remote garage door controller

Adds remote operation to a Linear garage door opener using an ESP32-S3 and an
optocoupler relay. The relay momentarily bridges the opener's pushbutton
terminals, exactly like pressing the wall button.

There is no door sensor. This is a remote wall button: it presses, and that is
all it does. It cannot tell you whether the door is open or closed, and it
does not pretend to. Use a camera in the garage for that, and look at it
before you press.

The device is LAN only. Reaching it from outside the house is a separate
service's job, and it is in this repo: `server/` is a small Python API that
holds the device token, authenticates every caller, and makes the LAN-local
call to the ESP32. Nothing routes to the controller from the internet.

That server runs on **Linux, macOS or Windows**, on whatever machine you
already leave switched on - a Raspberry Pi, a NAS, a mini PC, the old laptop
in the closet. It is one Python file with one optional dependency. There is a
Docker Compose stack, and a native install for each of the three platforms.
See `server/README.md`, and `docs/remote-access.md` for how to reach it from
outside.

## Quick start

    # 1. configure
    cp firmware/secrets.example.py firmware/secrets.py
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    $EDITOR firmware/secrets.py

    # 2. flash MicroPython, then deploy       (docs/flashing.md)
    ./tools/deploy.sh --reset

    # 3. wire it up                            (docs/hardware.md)

    # 4. drive it
    export GARAGE_HOST=garage.home.arpa GARAGE_TOKEN=...
    ./tools/garage.py status
    ./tools/garage.py toggle

    # 5. optional: reach it from outside the house  (server/README.md)
    cd server && cp garage-api.env.example garage-api.env
    $EDITOR garage-api.env
    docker compose up -d --build

`DRY_RUN` in `firmware/config.py` gates whether the relay pin is driven at all.
It is currently `False`, because this build is commissioned and moving a real
door. Set it back to `True` before any bench work: with it on, `POST /toggle`
only writes a log line and the pin is never configured, so you can exercise
the whole API with the relay wired and nothing will move.

## API

The device's own API. The server in `server/` puts the same commands one hop
further out, namespaced under `/garage`, behind a credential you can revoke.

| Method | Path | Auth | Result |
|---|---|---|---|
| GET | `/health` | no | liveness, uptime |
| GET | `/status` | no | moving hint, uptime, RSSI, IP, reset cause |
| GET | `/log` | no | recent events from the in-memory ring buffer |
| POST | `/toggle` | yes | pulse the relay for 500 ms |

There is deliberately no `/open` or `/close`. Without a door sensor the device
cannot distinguish an open door from a closed one, so a "close" command would
be a coin flip that opens the garage half the time. One button, the same one
that is on the wall. Both paths return 404.

The `moving` field in `/status` is a timer counting down `TRAVEL_MS` from the
last pulse, not a measurement. It means "we pulsed recently, so the door is
probably still travelling." It is not evidence the door moved at all.

Auth is a shared secret in the `X-Auth-Token` header, compared in constant
time. Everything returns JSON with an `ok` field.

    curl -X POST -H "X-Auth-Token: $GARAGE_TOKEN" http://$GARAGE_HOST/toggle

A second pulse inside the cooldown window returns 429 rather than reversing a
door that just started moving.

## The server

`server/` is the other half of the build: a small API that fronts the device
so you can reach the door from outside the house without exposing anything.
It holds the device token, authenticates every caller, applies a per-caller
scope and a cooldown, and then makes the one LAN call the device understands.

    phone / laptop
      -> https://api.example.com/garage/toggle
      -> Cloudflare Access                  service token policy, at the edge
      -> cloudflared -> the server          on a machine in your house
      -> http://192.168.1.50/toggle         X-Auth-Token, LAN only
      -> the relay closes for 500 ms

**It runs anywhere.** One Python file, standard library only unless you use
Cloudflare Access, which adds PyJWT. Point it at a machine that stays on and
is on the same LAN as the ESP32 - a Raspberry Pi, a NAS, a mini PC, a desktop,
the laptop with the cracked screen. Pick an install:

| | Docker | Native |
|---|---|---|
| **Linux** | `docker compose up -d --build` | systemd unit, `sudo ./install/install-unix.sh` |
| **macOS** | Docker Desktop, same command | launchd daemon, same install script |
| **Windows** | Docker Desktop, same command | Scheduled Task, `.\install\install-windows.ps1` |

The native installs run at boot with no user logged in, which Docker Desktop
on macOS and Windows does not. On Linux either is fine.

Two ways to reach it, chosen with `AUTH_MODE`:

- **`access`** - a Cloudflare Tunnel and an Access service token policy. A
  real HTTPS URL with nothing port forwarded, and the only option an iPhone
  Shortcut can use, because a Shortcut cannot bring up a VPN first.
- **`token`** - no Cloudflare at all. Reach the machine over Tailscale,
  WireGuard or an SSH forward, and the server checks a bearer key per client
  on top of that.

| Method | Path | Result |
|---|---|---|
| POST | `/garage/toggle` | pulse the relay |
| POST | `/garage/selftest` | the same path, no pulse |
| GET | `/garage/status` | the device's status |
| GET | `/garage/log` | the device's event ring |
| GET | `/garage/audit` | who has pressed the button |
| GET | `/service/actions` | what these credentials may call |

Setup, per-OS notes, the Cloudflare dashboard steps and the iPhone Shortcut
recipes are all in `server/README.md`.

## Safety properties

These are the invariants the build is made of. Changing any of them should be
a deliberate decision, not a side effect of something else.

In the firmware:

- The relay pin is idle at boot, after a crash, and after a watchdog reset.
  It is initialised with `Pin(..., value=idle)` so there is no window where
  it is driven active.
- The relay only ever pulses. `Relay.poll()` releases it from the main loop;
  no code path can latch it on.
- GPIO 5 is a plain GPIO. Strapping pins 0, 3, 45 and 46 are never used.
- A hardware watchdog reboots a wedged board. The reboot cannot move the door.
- The only electrical connection to the opener is the isolated dry contact.
  Nothing touches the opener's 28 V supply.

In the server:

- `API_SCOPES` is deny by default, and empty is a fatal boot error. There is
  no safe permissive default for something that opens a garage door.
- `GARAGE_TOKEN` unset is deliberately not fatal. The service boots, the
  read-only routes work, and `/garage/toggle` refuses with a 502. Failing
  closed for a door means refusing to move it, not refusing to start.
- Nothing proxies a request through to the device. `device_call` hand-builds
  one with a single header, because the firmware's `MAX_REQUEST_BYTES` is
  2048 and a tunneled request is larger than that. Calls are serialised; the
  device serves one connection at a time.
- The device token never leaves the machine the server runs on. Clients hold
  a credential that can be revoked from a dashboard instead.
- The process is unprivileged, read-only, no volumes, no host paths, no
  capabilities. Every action it takes is one outbound HTTP call.

## Layout

    firmware/     MicroPython: config.py, garage.py, main.py, secrets.example.py
    server/       app.py             the API that fronts the device
                  run.py             start it from an env file, any OS
                  README.md          install, Cloudflare setup, troubleshooting
                  garage-api.env.example  every setting, with the reasoning
                  compose.yml        docker, with an optional cloudflared
                  Dockerfile         unprivileged, read-only, no volumes
                  install/           systemd, launchd, Windows scheduled task
                  cloudflared/       tunnel config example
    tools/        deploy.sh          push firmware to the board over USB
                  deploy.py          the same, cross-platform, and it names
                                     the UART bridge port by vendor id
                  garage.py          full client for both APIs: status, log,
                                     audit, actions, selftest, toggle, watch
                  toggle.sh          one-shot pulse, nothing else
                  verify_remote.sh   check the Cloudflare Access path end to end
                  smoke_test.sh      exercise every device endpoint
                  smoke_test.py      the same, without curl or bash
                  simulate.py        run the firmware on a desktop, no hardware
    docs/         hardware.md, flashing.md, remote-access.md
    enclosure/    build_enclosure.py  parametric Fusion 360 model, source of truth
                  *.stl               exported base and lid
    article/      medium-article.md   the writeup, not part of the build
                  medium-draft.md     the outline it came from
                  assets.md           shot list and pre-publish checklist

The `.py` tools are there so the build can be followed from Windows, where
neither bash nor curl is a given. They are standard library only and behave
identically to their shell counterparts; `garage.py` and `simulate.py` already
ran anywhere. Two implementations of the same check can drift, so if the shell
versions stop earning their keep, the Python ones are the better survivors.

## Testing without hardware

`tools/simulate.py` runs the real firmware on a desktop against stub hardware,
so the API, the auth path and the 500 ms pulse can all be exercised before the
parts are wired.

    ./tools/simulate.py --port 8099
    GARAGE_HOST=127.0.0.1:8099 GARAGE_TOKEN=simulator-token ./tools/smoke_test.sh --pulse

## Status

Deployed and in service. The controller sits on top of the opener's motor head
in its printed enclosure, wired to the `PUSHBUTTON` and `COMMON` terminals, and
moves the door on request.

Verified on the hardware, not just in simulation:

- 11/11 endpoint checks pass against the device
- Relay module is active HIGH; `RELAY_ACTIVE_HIGH = True`
- A 10k pull-down on GPIO 5 holds the relay off through the window between
  power-on and MicroPython configuring the pin. Confirmed silent across
  repeated resets and cold power-cycles, which is the test that matters
- Contact closes for roughly 500 ms per pulse, measured on a meter
- WiFi sits at -18 to -32 dBm in its installed position

Remote access is the Cloudflare Access path in `docs/remote-access.md`, served
by `server/` and driven from an iPhone Shortcut.

Outstanding: the enclosure has been revised since the installed one was
printed. The current STLs add a 180 degree ESP32 rotation so the USB ports
clear their guide wall, a wider and taller USB opening, a lid grip tongue, and
a looser slide fit. Reprint both parts when convenient; nothing is wrong with
the installed one.
