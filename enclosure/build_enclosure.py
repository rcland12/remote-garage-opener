"""Parametric enclosure for the remote garage controller.

Source of truth for the Fusion 360 model. Idempotent: running it wipes the
timeline, sketches and bodies in the active design and rebuilds from the
parameters below. Change a dimension by editing the specs table here and
re-running, never by dragging sketch geometry in Fusion.

Executed through the fusion360 MCP bridge:

    fusion_execute("execute_script", {"code": <contents of this file>})

Bridge notes, learned the hard way:
  - Every numeric value in the Fusion API is centimetres, including sketch
    point coordinates. User parameters carry their own display unit, so a
    parameter reading 6.35 with unit "mm" is 63.5 mm.
  - add_parameter's `units` argument is ignored and its `expression` argument
    is not read at all; it takes a numeric `value` in cm. Set real expressions
    through this script instead.
  - The bridge execs this with split globals and locals, so a function defined
    here cannot see module level names. Keep everything inline, no helpers.
  - execute_script cannot return a value and list_parameters does not report
    comments. To read something back, encode it in a body name and call
    get_bodies_info, which is cheap. That is what the base body rename at the
    bottom is for.
  - Never call ui.messageBox. It opens a modal dialog on the Windows machine
    and blocks the bridge until somebody clicks it.

Layout, looking down with X running along the length:

    X=0 end          entry end. Lid slides in here. USB cable exits here.
                     The ESP32's USB ports sit 0.5 mm off this wall.
    X=OL end         relay's NO/COM terminals, opener wire exits here.
                     Lid bottoms out against this wall.
    low Y            ESP32, component side down, pins and Dupont shells up.
    high Y           relay, component side up, blue can up.
    between them     a wire channel for the three Dupont runs.

Why the ESP32 faces down: the Dupont wires have to reach the pins, so the pins
must point up, which puts the component side against the rails. Its LED, reset
and BOOT buttons therefore face the floor and are unreachable once assembled.
That is a real cost, accepted deliberately. Flashing happens over the COM port
with auto-reset, so the buttons are not needed in normal use.

Why the ESP32 is rotated 180 degrees from the obvious arrangement: the guide
wall at esp_x1 has to sit against *some* end of the board, and if it sits
against the USB end it blocks the ports. Putting the antenna end there instead
means the guide can stay and the USB ports get a clear wall to exit through.
It also puts the USB cable on the same side as the owner's wall outlet.

Consequence worth knowing: the antenna now runs alongside the relay rather
than being offset along the length, separated only by board_gap. Bench RSSI
was -18 to -24 dBm, so there is enormous margin, but if range looks poor after
install the fix is to increase board_gap or antenna_keepout. Check
GET /status rssi from the garage before assuming it is fine.
"""

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
des = adsk.fusion.Design.cast(app.activeProduct)
root = des.rootComponent
up = des.userParameters

# --- Parameters -------------------------------------------------------------
# name, expression, comment. Order matters: derived values reference earlier
# names, so a parameter must appear after everything it depends on.

