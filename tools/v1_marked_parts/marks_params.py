"""Parameters of the marked V1 arm parts and the two pure decisions of their
generator.

``build_marked_parts.py`` reads the owner's assembly STEP, finds every joint's
servo frame from the B-rep, cuts an engraved arrow (0.5 mm) and a colour-dot
pocket (Ø4 × 0.4 mm) into ten printed parts, verifies every cut and writes STL +
STEP + previews. This module holds everything that decides WHAT it cuts and
WHEN it refuses, stdlib + the servo spec only, so
``robotis_ai_setup/tests/test_v1_marked_parts_params.py`` checks it in CI:

* ``pose_findings(measured)`` — the STEP must be at the URDF zero pose. Every
  arrow is placed relative to the reference horn hole ``H = c + 7·u(θ_ref)`` of
  its servo, so a STEP exported with a joint turned would cut every downstream
  arrow at the wrong hole while every cut check still passes. Three checks per
  joint: a horn hole at H, the matched-hole pattern, each joint frame relative to
  J1's. No flag overrides a finding.
* ``placement_findings(outcomes, expected)`` — a mark the search placed
  elsewhere than ``EXPECTED`` refuses unless the owner looked at the previews
  and passed ``--accept-placements``.

Servo frame (per joint): ``n`` out of the horn face, ``c`` on the output axis at
the case's output face, ``r`` toward the case's long end, ``w = n × r``;
``u(θ) = cos θ·r + sin θ·w`` (the gauge's frame, ``tools/servo_gauge``).
"""

from __future__ import annotations

import math
import os
import sys

_ARM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                        'robotis_ai_setup', 'docker', 'open_manipulator')
if _ARM_DIR not in sys.path:
    sys.path.insert(0, _ARM_DIR)

import feetech_servo_spec as fss  # noqa: E402

# ── what is marked ──
JOINTS = fss.JOINT_NAMES
JOINT_SHORT = ('J1', 'J2', 'J3', 'J4', 'J5', 'claw')
THETA_REF_DEG = fss.REFERENCE_THETA_DEG
MARK_COLOUR_HEX = fss.COLOUR_HEX
SERVO_PRODUCTS = ('feetech_servo_sts3215', 'feetech_servo_sts3250')
HORN_PRODUCT = 'servo_horn'
# the part the joint turns (its horn holes are matched by this part's holes)
CHILD_PART = {'joint1': 'link1_motorpart_new', 'joint2': 'link2part', 'joint3': 'link3_part',
              'joint4': 'link5_part', 'joint5': 'last_link', 'RL_joint': 'Spurgear_18T_'}
# (STEP product name, output stem, match); the gear's product name continues
# „Metric - Spur gear 1M 18T …", so it is matched by prefix, every other exactly
PARTS = (
    ('Base_part', 'base_part', 'exact'),
    ('base_lid', 'base_lid', 'exact'),
    ('link1_motorpart_new', 'link1_motorpart_new', 'exact'),
    ('link2part', 'link2part', 'exact'),
    ('link2_motor_moutingpart', 'link2_motor_moutingpart', 'exact'),
    ('link3_part', 'link3_part', 'exact'),
    ('link4_part', 'link4_part', 'exact'),
    ('link5_part', 'link5_part', 'exact'),
    ('last_link', 'last_link', 'exact'),
    ('Spurgear_18T_', 'spurgear_18t', 'prefix'),
)
# (product, mark label, kind, joint index), in cut order per part. A dot marks the
# pocket a servo goes into (its colour); an arrow on the part a joint turns points
# at the marked horn hole (J1: the lid, OQ4; J4: the hole that stays empty, OQ3).
MARKS = (
    ('Base_part', 'dot J1', 'dot', 0),
    ('base_lid', 'arrow J1', 'arrow', 0),
    ('link1_motorpart_new', 'arrow J1', 'arrow', 0),
    ('link1_motorpart_new', 'dot J2', 'dot', 1),
    ('link2part', 'arrow J2', 'arrow', 1),
    ('link2_motor_moutingpart', 'dot J3', 'dot', 2),
    ('link3_part', 'arrow J3', 'arrow', 2),
    ('link4_part', 'dot J4', 'dot', 3),
    ('link5_part', 'arrow J4', 'arrow', 3),
    ('link5_part', 'dot J5', 'dot', 4),
    ('last_link', 'arrow J5', 'arrow', 4),
    ('last_link', 'dot claw', 'dot', 5),
    ('Spurgear_18T_', 'arrow claw', 'arrow', 5),
)

