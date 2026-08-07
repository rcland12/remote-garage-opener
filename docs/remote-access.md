# Reaching the door from outside the house

The device itself is deliberately dumb about the internet: LAN only, plain
HTTP, one shared token. That posture is only safe because nothing routes to it
from outside. This document covers how to close that gap without ever port
forwarding to the ESP32.

Do not port forward to the controller. It has no TLS, no rate limiting and no
firmware update path. A single leaked token would be an open garage.

## The shape

    phone / laptop  --VPN or Access-->  local server (LAN)  --HTTP-->  ESP32
      anywhere                                                          garage

The local server is the only thing that talks to the controller. It is already
on the LAN and you already reach it over SSH, so it does the authenticating
and the exposing.

Option D is what is deployed. A and B remain the fallbacks for when the
tunnel, the Cloudflare account or the shim is the thing that is broken.

## Option A: Tailscale, recommended

Install Tailscale on the local server and on your phone and laptop. The
controller stays untouched.

    curl -fsSL https://tailscale.com/install.sh | sh
    sudo tailscale up

Now from anywhere:

    ssh you@server-name
    GARAGE_HOST=garage.home.arpa GARAGE_TOKEN=... ~/garage/tools/garage.py toggle

To skip the SSH step and get a URL your phone can hit, run a small reverse
proxy on the server and publish it on the tailnet only:

    sudo tailscale serve --bg --https=443 http://192.168.1.50:80

That gives you `https://server-name.your-tailnet.ts.net/`, TLS terminated by
Tailscale, reachable only by devices you have authorised. You still need the
token header on `POST /toggle`, so a compromised tailnet device is not
automatically a compromised garage.

Do not use `tailscale funnel` here. Funnel publishes to the public internet.

## Option B: WireGuard

Same shape, more setup, no third party. Stand up WireGuard on the local
server, route your LAN subnet to VPN clients, and connect before using the
client tool. Once connected, the controller is just another LAN address.

## Option C: SSH port forward, zero extra software

Good enough if you only ever act from a laptop:

    ssh -L 8080:192.168.1.50:80 you@server
    GARAGE_HOST=127.0.0.1:8080 GARAGE_TOKEN=... ./tools/garage.py status

## Option D: Cloudflare Access, deployed

This is the path an iPhone Shortcut can use, because a Shortcut cannot bring
up a VPN first. It gets a real HTTPS URL without port forwarding anything: the
tunnel is an outbound connection from the server, so the LAN keeps no inbound
holes.

    iPhone Shortcut
      -> https://website.com/toggle   CF-Access-Client-Id + -Secret
      -> Cloudflare Access                        service token policy, edge
      -> cloudflared -> nginx -> garage shim      on the local server
      -> http://192.168.1.50/toggle                X-Auth-Token
      -> relay closes for 500 ms

The shim lives in the server repo at `garage/`, alongside the rest of the
stack; `garage/README.md` there has the build and dashboard setup. Two things
about it matter from this side:

- **The device token never leaves the server.** The phone holds Cloudflare
  service token credentials, which can be revoked from the Zero Trust
  dashboard in seconds. It never holds `API_TOKEN`. Losing the phone does not
  mean re-flashing the ESP32.
- **The device never sees a tunneled request.** `MAX_REQUEST_BYTES` is 2048
  and a Cloudflare request with an Access assertion on it is bigger than that.
  The shim throws all of it away and sends the controller three lines.

### The iPhone Shortcut

Shortcuts app > new shortcut > Get Contents of URL:

    URL     https://garage.website.com/toggle
    Method  POST
    Headers CF-Access-Client-Id      <client id>.access
            CF-Access-Client-Secret  <client secret>

Add a display action after it so a failure is visible rather than silent. A
shortcut with no display action run from the Home Screen finishes without
saying anything, which is the wrong behaviour for a door. Use **Quick Look**
while testing, since it shows the raw JSON, and **Show Notification** on the
real one, since a banner beats a full-screen takeover. (Older guides say
"Show Result"; that name is from the Workflow era and is not dependable in
current Shortcuts. Action names move between iOS versions, so search rather
than trusting any exact name here.)
Add it to the home screen if you want, but see the warning below about what a
one-tap door button on a lock screen means.

