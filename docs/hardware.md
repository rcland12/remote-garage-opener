# Hardware assembly

Build and wiring instructions for the remote garage controller. Work through
the steps in order. Steps 1 through 4 happen at a desk with nothing connected
to the opener, so a mistake costs you nothing.

## Safety

Read this once before starting.

- A garage door is the heaviest moving object in a house. It can injure a
  person or crush a car roof. Everything below assumes the opener's photo-eye
  safety sensors are installed and working. Test them before you commission
  this: wave a broom through the beam while the door is closing and confirm
  the door reverses. If it does not reverse, fix that before adding remote
  control.
- Unplug the opener from its ceiling outlet before touching its terminal
  screws. The motor and light circuit are line voltage.
- Operating a door you cannot see is the real risk this project introduces,
  and this build has no door sensor at all. It cannot tell you whether the
  door is open, let alone whether someone is standing in the doorway. A camera
  pointed at the garage is not optional here; it is the only feedback you get.
  Look at it before every remote operation.
- Nothing in this build connects to the opener's 28 V accessory supply. The
  only electrical connection to the opener is the isolated relay contact
  across the two pushbutton terminals.
- Route the low-voltage wiring away from the door's moving parts, the
  counterbalance springs and the chain or belt.

## Parts

| Item | Notes |
|---|---|
| ESP32-S3 dev board | Powered by USB from a 5 V phone charger |
| 1-channel relay module | 3.3 V trigger, optocoupler isolated, 3 V coil |
| Female-to-female Dupont jumpers | ESP32 to relay module, 3 needed |
| 470 uF electrolytic capacitor | Across relay VCC and GND, see step 2 |
| 16 AWG 2-conductor wire | Controller to opener terminals |
| USB cable and 5 V charger | Data-capable cable, you will flash over it |
| Multimeter | Needed for steps 2, 4 and 5. Not optional |
| Camera for the garage | Separate from this project, but do not skip it |

## Step 1: bench test the board, no relay attached

Flash MicroPython and the firmware first, following `docs/flashing.md`. Leave
`DRY_RUN = True` in `firmware/config.py` for now.

Power the board over USB, find its IP from the serial console, and confirm the
API answers:

    GARAGE_HOST=<ip> ./tools/smoke_test.sh

All read-only checks should pass. At this point `POST /toggle` only writes a
log line; the relay pin is never driven.

## Step 2: determine your relay module's trigger polarity

Most cheap optocoupler relay boards are active LOW: the relay energises when
IN is pulled to ground. Some are active HIGH. Get this right or the relay will
sit closed whenever the board is idle, which would hold the opener's button
down permanently.

With the relay module powered but its screw terminals connected to nothing:

1. Connect relay VCC to the ESP32 3V3 pin, relay GND to an ESP32 GND pin.
2. Leave relay IN disconnected. Note whether the relay's LED is on and whether
   you heard it click when power was applied.
3. Touch the IN pin briefly to GND. If the relay clicks in, it is active LOW.
4. Touch IN briefly to 3V3. If it clicks in, it is active HIGH.

Set `RELAY_ACTIVE_HIGH` in `firmware/config.py` accordingly.

Measured on this build: the module is a Songle SRD-03VDC-SL-C board with screw
terminal inputs rather than a pin header, and it is **active HIGH**. IN to 3V3
energises it, IN to GND does nothing. `RELAY_ACTIVE_HIGH = True` is set.

Active HIGH carries a boot risk that active LOW does not. Idle is a LOW pin,
but from power-on until MicroPython configures GPIO 5, that pin is an
undriven input and floats. A pin floating high is a relay clicking on boot,
which is a garage door opening during a thunderstorm. Step 4 tests for this
explicitly. If it fails, fit a 10k resistor from GPIO 5 to GND, which holds IN
low through the float window without affecting the firmware's ability to drive
it high.

While you are here, check the coil voltage printed on the relay can. Match the
supply to the coil; this goes wrong in both directions.

- Coil says 5 V: power VCC from the dev board's 5V (VBUS) pin, not 3V3, and
  leave IN on GPIO 5. The optocoupler input still triggers happily from 3.3 V.
  A 5 V coil on the 3V3 regulator will brown out the board.
- Coil says 3 V or 3.3 V: power VCC from 3V3. Do not "fix" a brownout by
  moving it to 5V, which puts roughly 65 percent over-voltage across a 3 V
  coil. See the capacitor note below instead.

A 3 V coil draws on the order of 100 to 150 mA while energised, and it pulls
that current in a step. The dev board's regulator can supply it, but the
inrush lands on the same 3V3 rail the radio uses. Fit a bulk capacitor,
100 uF or larger electrolytic, directly across the relay module's VCC and GND
pins, minding polarity. That is cheap insurance against a reset when the relay
fires and it costs nothing if the rail was fine anyway.

You do not need to add a flyback diode. These optocoupler modules already have
one across the coil, along with the driver transistor.

## Step 3: wire the relay to the ESP32

Unplug USB power first.

| ESP32-S3 pin | Relay module pin |
|---|---|
| 3V3 (or 5V, see step 2) | VCC |
| GND | GND |
| GPIO 5 | IN |

GPIO 5 is a plain GPIO on the ESP32-S3. Do not move the relay to GPIO 0, 3, 45
or 46. Those are strapping pins; they are sampled and can glitch at reset,
which would pulse the door every time the board reboots.

