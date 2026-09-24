# Progress

State of the build. Written down because the useful parts are the decisions
and the measurements, not the code, and neither is recoverable from a diff.

## Where it is

Deployed and in service. The controller sits on top of the opener's motor
head, wired to the `PUSHBUTTON` and `COMMON` terminals, and moves the door.

## Firmware

Three files plus a secrets template, MicroPython 1.28.0 on an ESP32-S3.

- `main.py` - WiFi with auto-reconnect, HTTP API, one cooperative loop. The
  listening socket is polled with a 20 ms timeout so servicing the relay and
  the watchdog never waits on a client, and no client waits on a 500 ms pulse.
- `garage.py` - relay pulse state machine and the event ring buffer.
- `config.py` - pins, timings, `DRY_RUN`, relay polarity.

Four routes: `GET /health`, `GET /status`, `GET /log`, `POST /toggle`.

There is no `/open` or `/close`. There was, early on, and they read a reed
switch to decide whether to no-op. Both are gone along with the reed switch
itself. Without a sensor those endpoints can only guess, and a wrong guess on
`/close` opens the garage and leaves it open.

Worth knowing about the removal: with the reed switch gone but the endpoints
still present, `/open` would have returned `"ok": true, "action": "none"` and
never pulsed the relay, because the unconnected pin read as "already open"
forever. `tools/smoke_test.sh` only exercised `/toggle`, so it passed 9/9 while
`/open` was silently dead. The smoke test now asserts both paths return 404.

## Hardware

| Item | Value |
|---|---|
| Board | ESP32-S3 N16R8, 16 MB flash, 8 MB octal PSRAM |
| Bridge chip | CH343 (`1a86:55d3`), enumerates as `/dev/ttyACM0` |
| Relay | Songle SRD-03VDC-SL-C module, screw terminal inputs |
| Trigger polarity | **Active HIGH**, measured |
| Pull-down | 10k, GPIO 5 to GND |

Two things here cost real time and are worth remembering.

**The board has two USB-C ports, `COM` and `USB`.** Use `COM`. It is the UART
bridge, its DTR/RTS drive EN and GPIO 0, so esptool enters the bootloader on
its own and the port survives whatever the firmware does. Do not identify it
by device name: this board's CH343 is a CDC-class part, so `COM` appears as
`ttyACM0`, the same name native USB would use. Tell them apart by USB vendor
id, `1a86` for the bridge and `303a` for Espressif.

**The relay is active HIGH, which is the uncommon case.** Idle is therefore a
LOW pin, and between power-on and MicroPython configuring GPIO 5 that pin is
an undriven input that floats. A pin floating high is a relay clicking on
boot, which is a garage door opening during a thunderstorm. The 10k pull-down
exists for exactly that window. It was fitted before testing rather than after,
because whether a floating pin drifts high enough depends on temperature and
humidity, and passing three resets on a desk in July proves nothing about a
cold morning in January.

## Enclosure

Parametric Fusion 360 model, driven over an SSH tunnel through the fusion360
MCP bridge. `enclosure/build_enclosure.py` is the source of truth; it is
idempotent and rebuilds everything from its parameter table.

Both boards are retained by their edges, not screwed down: neither has usable
mounting holes. Rails support the PCB edges, guide walls locate them
laterally, and a sliding lid closes it.

Decisions that are not obvious from the geometry:

- **The ESP32 faces component-side down.** The Dupont wires must reach the
  pins, so the pins point up. The cost is that its LED, reset and BOOT buttons
  face the floor and are unreachable once assembled. Acceptable because
  flashing happens over `COM` with auto-reset.
- **The ESP32 is rotated 180 degrees** from the arrangement you would first
  reach for. The guide wall has to bear on one end of the board, and against
  the USB end it blocks the ports. Against the antenna end it does not. This
  also puts the USB cable on the same side as the wall outlet.
- **The antenna keep-out was relaxed from 20 mm to 10 mm** to shorten the box.
  Measured RSSI in the installed position is -18 to -32 dBm, roughly 50 dB of
  margin, so this is not close to mattering. If range ever looks marginal,
  widen `board_gap` or put the keep-out back.
- **Vented rather than sealed.** South Georgia: hot, very humid. A sealed but
  non-waterproof box breathes through its seams as temperature swings and
  accumulates condensation with no way out. Vents low on one wall and high on
  the opposite drive convection. Weep slots sit at the floor line because a
  box lying flat cannot drain downward.

The installed enclosure predates the 180 degree rotation, the enlarged USB
opening, the lid grip tongue and the looser slide fit. Reprint when convenient.

## Remote access

Cloudflare Tunnel plus an Access-gated API, driven from an iPhone Shortcut.
The device's own posture is unchanged and must stay that way: LAN only, plain
HTTP, one shared token, nothing routed to it from outside.

The server holds `API_TOKEN`; the phone holds only Cloudflare service token
credentials, which are revocable from the dashboard in seconds. Losing the
phone does not mean reflashing the ESP32.

**That server now lives in this repo, at `server/`.** It was one area of a
larger multi-purpose endpoint on the owner's own server; the garage part was
lifted out into a standalone service so the build is complete in one place -
until now, following this repo got you a controller you could only reach from
the LAN, with the other half described but not shipped. Decisions made in the
move:

- **Only the garage area came across.** The original also fronted a passcode
  endpoint, fail2ban unbans and AI usage counters. None of that belongs here
  and none of it was copied.
- **A second auth mode, `AUTH_MODE=token`.** The original assumed Cloudflare
  Access, which assumes a domain and a Zero Trust account. `docs/remote-access.md`
  has always listed Tailscale as the recommended option, so the service now
  accepts a per-client bearer key for the deployments where there is no
  Cloudflare in the path. `access` is still the default.
- **No nginx.** Upstream, nginx did the path allowlisting and rate limiting.
  With five routes and an app that 404s everything else, the allowlist is
  redundant; the rate limit moved in-process. One less thing to install on
  whatever machine is going to run this.
- **Native installs for all three platforms, not just Docker.** Docker
  Desktop on macOS and Windows starts at login, not at boot, which is the
  wrong behaviour for a service that has to answer when nobody is home. The
  systemd unit, the launchd daemon and the Windows scheduled task all start
  without a session.
- **`tools/garage.py` and `tools/verify_remote.sh` learned the namespaced
  paths.** The upstream API puts everything under `/garage/*`; the client was
  still asking the tunnel for `/status`. `garage.py` also picked up
  `GARAGE_API_KEY` for token mode and an `actions` command.

`tools/verify_remote.sh` checks that path end to end without moving the door.
It has one blind spot it reports honestly: the controller's only
token-checked route is the one that pulses the relay, so nothing can confirm
`GARAGE_TOKEN` is correct without opening the garage. After rotating it, the
first `/toggle` is the test, and it is one to run standing in the garage.

## Standing constraints

- Camera first, every time. The device cannot tell you which way the door will
  go, and it cannot tell you whether anyone is standing in the doorway.
- Do not automate `/toggle`. A schedule or a geofence has no idea what the
  door is doing, and the failure mode is an open garage you find out about
  when you get home.
- Test the opener's photo-eye sensors periodically. Operating a door you
  cannot see makes them the only thing protecting the doorway.
