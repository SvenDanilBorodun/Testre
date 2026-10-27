"""Parameters of the V1 servo gauge: the cradle that holds one STS3215/STS3250
case and the two stepped floating key caps (``J15`` for joints 1–5, ``GR`` for
the claw). ``build_gauge.py`` builds the solids from these numbers;
``robotis_ai_setup/tests/test_v1_gauge_params.py`` pins every value and checks
the rules that tie them together, deps-free in CI.

Frame (build spec S12): origin on the servo's output axis at the case's output
face; z = n (out of the horn face), x = r (toward the case's long end), y = w.
Angles θ run counter-clockwise seen from the horn side, θ = 0 along +r, the
convention of ``feetech_servo_spec.REFERENCE_THETA_DEG``.

Every case number is measured from the CAD (probes p14–p16, p30, p31; both
models identical). The real case and horn are measured at P1 before a series
gauge is printed (``docs/KNOWN-ISSUES.md``). Stdlib + the servo spec only.
"""

from __future__ import annotations

import os
import sys

_ARM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                        'robotis_ai_setup', 'docker', 'open_manipulator')
if _ARM_DIR not in sys.path:
    sys.path.insert(0, _ARM_DIR)

import feetech_servo_spec as fss  # noqa: E402

# ── the case (CAD; datasheet 45.2 × 24.7 × 35, so the pocket allows +0.2 in r and w) ──
CASE_R = (-10.0, 35.0)
CASE_W_HALF = 12.25
CASE_N = (-32.4, 2.6)
CASE_DATASHEET_MM = (45.2, 24.7, 35.0)
# name -> (n, (r0, r1), (w0, w1)) of a planar face with outward normal −n
CASE_BOTTOM_FACES = {
    'plateau': (-29.0, (-9.72, 11.65), (-11.97, 11.97)),
    'square': (-29.2, (-5.0, 5.0), (-5.0, 5.0)),
    'boss': (-33.10, (-2.75, 2.75), (-2.75, 2.75)),
    'foot_frame': (-32.40, (19.77, 29.20), (-8.74, 8.74)),
    'label_recess': (-32.25, (21.5, 27.5), (-6.0, 6.0)),     # a 0.15 mm recessed label field: no pad here
    'band': (-30.50, (29.8, 34.2), (-9.3, 9.3)),
}
CASE_CONNECTOR_POCKETS = (((12.15, 15.75), (-9.0, -0.5)), ((12.15, 15.75), (0.5, 9.0)))
CASE_CONNECTOR_POCKET_TOP_N = -19.4
# where the −29.0 plateau is flush (p31): along r at w ±9, and along w at r −6
PLATEAU_FLUSH_ALONG_R = ((-9.7, -0.3), 9.0)
PLATEAU_FLUSH_ALONG_W = ((1.0, 11.9), -6.0)
CASE_SIDE_FACE = {'w': 12.25, 'n': ((-18.9, -6.1), (-26.88, -19.0)), 'r': (-6.57, 33.0)}   # groove at n −19
CASE_NEAR_END_FACE = {'r': -10.0, 'n': (-18.9, -6.1), 'w_half': 8.82}
CASE_FAR_END_FACE = {'r': 35.0, 'n': (-18.9, -6.1), 'w_half': 10.25}
# The case volume the cradle must never enter, as boxes (r0, w0, n0, r1, w1, n1). A superset of the
# measured bottom (p30, p31): the bottom below n −29 is the boss, the −29.2 square and, from r 6.0 on,
# the −30.5 band and side strips, the chamfers into the −32.4 foot frame and the frame itself.
CASE_ENVELOPE_BOXES = (
    (-10.0, -12.25, -29.0, 35.0, 12.25, 2.6),
    (-2.75, -2.75, -33.10, 2.75, 2.75, -29.0),
    (-5.0, -5.0, -29.2, 5.0, 5.0, -29.0),
    (6.0, -12.25, -32.40, 29.8, 12.25, -29.0),
    (29.8, -12.25, -30.50, 35.0, 12.25, -29.0),
)
# The highest case point under each cap's marking-arm footprint (|perpendicular| <= 3), per r bin (p16):
# (r_from, r_to, n). J15: along θ 0 — 0.0 to r 10.25, a chamfer to 2.6 by r 11.5 (taken at 2.6),
# 2.6 to 14.5, 2.45 from 14.75 to 30.5, 2.6 to 33.5. GR: along θ 81.97° — 0.0 to r 12.5, edge drop at 12.75.
CASE_TOP = {
    'J15': ((9.0, 10.25, 0.0), (10.25, 11.5, 2.6), (11.5, 14.6, 2.6), (14.6, 30.6, 2.45), (30.6, 35.0, 2.6)),
    'GR': ((9.0, 12.5, 0.0), (12.5, 13.6, -2.86)),
}