specs = [
    # Measured with calipers. These are the numbers to revisit if the fit is
    # wrong; everything else follows from them.
    ("esp_len", "63.5 mm", "MEASURED: ESP32-S3 PCB length including antenna overhang"),
    ("esp_wid", "28.5 mm", "MEASURED: ESP32-S3 PCB width"),
    ("esp_h", "24.25 mm", "MEASURED: full stack, component tips to top of seated Dupont shells"),
    ("esp_comp_h", "5.1 mm", "MEASURED: how far components stand off the ESP32 component face"),
    ("esp_pcb_t", "1.6 mm", "ESP32-S3 PCB thickness, nominal"),
    ("relay_len", "70 mm", "MEASURED: relay module length"),
    ("relay_wid", "17.1 mm", "MEASURED: relay module width"),
    ("relay_h", "19.5 mm", "MEASURED: relay overall height including underside pins"),
    ("relay_pcb_t", "1.6 mm", "Relay PCB thickness, nominal"),
    # Shell.
    ("wall_thk", "2 mm", "Shell wall. CLAUDE.md minimum 1.6, 2.0 for garage heat margin"),
    ("floor_thk", "2 mm", "Base floor thickness"),
    ("lid_thk", "2 mm", "Lid thickness"),
    ("fit_clr", "0.45 mm", "Slide clearance. Raised from 0.3 after the printed lid was too tight"),
    # Board retention. No screws; neither board has mounting holes.
    ("pcb_clr", "0.5 mm", "Lateral play around each PCB edge in its pocket"),
    ("esp_lift", "6.5 mm", "ESP32 rail height. Must exceed esp_comp_h or the board will not seat"),
    ("relay_lift", "3 mm", "Relay rail height, clears its 2.5 mm solder tails"),
    ("rib_w", "2.5 mm", "Width of the floor support rails"),
    ("guide_h", "4 mm", "Height of the lateral guide walls above the support rails"),
    ("wire_headroom", "10 mm", "Space above the Dupont shells for wires to bend and route"),
    ("board_gap", "10 mm", "Gap between pockets: two 2.5 mm guides plus a 5 mm wire channel"),
    # Keep-out. Relaxed from CLAUDE.md's 20 mm at the owner's request to
    # shorten the box; see the module docstring.
    ("antenna_keepout", "10 mm", "Clearance from the ESP32 antenna end to the relay"),
    ("wire_entry", "12 mm", "Gap behind the relay's output terminals so wire can enter them"),
    # Openings.
    ("usb_hole_w", "22 mm", "Wide enough for the cable to angle into either USB-C port"),
    ("usb_hole_h", "10 mm", "Raised from 7 mm: the printed 7 mm opening needed filing"),
    ("wire_hole_w", "5 mm", "Width of each opener wire hole"),
    ("wire_hole_h", "5 mm", "Height of each opener wire hole"),
    ("push_hole_w", "8 mm", "Poke hole in the far wall for pushing a swollen lid back out"),
    # Lid slide.
    ("ledge_d", "3 mm", "How far the lid slide ledges protrude inward from each long wall"),
    ("ledge_t", "1.5 mm", "Thickness of the lower slide ledge"),
    ("cap_t", "1.5 mm", "Thickness of the upper capture rail above the lid"),
    ("lid_grip_len", "10 mm", "Tongue projecting past the entry wall so the lid can be gripped"),
    ("lid_grip_w", "24 mm", "Width of the lid grip tongue"),
    # Ventilation and drainage.
    ("vent_w", "2.5 mm", "Ventilation slot width"),
    ("vent_h", "14 mm", "Ventilation slot height"),
    ("vent_pitch", "5.5 mm", "Centre to centre spacing of vent slots"),
    ("weep_w", "4 mm", "Weep slot width at the floor line"),
    ("weep_h", "1.5 mm", "Weep slot height at the floor line"),
    # Strain relief.
    ("tab_out", "8 mm", "How far the zip-tie anchor tabs project past the end walls"),
    ("tab_h", "2.5 mm", "Anchor tab thickness. Sits at z=0 so the bottom stays flat"),
    # Derived.
    ("inner_len", "antenna_keepout + relay_len + wire_entry + pcb_clr", "DERIVED cavity length"),
    ("inner_wid", "esp_wid + board_gap + relay_wid + pcb_clr * 4", "DERIVED cavity width"),
    ("inner_h", "esp_lift + esp_h - esp_comp_h + wire_headroom", "DERIVED cavity height, the ESP32 stack governs"),
    ("outer_len", "inner_len + wall_thk * 2", "DERIVED outer length, excluding the anchor tabs"),
    ("outer_wid", "inner_wid + wall_thk * 2", "DERIVED outer width"),
    ("outer_h", "inner_h + floor_thk + lid_thk + fit_clr + cap_t", "DERIVED total height including the lid channel"),
]

for name, expr, comment in specs:
    existing = up.itemByName(name)
    if existing:
        existing.expression = expr
        existing.comment = comment
    else:
        up.add(name, adsk.core.ValueInput.createByString(expr), "mm", comment)

# --- Wipe previous geometry -------------------------------------------------
# The timeline has to go first. Deleting bodies alone leaves features behind,
# and a half-failed feature puts the timeline in a state where
# get_timeline_info returns "Associated feature is invalid".

