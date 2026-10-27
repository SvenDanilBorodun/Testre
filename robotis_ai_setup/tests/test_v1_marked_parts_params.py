"""Deps-free tests for ``tools/v1_marked_parts/marks_params.py`` — the parts,
marks, sizes and tolerances of the marked V1 arm parts, and the two pure
decisions the OCP generator ``build_marked_parts.py`` only measures for:

* ``pose_findings`` — a STEP exported with any joint turned would cut every
  downstream arrow at the wrong horn hole while every cut check still passes, so
  the generator refuses a STEP that is not at the URDF zero pose, and NO flag
  overrides that (``TestCliContract`` proves the flag cannot reach the check);
* ``placement_findings`` — a mark that moved after a CAD change refuses unless
  the owner looked and passed ``--accept-placements``.

The fixture ``S03`` is the zero-pose measurement of the owner's current STEP
(probe s03): every check passes on it, and each case below changes one value
the way a turned joint or a moved mark would.
"""

import ast
import copy
import inspect
import math
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_MARKS_DIR = os.path.join(_REPO, 'tools', 'v1_marked_parts')
_DOCKER = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator')
for _p in (_DOCKER, _MARKS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import feetech_servo_spec as fss  # noqa: E402
import marks_params as mp  # noqa: E402

_BUILD = os.path.join(_MARKS_DIR, 'build_marked_parts.py')

S03 = {
    'joint1': {'h_dist_mm': 0.0, 'matched_thetas_deg': [0.0, 90.0, 180.0, 270.0],
               'rel_n': (0.0, 0.0, 1.0), 'rel_u': (1.0, 0.0, 0.0)},
    'joint2': {'h_dist_mm': 0.0, 'matched_thetas_deg': [0.0, 90.0, 180.0],
               'rel_n': (0.0, -1.0, 0.0), 'rel_u': (0.0, 0.0, -1.0)},
    'joint3': {'h_dist_mm': 0.0, 'matched_thetas_deg': [180.0, 270.0, 360.0],
               'rel_n': (0.0, -1.0, 0.0), 'rel_u': (0.0, 0.0, -1.0)},
    'joint4': {'h_dist_mm': 0.0, 'matched_thetas_deg': [90.0, 180.0, 270.0],
               'rel_n': (0.0, -1.0, 0.0), 'rel_u': (1.0, 0.0, 0.0)},
    'joint5': {'h_dist_mm': 0.0, 'matched_thetas_deg': [0.0, 90.0, 180.0, 270.0],
               'rel_n': (-1.0, 0.0, 0.0), 'rel_u': (0.0, 0.0, 1.0)},
    'RL_joint': {'h_dist_mm': 0.0001, 'matched_thetas_deg': [81.97, 171.97],
                 'rel_n': (0.0, 0.0, 1.0), 'rel_u': (-0.990195, 0.139692, 0.0)},
}


def _rot(v, axis, deg):
    a = math.radians(deg)
    n = math.sqrt(sum(x * x for x in axis))
    k = tuple(x / n for x in axis)
    c, s = math.cos(a), math.sin(a)
    cross = (k[1] * v[2] - k[2] * v[1], k[2] * v[0] - k[0] * v[2], k[0] * v[1] - k[1] * v[0])
    dot = sum(x * y for x, y in zip(k, v))
    return tuple(v[i] * c + cross[i] * s + k[i] * dot * (1 - c) for i in range(3))


class TestPartsAndMarks(unittest.TestCase):
    def test_the_ten_parts(self):
        self.assertEqual(mp.PARTS, (
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
        ))

    def test_the_thirteen_marks_in_cut_order(self):
        self.assertEqual(mp.MARKS, (
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
        ))
        products = [p for p, _, _ in mp.PARTS]
        for product, label, kind, joint in mp.MARKS:
            self.assertIn(product, products)
            self.assertTrue(label.startswith(kind + ' '))
            self.assertEqual(label.split(' ')[1], mp.JOINT_SHORT[joint])

    def test_arrows_and_dots_only_on_their_parts(self):
        arrows = {p for p, _, k, _ in mp.MARKS if k == 'arrow'}
        dots = {p for p, _, k, _ in mp.MARKS if k == 'dot'}
        self.assertEqual(arrows, {'base_lid', 'link1_motorpart_new', 'link2part', 'link3_part', 'link5_part',
                                  'last_link', 'Spurgear_18T_'})
        self.assertEqual(dots, {'Base_part', 'link1_motorpart_new', 'link2_motor_moutingpart', 'link4_part',
                                'link5_part', 'last_link'})
        # one dot and one arrow per joint; J1's arrow is on the lid AND on link1 (OQ4)
        for j in range(6):
            kinds = sorted(k for _, _, k, jj in mp.MARKS if jj == j)
            self.assertEqual(kinds, ['arrow', 'arrow', 'dot'] if j == 0 else ['arrow', 'dot'], f'joint {j + 1}')

    def test_joints_children_colours_and_reference_angles_come_from_the_spec(self):
        self.assertEqual(mp.JOINTS, fss.JOINT_NAMES)
        self.assertEqual(mp.JOINT_SHORT, ('J1', 'J2', 'J3', 'J4', 'J5', 'claw'))
        self.assertEqual(mp.CHILD_PART, {'joint1': 'link1_motorpart_new', 'joint2': 'link2part',
                                         'joint3': 'link3_part', 'joint4': 'link5_part', 'joint5': 'last_link',
                                         'RL_joint': 'Spurgear_18T_'})
        self.assertEqual(mp.MARK_COLOUR_HEX, fss.COLOUR_HEX)
        self.assertEqual(mp.THETA_REF_DEG, fss.REFERENCE_THETA_DEG)
        self.assertEqual(mp.THETA_REF_DEG, (0.0, 0.0, 0.0, 0.0, 0.0, 81.97))
        self.assertEqual(mp.SERVO_PRODUCTS, ('feetech_servo_sts3215', 'feetech_servo_sts3250'))
        self.assertEqual(mp.HORN_PRODUCT, 'servo_horn')

    def test_mark_sizes(self):
        self.assertEqual(mp.ARROW_DEPTH, 0.5)
        self.assertEqual(mp.ARROW_SHAFT_W, 1.2)
        self.assertEqual(mp.ARROW_HEAD_FRAC, 0.33)
        self.assertEqual(mp.DOT_D, 4.0)
        self.assertEqual(mp.DOT_DEPTH, 0.4)
        self.assertEqual(mp.ARROW_SIZES, ((6.0, 3.0), (5.0, 2.6), (4.0, 2.2)))
        for (l0, w0), (l1, w1) in zip(mp.ARROW_SIZES, mp.ARROW_SIZES[1:]):
            self.assertGreater(l0, l1, 'the size ladder descends')
            self.assertGreater(w0, w1)
        self.assertEqual(mp.TOOL_LEAD, 0.05)

    def test_the_analytic_tool_volumes(self):
        for value, want in ((mp.arrow_volume(6.0, 3.0), 3.897), (mp.arrow_volume(5.0, 2.6), 3.083),
                            (mp.arrow_volume(4.0, 2.2), 2.334), (mp.dot_volume(), 5.027)):
            self.assertAlmostEqual(value, want, delta=0.0006)

    def test_search_parameters(self):
        self.assertEqual(mp.HOLE_RADIUS_MM, 7.0)
        self.assertEqual(mp.D_MIN, (3.0, 3.0, 3.0, 0.0, 3.0, 3.0))
        self.assertEqual([j for j, d in enumerate(mp.D_MIN) if d == 0.0], [3], 'D_MIN 0.0 only for J4')
        self.assertEqual(mp.D_MAX, 12.0)
        self.assertEqual(mp.D_STEP, 0.2)
        self.assertEqual(mp.D_PREFER_MAX, 6.0)
        self.assertEqual(len(mp.DPHI_LADDER_DEG), 72)
        self.assertEqual(mp.DPHI_LADDER_DEG[:5], (0, 5, -5, 10, -10))
        self.assertEqual(mp.FOOTPRINT_GROW, 0.6)
        self.assertEqual(mp.SAMPLE_STEP, 0.2)
        self.assertEqual(mp.FACE_NORMAL_TOL_DEG, 2.0)
        self.assertEqual(mp.FACE_HEIGHT_TOL, 0.01)
        self.assertEqual(mp.ARROW_MIN_BELOW, 1.3)
        self.assertEqual(mp.DOT_SAMPLE_R, 2.6)
        self.assertEqual(mp.DOT_MIN_BELOW, 1.2)
        self.assertEqual(mp.DOT_TARGET_R, (37.0, 18.3, 17.3, 22.3, 12.0, 12.3))
        self.assertEqual(mp.DOT_GRID, ((-45.0, 60.0), (-45.0, 45.0), 0.5))
        self.assertEqual(mp.DOT_RULE_A_BAND, (-10.0, 15.0))
        self.assertEqual(mp.OCCLUSION_STEP, 0.9)
        self.assertEqual(mp.OCCLUSION_RANGE, 60.0)
        self.assertEqual(len(mp.PSI_LADDER_DEG), 24)
        self.assertEqual(mp.PSI_LADDER_DEG[:3], (0, 15, -15))
        self.assertEqual(mp.RULE_B_HEIGHTS, (1.0, -12.0, 0.5))
        self.assertEqual(mp.RULE_B_LATERAL, (-15.0, 15.0, 0.5))
        self.assertEqual(mp.RULE_B_START, 120.0)
        self.assertEqual(mp.RULE_B_NORMAL_TOL_DEG, 3.0)

    def test_verification_limits(self):
        self.assertEqual(mp.CLEARANCE_MIN, 0.6)
        self.assertEqual(mp.WALL_MIN, 0.8)
        self.assertEqual(mp.VOLUME_TOL_FRAC, 0.02)
        self.assertEqual(mp.BBOX_TOL, 1e-6)
        self.assertEqual(mp.PART_VOLUME_TOL_FRAC, 0.001)
        self.assertEqual(mp.MESH_DEFLECTION, 0.005)
        self.assertEqual(mp.MESH_ANGULAR, 0.2)
        self.assertEqual(mp.MESH_WELD, 1e-5)
        self.assertEqual(mp.MESH_RETRIES, 2)
        self.assertEqual(mp.HORN_TOP_N, (4.70, 4.70, 4.70, 4.70, 4.70, 5.70))
        self.assertEqual(mp.HORN_TOP_TOL, 0.05)

    def test_previews(self):
        self.assertEqual(mp.PANEL_PX, 400)
        self.assertEqual(mp.PANEL_FIELD_MM, (30.0, 30.0, 30.0, 40.0, 30.0, 30.0))
        self.assertEqual(mp.ISO_PX, (800, 600))
        self.assertEqual(mp.ISO_FIELD_FACTOR, 1.1)
        self.assertEqual(mp.PART_GREY, 0.45)
        self.assertEqual(mp.SHADE, (0.35, 0.65))


class TestZeroPoseTables(unittest.TestCase):
    def test_matched_hole_thetas(self):
        self.assertEqual(mp.MATCHED_HOLE_THETAS, {
            'joint1': (0.0, 90.0, 180.0, 270.0), 'joint2': (0.0, 90.0, 180.0), 'joint3': (0.0, 180.0, 270.0),
            'joint4': (90.0, 180.0, 270.0), 'joint5': (0.0, 90.0, 180.0, 270.0), 'RL_joint': (81.97, 171.97)})
        self.assertNotIn(0.0, mp.MATCHED_HOLE_THETAS['joint4'], 'J4: the marked hole stays empty (OQ3)')
        for i, j in enumerate(mp.JOINTS):
            for th in mp.MATCHED_HOLE_THETAS[j]:
                k = (th - fss.HOLE_THETA_OFFSET_DEG[i]) / 90.0
                self.assertAlmostEqual(k, round(k), places=9, msg=f'{j} {th} is no horn hole')

    def test_rel_frames(self):
        self.assertEqual(mp.REL_FRAMES, {
            'joint2': ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0)), 'joint3': ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
            'joint4': ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0)), 'joint5': ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            'RL_joint': ((0.0, 0.0, 1.0), (-0.990195, 0.139692, 0.0))})
        for j, (n, u) in mp.REL_FRAMES.items():
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in n)), 1.0, delta=1e-6, msg=j)
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in u)), 1.0, delta=1e-6, msg=j)
            self.assertAlmostEqual(sum(a * b for a, b in zip(n, u)), 0.0, delta=1e-6, msg=f'{j}: n not ⊥ u')
        self.assertEqual(mp.J1_REL_FRAME, ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)))

    def test_pose_tolerances(self):
        self.assertEqual(mp.POSE_H_TOL_MM, 0.05)
        self.assertEqual(mp.POSE_THETA_TOL_DEG, 0.5)
        self.assertEqual(mp.POSE_FRAME_TOL_DEG, 1.0)

    def test_frame_finding_tolerances(self):
        self.assertEqual(mp.HORN_HOLE_RADIUS, (1.25, 0.01))
        self.assertEqual(mp.CHILD_HOLE_RADIUS, (1.2, 1.8))
        self.assertEqual(mp.COAXIAL_MM, 0.05)
        self.assertEqual(mp.COAXIAL_DEG, 0.5)
        self.assertEqual(mp.HORN_CHILD_AXIAL_MM, 15.0)
        self.assertEqual(mp.SERVO_AXIAL_MM, 10.0)
        self.assertEqual(mp.OUTPUT_FACE_TOL_DEG, 1.0)

    def test_placement_fence_tolerances(self):
        self.assertEqual(mp.FENCE_ARROW_DPHI_DEG, 5.0)
        self.assertEqual(mp.FENCE_ARROW_D_MM, 0.4)
        self.assertEqual(mp.FENCE_DOT_CENTRE_MM, 1.0)
        self.assertEqual(mp.FENCE_DOT_B_MM, 1.0)