# ── the horn (CAD; plain Ø2.50 holes and the claw's 5.70 seat are ASSUMPTIONS until P1) ──
HORN_D = 19.2
HORN_PCD = 14.00
HORN_HOLE_D = 2.50
HORN_CENTRE_BORE_D = 3.2
HORN_TOP_N_CAD = {'J15': 4.70, 'GR': 5.70}
CAP_DESIGN_SEAT_N = 4.70                       # both caps are designed for this seat ...
CAP_CHECK_SEATS_N = {'J15': (4.70,), 'GR': (4.70, 5.70)}   # ... and the GR cap is checked on the CAD's 5.70 too

# ── the caps ──
CAP_IDS = ('J15', 'GR')


def _per_cap(values):
    """The one value every joint using a cap shares (``fss.GAUGE_CAP``)."""
    out = {}
    for cap, value in zip(fss.GAUGE_CAP, values):
        if out.setdefault(cap, value) != value:
            raise ValueError(f'joints using cap {cap} disagree: {out[cap]} vs {value}')
    return {cap: out[cap] for cap in CAP_IDS}


REFERENCE_THETA_DEG = _per_cap(fss.REFERENCE_THETA_DEG)        # the marked horn hole
HOLE_THETA_OFFSET_DEG = _per_cap(fss.HOLE_THETA_OFFSET_DEG)    # horn holes at offset + k·90
DISC_R = 9.4
CAP_THICKNESS = 4.0                    # the disc top above the seat
CAP_BORE_D = 7.0                       # screw head + torque-seal dot
PIN_D = 2.46                           # printed; or ground Ø2.45 pins pressed into Ø2.42
PIN_D_GROUND = 2.45
PIN_HOLE_D_FOR_GROUND = 2.42
PIN_LEN = 2.0
PIN_PCD_R = 7.00
PIN_THETA_DEG = {'J15': (90.0, 270.0), 'GR': (171.97, -8.03)}   # the opposite pair, never the reference hole
MARK_ARM_THETA_DEG = dict(REFERENCE_THETA_DEG)
MARK_ARM_WIDTH = 6.0
STEP_START_R = 9.9                     # HORN_D / 2 + 0.3: past the horn rim
LAND_GAP = 0.30                        # the land beside the throat sits this far above the case strip
# (r_from, r_to, underside n) at the design seat; the cap rides 1.0 higher on a 5.70 seat
MARK_STEPS = {'J15': ((9.9, 14.6, 2.9), (14.6, 18.0, 2.75)), 'GR': ((9.9, 13.6, 0.3),)}
SLOT_R = {'J15': (5.0, 17.5), 'GR': (5.0, 12.8)}
THROAT_W = 1.0                         # V-groove: a throat from below up to LAND_H above the underside,
LAND_H = 0.5                           # then flanks at V_HALF_ANGLE_DEG to the top
V_HALF_ANGLE_DEG = 30.0
DOWEL_ARM_THETA_DEG = 0.0              # on the w = 0 line: an r-offset of the axis rotates nothing
DOWEL_ARM_WIDTH = 8.0
DOWEL_ARM_UNDERSIDE_N = 4.0
DOWEL_ARM_END_R = 45.0
DOWEL_SLOT_W = 3.02
DOWEL_SLOT_R = (39.0, 45.0)            # radial, open at the end
DOWEL_AXIS_RW = (42.0, 0.0)
DOWEL_PIN_D = 3.0                      # Ø3.0 h6 steel pin

