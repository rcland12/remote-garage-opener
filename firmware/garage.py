"""Hardware layer: relay pulse and event ring buffer.

Nothing in here blocks. The relay is a small state machine driven by
Relay.poll(), so an in-flight pulse never stalls the HTTP server.

There is no door sensor. The relay is the only piece of hardware this
firmware talks to.

All methods take an explicit monotonic millisecond clock (uptime_ms) rather
than calling time.ticks_ms() themselves. The main loop owns that clock and
handles ticks wraparound in one place.
"""

import config
from machine import Pin


class EventLog:
    """Fixed-size ring buffer of recent events, served at GET /log."""

    def __init__(self, size):
        self._size = size
        self._buf = []
        self._seq = 0

    def add(self, event, detail=None, uptime_ms=0):
        self._seq += 1
        entry = {"seq": self._seq, "t": uptime_ms // 1000, "event": event}
        if detail is not None:
            entry["detail"] = detail
        self._buf.append(entry)
        if len(self._buf) > self._size:
            self._buf.pop(0)
        # Also goes to the USB serial console, which is how you debug a board
        # that cannot reach the network yet.
        print(
            "[%6ds] %s %s"
            % (entry["t"], event, "" if detail is None else detail)
        )
        return entry

    def entries(self):
        return list(self._buf)


class Relay:
    """Momentary dry-contact output. Pulses only, never latches."""

    def __init__(self, log):
        self._log = log
        self._active = 1 if config.RELAY_ACTIVE_HIGH else 0
        self._idle = 0 if config.RELAY_ACTIVE_HIGH else 1
        self._pin = None
        if not config.DRY_RUN:
            # value= is passed to the constructor so the pin goes straight to
            # the idle level. Driving it OUT first and setting it afterwards
            # would leave a window where the relay could click.
            self._pin = Pin(config.RELAY_PIN, Pin.OUT, value=self._idle)
        self._release_at = None
        self._ready_at = 0

    @property
    def pulsing(self):
        return self._release_at is not None

    def ready(self, uptime_ms):
        """True if a new pulse is allowed right now."""
        return not self.pulsing and uptime_ms >= self._ready_at

    def pulse(self, uptime_ms, source="api"):
        """Start a pulse. Returns True if started, False if rejected."""
        if not self.ready(uptime_ms):
            return False
        if self._pin is not None:
            self._pin.value(self._active)
        self._release_at = uptime_ms + config.PULSE_MS
        self._log.add(
            "relay_pulse",
            "%s%s"
            % (source, " (DRY_RUN, pin not driven)" if config.DRY_RUN else ""),
            uptime_ms,
        )
        return True

    def poll(self, uptime_ms):
        """Release the contact once the pulse window has elapsed."""
        if self._release_at is not None and uptime_ms >= self._release_at:
            self._release()
            self._ready_at = uptime_ms + config.COOLDOWN_MS

    def _release(self):
        if self._pin is not None:
            self._pin.value(self._idle)
        self._release_at = None

    def safe_off(self):
        """Belt and braces: drop the contact on any unexpected error path."""
        self._release()