# ── finding the frames (p29b: equal to the probe frames to 0.0005 mm / 0.0000°) ──
HORN_HOLE_RADIUS = (1.25, 0.01)        # Ø2.50 horn holes: (radius, tolerance)
CHILD_HOLE_RADIUS = (1.2, 1.8)         # a child-part hole matching a horn hole
COAXIAL_MM = 0.05
COAXIAL_DEG = 0.5
HORN_CHILD_AXIAL_MM = 15.0             # the child hole's face centre within this of the horn hole, along the axis
SERVO_AXIAL_MM = 10.0                  # the servo's coaxial cylinder within this of the horn
OUTPUT_FACE_TOL_DEG = 1.0
HOLE_RADIUS_MM = 7.0                   # H = c + 7·u(θ_ref): the horn holes' circle

# ── the zero-pose check (s03) ──
POSE_H_TOL_MM = 0.05                   # (1) a horn-hole axis passes within this of H (≈ 0.41° at r 7)
POSE_THETA_TOL_DEG = 0.5               # (2) each matched hole angle, compared mod 360
POSE_FRAME_TOL_DEG = 1.0               # (3) n and u(θ_ref) in J1's basis (r1, w1, n1)
MATCHED_HOLE_THETAS = {
    'joint1': (0.0, 90.0, 180.0, 270.0), 'joint2': (0.0, 90.0, 180.0), 'joint3': (0.0, 180.0, 270.0),
    'joint4': (90.0, 180.0, 270.0),       # the reference hole at 0° is the empty one (OQ3)
    'joint5': (0.0, 90.0, 180.0, 270.0), 'RL_joint': (81.97, 171.97),
}
J1_REL_FRAME = ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0))     # J1's own (n, u) in its basis, by definition
REL_FRAMES = {   # joint -> (n, u(θ_ref)) in J1's basis
    'joint2': ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
    'joint3': ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
    'joint4': ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
    'joint5': ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    'RL_joint': ((0.0, 0.0, 1.0), (-0.990195, 0.139692, 0.0)),
}

# ── the marks ──
ARROW_DEPTH = 0.5
ARROW_SHAFT_W = 1.2
ARROW_HEAD_FRAC = 0.33                 # head length = 0.33 L
ARROW_SIZES = ((6.0, 3.0), (5.0, 2.6), (4.0, 2.2))   # (L, W), largest first
DOT_D = 4.0
DOT_DEPTH = 0.4
TOOL_LEAD = 0.05                       # every tool starts this far above the face


def arrow_area(length, width):
    head = ARROW_HEAD_FRAC * length
    return width * head / 2.0 + (length - head) * ARROW_SHAFT_W


def arrow_volume(length, width):
    """The material an arrow of ``(length, width)`` removes (V2)."""
    return arrow_area(length, width) * ARROW_DEPTH


def dot_volume():
    return math.pi * (DOT_D / 2.0) ** 2 * DOT_DEPTH