tl = des.timeline
for i in range(tl.count - 1, -1, -1):
    try:
        tl.item(i).deleteMe()
    except:
        pass
while root.bRepBodies.count:
    try:
        root.bRepBodies.item(0).deleteMe()
    except:
        break
while root.sketches.count:
    try:
        root.sketches.item(0).deleteMe()
    except:
        break

# --- Read parameter values (centimetres) ------------------------------------

pv = {}
for i in range(up.count):
    p = up.item(i)
    pv[p.name] = p.value

W = pv["wall_thk"]
FT = pv["floor_thk"]
EL = pv["esp_lift"]
RL = pv["relay_lift"]
GH = pv["guide_h"]
RW = pv["rib_w"]
PC = pv["pcb_clr"]
OL = pv["outer_len"]
OW = pv["outer_wid"]
OH = pv["outer_h"]
IH = pv["inner_h"]
WE = pv["wire_entry"]
VW = pv["vent_w"]
VH = pv["vent_h"]
VP = pv["vent_pitch"]
WW = pv["weep_w"]
WH = pv["weep_h"]
HW = pv["wire_hole_w"]
HH = pv["wire_hole_h"]
LT = pv["lid_thk"]
FC = pv["fit_clr"]
LD = pv["ledge_d"]
LGT = pv["ledge_t"]
CT = pv["cap_t"]
UHW = pv["usb_hole_w"]
UHH = pv["usb_hole_h"]
TO = pv["tab_out"]
TH = pv["tab_h"]
GL = pv["lid_grip_len"]
GW = pv["lid_grip_w"]
PHW = pv["push_hole_w"]

ZC = FT + IH  # cavity ceiling, and the underside of the lid
exts = root.features.extrudeFeatures

# --- Outer shell and cavity -------------------------------------------------
# The cavity is cut the full height so the walls are hollow all the way up.
# The slide ledges and capture rails are added back afterwards, which keeps
# the walls a constant 2 mm rather than grooving into them.

sk = root.sketches.add(root.xYConstructionPlane)
sk.name = "base_outer"
sk.sketchCurves.sketchLines.addTwoPointRectangle(
    adsk.core.Point3D.create(0, 0, 0), adsk.core.Point3D.create(OL, OW, 0)
)
ei = exts.createInput(
    sk.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation
)
ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal(OH))
bf = exts.add(ei)
base_body = bf.bodies.item(0)
base_body.name = "enclosure_base"

sk2 = root.sketches.add(root.xYConstructionPlane)
sk2.name = "base_cavity"
sk2.sketchCurves.sketchLines.addTwoPointRectangle(
    adsk.core.Point3D.create(W, W, 0),
    adsk.core.Point3D.create(OL - W, OW - W, 0),
)
ei2 = exts.createInput(
    sk2.profiles.item(0), adsk.fusion.FeatureOperations.CutFeatureOperation
)
ei2.startExtent = adsk.fusion.OffsetStartDefinition.create(
    adsk.core.ValueInput.createByReal(FT)
)
ei2.setDistanceExtent(False, adsk.core.ValueInput.createByReal(OH - FT + 0.5))
exts.add(ei2)

# --- Board positions --------------------------------------------------------

esp_x0 = W + PC          # USB end, hard against the entry wall
esp_x1 = esp_x0 + pv["esp_len"]   # antenna end
esp_y0 = W + PC
esp_y1 = esp_y0 + pv["esp_wid"]
esp_mid = (esp_y0 + esp_y1) / 2.0
rly_x1 = OL - W - WE     # output terminals, wire_entry back from the far wall
rly_x0 = rly_x1 - pv["relay_len"]
rly_y1 = OW - W - PC
rly_y0 = rly_y1 - pv["relay_wid"]
rly_mid = (rly_y0 + rly_y1) / 2.0

# --- Feature table ----------------------------------------------------------
# name, x0, y0, x1, y1, z_start, z_height, operation
#
# Rails run into the shell walls rather than stopping at the PCB edge.
# Stopping at the edge leaves a 0.5 mm wide, 6.5 mm tall air sliver between
# rail and wall that no slicer renders properly. The PCB only needs its edge
# supported, so over-wide rails cost nothing.
#
# Cuts overshoot the wall by 0.05 cm on each side so they always break through
# regardless of floating point rounding.

