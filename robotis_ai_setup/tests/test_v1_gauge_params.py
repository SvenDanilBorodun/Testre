"""Deps-free tests for ``tools/servo_gauge/gauge_params.py`` — the numbers the
V1 servo gauge (the cradle and the two stepped floating key caps) is built
from, and the rules that tie them to the servo spec and the measured case.

The gauge sets every servo's mechanical zero: the cap's pins locate it on the
horn, its slot guides ONE paint stroke over the reference hole, the horn rim and
the case strip. A pin on the wrong hole, a slot that misses the reference hole
or a step that sits on the case would mark a wrong zero with no later check, so
every value of build spec §6A is pinned here and every geometric rule is checked
from the numbers. The OCP build and its ``--self-check`` run only on a machine
with the CAD venv; ``build_gauge.py`` is checked here only as source (it must
import OCP and numpy inside functions, so CI can compile it without them).
"""

import ast
import math
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_GAUGE_DIR = os.path.join(_REPO, 'tools', 'servo_gauge')
_DOCKER = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator')
for _p in (_DOCKER, _GAUGE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import feetech_servo_spec as fss  # noqa: E402
import gauge_params as gp  # noqa: E402

_BUILD = os.path.join(_GAUGE_DIR, 'build_gauge.py')


def _on_hole_set(theta, offset):
    k = (theta - offset) / 90.0
    return abs(k - round(k)) < 1e-9


def _circ(a, b):
    d = (a - b) % 360.0
    return min(d, 360.0 - d)


def _case_top_max(table, a, b):
    """Highest case-top level of ``table`` over the half-open r range [a, b)."""
    levels = [h for lo, hi, h in table if lo < b and hi > a]
    return max(levels)


class TestMeasuredCase(unittest.TestCase):
    """The measured CAD case (p14–p16, p30, p31; both models identical)."""

    def test_outline(self):
        self.assertEqual(gp.CASE_R, (-10.0, 35.0))
        self.assertEqual(gp.CASE_W_HALF, 12.25)
        self.assertEqual(gp.CASE_N, (-32.4, 2.6))
        self.assertEqual(gp.CASE_DATASHEET_MM, (45.2, 24.7, 35.0))

    def test_bottom_faces(self):
        self.assertEqual(gp.CASE_BOTTOM_FACES['plateau'], (-29.0, (-9.72, 11.65), (-11.97, 11.97)))
        self.assertEqual(gp.CASE_BOTTOM_FACES['square'], (-29.2, (-5.0, 5.0), (-5.0, 5.0)))
        self.assertEqual(gp.CASE_BOTTOM_FACES['boss'], (-33.10, (-2.75, 2.75), (-2.75, 2.75)))
        self.assertEqual(gp.CASE_BOTTOM_FACES['foot_frame'], (-32.40, (19.77, 29.20), (-8.74, 8.74)))
        self.assertEqual(gp.CASE_BOTTOM_FACES['label_recess'], (-32.25, (21.5, 27.5), (-6.0, 6.0)))
        self.assertEqual(gp.CASE_BOTTOM_FACES['band'], (-30.50, (29.8, 34.2), (-9.3, 9.3)))
        self.assertEqual(gp.CASE_CONNECTOR_POCKETS, (((12.15, 15.75), (-9.0, -0.5)),
                                                     ((12.15, 15.75), (0.5, 9.0))))
        self.assertEqual(gp.CASE_CONNECTOR_POCKET_TOP_N, -19.4)
        self.assertEqual(gp.PLATEAU_FLUSH_ALONG_R, ((-9.7, -0.3), 9.0))
        self.assertEqual(gp.PLATEAU_FLUSH_ALONG_W, ((1.0, 11.9), -6.0))

    def test_side_and_end_faces(self):
        self.assertEqual(gp.CASE_SIDE_FACE, {'w': 12.25, 'n': ((-18.9, -6.1), (-26.88, -19.0)),
                                             'r': (-6.57, 33.0)})
        self.assertEqual(gp.CASE_NEAR_END_FACE, {'r': -10.0, 'n': (-18.9, -6.1), 'w_half': 8.82})
        self.assertEqual(gp.CASE_FAR_END_FACE, {'r': 35.0, 'n': (-18.9, -6.1), 'w_half': 10.25})

    def test_case_top_under_the_arms(self):
        self.assertEqual(gp.CASE_TOP['J15'], ((9.0, 10.25, 0.0), (10.25, 11.5, 2.6), (11.5, 14.6, 2.6),
                                              (14.6, 30.6, 2.45), (30.6, 35.0, 2.6)))
        self.assertEqual(gp.CASE_TOP['GR'], ((9.0, 12.5, 0.0), (12.5, 13.6, -2.86)))
        for table in gp.CASE_TOP.values():
            for (a0, b0, _), (a1, b1, _) in zip(table, table[1:]):
                self.assertEqual(b0, a1, 'the case-top table must be contiguous')

    def test_horn(self):
        self.assertEqual(gp.HORN_D, 19.2)
        self.assertEqual(gp.HORN_PCD, 14.00)
        self.assertEqual(gp.HORN_HOLE_D, 2.50)
        self.assertEqual(gp.HORN_CENTRE_BORE_D, 3.2)
        self.assertEqual(gp.HORN_TOP_N_CAD, {'J15': 4.70, 'GR': 5.70})
        self.assertEqual(gp.CAP_DESIGN_SEAT_N, 4.70)
        self.assertEqual(gp.CAP_CHECK_SEATS_N, {'J15': (4.70,), 'GR': (4.70, 5.70)})

    def test_the_envelope_holds_every_measured_bottom_face(self):
        # Every bottom face lies inside the envelope the self-check tests the cradle against.
        for name, (n, (r0, r1), (w0, w1)) in gp.CASE_BOTTOM_FACES.items():
            for r in (r0, r1):
                for w in (w0, w1):
                    inside = any(b[0] <= r <= b[3] and b[1] <= w <= b[4] and b[2] <= n <= b[5]
                                 for b in gp.CASE_ENVELOPE_BOXES)
                    self.assertTrue(inside, f'{name} corner ({r}, {w}, {n}) outside the envelope')


class TestCaps(unittest.TestCase):
    def _joints(self, cap):
        return [i for i, c in enumerate(fss.GAUGE_CAP) if c == cap]

    def test_two_caps_one_per_spec_label(self):
        self.assertEqual(gp.CAP_IDS, ('J15', 'GR'))
        self.assertEqual(set(fss.GAUGE_CAP), set(gp.CAP_IDS))
        self.assertEqual(self._joints('J15'), [0, 1, 2, 3, 4])
        self.assertEqual(self._joints('GR'), [5])

    def test_disc_and_bore(self):
        self.assertEqual(gp.DISC_R, 9.4)
        self.assertEqual(gp.CAP_THICKNESS, 4.0)
        self.assertEqual(gp.CAP_BORE_D, 7.0)
        self.assertLess(gp.DISC_R, gp.HORN_D / 2, 'the disc rests on the horn top inside its rim')
        self.assertGreater(gp.CAP_BORE_D, gp.HORN_CENTRE_BORE_D)

    def test_pins(self):
        self.assertEqual(gp.PIN_D, 2.46)
        self.assertTrue(2.45 <= gp.PIN_D <= 2.48)
        self.assertLess(gp.PIN_D, gp.HORN_HOLE_D)
        self.assertEqual(gp.PIN_D_GROUND, 2.45)
        self.assertEqual(gp.PIN_HOLE_D_FOR_GROUND, 2.42)
        self.assertLess(gp.PIN_HOLE_D_FOR_GROUND, gp.PIN_D_GROUND, 'a pressed pin needs interference')
        self.assertEqual(gp.PIN_LEN, 2.0)
        self.assertEqual(gp.PIN_PCD_R, 7.00)
        self.assertEqual(gp.PIN_PCD_R, gp.HORN_PCD / 2)
        self.assertEqual(gp.PIN_THETA_DEG, {'J15': (90.0, 270.0), 'GR': (171.97, -8.03)})

    def test_the_pins_are_the_opposite_pair_on_the_hole_set_never_the_reference_hole(self):
        for cap in gp.CAP_IDS:
            ref = gp.REFERENCE_THETA_DEG[cap]
            a, b = gp.PIN_THETA_DEG[cap]
            self.assertAlmostEqual(_circ(a, b), 180.0, places=9, msg=f'{cap}: the pins are not opposite')
            for i in self._joints(cap):
                offset = fss.HOLE_THETA_OFFSET_DEG[i]
                for pin in (a, b):
                    self.assertTrue(_on_hole_set(pin, offset), f'{cap} pin {pin} is not a horn hole of joint {i + 1}')
            for pin in (a, b):
                self.assertGreater(_circ(pin, ref), 1.0, f'{cap}: a pin sits in the reference hole')

    def test_the_reference_theta_is_the_spec_value_of_every_joint_using_the_cap(self):
        self.assertEqual(gp.REFERENCE_THETA_DEG, {'J15': 0.0, 'GR': 81.97})
        for cap in gp.CAP_IDS:
            for i in self._joints(cap):
                self.assertEqual(fss.REFERENCE_THETA_DEG[i], gp.REFERENCE_THETA_DEG[cap])
                self.assertTrue(_on_hole_set(gp.REFERENCE_THETA_DEG[cap], fss.HOLE_THETA_OFFSET_DEG[i]))

    def test_the_marking_arm_runs_along_the_reference_hole(self):
        self.assertEqual(gp.MARK_ARM_THETA_DEG, gp.REFERENCE_THETA_DEG)
        self.assertEqual(gp.MARK_ARM_WIDTH, 6.0)

    def test_the_slot_passes_over_the_reference_hole_and_past_the_horn_rim(self):
        self.assertEqual(gp.SLOT_R, {'J15': (5.0, 17.5), 'GR': (5.0, 12.8)})
        hole_r = gp.HORN_HOLE_D / 2
        for cap, (r0, r1) in gp.SLOT_R.items():
            self.assertLessEqual(r0, gp.PIN_PCD_R - hole_r, f'{cap}: slot starts beyond the reference hole')
            self.assertGreaterEqual(r1, gp.PIN_PCD_R + hole_r)
            self.assertGreater(r1, gp.HORN_D / 2, f'{cap}: slot ends on the horn')
            lo, hi = gp.CASE_STRIP_R[cap]
            self.assertGreater(lo, gp.HORN_D / 2)
            self.assertLessEqual(hi, r1 - gp.REACH_END_MARGIN, f'{cap}: case strip beyond the probe range')

    def test_the_v_groove(self):
        self.assertEqual(gp.THROAT_W, 1.0)
        self.assertEqual(gp.LAND_H, 0.5)
        self.assertEqual(gp.V_HALF_ANGLE_DEG, 30.0)

    def test_the_step_starts_past_the_rim(self):
        self.assertEqual(gp.STEP_START_R, 9.9)
        self.assertAlmostEqual(gp.STEP_START_R, gp.HORN_D / 2 + 0.3, places=9)
        for cap, steps in gp.MARK_STEPS.items():
            self.assertEqual(steps[0][0], gp.STEP_START_R, cap)
            for (a0, b0, _), (a1, b1, _) in zip(steps, steps[1:]):
                self.assertEqual(b0, a1)

    def test_each_step_underside_is_the_case_top_plus_the_land_gap(self):
        self.assertEqual(gp.LAND_GAP, 0.30)
        self.assertEqual(gp.MARK_STEPS, {'J15': ((9.9, 14.6, 2.9), (14.6, 18.0, 2.75)),
                                         'GR': ((9.9, 13.6, 0.3),)})
        for cap, steps in gp.MARK_STEPS.items():
            for a, b, underside in steps:
                top = _case_top_max(gp.CASE_TOP[cap], a, b)
                self.assertAlmostEqual(underside, top + gp.LAND_GAP, places=9,
                                       msg=f'{cap} step r {a}–{b}: underside {underside}, case top {top}')
            self.assertGreaterEqual(steps[-1][1], gp.SLOT_R[cap][1], f'{cap}: slot runs off the stepped arm')

    def test_the_dowel_arm_and_slot(self):
        self.assertEqual(gp.DOWEL_ARM_THETA_DEG, 0.0)
        self.assertEqual(gp.DOWEL_ARM_WIDTH, 8.0)
        self.assertEqual(gp.DOWEL_ARM_UNDERSIDE_N, 4.0)
        self.assertEqual(gp.DOWEL_ARM_END_R, 45.0)
        self.assertEqual(gp.DOWEL_SLOT_W, 3.02)
        self.assertEqual(gp.DOWEL_SLOT_R, (39.0, 45.0))
        self.assertEqual(gp.DOWEL_SLOT_R[1], gp.DOWEL_ARM_END_R, 'the dowel slot is open at the end')
        self.assertEqual(gp.DOWEL_AXIS_RW, (42.0, 0.0))
        self.assertTrue(gp.DOWEL_SLOT_R[0] < gp.DOWEL_AXIS_RW[0] < gp.DOWEL_SLOT_R[1])
        self.assertEqual(gp.DOWEL_PIN_D, 3.0)
        self.assertGreater(gp.DOWEL_SLOT_W, gp.DOWEL_PIN_D)
        self.assertLess(gp.DOWEL_SLOT_W - gp.DOWEL_PIN_D, 0.05, 'the slot locks the rotation')
        # The dowel arm clears the highest case point under it.
        self.assertGreater(gp.DOWEL_ARM_UNDERSIDE_N, max(h for _, _, h in gp.CASE_TOP['J15']))
        # On the w = 0 line: an r-offset of the axis rotates nothing (S12).
        self.assertEqual(gp.DOWEL_AXIS_RW[1], 0.0)

    def test_the_gr_cap_rides_higher_on_the_cad_seat(self):
        self.assertAlmostEqual(gp.HORN_TOP_N_CAD['GR'] - gp.CAP_DESIGN_SEAT_N, 1.0, places=9)

    def test_self_check_parameters(self):
        self.assertEqual(gp.REACH_PROBE_D, 0.7)
        self.assertEqual(gp.REACH_STEP, 0.25)
        self.assertEqual(gp.REACH_END_MARGIN, 0.4)
        self.assertEqual(gp.CASE_STRIP_R, {'J15': (11.6, 17.1), 'GR': (10.2, 12.4)})
        self.assertEqual(gp.GUIDE_W, 0.65)
        self.assertAlmostEqual(gp.GUIDE_W, gp.THROAT_W / 2 + 0.15, places=9)
        self.assertEqual(gp.INTERFERENCE_W, (-2.9, 0.0, 2.9))
        self.assertEqual(gp.INTERFERENCE_R, (9.9, 36.0, 0.25))
        self.assertEqual(gp.INTERFERENCE_CLEARANCE, 0.23)
        self.assertLess(max(abs(w) for w in gp.INTERFERENCE_W), gp.MARK_ARM_WIDTH / 2)
        self.assertEqual(gp.EXPECTED_REACH_COUNTS, {'J15': (16, 22), 'GR': (16, 9)})


class TestCradle(unittest.TestCase):
    def test_base_plate_and_feet(self):
        self.assertEqual(gp.BASE_PLATE, {'n': (-40.0, -34.0), 'r': (-16.15, 46.0), 'w': (-16.40, 16.60)})
        self.assertEqual(gp.CABLE_OPENING, {'r': (10.5, 17.5), 'w_half': 10.5})
        self.assertEqual(gp.FOOT_SIZE, 6.0)
        self.assertEqual(gp.FOOT_N, (-60.0, -40.0))
        self.assertEqual(gp.CABLE_LINE, {'r': 14.0, 'w': 0.0, 'n': (-60.0, -30.0)})

    def test_the_cable_opening_lies_under_both_connector_pockets(self):
        lo, hi = gp.CABLE_OPENING['r']
        for (r0, r1), (w0, w1) in gp.CASE_CONNECTOR_POCKETS:
            self.assertTrue(lo <= r0 and r1 <= hi)
            self.assertTrue(-gp.CABLE_OPENING['w_half'] <= w0 and w1 <= gp.CABLE_OPENING['w_half'])
        self.assertTrue(lo < gp.CABLE_LINE['r'] < hi)

    def test_the_floor_clears_the_lowest_case_point(self):
        boss_n = gp.CASE_BOTTOM_FACES['boss'][0]
        self.assertEqual(gp.BASE_PLATE['n'][1], -34.0)
        self.assertGreater(boss_n - gp.BASE_PLATE['n'][1], 0.5)

    def test_bottom_pads_sit_on_the_listed_faces(self):
        self.assertEqual(gp.BOTTOM_PADS, (
            {'name': 'P1', 'r': -6.0, 'w': 9.0, 'd': 4.0, 'top_n': -29.0, 'face': 'plateau'},
            {'name': 'P2', 'r': -6.0, 'w': -9.0, 'd': 4.0, 'top_n': -29.0, 'face': 'plateau'},
            {'name': 'P3', 'r': 32.0, 'w': 0.0, 'd': 3.0, 'top_n': -30.5, 'face': 'band'},
        ))
        for pad in gp.BOTTOM_PADS:
            n, (r0, r1), (w0, w1) = gp.CASE_BOTTOM_FACES[pad['face']]
            rad = pad['d'] / 2
            self.assertEqual(pad['top_n'], n, pad['name'])
            self.assertTrue(r0 <= pad['r'] - rad and pad['r'] + rad <= r1, pad['name'])
            self.assertTrue(w0 <= pad['w'] - rad and pad['w'] + rad <= w1, pad['name'])
            # never on the recessed label field, the −29.2 square or the boss
            for other in ('label_recess', 'square', 'boss'):
                _, (q0, q1), (v0, v1) = gp.CASE_BOTTOM_FACES[other]
                overlap = pad['r'] + rad > q0 and pad['r'] - rad < q1 and pad['w'] + rad > v0 and pad['w'] - rad < v1
                self.assertFalse(overlap, f"{pad['name']} overlaps {other}")
        # P1/P2 lie on the measured flush lines of the plateau.
        (fr0, fr1), fw = gp.PLATEAU_FLUSH_ALONG_R
        (gw0, gw1), gr = gp.PLATEAU_FLUSH_ALONG_W
        for pad in gp.BOTTOM_PADS[:2]:
            rad = pad['d'] / 2
            self.assertEqual(abs(pad['w']), fw)
            self.assertTrue(fr0 <= pad['r'] - rad and pad['r'] + rad <= fr1)
            self.assertEqual(pad['r'], gr)
            self.assertTrue(gw0 <= abs(pad['w']) - rad and abs(pad['w']) + rad <= gw1)

    def test_walls(self):
        self.assertEqual(gp.WALL_THICKNESS, 4.0)
        self.assertEqual(gp.WALL_TOP_N, -3.0)
        self.assertEqual(gp.WALL_INNER, {'-w': -12.40, '+w': 12.60, 'near': -10.15, 'far': 35.35})
        self.assertEqual(gp.LEAD_IN, 1.0)
        # The pocket is larger than the CAD case and the datasheet case.
        self.assertGreater(gp.WALL_INNER['+w'] - gp.WALL_INNER['-w'], gp.CASE_DATASHEET_MM[1])
        self.assertGreater(gp.WALL_INNER['far'] - gp.WALL_INNER['near'], gp.CASE_DATASHEET_MM[0])
        self.assertLess(gp.WALL_INNER['-w'], -gp.CASE_W_HALF)
        self.assertGreater(gp.WALL_INNER['+w'], gp.CASE_W_HALF)
        self.assertLess(gp.WALL_INNER['near'], gp.CASE_R[0])
        self.assertGreater(gp.WALL_INNER['far'], gp.CASE_R[1])
        # The outer faces of the side walls are the base plate's w edges.
        self.assertAlmostEqual(gp.WALL_INNER['-w'] - gp.WALL_THICKNESS, gp.BASE_PLATE['w'][0], places=9)
        self.assertAlmostEqual(gp.WALL_INNER['+w'] + gp.WALL_THICKNESS, gp.BASE_PLATE['w'][1], places=9)

    def test_datum_pads_touch_the_case_faces(self):
        self.assertEqual(gp.SIDE_PADS, ({'r': 0.0, 'n': -10.0}, {'r': 25.0, 'n': -10.0}))
        self.assertEqual(gp.END_PAD, {'w': 0.0, 'n': -12.0})
        self.assertEqual(gp.DATUM_PAD_D, 3.0)
        self.assertEqual(gp.DATUM_PAD_H, 0.15)
        self.assertAlmostEqual(gp.WALL_INNER['-w'] + gp.DATUM_PAD_H, -gp.CASE_SIDE_FACE['w'], places=9)
        self.assertAlmostEqual(gp.WALL_INNER['near'] + gp.DATUM_PAD_H, gp.CASE_NEAR_END_FACE['r'], places=9)
        rad = gp.DATUM_PAD_D / 2
        r0, r1 = gp.CASE_SIDE_FACE['r']
        n0, n1 = gp.CASE_SIDE_FACE['n'][0]
        for pad in gp.SIDE_PADS:
            self.assertTrue(r0 <= pad['r'] - rad and pad['r'] + rad <= r1)
            self.assertTrue(n0 <= pad['n'] - rad and pad['n'] + rad <= n1)
        e0, e1 = gp.CASE_NEAR_END_FACE['n']
        self.assertTrue(e0 <= gp.END_PAD['n'] - rad and gp.END_PAD['n'] + rad <= e1)
        self.assertLessEqual(abs(gp.END_PAD['w']) + rad, gp.CASE_NEAR_END_FACE['w_half'])

    def test_flexures_push_the_case_onto_the_pads(self):
        self.assertEqual(gp.FLEXURE_W, 8.0)
        self.assertEqual(gp.FLEXURE_T, 1.5)
        self.assertEqual(gp.FLEXURE_ROOT_N, -27.0)
        self.assertEqual(gp.FLEXURE_FREE_N, -13.0)
        self.assertEqual(gp.FLEXURE_SLOT, 0.8)
        self.assertEqual(gp.BUMP_D, 2.0)
        self.assertEqual(gp.BUMP_N, -14.0)
        self.assertEqual(gp.FLEXURE_PLUS_W_R, (8.5, 16.5))
        self.assertEqual(gp.FLEXURE_FAR_W, (-4.0, 4.0))
        self.assertEqual(gp.BUMP_APEX, {'+w': 12.20, 'far': 34.95})
        # 0.05 inside the CAD case, 0.25 inside a datasheet case
        self.assertAlmostEqual(gp.CASE_W_HALF - gp.BUMP_APEX['+w'], 0.05, places=9)
        self.assertAlmostEqual(gp.CASE_R[1] - gp.BUMP_APEX['far'], 0.05, places=9)
        self.assertAlmostEqual(gp.FLEXURE_PLUS_W_R[1] - gp.FLEXURE_PLUS_W_R[0], gp.FLEXURE_W, places=9)
        self.assertAlmostEqual(gp.FLEXURE_FAR_W[1] - gp.FLEXURE_FAR_W[0], gp.FLEXURE_W, places=9)
        # The bump protrudes less than its radius from the tongue (a half-cylinder segment).
        for side, wall in (('+w', gp.WALL_INNER['+w']), ('far', gp.WALL_INNER['far'])):
            self.assertLess(wall - gp.BUMP_APEX[side], gp.BUMP_D / 2)
        # The bumps press on the measured faces.
        r0, r1 = gp.CASE_SIDE_FACE['r']
        self.assertTrue(r0 <= gp.FLEXURE_PLUS_W_R[0] and gp.FLEXURE_PLUS_W_R[1] <= r1)
        fn0, fn1 = gp.CASE_SIDE_FACE['n'][0]
        self.assertTrue(fn0 <= gp.BUMP_N <= fn1)
        self.assertLessEqual(max(abs(x) for x in gp.FLEXURE_FAR_W), gp.CASE_FAR_END_FACE['w_half'])
        self.assertTrue(gp.FLEXURE_ROOT_N < gp.BUMP_N < gp.FLEXURE_FREE_N)

    def test_dowel_post(self):
        self.assertEqual(gp.DOWEL_POST, {'r': (40.0, 44.0), 'w_half': 4.0, 'top_n': -3.0})
        self.assertEqual(gp.DOWEL_HOLE_D, 2.95)
        self.assertLess(gp.DOWEL_HOLE_D, gp.DOWEL_PIN_D, 'the steel pin is pressed in')
        self.assertEqual(gp.DOWEL_PIN_TOP_N, 11.0)
        self.assertGreater(gp.DOWEL_PIN_TOP_N, gp.CAP_DESIGN_SEAT_N + gp.CAP_THICKNESS)
        lo, hi = gp.DOWEL_POST['r']
        self.assertTrue(lo < gp.DOWEL_AXIS_RW[0] < hi)
        self.assertLessEqual(hi, gp.BASE_PLATE['r'][1])
        self.assertGreater(lo, gp.WALL_INNER['far'] + gp.WALL_THICKNESS, 'the post stands free of the far wall')
        # the press-fit depth and the pin length meet at the pin top
        self.assertAlmostEqual(gp.DOWEL_HOLE_BOTTOM_N + gp.DOWEL_PIN_LEN, gp.DOWEL_PIN_TOP_N, places=9)
        self.assertGreater(gp.DOWEL_HOLE_BOTTOM_N, gp.BASE_PLATE['n'][0])

    def test_p0_arrow(self):
        self.assertEqual(gp.P0_ARROW, {'radius': 12.0, 'theta_deg': (160.0, 200.0), 'head_at_deg': 200.0,
                                       'width': 0.6, 'depth': 0.5, 'top_n': -3.0})
        # counter-clockwise seen from above with the horn up: the head at the larger angle
        self.assertGreater(gp.P0_ARROW['head_at_deg'], gp.P0_ARROW['theta_deg'][0])
        # it lies in the near-end wall top
        for th in gp.P0_ARROW['theta_deg']:
            r = gp.P0_ARROW['radius'] * math.cos(math.radians(th))
            self.assertTrue(gp.WALL_INNER['near'] - gp.WALL_THICKNESS < r < gp.WALL_INNER['near'])
        self.assertEqual(gp.P0_ARROW['top_n'], gp.WALL_TOP_N)


class TestBuildScriptSource(unittest.TestCase):
    def setUp(self):
        with open(_BUILD, encoding='utf-8') as f:
            self.src = f.read()
        self.tree = ast.parse(self.src, filename=_BUILD)

    def test_compiles(self):
        compile(self.src, _BUILD, 'exec')

    def test_ocp_and_numpy_are_imported_only_inside_functions(self):
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            for name in names:
                self.assertFalse(name.split('.')[0] in ('OCP', 'numpy', 'cadquery'),
                                 f'top-level import of {name}')
        inner = [n for n in ast.walk(self.tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                 and n not in self.tree.body]
        self.assertTrue(any(isinstance(n, ast.ImportFrom) and (n.module or '').startswith('OCP') for n in inner))

    def test_cli(self):
        calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'add_argument']
        flags = {c.args[0].value: c for c in calls}
        self.assertEqual(set(flags), {'--out', '--self-check'})
        req = {k.arg: k.value.value for k in flags['--out'].keywords if k.arg == 'required'}
        self.assertEqual(req, {'required': True})

    def test_no_personal_path(self):
        self.assertNotIn('/Users/', self.src)


if __name__ == '__main__':
    unittest.main()