If your relay module has a separate JD-VCC jumper, leave the factory jumper in
place. That jumper is what shares the logic supply with the coil, which is
what you want for a single-supply build.

## Step 4: verify the pulse, still not connected to the opener

Set `DRY_RUN = False` in `firmware/config.py`, redeploy, and put a multimeter
in continuity or resistance mode across the relay's COM and NO screw
terminals. It should read open circuit.

Fire a pulse:

    GARAGE_HOST=<ip> GARAGE_TOKEN=<token> ./tools/garage.py toggle

You should hear one click, see the relay LED flash, and the meter should beep
for about half a second before going open again. Confirm all three:

- Exactly one click in, one click out. Never a latch.
- `GET /log` shows a single `relay_pulse` entry.
- Press the board's reset button. The relay must not click at all during
  boot. Do not proceed to step 5 until a reset is silent.
- Pull the USB cable and plug it back in. Same requirement: no click. A cold
  power-up is a different case from a reset button, because the 3V3 rail is
  ramping at the same time the pin is floating. Test both.

If a reset does click, this build is active HIGH and the cause is almost
certainly GPIO 5 floating before the firmware drives it. Fit a 10k resistor
between GPIO 5 and GND. It holds IN low through the float window and is far
too weak to interfere with the firmware driving the pin high afterwards.
Re-run both tests before continuing.

## Step 5: identify the opener's pushbutton terminals

Unplug the opener.

The Linear opener has a low-voltage terminal strip on the back or side of the
motor head, where the wall button wires land. Some Linear wall stations use
two wires, some use three (the third drives the light). You want the two
terminals that the existing wall button's switch contacts bridge.

Confirm with the multimeter before you connect anything:

1. Plug the opener back in. Do not touch the line-voltage side.
2. Measure DC voltage across the candidate pair. Expect something in the
   region of 5 to 24 V open circuit. This is the opener sensing an open
   switch.
3. If you measure around 28 V, you are on the accessory supply pair. Move off
   it. Nothing in this project touches that.
4. Have someone press the wall button, or briefly bridge the pair with a short
   jumper wire. The door should run. That confirms the pair, and it confirms
   that a dry contact is all the opener wants.

Unplug the opener again once you have identified the pair.

## Step 6: connect the relay to the opener

Run the 16 AWG two-conductor wire from the controller's location to the
opener's terminal strip. 16 AWG is far heavier than this signal needs, which
is fine; it just makes the screw terminals a little crowded.

| Relay screw terminal | Opener terminal |
|---|---|
| COM | one of the pushbutton pair |
| NO | the other of the pushbutton pair |

Polarity does not matter. This is a dry contact, exactly equivalent to the
wall button's own switch. Leave the existing wall button wired in parallel;
both will keep working.

Do not use the NC terminal. NC is closed at rest, which would hold the
opener's button pressed.

## Step 7: the camera

This build has no door sensor, so the API cannot tell you whether the door is
open or closed. A camera in the garage is what fills that gap, and it fills it
better than a reed switch would: it shows you the doorway, not just the door.

Set it up before you start operating the door remotely, not after. It does not
need to integrate with this project in any way. It needs to be something you
can pull up on your phone, in a second or two, from wherever you are.

The habit that makes this safe is: look, then press. A reed switch could never
have told you a bicycle was leaning in the doorway.

## Step 8: commissioning

1. Power the controller from the 5 V phone charger, not from a laptop.
2. Stand where you can see the door.
3. `./tools/garage.py status`, confirm the API answers.
4. `./tools/garage.py toggle`, confirm the door runs.
5. Let it finish. Confirm `moving` clears about 15 seconds later.
6. Reset the board while the door is closed. Confirm the door does not move.
7. Pull the charger and plug it back in. Confirm the door does not move, and
   that the API comes back on its own within about 30 seconds.

Steps 6 and 7 are the ones that matter. A controller that pulses the door on
power-up is a controller that opens your garage during a thunderstorm.

Then repeat step 4 once from outside the house, over the VPN, with the camera
up on your phone. That is the real workflow; run it once while you are still
close enough to walk back and fix things.

## Cable strain relief

Anchor both cables so a tug lands on a zip tie and not on a solder joint or a
Dupont pin: the USB cable and the pair going to the opener.

The printed enclosure has an anchor tab outside each cable exit, with a pair of
slots through it. Thread a tie up through one slot, over the cable, and down
through the other. Both tabs sit at the floor line, flush with the bottom face,
so they do not interfere with the adhesive strips.

Anchoring to a nearby joist or the opener's rail bracket also works, and is
worth doing as well as the tabs for the run to the opener terminals, which is
the longer and more exposed of the two.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Relay clicks once at every boot | Trigger polarity wrong, or relay moved to a strapping pin. Recheck step 2 |
| Relay clicks but the door does nothing | Wrong terminal pair, or wired to NC instead of NO |
| Door runs twice per request | `PULSE_MS` too long for this opener, try 300 ms |
| `POST /open` returns 404 | Correct. There is no door sensor, so only `/toggle` exists. See the README |
| API unreachable after a few days | Check `GET /status` for `reset_cause`. `watchdog` means the loop wedged and recovered |
| Board reboots when the relay fires | Coil inrush is browning out the 3V3 rail. Add the bulk capacitor from step 2. Only move VCC to 5V if the coil is actually rated 5 V |