boxes = []
boxes.append(["esp_rail_a", W, W, esp_x1, esp_y0 + RW, FT, EL, "join"])
boxes.append(["esp_rail_b", W, esp_y1 - RW, esp_x1, esp_y1, FT, EL, "join"])
boxes.append(["rly_rail_a", rly_x0, rly_y0, rly_x1, rly_y0 + RW, FT, RL, "join"])
boxes.append(["rly_rail_b", rly_x0, rly_y1 - RW, rly_x1, OW - W, FT, RL, "join"])
boxes.append(["esp_guide_y", W, esp_y1, esp_x1, esp_y1 + RW, FT, EL + GH, "join"])
# This guide sits against the ANTENNA end. Do not move it to esp_x0; that is
# the USB end and a wall there blocks the ports.
boxes.append(["esp_guide_x_antenna", esp_x1, W, esp_x1 + RW, esp_y1 + RW, FT, EL + GH, "join"])
boxes.append(["rly_guide_y", rly_x0, rly_y0 - RW, rly_x1, rly_y0, FT, RL + GH, "join"])
boxes.append(["rly_guide_x", rly_x0 - RW, rly_y0 - RW, rly_x0, OW - W, FT, RL + GH, "join"])
boxes.append(["slide_ledge_a", W, W, OL - W, W + LD, ZC - LGT, LGT, "join"])
boxes.append(["slide_ledge_b", W, OW - W - LD, OL - W, OW - W, ZC - LGT, LGT, "join"])
boxes.append(["slide_cap_a", W, W, OL - W, W + LD, ZC + LT + FC, CT, "join"])
boxes.append(["slide_cap_b", W, OW - W - LD, OL - W, OW - W, ZC + LT + FC, CT, "join"])
boxes.append(["tab_usb", -TO, esp_mid - 0.8, 0, esp_mid + 0.8, 0, TH, "join"])
boxes.append(["tab_wire", OL, rly_mid - 0.9, OL + TO, rly_mid + 0.9, 0, TH, "join"])
boxes.append(["lid_entry_slot", -0.05, W, W + 0.05, OW - W, ZC, LT + FC, "cut"])
boxes.append(["wire_hole_a", OL - W - 0.05, rly_mid - HW - 0.15, OL + 0.05, rly_mid - 0.15, FT + 0.7, HH, "cut"])
boxes.append(["wire_hole_b", OL - W - 0.05, rly_mid + 0.15, OL + 0.05, rly_mid + HW + 0.15, FT + 0.7, HH, "cut"])
boxes.append(["usb_window", -0.05, esp_mid - UHW / 2.0, W + 0.05, esp_mid + UHW / 2.0, FT + 0.15, UHH, "cut"])
boxes.append(["lid_push_hole", OL - W - 0.05, OW / 2.0 - PHW / 2.0, OL + 0.05, OW / 2.0 + PHW / 2.0, ZC, LT + FC, "cut"])
boxes.append(["tie_slot_u1", -0.5, esp_mid - 0.55, -0.2, esp_mid - 0.3, -0.05, TH + 0.1, "cut"])
boxes.append(["tie_slot_u2", -0.5, esp_mid + 0.3, -0.2, esp_mid + 0.55, -0.05, TH + 0.1, "cut"])
boxes.append(["tie_slot_w1", OL + 0.2, rly_mid - 0.6, OL + 0.5, rly_mid - 0.35, -0.05, TH + 0.1, "cut"])
boxes.append(["tie_slot_w2", OL + 0.2, rly_mid + 0.35, OL + 0.5, rly_mid + 0.6, -0.05, TH + 0.1, "cut"])

# Convection vents: low on the y=0 wall, high on the opposite wall, so air
# enters cool at the bottom and leaves warm at the top. Nothing on the top
# face, where dust settles and anything dripping would land.
vx = 1.0
n = 0
while vx < OL - 1.5:
    boxes.append(["vent_low_" + str(n), vx, -0.05, vx + VW, W + 0.05, FT + 0.1, VH, "cut"])
    boxes.append(["vent_high_" + str(n), vx, OW - W - 0.05, vx + VW, OW + 0.05, FT + 1.8, VH, "cut"])
    vx = vx + VP
    n = n + 1