# ── the placement search (p24, on the uncut part) ──
D_MIN = (3.0, 3.0, 3.0, 0.0, 3.0, 3.0)  # the arrow tip's distance from H; J4 may start at its empty hole
D_MAX = 12.0
D_STEP = 0.2
D_PREFER_MAX = 6.0                     # the largest size whose d is <= this, else the smallest d
DPHI_LADDER_DEG = tuple((k + 1) // 2 * 5 * (1 if k % 2 else -1) for k in range(72))   # 0, +5, −5, +10, …
FOOTPRINT_GROW = 0.6
SAMPLE_STEP = 0.2
FACE_NORMAL_TOL_DEG = 2.0
FACE_HEIGHT_TOL = 0.01
ARROW_MIN_BELOW = 1.3
DOT_SAMPLE_R = 2.6
DOT_MIN_BELOW = 1.2
DOT_TARGET_R = (37.0, 18.3, 17.3, 22.3, 12.0, 12.3)   # P* = c + r·u(θ_ref): the end of the visible case strip
DOT_GRID = ((-45.0, 60.0), (-45.0, 45.0), 0.5)          # (a range along u, b range along v = n × u, step)
DOT_RULE_A_BAND = (-10.0, 15.0)                         # face height above the output face (rule A)
OCCLUSION_STEP = 0.9
OCCLUSION_RANGE = 60.0
PSI_LADDER_DEG = tuple((k + 1) // 2 * 15 * (1 if k % 2 else -1) for k in range(24))   # rule B, from θ_ref
RULE_B_HEIGHTS = (1.0, -12.0, 0.5)     # (from, to, step) along n
RULE_B_LATERAL = (-15.0, 15.0, 0.5)
RULE_B_START = 120.0                   # the ray starts this far outside, toward the axis
RULE_B_NORMAL_TOL_DEG = 3.0

# ── verification (V1–V8) ──
CLEARANCE_MIN = 0.6                    # exact B-rep distance from the tool to every other face
WALL_MIN = 0.8                         # material left under the floor
VOLUME_TOL_FRAC = 0.02                 # V2: removed volume vs the analytic tool volume
BBOX_TOL = 1e-6                        # V3
PART_VOLUME_TOL_FRAC = 0.001           # V7
MESH_DEFLECTION = 0.005                # V8: 0.02 left link2part non-watertight (p28b)
MESH_ANGULAR = 0.2
MESH_WELD = 1e-5
MESH_RETRIES = 2                       # halve the deflection up to twice more, then refuse
HORN_TOP_N = (4.70, 4.70, 4.70, 4.70, 4.70, 5.70)   # --self-check warning only
HORN_TOP_TOL = 0.05

# ── previews (BRepMesh → numpy z-buffer → PNG) ──
PANEL_PX = 400
PANEL_FIELD_MM = (30.0, 30.0, 30.0, 40.0, 30.0, 30.0)
ISO_PX = (800, 600)
ISO_FIELD_FACTOR = 1.1
PART_GREY = 0.45
SHADE = (0.35, 0.65)                   # 0.35 + 0.65·|n̂·view|

# ── the placement fence (owner R4-3) ──
FENCE_ARROW_DPHI_DEG = 5.0
FENCE_ARROW_D_MM = 0.4
FENCE_DOT_CENTRE_MM = 1.0
FENCE_DOT_B_MM = 1.0

# The outcomes on the owner's current STEP (p24, verified by p25 and p28c). A CAD
# change that moves a mark refuses until the owner looks at the previews, runs with
# --accept-placements and pastes the block the generator prints here.
EXPECTED = {
    ('Base_part', 'dot J1'): {'kind': 'dot', 'rule': 'B', 'psi_deg': 0.0, 'height_mm': -3.5, 'lateral_mm': -10.5},
    ('base_lid', 'arrow J1'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 0.0, 'd_mm': 9.8},
    ('link1_motorpart_new', 'arrow J1'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 70.0, 'd_mm': 3.4},
    ('link1_motorpart_new', 'dot J2'): {'kind': 'dot', 'rule': 'A', 'centre_mm': (21.0, 0.0)},
    ('link2part', 'arrow J2'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 65.0, 'd_mm': 3.0},
    ('link2_motor_moutingpart', 'dot J3'): {'kind': 'dot', 'rule': 'A', 'centre_mm': (20.0, 0.0)},
    ('link3_part', 'arrow J3'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': -65.0, 'd_mm': 3.0},
    ('link4_part', 'dot J4'): {'kind': 'dot', 'rule': 'A', 'centre_mm': (25.0, 0.0)},
    ('link5_part', 'arrow J4'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 155.0, 'd_mm': 9.6},
    ('link5_part', 'dot J5'): {'kind': 'dot', 'rule': 'C', 'centre_mm': (9.5, 14.0)},
    ('last_link', 'arrow J5'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 55.0, 'd_mm': 3.8},
    ('last_link', 'dot claw'): {'kind': 'dot', 'rule': 'A', 'centre_mm': (8.0, -10.5)},
    ('Spurgear_18T_', 'arrow claw'): {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': -5.0, 'd_mm': 3.0},
}

_FENCED_KEYS = {'arrow': ('size', 'dphi_deg', 'd_mm'), 'A': ('rule', 'centre_mm'), 'C': ('rule', 'centre_mm'),
                'B': ('rule', 'psi_deg', 'height_mm', 'lateral_mm')}


_POSE_TAIL = 'bitte das Modell in der URDF-Nullstellung exportieren.'


def _finite(value):
    if isinstance(value, (list, tuple)):
        return len(value) > 0 and all(_finite(v) for v in value)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _angle_deg(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 180.0
    c = sum(x * y for x, y in zip(a, b)) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _circular(a, b):
    d = (a - b) % 360.0
    return min(d, 360.0 - d)


def _pattern_matches(got, want):
    """``got`` and ``want`` are the same set of angles, each within POSE_THETA_TOL_DEG mod 360."""
    if len(got) != len(want):
        return False
    left = list(got)
    for w in want:
        hit = [g for g in left if _circular(g, w) <= POSE_THETA_TOL_DEG]
        if not hit:
            return False
        left.remove(hit[0])
    return True


def _judgeable(m):
    """Every value a pose decision reads is present and finite."""
    if not isinstance(m, dict):
        return False
    thetas, rel_n, rel_u = m.get('matched_thetas_deg'), m.get('rel_n'), m.get('rel_u')
    return (_finite(m.get('h_dist_mm'))
            and isinstance(thetas, (list, tuple)) and (len(thetas) == 0 or _finite(thetas))
            and isinstance(rel_n, (list, tuple)) and len(rel_n) == 3 and _finite(rel_n)
            and isinstance(rel_u, (list, tuple)) and len(rel_u) == 3 and _finite(rel_u))


def pose_findings(measured):
    """The zero-pose decision. ``measured`` maps every joint of ``JOINTS`` to
    ``{'h_dist_mm', 'matched_thetas_deg', 'rel_n', 'rel_u'}``; returns one German
    line per failed check and joint (empty = the STEP is at the URDF zero pose).
    A joint that is missing or carries a non-finite value cannot be judged and
    refuses too."""
    out = []

    def finding(joint, check):
        label = fss.JOINT_LABEL_DE[JOINTS.index(joint)]
        out.append(f'STEP nicht in der Nullstellung: {joint} ({label}) — {check}; {_POSE_TAIL}')

    for joint in JOINTS:
        m = measured.get(joint) if isinstance(measured, dict) else None
        if not _judgeable(m):
            finding(joint, 'nicht beurteilbar (Messwerte fehlen oder sind ungültig)')
            continue
        if m['h_dist_mm'] > POSE_H_TOL_MM:
            finding(joint, f"kein Hornloch an der markierten Stelle (nächste Lochachse {m['h_dist_mm']:.3f} mm "
                           f'entfernt, erlaubt {POSE_H_TOL_MM} mm)')
        want = MATCHED_HOLE_THETAS[joint]
        got = [float(t) for t in m['matched_thetas_deg']]
        if not _pattern_matches(got, want):
            shown = ', '.join(f'{t % 360.0:.2f}°' for t in sorted(got, key=lambda t: t % 360.0)) or 'keines'
            finding(joint, f"Lochbild der genutzten Hornlöcher {shown} statt {', '.join(f'{t:g}°' for t in want)}")
        n_want, u_want = J1_REL_FRAME if joint == JOINTS[0] else REL_FRAMES[joint]
        dn, du = _angle_deg(m['rel_n'], n_want), _angle_deg(m['rel_u'], u_want)
        if dn > POSE_FRAME_TOL_DEG or du > POSE_FRAME_TOL_DEG:
            finding(joint, f'Lage gegenüber Gelenk 1 weicht ab (Achse {dn:.2f}°, Markierungsrichtung {du:.2f}°, '
                           f'erlaubt {POSE_FRAME_TOL_DEG:g}°)')
    return out


def describe(entry):
    """One placement in German, for the generator's lines and the fence's findings."""
    if entry is None:
        return 'keine Platzierung'
    if entry.get('kind') == 'arrow':
        size = entry.get('size') or ('?', '?')
        return f"Pfeil {size[0]}×{size[1]} mm, Δφ {entry.get('dphi_deg')}°, d {entry.get('d_mm')} mm"
    if entry.get('rule') == 'B':
        return (f"Punkt Regel B, ψ {entry.get('psi_deg')}°, Höhe {entry.get('height_mm')} mm, "
                f"seitlich {entry.get('lateral_mm')} mm")
    return f"Punkt Regel {entry.get('rule')}, Mitte {entry.get('centre_mm')} mm"


def _within_fence(found, want):
    try:
        if found.get('kind') != want['kind']:
            return False
        if want['kind'] == 'arrow':
            return (tuple(found['size']) == tuple(want['size'])
                    and _circular(found['dphi_deg'], want['dphi_deg']) <= FENCE_ARROW_DPHI_DEG + 1e-9
                    and abs(found['d_mm'] - want['d_mm']) <= FENCE_ARROW_D_MM + 1e-9)
        if found.get('rule') != want['rule']:
            return False
        if want['rule'] == 'B':
            return (_circular(found['psi_deg'], want['psi_deg']) <= 1e-6
                    and abs(found['height_mm'] - want['height_mm']) <= FENCE_DOT_B_MM + 1e-9
                    and abs(found['lateral_mm'] - want['lateral_mm']) <= FENCE_DOT_B_MM + 1e-9)
        return math.dist(found['centre_mm'], want['centre_mm']) <= FENCE_DOT_CENTRE_MM + 1e-9
    except (KeyError, TypeError, ValueError):
        return False


def placement_findings(outcomes, expected):
    """The placement fence (owner R4-3): one German line per mark whose outcome
    lies outside the fence around ``expected`` (arrow: same size, Δφ within ±5°,
    d within ±0.4 mm; dot: same rule, centre within 1.0 mm, rule B: ψ equal and
    height / lateral within 1.0 mm), per mark missing from ``outcomes`` and per
    outcome of a mark ``expected`` does not know."""
    out = []
    for key, want in expected.items():
        found = outcomes.get(key)
        if found is None or not _within_fence(found, want):
            out.append(f'Markierung {key[0]} {key[1]} weicht von EXPECTED ab: {describe(found)} '
                       f'statt {describe(want)}')
    for key in outcomes:
        if key not in expected:
            out.append(f'Markierung {key[0]} {key[1]} weicht von EXPECTED ab: {describe(outcomes[key])} '
                       f'statt keiner Markierung')
    return out


def _fenced(entry):
    if entry.get('kind') == 'arrow':
        keys = ('kind',) + _FENCED_KEYS['arrow']
    else:
        keys = ('kind',) + _FENCED_KEYS.get(entry.get('rule'), ('rule',))
    return {k: entry[k] for k in keys if k in entry}


def _literal(value):
    if isinstance(value, float):
        return repr(round(value, 3) + 0.0)      # + 0.0 turns a -0.0 into 0.0
    if isinstance(value, tuple):
        return '(' + ', '.join(_literal(v) for v in value) + ')'
    return repr(value)


def format_expected(outcomes):
    """``outcomes`` as a ready-to-paste ``EXPECTED = {…}`` block (fenced keys only, MARKS order)."""
    order = [(p, label) for p, label, _, _ in MARKS]
    keys = [k for k in order if k in outcomes] + sorted(k for k in outcomes if k not in order)
    lines = ['EXPECTED = {']
    for key in keys:
        entry = _fenced(outcomes[key])
        body = ', '.join(f'{k!r}: {_literal(v)}' for k, v in entry.items())
        lines.append(f'    ({key[0]!r}, {key[1]!r}): {{{body}}},')
    lines.append('}')
    return '\n'.join(lines)
