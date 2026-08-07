"""Tunable configuration for the garage door controller.

Pin assignments here must stay in sync with the wiring table in CLAUDE.md
and docs/hardware.md.
"""

# --- Safety -----------------------------------------------------------------

# When True the relay pin is never driven; pulses are logged only.
# Leave this True for bench testing, set to False once the relay is wired
# to the opener and you are ready to move the actual door.
DRY_RUN = False

# --- Pins -------------------------------------------------------------------

# Relay trigger. GPIO 5 is a plain GPIO on the ESP32-S3, not a strapping pin.
# NEVER move this to GPIO 0, 3, 45 or 46: those float or glitch during boot
# and would pulse the door on every reset.
RELAY_PIN = 5

# There is no door sensor. This controller pulses the opener's pushbutton and
# nothing else; it cannot report whether the door is open or closed. Knowing
# the door's state is handled out of band, by a camera pointed at the garage.

# Measured on this build's module, a Songle SRD-03VDC-SL-C board with screw
# terminal inputs: IN touched to 3V3 energises the relay, IN to GND does not.
# That makes it active HIGH, which is the less common case for these boards.
#
# Consequence worth knowing: idle is now a LOW pin. Between power-on and the
# moment MicroPython configures GPIO 5, the pin is an undriven input and
# floats. If it floats high enough, the relay can click during boot. Step 4 of
# docs/hardware.md tests exactly this, and the fix is a 10k pull-down resistor
# from GPIO 5 to GND. Do not skip that test.
RELAY_ACTIVE_HIGH = True

# --- Timing -----------------------------------------------------------------

# How long the relay contact stays closed. This emulates a wall-button press.
# Never made longer, and the relay is always released by the main loop.
PULSE_MS = 500

# Ignore new pulse requests while a pulse is in flight, plus this cooldown.
# Stops a double-tap or a retrying client from immediately reversing the door.
COOLDOWN_MS = 2000

# Roughly how long the door takes to travel end to end. Used only to report
# a "moving" hint in /status, meaning "we pulsed recently, so the door is
# probably still travelling". It is a timer, not a measurement, and it does
# not gate anything.
TRAVEL_MS = 15000

# --- Network ----------------------------------------------------------------

HTTP_PORT = 80

# Header carrying the shared secret on state-changing requests.
AUTH_HEADER = "x-auth-token"

# Per-connection socket timeout, seconds. Keep well under WDT_TIMEOUT_MS.
CLIENT_TIMEOUT_S = 5

# Reject oversized requests instead of buffering them.
MAX_REQUEST_BYTES = 2048

# How often to re-check the WiFi link and attempt a reconnect.
WIFI_RETRY_MS = 10000

# --- Watchdog ---------------------------------------------------------------

# Hardware watchdog. If the main loop wedges, the board reboots. A reboot
# leaves the relay pin idle, so it cannot pulse the door.
WDT_ENABLED = True
WDT_TIMEOUT_MS = 20000

# --- Logging ----------------------------------------------------------------

# Number of events kept in the in-memory ring buffer served at /log.
LOG_SIZE = 40