**Build it against `/selftest` first.** Same method, same headers, same route
through Access and the tunnel and the shim to the controller, minus the pulse.
Get a `{"ok": true, "action": "selftest"}` back, then change the URL to
`/toggle` once and never touch it again.

    ./tools/garage.py selftest      # the same check from the server

Run it after a router or DHCP change, or any time you want to know the remote
path is intact without finding out the hard way.

It has one blind spot, and it reports it as `device_token_verified: false`.
The controller's unauthenticated routes answer the same whether the shared
token is right or wrong, and its only token-checked route is the one that
moves the door, so **`/selftest` cannot tell you `GARAGE_TOKEN` is correct.**
After rotating that token the first `/toggle` is the test, and it is the kind
of test you want to run standing in the garage rather than from a car.

The firmware has no endpoint to close that gap on purpose. The device lives
inside the opener head in a rental; adding a route means taking it down to
reflash it, which is too much for a check that only matters on the day you
rotate a token.

To confirm the gate is actually closed, run the same request with the headers
removed. It must come back as a Cloudflare login page, never as JSON:

    curl -sS -o /dev/null -w '%{http_code}\n' https://garage.website.com/status

### What this does and does not buy you

It closes the network problem: nothing on the public internet reaches the
controller, and the credential that does reach it is revocable and logged, at
the edge by Cloudflare and locally by the shim at `GET /audit`.

It does not close the problem in the next section. A URL that opens the garage
from anywhere is still a `/toggle`, and it still cannot tell you which way the
door will go. Making the button easier to press does not make it smarter.

Concretely: the service token sits in the Shortcut on the phone. Anyone with
the unlocked phone has the garage. That is the same trust as a clipped-on
remote in the car, which is worth saying out loud because an HTTPS endpoint
with a signed JWT in front of it can feel like more security than it is.

## Pin the device's address

Whichever option you pick, give the ESP32 a fixed address so the host name in
your scripts stays valid. Either reserve its MAC in the router's DHCP table,
which is the tidier option, or set `STATIC_IP` in `firmware/secrets.py`:

    STATIC_IP = ("192.168.1.50", "255.255.255.0", "192.168.1.1", "192.168.1.1")

## Keeping the token honest

- The token lives in `firmware/secrets.py` on the device and in
  `garage/garage.env` on the server, encrypted in git as `garage.env.sops`.
  Nowhere else, and never on a phone.
- Rotate it by editing `secrets.py`, redeploying, updating `GARAGE_TOKEN` in
  `garage.env` and restarting the shim. The Cloudflare service token rotates
  independently, in the dashboard, and needs no change on the device.
- Every rejected request is logged. `GET /log` showing `auth_fail` entries you
  did not cause means something on your LAN is probing the device.
- `GET /status`, `/health` and `/log` are unauthenticated by design, so a
  monitoring check does not need the secret. They cannot move the door, and
  they carry no door state to leak. If you would rather not expose uptime and
  SSID to the LAN, add the same `token_ok` check to those routes in
  `firmware/main.py`.

## /toggle is the only door command

`POST /toggle` presses the button, exactly like the wall switch: if the door
was moving it stops, if it was stopped it reverses. When you are not standing
there, that is a coin flip.

There is no `/open` or `/close` to prefer instead. This device has no door
sensor, so it cannot know which way a pulse will send the door. Endpoints that
claimed otherwise would be guessing, and a wrong guess on `/close` opens your
garage and leaves it open.

The consequence is that **you have to resolve the ambiguity yourself, before
you press.** Pull up the garage camera, see which way the door is, then
toggle. Do not automate this. A script that fires `/toggle` on a schedule or a
geofence has no idea what it is doing, and the failure mode is an open garage
you do not find out about until you get home.

Nothing on the device can tell you the doorway is clear either. That is also
what the camera is for.