# ── the cradle (case frame; pads and flexures on the measured faces) ──
BASE_PLATE = {'n': (-40.0, -34.0), 'r': (-16.15, 46.0), 'w': (-16.40, 16.60)}
CABLE_OPENING = {'r': (10.5, 17.5), 'w_half': 10.5}   # the connectors open downward in CAD (ASSUMPTION, P1)
CABLE_LINE = {'r': 14.0, 'w': 0.0, 'n': (-60.0, -30.0)}
FOOT_SIZE = 6.0                        # four feet at the plate corners; the cable passes below
FOOT_N = (-60.0, -40.0)
BOTTOM_PADS = (
    {'name': 'P1', 'r': -6.0, 'w': 9.0, 'd': 4.0, 'top_n': -29.0, 'face': 'plateau'},
    {'name': 'P2', 'r': -6.0, 'w': -9.0, 'd': 4.0, 'top_n': -29.0, 'face': 'plateau'},
    {'name': 'P3', 'r': 32.0, 'w': 0.0, 'd': 3.0, 'top_n': -30.5, 'face': 'band'},
)
WALL_THICKNESS = 4.0
WALL_TOP_N = -3.0
WALL_INNER = {'-w': -12.40, '+w': 12.60, 'near': -10.15, 'far': 35.35}
LEAD_IN = 1.0                          # 1.0 × 45° on the inner top edges
DATUM_PAD_D = 3.0
DATUM_PAD_H = 0.15
SIDE_PADS = ({'r': 0.0, 'n': -10.0}, {'r': 25.0, 'n': -10.0})   # on the −w wall, apex w −12.25
END_PAD = {'w': 0.0, 'n': -12.0}                                # on the near-end wall, apex r −10.0
FLEXURE_W = 8.0
FLEXURE_T = 1.5                        # the wall's inner 1.5 mm; the outer 2.5 mm behind it removed
FLEXURE_ROOT_N = -27.0
FLEXURE_FREE_N = -13.0
FLEXURE_SLOT = 0.8                     # cut free on both sides and above
FLEXURE_PLUS_W_R = (8.5, 16.5)
FLEXURE_FAR_W = (-4.0, 4.0)
BUMP_D = 2.0                           # a half-cylinder along the tongue at BUMP_N
BUMP_N = -14.0
BUMP_APEX = {'+w': 12.20, 'far': 34.95}
DOWEL_POST = {'r': (40.0, 44.0), 'w_half': 4.0, 'top_n': -3.0}
DOWEL_HOLE_D = 2.95                    # the Ø3.0 h6 pin pressed in
DOWEL_HOLE_BOTTOM_N = -17.0            # 14 mm press depth
DOWEL_PIN_LEN = 28.0
DOWEL_PIN_TOP_N = 11.0
# the P0 direction arrow: counter-clockwise seen from above with the horn up
P0_ARROW = {'radius': 12.0, 'theta_deg': (160.0, 200.0), 'head_at_deg': 200.0,
            'width': 0.6, 'depth': 0.5, 'top_n': -3.0}
P0_ARROW_HEAD = {'length': 2.0, 'width': 1.4}

# ── --self-check ──
REACH_PROBE_D = 0.7
REACH_STEP = 0.25
REACH_END_MARGIN = 0.4                 # the probe stays this far inside the slot ends
CASE_STRIP_R = {'J15': (11.6, 17.1), 'GR': (10.2, 12.4)}
EXPECTED_REACH_COUNTS = {'J15': (16, 22), 'GR': (16, 9)}   # (horn, case) probe positions, p21b
GUIDE_W = 0.65                         # the first cap material beside the throat
INTERFERENCE_W = (-2.9, 0.0, 2.9)
INTERFERENCE_R = (9.9, 36.0, 0.25)     # (from, to, step)
INTERFERENCE_CLEARANCE = 0.23
PAD_APEX_PROBE = 0.01
ENVELOPE_MAX_SHARED_MM3 = 0.001
STL_DEFLECTION = 0.01
STL_ANGULAR = 0.2