class TestExpected(unittest.TestCase):
    def test_expected_covers_every_mark_exactly(self):
        self.assertEqual(set(mp.EXPECTED), {(p, label) for p, label, _, _ in mp.MARKS})

    def test_expected_is_the_measured_outcome_table(self):
        e = mp.EXPECTED
        self.assertEqual(e[('base_lid', 'arrow J1')], {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 0.0, 'd_mm': 9.8})
        self.assertEqual(e[('link1_motorpart_new', 'arrow J1')],
                         {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 70.0, 'd_mm': 3.4})
        self.assertEqual(e[('link2part', 'arrow J2')], {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 65.0, 'd_mm': 3.0})
        self.assertEqual(e[('link3_part', 'arrow J3')], {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': -65.0, 'd_mm': 3.0})
        self.assertEqual(e[('link5_part', 'arrow J4')], {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 155.0, 'd_mm': 9.6})
        self.assertEqual(e[('last_link', 'arrow J5')], {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': 55.0, 'd_mm': 3.8})
        self.assertEqual(e[('Spurgear_18T_', 'arrow claw')],
                         {'kind': 'arrow', 'size': (6.0, 3.0), 'dphi_deg': -5.0, 'd_mm': 3.0})
        self.assertEqual(e[('Base_part', 'dot J1')],
                         {'kind': 'dot', 'rule': 'B', 'psi_deg': 0.0, 'height_mm': -3.5, 'lateral_mm': -10.5})
        self.assertEqual(e[('link1_motorpart_new', 'dot J2')], {'kind': 'dot', 'rule': 'A', 'centre_mm': (21.0, 0.0)})
        self.assertEqual(e[('link2_motor_moutingpart', 'dot J3')], {'kind': 'dot', 'rule': 'A', 'centre_mm': (20.0, 0.0)})
        self.assertEqual(e[('link4_part', 'dot J4')], {'kind': 'dot', 'rule': 'A', 'centre_mm': (25.0, 0.0)})
        self.assertEqual(e[('link5_part', 'dot J5')], {'kind': 'dot', 'rule': 'C', 'centre_mm': (9.5, 14.0)})
        self.assertEqual(e[('last_link', 'dot claw')], {'kind': 'dot', 'rule': 'A', 'centre_mm': (8.0, -10.5)})

    def test_every_expected_arrow_is_a_ladder_size_and_value(self):
        for (product, label), e in mp.EXPECTED.items():
            if e['kind'] != 'arrow':
                continue
            self.assertIn(e['size'], mp.ARROW_SIZES)
            self.assertIn(e['dphi_deg'], mp.DPHI_LADDER_DEG)
            joint = [j for p, lb, _, j in mp.MARKS if (p, lb) == (product, label)][0]
            self.assertGreaterEqual(e['d_mm'], mp.D_MIN[joint])
            self.assertLessEqual(e['d_mm'], mp.D_MAX)

    def test_format_expected_round_trips(self):
        text = mp.format_expected(mp.EXPECTED)
        self.assertTrue(text.startswith('EXPECTED = {'))
        ns = {}
        exec(text, ns)  # noqa: S102 - the generator prints this block for pasting into marks_params.py
        self.assertEqual(ns['EXPECTED'], mp.EXPECTED)


class TestPoseFindings(unittest.TestCase):
    def _one_naming(self, findings, joint):
        self.assertEqual(len(findings), 1, findings)
        self.assertIn(joint, findings[0])
        self.assertTrue(findings[0].startswith('STEP nicht in der Nullstellung: '), findings[0])
        self.assertTrue(findings[0].endswith('bitte das Modell in der URDF-Nullstellung exportieren.'), findings[0])

    def test_s03_is_at_the_zero_pose(self):
        self.assertEqual(mp.pose_findings(S03), [])

    def test_joint5_given_with_the_360_wrap(self):
        m = copy.deepcopy(S03)
        m['joint5']['matched_thetas_deg'] = [90.0, 180.0, 270.0, 360.0]
        self.assertEqual(mp.pose_findings(m), [])

    def test_joint3_turned_by_5_degrees(self):
        m = copy.deepcopy(S03)
        m['joint3']['matched_thetas_deg'] = [t + 5 for t in S03['joint3']['matched_thetas_deg']]
        self._one_naming(mp.pose_findings(m), 'joint3')

    def test_joint2_turned_by_90_degrees(self):
        m = copy.deepcopy(S03)
        m['joint2']['matched_thetas_deg'] = [t + 90 for t in S03['joint2']['matched_thetas_deg']]
        self._one_naming(mp.pose_findings(m), 'joint2')

    def test_no_hole_at_h(self):
        m = copy.deepcopy(S03)
        m['joint4']['h_dist_mm'] = 0.06
        self._one_naming(mp.pose_findings(m), 'joint4')
        m['joint4']['h_dist_mm'] = 0.04
        self.assertEqual(mp.pose_findings(m), [])

    def test_a_frame_off_relative_to_j1(self):
        m = copy.deepcopy(S03)
        m['RL_joint']['rel_n'] = _rot(S03['RL_joint']['rel_n'], S03['RL_joint']['rel_u'], 2.0)
        self._one_naming(mp.pose_findings(m), 'RL_joint')
        m['RL_joint']['rel_n'] = _rot(S03['RL_joint']['rel_n'], S03['RL_joint']['rel_u'], 0.5)
        self.assertEqual(mp.pose_findings(m), [])

    def test_unjudgeable_refuses(self):
        m = copy.deepcopy(S03)
        del m['joint2']
        self._one_naming(mp.pose_findings(m), 'joint2')
        m = copy.deepcopy(S03)
        m['joint1']['rel_u'] = (float('nan'), 0.0, 0.0)
        self._one_naming(mp.pose_findings(m), 'joint1')
        m = copy.deepcopy(S03)
        m['joint4']['h_dist_mm'] = float('inf')
        self._one_naming(mp.pose_findings(m), 'joint4')
        m = copy.deepcopy(S03)
        m['joint5']['matched_thetas_deg'] = [0.0, float('nan'), 180.0, 270.0]
        self._one_naming(mp.pose_findings(m), 'joint5')
        m = copy.deepcopy(S03)
        m['joint3']['matched_thetas_deg'] = None
        self._one_naming(mp.pose_findings(m), 'joint3')
        m = copy.deepcopy(S03)
        m['joint2']['rel_n'] = (0.0, -1.0)
        self._one_naming(mp.pose_findings(m), 'joint2')

    def test_a_whole_joint_turned_shows_in_the_downstream_frames(self):
        # joint 2 turned by 90° about its own axis moves joint 3's frame by 90° (s03): (3) fails downstream.
        m = copy.deepcopy(S03)
        axis = S03['joint2']['rel_n']
        for j in ('joint3', 'joint4', 'joint5', 'RL_joint'):
            m[j]['rel_n'] = _rot(S03[j]['rel_n'], axis, 90.0)
            m[j]['rel_u'] = _rot(S03[j]['rel_u'], axis, 90.0)
        found = mp.pose_findings(m)
        self.assertEqual({f.split(':')[1].split()[0] for f in found}, {'joint3', 'joint4', 'joint5', 'RL_joint'})

    def test_the_decision_takes_exactly_one_parameter(self):
        self.assertEqual(len(inspect.signature(mp.pose_findings).parameters), 1)


class TestPlacementFindings(unittest.TestCase):
    def setUp(self):
        self.base = copy.deepcopy(mp.EXPECTED)

    def _n(self, outcomes):
        found = mp.placement_findings(outcomes, mp.EXPECTED)
        for line in found:
            self.assertTrue(line.startswith('Markierung '), line)
            self.assertIn(' weicht von EXPECTED ab: ', line)
        return len(found)

    def test_expected_fed_back(self):
        self.assertEqual(mp.placement_findings(self.base, mp.EXPECTED), [])

    def test_arrow_angle(self):
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['dphi_deg'] += 6
        self.assertEqual(self._n(o), 1)
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['dphi_deg'] += 4
        self.assertEqual(self._n(o), 0)

    def test_arrow_angle_is_circular(self):
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['dphi_deg'] += 360.0
        self.assertEqual(self._n(o), 0)

    def test_arrow_distance(self):
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['d_mm'] += 0.5
        self.assertEqual(self._n(o), 1)
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['d_mm'] += 0.3
        self.assertEqual(self._n(o), 0)

    def test_arrow_size(self):
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['size'] = (5.0, 2.6)
        self.assertEqual(self._n(o), 1)

    def test_dot_rule(self):
        o = copy.deepcopy(self.base)
        o[('link4_part', 'dot J4')]['rule'] = 'C'
        self.assertEqual(self._n(o), 1)

    def test_dot_centre(self):
        o = copy.deepcopy(self.base)
        c = o[('link4_part', 'dot J4')]['centre_mm']
        o[('link4_part', 'dot J4')]['centre_mm'] = (c[0] + 1.2, c[1])
        self.assertEqual(self._n(o), 1)
        o[('link4_part', 'dot J4')]['centre_mm'] = (c[0] + 0.8, c[1])
        self.assertEqual(self._n(o), 0)

    def test_rule_b_dot(self):
        o = copy.deepcopy(self.base)
        o[('Base_part', 'dot J1')]['height_mm'] += 1.2
        self.assertEqual(self._n(o), 1)
        o = copy.deepcopy(self.base)
        o[('Base_part', 'dot J1')]['psi_deg'] = 15.0
        self.assertEqual(self._n(o), 1)
        o = copy.deepcopy(self.base)
        o[('Base_part', 'dot J1')]['lateral_mm'] += 0.8
        self.assertEqual(self._n(o), 0)

    def test_a_missing_mark(self):
        o = copy.deepcopy(self.base)
        del o[('last_link', 'dot claw')]
        self.assertEqual(self._n(o), 1)

    def test_an_unknown_mark(self):
        o = copy.deepcopy(self.base)
        o[('last_link', 'dot X')] = {'kind': 'dot', 'rule': 'A', 'centre_mm': (0.0, 0.0)}
        self.assertEqual(self._n(o), 1)

    def test_extra_outcome_keys_are_ignored(self):
        o = copy.deepcopy(self.base)
        o[('link2part', 'arrow J2')]['clearance_mm'] = 1.15
        self.assertEqual(self._n(o), 0)


class TestCliContract(unittest.TestCase):
    def setUp(self):
        with open(_BUILD, encoding='utf-8') as f:
            self.src = f.read()
        self.tree = ast.parse(self.src, filename=_BUILD)

    def test_compiles_and_names_no_personal_path(self):
        compile(self.src, _BUILD, 'exec')
        self.assertNotIn('/Users/', self.src)

    def test_ocp_and_numpy_are_imported_only_inside_functions(self):
        for node in self.tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            for name in names:
                self.assertNotIn(name.split('.')[0], ('OCP', 'numpy', 'cadquery'), f'top-level import of {name}')

    def test_the_four_flags_and_no_other(self):
        calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'add_argument']
        flags = {c.args[0].value: {k.arg: getattr(k.value, 'value', None) for k in c.keywords} for c in calls}
        self.assertEqual(set(flags), {'--step', '--out', '--self-check', '--accept-placements'})
        self.assertIs(flags['--step'].get('required'), True)
        self.assertEqual(flags['--self-check'].get('action'), 'store_true')
        self.assertEqual(flags['--accept-placements'].get('action'), 'store_true')

    def _accept_reads(self):
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.Attribute) and n.attr == 'accept_placements']

    def test_the_flag_is_read_once_inside_the_test_of_the_placement_if(self):
        reads = self._accept_reads()
        self.assertEqual(len(reads), 1, 'accept_placements must be read exactly once')
        assigned = set()
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, 'attr', getattr(node.value.func, 'id', None)) == 'placement_findings'):
                assigned |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        self.assertTrue(assigned, 'no name is assigned from placement_findings(…)')
        tests = [n.test for n in ast.walk(self.tree) if isinstance(n, ast.If)]
        holder = [t for t in tests if any(x is reads[0] for x in ast.walk(t))]
        self.assertEqual(len(holder), 1, 'the flag is not read inside an if test')
        names = {x.id for x in ast.walk(holder[0]) if isinstance(x, ast.Name)}
        self.assertTrue(names & assigned, 'the if that reads the flag does not read the placement findings')

    def test_the_pose_check_runs_before_the_flag_is_read_and_takes_one_argument(self):
        pose_calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                      and getattr(n.func, 'attr', getattr(n.func, 'id', None)) == 'pose_findings']
        self.assertEqual(len(pose_calls), 1)
        self.assertEqual(len(pose_calls[0].args), 1)
        self.assertEqual(pose_calls[0].keywords, [])
        self.assertLess(pose_calls[0].lineno, self._accept_reads()[0].lineno)
        # no argument of the pose check can carry the flag
        self.assertFalse(any(isinstance(x, ast.Attribute) and x.attr == 'accept_placements'
                             for x in ast.walk(pose_calls[0])))


if __name__ == '__main__':
    unittest.main()