# Weep slots sit at the cavity floor line. The box lies flat on the opener so
# condensation cannot drain downward; it has to leave sideways.
wn = 0
for wx in [2.0, 5.0, 8.0]:
    boxes.append(["weep_" + str(wn), wx, -0.05, wx + WW, W + 0.05, FT, WH, "cut"])
    wn = wn + 1

# --- Build the base ---------------------------------------------------------
# Each feature is trapped individually. One bad cut aborting the whole run
# leaves a half-built body and an invalid timeline, which is far harder to
# diagnose than a list of names that failed.

fails = []
for b in boxes:
    try:
        s = root.sketches.add(root.xYConstructionPlane)
        s.name = b[0]
        s.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(b[1], b[2], 0),
            adsk.core.Point3D.create(b[3], b[4], 0),
        )
        if b[7] == "join":
            op = adsk.fusion.FeatureOperations.JoinFeatureOperation
        else:
            op = adsk.fusion.FeatureOperations.CutFeatureOperation
        e2 = exts.createInput(s.profiles.item(0), op)
        e2.participantBodies = [base_body]
        e2.startExtent = adsk.fusion.OffsetStartDefinition.create(
            adsk.core.ValueInput.createByReal(b[5])
        )
        e2.setDistanceExtent(False, adsk.core.ValueInput.createByReal(b[6]))
        exts.add(e2)
    except:
        fails.append(b[0])

# --- Lid --------------------------------------------------------------------
# Built beside the base, flat at z=0, so the exported STL lands on the build
# plate rather than floating at cavity height. It is not modelled in its
# assembled position; move it if you need to check the fit visually.

ly0 = OW + 1.0
lw = OW - 2 * W - 2 * FC
lyc = ly0 + lw / 2.0

skl = root.sketches.add(root.xYConstructionPlane)
skl.name = "lid_plate"
skl.sketchCurves.sketchLines.addTwoPointRectangle(
    adsk.core.Point3D.create(0, ly0, 0),
    adsk.core.Point3D.create(OL - W - FC, ly0 + lw, 0),
)
eil = exts.createInput(
    skl.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation
)
eil.setDistanceExtent(False, adsk.core.ValueInput.createByReal(LT))
lf = exts.add(eil)
lid_body = lf.bodies.item(0)
lid_body.name = "enclosure_lid"

# Grip tongue. A flush lid in a friction channel has nothing to pull on once
# it is seated; this projects past the entry wall so it can be pinched out.
try:
    s = root.sketches.add(root.xYConstructionPlane)
    s.name = "lid_grip"
    s.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(-GL, lyc - GW / 2.0, 0),
        adsk.core.Point3D.create(0.05, lyc + GW / 2.0, 0),
    )
    e5 = exts.createInput(
        s.profiles.item(0), adsk.fusion.FeatureOperations.JoinFeatureOperation
    )
    e5.participantBodies = [lid_body]
    e5.setDistanceExtent(False, adsk.core.ValueInput.createByReal(LT))
    exts.add(e5)
except:
    fails.append("lid_grip")

# --- Readback ---------------------------------------------------------------
# Encoded in the body name because execute_script cannot return a value.
# Call get_bodies_info and read the suffix, then rename to enclosure_base.

p = up.itemByName("zz_fail_tokens")
if p:
    p.expression = str(len(fails)) + " mm"
else:
    up.add("zz_fail_tokens", adsk.core.ValueInput.createByString(str(len(fails)) + " mm"), "mm", "diagnostic")
base_body.name = "base_f" + str(len(fails))

# TODO:
#   - Fillet the outer vertical edges. Cosmetic; skipped because selecting
#     specific edges through the bridge is fiddly and the model currently
#     builds with zero failures.
#   - Copy exported STLs into this directory. They are written to the Windows
#     machine's Desktop; there is no shared path from the Linux repo host.
