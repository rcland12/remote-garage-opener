# The garage API

The piece that stands between the internet and the ESP32.

    phone / laptop
      -> https://api.example.com/garage/toggle
      -> Cloudflare Access                    service token policy, at the edge
      -> cloudflared -> this service          on a machine in your house
      -> http://192.168.1.50/toggle           X-Auth-Token, LAN only
      -> the relay closes for 500 ms

The controller is LAN only, plain HTTP, one shared token, and it must stay
that way. It has no TLS, no rate limiting and no update path. This service is
what makes that posture safe: it is the only thing that ever calls the device,
it holds the device token, and it authenticates every caller before it will
make that call. Nothing is port forwarded and the router keeps no inbound
holes.

It is one Python file with one optional dependency, so the machine it runs on
can be almost anything: a Raspberry Pi, a NAS, a mini PC, the laptop with the
cracked screen in the closet. It needs to be on the same LAN as the ESP32, to
stay powered on, and to have Python 3.9 or newer or Docker. That is the entire
requirement list.

- [Pick a machine](#pick-a-machine)
- [Pick how you reach it](#pick-how-you-reach-it)
- [Endpoints](#endpoints)
- [Configuration](#configuration)
- [Install: Docker](#install-docker) - Linux, macOS, Windows
- [Install: Linux](#install-linux) - systemd
- [Install: macOS](#install-macos) - launchd
- [Install: Windows](#install-windows) - Scheduled Task
- [Cloudflare Tunnel](#cloudflare-tunnel)
- [Verify](#verify)
- [iPhone Shortcuts](#iphone-shortcuts)
- [How it stays safe](#how-it-stays-safe)
- [Troubleshooting](#troubleshooting)

## Pick a machine

| Host | Notes |
|---|---|
| Raspberry Pi (any model with WiFi or ethernet) | The obvious answer. A Pi Zero 2 W is plenty; this service idles at a few MB |
| An old laptop | Works well. Close the lid and disable sleep - see the per-OS notes below. A laptop has a built-in UPS, which a Pi does not |
| A NAS (Synology, unRAID, TrueNAS) | Use the Docker path. Most NAS software has a Compose UI |
| A mini PC or a home server | Same as any Linux box |
| A desktop that is already always on | Fine. It just has to be up when you want the door to move |

Whatever it is, give it a static address or a DHCP reservation too. Not for
this service's sake - nothing calls it by name on the LAN - but because you
will want to SSH to it later, and hunting for it on the day the tunnel is down
is the wrong time.

Do **not** run this on a VPS or anything outside the house. It has to reach
the ESP32 over the LAN, and the whole design rests on the device being
unreachable from anywhere else.

## Pick how you reach it

Two modes, set by `AUTH_MODE` in the env file. They differ in who checks the
caller's credentials, not in what the service does afterwards.

### `AUTH_MODE=access` - Cloudflare Access

A public hostname, gated at Cloudflare's edge by a service token policy, with
a tunnel from your machine outward so nothing is forwarded inward. This is the
mode a **phone Shortcut** can use, because a Shortcut cannot bring up a VPN
first and then make a request.

Needs: a domain on Cloudflare, a free Zero Trust account. Follow
[Cloudflare Tunnel](#cloudflare-tunnel) below.

### `AUTH_MODE=token` - a private network

No Cloudflare, no public hostname. You reach the machine over Tailscale,
WireGuard or an SSH port forward, and this service checks a bearer key per
client on top of that.

Needs: nothing but the private network. `tailscale up` on two devices is
about ninety seconds of work.

    curl -H "X-Api-Key: $KEY" http://laptop.your-tailnet.ts.net:8080/garage/status

**Never expose token mode to the internet.** There is no TLS here and no
lockout, so a public listener is one guessed key away from an open garage. If
you want a URL from anywhere, use access mode - that is what it is for.

`docs/remote-access.md` in this repo compares the transports in more detail,
including plain SSH forwarding, which needs no key at all.

## Endpoints

Everything except `/service/health` requires credentials **and** a scope.

| Method | Path | Action | Result |
|---|---|---|---|
| POST | `/garage/toggle` | `garage.toggle` | Pulse the relay for 500 ms |
| POST | `/garage/selftest` | `garage.selftest` | The same path, no pulse |
| GET | `/garage/status` | `garage.status` | Device uptime, RSSI, moving hint |
| GET | `/garage/log` | `garage.log` | The device's event ring buffer |
| GET | `/garage/audit` | `garage.audit` | Who has pressed the button |
| GET | `/service/actions` | (no scope needed) | What this caller may do |
| GET | `/service/audit` | `service.audit` | Every event, not just the door |
| GET | `/service/health` | (no auth, do not publish) | Liveness, for health checks |

**There is no `/open` and no `/close`**, here or on the device. The controller
has no door sensor, so it cannot know which way a pulse will send the door,
and a `/close` that guessed wrong would open the garage and leave it open.
Look at the camera, then toggle.

Every response is JSON with an `ok` field. No route takes a request body; any
body sent is read and discarded.

Every path is namespaced by area, including the service's own introspection
under `/service`. Nothing sits at a bare `/status` or `/log`. The action name
is the path with dots for slashes, which is what makes an `API_SCOPES` pattern
like `garage.*` line up with `/garage/*`.

## Configuration

One file, `garage-api.env`. Copy the example and read the comments in it -
they are the reference, and this section is the summary.

    cp garage-api.env.example garage-api.env
    chmod 600 garage-api.env

| Variable | Required | What it is |
|---|---|---|
| `AUTH_MODE` | - | `access` (default) or `token` |
| `ACCESS_TEAM_DOMAIN` | access mode | `<team>.cloudflareaccess.com` |
| `ACCESS_AUD` | access mode | The Access application's Audience tag |
| `API_CLIENTS` | token mode | `name:key;name:key`, minimum 32 chars each |
| `API_SCOPES` | **yes** | Who may call what. Deny by default |
| `GARAGE_HOST` | - | The ESP32's LAN address. Default `192.168.1.50` |
| `GARAGE_TOKEN` | for the door | Must match `API_TOKEN` in `firmware/secrets.py` |
| `TOGGLE_COOLDOWN_S` | - | `5`. Seconds before a second pulse is allowed |
| `DEVICE_TIMEOUT_S` | - | `8` |
| `LISTEN_ADDR` / `LISTEN_PORT` | - | `0.0.0.0` / `8080` |
| `RATE_LIMIT_PER_MIN` | - | `60` per identity. `0` disables |
| `AUDIT_SIZE` | - | `200` events kept in memory |

`API_SCOPES` is the only variable that is fatal if missing: the service
refuses to boot without it, because there is no safe permissive default for
something that can open a garage door. `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`
are fatal in access mode for the same reason.

`GARAGE_TOKEN` is deliberately **not** fatal. Unset, the service starts, the
read-only routes work, and `/garage/toggle` refuses with a clear 502. Failing
closed for a door means refusing to move it.

### `API_SCOPES`

Deny by default. An identity is either an Access service token Client ID
(including the trailing `.access`) or an email, in access mode; or a name from
`API_CLIENTS`, in token mode. Actions are exact names, prefix wildcards
(`garage.*`), or a bare `*`.

    API_SCOPES=aaa.access=garage.toggle,garage.selftest;you@example.com=*
    API_SCOPES=phone=garage.toggle,garage.selftest;laptop=*

**Write it as one unbroken line.** An env file has no line continuation - a
trailing backslash becomes part of the value and that entry silently never
matches. An identity listed twice keeps only the last entry, so give each one
entry with all of its actions in it.

Issue one credential per client and scope it to what that client does. A
roommate who only opens the door wants `garage.toggle,garage.selftest` - not
`garage.audit`, which is the record of who opened it and when.

### Check it before you start anything

    python3 run.py --check

Prints the mode, the listen address, the device address and the identities it
parsed, then exits without binding a socket. Every configuration error this
service can have produces a specific message here rather than a mystery 502
later.

## Install: Docker

The same three commands on all three operating systems. Needs Docker Engine
with the Compose plugin on Linux, or Docker Desktop on macOS and Windows.

    cd server
    cp garage-api.env.example garage-api.env      # then edit it
    docker compose up -d --build

    docker compose logs -f garage-api
    docker compose ps

Add the tunnel once you have set up Cloudflare below:

    docker compose --profile tunnel up -d

The container publishes to `127.0.0.1:8080` by default, which is all
cloudflared needs - it reaches the service over the compose network, not the
published port. Override the bind address when something else has to reach it
directly:

    BIND_ADDR=0.0.0.0 docker compose up -d          # the whole LAN
    BIND_ADDR=100.101.102.103 docker compose up -d  # a Tailscale address only

Publishing a port has nothing to do with the container's ability to reach your
LAN. Outbound to the ESP32 works either way.

The container runs unprivileged: `read_only: true`, `cap_drop: ALL`, no
volumes, no host paths. Nothing it does needs any of that.

**On Windows**, run this from PowerShell in WSL2-backed Docker Desktop and
make sure "Start Docker Desktop when you log in" is on, or the API is down
after every reboot until someone logs in. If that is not acceptable, use the
[Scheduled Task install](#install-windows) instead - it starts at boot with no
user session.

**On macOS**, Docker Desktop has the same caveat: it starts at login, not at
boot. Turn on "Start Docker Desktop when you sign in" and set the machine to
log in automatically, or use the [launchd install](#install-macos), which is a
LaunchDaemon and needs no session at all.

## Install: Linux

Native, no Docker, as a systemd service.

    cd server
    cp garage-api.env.example garage-api.env      # then edit it
    sudo ./install/install-unix.sh

    sudo systemctl enable --now garage-api
    journalctl -u garage-api -f

The script creates a `garage` system user, installs `app.py` and `run.py` to
`/opt/garage-api`, puts the env file at `/etc/garage-api.env` owned by that
user with mode 600, installs PyJWT if you are in access mode, and registers
the unit. It does not start anything: fill the env file in first.

The unit is locked down about as far as a network service can be -
`ProtectSystem=strict`, an empty capability bounding set, `AF_INET`/`AF_INET6`
sockets and nothing else. Read `install/garage-api.service`; if you would
rather install by hand, the header comment is the manual procedure.

To remove it: `sudo ./install/install-unix.sh --uninstall`.

**On a laptop**, stop it suspending when you close the lid:

    sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' \
        /etc/systemd/logind.conf
    sudo systemctl restart systemd-logind

## Install: macOS

Native, as a LaunchDaemon - so it runs whether or not anyone is logged in,
which is the only sensible arrangement for a machine in a closet.

    cd server
    cp garage-api.env.example garage-api.env      # then edit it
    sudo ./install/install-unix.sh

    sudo launchctl load -w /Library/LaunchDaemons/com.garage.api.plist
    tail -f /var/log/garage-api.log

Same script as Linux; it detects Darwin and installs the plist instead of a
unit. Everything lands in `/opt/garage-api`, the job runs as `nobody`, and the
env file is mode 600.

If `/usr/bin/python3` prompts to install the Command Line Tools, let it, or
run `xcode-select --install` first. A Homebrew python works too - pass
`PYTHON=/opt/homebrew/bin/python3` to the install script.

Stop it sleeping:

    sudo pmset -a sleep 0 disksleep 0
    sudo pmset -a powernap 0

On a laptop, `sudo pmset -b sleep 0` covers battery too, and closing the lid
still sleeps a Mac unless it is on external power. If the lid has to be shut,
keep it plugged in.

To remove it: `sudo ./install/install-unix.sh --uninstall`.

## Install: Windows

Native, as a Scheduled Task that starts at boot as `SYSTEM` - no login
required, no console window.

From an **elevated** PowerShell prompt:

    cd server
    Set-ExecutionPolicy -Scope Process Bypass
    .\install\install-windows.ps1

    # fill in C:\garage-api\garage-api.env, then
    Start-ScheduledTask -TaskName GarageApi
    Get-Content C:\garage-api\garage-api.log -Wait -Tail 20

The script needs Python from python.org with "Add python.exe to PATH" ticked.
It copies `app.py` and `run.py` to `C:\garage-api`, creates the env file from
the example, strips its ACL down to SYSTEM and Administrators, installs PyJWT
if you are in access mode, and registers the task.

A Scheduled Task rather than a real Windows service because a plain Python
script does not perform the Service Control Manager handshake a service
requires. The usual answer is NSSM or WinSW, and a third-party wrapper is a
poor trade for one background process. If you already run NSSM,
`nssm install GarageApi <python> C:\garage-api\run.py` works and gives you
`sc.exe query` for free.

    Get-ScheduledTask -TaskName GarageApi | Get-ScheduledTaskInfo   # status
    Stop-ScheduledTask -TaskName GarageApi                          # stop
    .\install\install-windows.ps1 -Uninstall                        # remove

Then stop the machine sleeping, or it will be asleep the one time you need it:

    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0

On a laptop, also set "Close the lid" to "Do nothing" under Control Panel >
Power Options > Choose what closing the lid does.

## Cloudflare Tunnel

Only for `AUTH_MODE=access`. Skip this entire section if you are using
Tailscale or WireGuard.

You need a domain whose nameservers point at Cloudflare, and a Zero Trust
account - the free plan covers up to 50 users, which is 49 more than this
needs.

### 1. Install cloudflared

Using the Docker profile in `compose.yml` skips this; the image brings its
own. Otherwise:

    # Linux (Debian/Ubuntu)
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
      | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
      https://pkg.cloudflare.com/cloudflared any main" \
      | sudo tee /etc/apt/sources.list.d/cloudflared.list
    sudo apt update && sudo apt install cloudflared

    # macOS
    brew install cloudflared

    # Windows
    winget install --id Cloudflare.cloudflared

### 2. Create the tunnel

    cloudflared tunnel login          # opens a browser, pick your domain
    cloudflared tunnel create garage  # prints a UUID and writes a json key
    cloudflared tunnel route dns garage api.example.com

`create` writes the tunnel's credentials to `~/.cloudflared/<uuid>.json`
(`%USERPROFILE%\.cloudflared\<uuid>.json` on Windows). **That file is the
tunnel's private key** - it alone is enough to serve traffic for your
hostname. Treat it like an SSH key.

### 3. Configure it

    cp cloudflared/config.example.yml cloudflared/config.yml
    $EDITOR cloudflared/config.yml

Set `tunnel:` to the UUID and both `hostname:` lines to your hostname. If you
are using the Docker profile, also copy the credentials in beside it:

    cp ~/.cloudflared/<uuid>.json cloudflared/credentials.json
    chmod 600 cloudflared/credentials.json

Both files are gitignored.

Note the first ingress rule, which 404s `/service/health` at the edge. That
route is unauthenticated - it is what the container health check calls - so it
has no business being reachable from the internet.

### 4. The Access application

Zero Trust > Access > Applications > Add an application > Self-hosted.

- Application domain: your hostname, **path blank**. A path-scoped app
  protects only that path and leaves the rest of the hostname public.
- App Launcher visibility: hide it.
- **Policy for phones**: action **Service Auth**, and include each service
  token **by name**. Not action Allow - that forces an interactive login a
  Shortcut cannot complete, which shows up as a 302 to the login page while
  you are sending perfectly valid credentials. Not "Any Access Service Token"
  either - that admits every token in the account, including ones you create
  later for something unrelated.
- **Policy for browsers**: action Allow, include Emails > your address.
- Never add a **Bypass** policy. It disables authentication entirely and is
  evaluated ahead of everything else.

Copy the **Application Audience (AUD) Tag** from the Overview tab into
`ACCESS_AUD`, and your team domain into `ACCESS_TEAM_DOMAIN`.

`api.example.com` is a hostname that tends to grow: today it is the garage,
next year it is the garage and two other things. Keep one Access application
covering the whole hostname with path blank for as long as this is the only
service on it. If you do add another, give each area its own application
scoped to its own path (`/garage`, `/whatever`) - each gets its own AUD tag,
and this service's `ACCESS_AUD` becomes the garage one. Do not leave a
path-scoped app as the only policy on the hostname; everything outside that
path would be public.

### 5. Service tokens

Zero Trust > Access > Service Auth > Service Tokens. One per client.

The Client ID ends in `.access`, and dropping that suffix is the single most
common cause of a correct-looking 403. The secret is shown once. Note the
expiry - that is the credential that actually lapses, usually a year later,
usually at an inconvenient moment.

Put each Client ID into `API_SCOPES` with the actions that client needs.

### 6. Start it

    docker compose --profile tunnel up -d

    # or, running cloudflared natively
    cloudflared tunnel --config cloudflared/config.yml run

To run a native cloudflared at boot: `sudo cloudflared service install` on
Linux and macOS, `cloudflared service install` from an elevated prompt on
Windows.

## Verify

From the machine itself:

    curl -sS http://127.0.0.1:8080/service/health

    # docker
    docker compose exec garage-api curl -sS http://127.0.0.1:8080/service/health

Then, in access mode, that the gate is actually closed. Every line must be
403 - a 200 here means the Access policy is not applied:

    for p in / /garage/status /service/actions; do
      curl -sS -m 15 -o /dev/null -w "$p -> %{http_code}\n" \
        "https://api.example.com$p"
    done

Then with credentials:

    curl -sS https://api.example.com/service/actions \
      -H "CF-Access-Client-Id: <id>.access" \
      -H "CF-Access-Client-Secret: <secret>"

`allowed` is exactly what that token may call. If something is missing, that
is `API_SCOPES`, not Cloudflare.

The repo's own client and checker both speak this API:

    export GARAGE_HOST=https://api.example.com
    export CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=...
    ../tools/garage.py selftest
    ../tools/garage.py status

    GARAGE_PUBLIC_HOST=api.example.com ../tools/verify_remote.sh

In token mode, the same client takes `GARAGE_API_KEY` instead:

    export GARAGE_HOST=http://laptop.your-tailnet.ts.net:8080
    export GARAGE_API_KEY=...
    ../tools/garage.py selftest

Finish with `POST /garage/selftest`, which travels the whole path without
pulsing. Only after that, `POST /garage/toggle` - with eyes on the camera.

**`/selftest` cannot tell you `GARAGE_TOKEN` is correct**, and says so in its
own response as `device_token_verified: false`. The controller's
unauthenticated routes answer the same whether the shared token is right or
wrong, and its only token-checked route is the one that moves the door. After
rotating that token the first `/toggle` is the test, and it is the kind of
test to run standing in the garage rather than from a car.

## iPhone Shortcuts

Access mode only; a Shortcut cannot bring up a VPN first.

Each Shortcut is one **Get Contents of URL** action. Tap **Show More** to
reveal Method, Headers and Request Body. Headers, on every one of them:

| Header | Value |
|---|---|
| `CF-Access-Client-Id` | Client ID, **including `.access`** |
| `CF-Access-Client-Secret` | Client Secret |

**Build `/service/actions` first.** It changes nothing and it proves the
token, the DNS, the Access policy and the tunnel all work - so a later failure
cannot be any of those. GET, two headers, then a display action.

**Then `/garage/selftest`.** POST, Request Body **None**. Identical path to
the toggle, no pulse. Get `{"ok": true, "action": "garage.selftest"}` back,
then duplicate the Shortcut and change the URL to `/garage/toggle` once.

**The toggle.** POST `/garage/toggle`, Request Body **None**. Put an **Ask for
Confirmation** in front of it. Give it an unmistakable name and icon - it is
the only one that moves something physical.

**Never put the toggle on Back Tap or on an automation.** A pocket double-tap
opens your garage. Home Screen or Siri is fine.

Always add a display action after the request, or a failure is silent - a
Shortcut with no output run from the Home Screen just finishes. Use **Quick
Look** while testing, since it shows the raw JSON, and **Show Notification**
on the real one, since a banner beats a full-screen takeover. Action names
move between iOS versions, so search rather than trusting any exact name here.

Reading a response: every reply is JSON with an `ok` field, which Shortcuts
parses into a dictionary. `ok` arrives as `1`/`0`, not `true`/`false`.

1. **Get Dictionary Value** > `ok` in `Contents of URL`
2. **If** `is` `1` > get the key you want > **Show Notification**
3. **Otherwise** > get `error` > **Show Notification**

If a Shortcut dies with a network error before producing any output, that is
Access rejecting the credentials - usually the missing `.access` suffix.

The service token sits in the Shortcut on the phone, so anyone with the
unlocked phone has the garage. That is the same trust as a remote clipped to
the car's visor, which is worth saying out loud because an HTTPS endpoint with
a signed JWT in front of it can feel like more security than it is.

## How it stays safe

The process runs unprivileged with a read-only filesystem, no volumes, no host
paths and no capabilities. Every action it takes is one outbound HTTP call to
the ESP32. If a change to this service ever seems to need a mount, a socket or
a capability, that is a reason to reconsider the change.

In order:

1. **The transport.** Cloudflare Access at the edge, or a private network the
   caller has to be on at all. Nothing failing that is forwarded.
2. **This service authenticating the caller.** In access mode it verifies the
   signed `Cf-Access-Jwt-Assertion` against Cloudflare's public keys, checking
   issuer and audience - not merely that the header is present. Something
   reaching the port by another route cannot forge one. In token mode it
   compares the key in constant time against every configured client.
3. **`API_SCOPES`**, deny by default, empty is a fatal boot error.
4. **A rate limit** per identity, and a toggle cooldown on top of it. A second
   pulse inside the window returns 429 rather than reversing a door that just
   started moving.
5. **The firmware's own 2 second cooldown**, underneath all of it.

**The device token never leaves this machine.** Phones hold Cloudflare service
token credentials, which are revocable from the dashboard in seconds. Losing
the phone does not mean retrieving the ESP32 and reflashing it.

**The device never sees a tunneled request.** `MAX_REQUEST_BYTES` in the
firmware is 2048, and a Cloudflare request carrying an Access assertion is
larger than that. Nothing may `proxy_pass` straight to the device. `device_call`
in `app.py` hand-builds a request with exactly one header for that reason, and
serialises calls because the firmware serves one connection at a time.

**Audit.** `/garage/audit` is the door; `/service/audit` is everything. Both
are in memory, bounded by `AUDIT_SIZE`, and lost on restart. The durable copy
is the process's stdout - `docker compose logs garage-api`,
`journalctl -u garage-api`, or `C:\garage-api\garage-api.log`.

**What none of this fixes.** A URL that opens the garage from anywhere is
still a `/toggle`, and it still cannot tell you which way the door will go, or
whether someone is standing in the doorway. Making the button easier to press
does not make it smarter. Camera first, every time.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The service exits immediately | It fails closed on missing config and says which variable. `python3 run.py --check` prints the same thing without binding a port |
| **403 `forbidden`** on everything, access mode | The assertion did not verify. Check `ACCESS_AUD` against the application's AUD tag, and the Client ID's trailing `.access` |
| **403 `forbidden`** on everything, token mode | The key does not match any `API_CLIENTS` entry. Header is `X-Api-Key` or `Authorization: Bearer` |
| **403 `identity is not scoped for X`** | The caller got in; `API_SCOPES` did not grant that action. `GET /service/actions` shows what they have |
| **302 to a login page** with valid headers | The Access policy action is Allow, not Service Auth |
| **429 `too many requests`** | `RATE_LIMIT_PER_MIN` tripped. Something is retrying in a loop |
| **429 `too soon after the last pulse`** | `TOGGLE_COOLDOWN_S`. Wait - a second pulse reverses a moving door |
| **502 `cannot reach the controller`** | The ESP32 is off the network, or `GARAGE_HOST` is wrong. Check with `curl -m5 http://<host>/status`, which needs no token |
| **502 `GARAGE_TOKEN is not configured`** | Unset in the env file. Read-only door routes still work |
| **502 `another request is already talking to the device`** | Two calls at once. The device serves one connection at a time |
| **Cloudflare 1033 / 502 at the edge** | The tunnel is down. `docker compose logs cloudflared`, or `cloudflared tunnel info <name>` |
| **404 `no such endpoint`** | Wrong path. Everything is namespaced: `/garage/status`, not `/status` |
| **Works, then stops after a reboot** | The service is not enabled at boot, or Docker Desktop starts at login rather than at boot. See the per-OS notes above |

Set something in the env file and nothing changed? A container's environment
is fixed at creation: `docker compose up -d`, not `docker compose restart`.
Native installs need `systemctl restart garage-api`, a `launchctl unload` and
`load`, or `Stop-ScheduledTask` and `Start-ScheduledTask`.

    docker compose logs -f garage-api      # one JSON line per event

## Adding an action

Think hard first. This service is small because it does one thing, and every
route added to it is a route that can be reached by anything that gets past
the edge. There is nothing else it is supposed to be able to do.

If you do: a handler returning `(status, payload)` with validation at the top,
an entry in `ROUTES` whose dotted name matches its path, the path added to
`cloudflared/config.yml` if it should not be public, and a line in
`garage-api.env.example` under `API_SCOPES`.
