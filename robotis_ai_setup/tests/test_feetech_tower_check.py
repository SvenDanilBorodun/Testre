# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-T1: the V1 driver's tower check, refuse-only (spec 40 WP-T1, revision 6;
owner decisions D53, D65, D77).

The activation glide — ``build_boot_home``: the straight joint line from the
measured pose to HOME, the claw blended to 0.90 — is judged before it is
driven, against the camera tower's ``TOWER_T1_PARTS`` with the driver's
allowance (``feetech_geometry.tower_glide_decision``). Held here:

* the acceptance set: over the 807 physically possible limp starts of
  ``fixtures/v1_limp_starts_b.json`` the check refuses exactly the fixture's
  605 (every one fallen back) and none of the 93 placed starts; its exact
  margins are the server guard's (``workflow/fixture_guard.py``), the
  smallest placed one 30.1 mm;
* the dense count (no box corner moves more than 3 mm between two samples);
* where a limp arm falls (``limp_fall_direction``, from the URDF's masses);
* what cannot be judged is refused;
* the node: ``run_home`` refuses a fallen-back start and writes nothing, glides
  from HOME's neighbourhood as before, and ``_boot_home_verify`` judges its
  re-send; under ``legacy`` nothing changes.

``feetech_geometry`` is pure Python and imported directly; the node is the REAL
``feetech_arm_node`` under fake ROS modules (``test_tower_state_topic``).
"""

import importlib.util
import json
import math
import os
import random
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from timeout_guard import BoundedTestCase
from test_tower_state_topic import DRIVER, FALLEN_BACK, HOME, make_node, scene_profile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_GEO = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator', 'feetech_geometry.py')
_URDF = os.path.join(_REPO, 'physical_ai_tools', 'physical_ai_manager', 'public', 'v1-urdf', 'v1.urdf')
_FIXTURE = os.path.join(_HERE, 'fixtures', 'v1_limp_starts_b.json')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fg = _load('feetech_geometry_tower_check', _GEO)
with open(_FIXTURE, encoding='utf-8') as _handle:
    FIXTURE = json.load(_handle)
GLIDES = FIXTURE['glides']
TARGET = list(FIXTURE['home']) + [FIXTURE['claw_end']]
RANGE = [tuple(r) for r in FIXTURE['range']]
T1_PARTS = ('block', 'head', 'lens', 'bridge', 'column', 'socket')
PLACED = [g for g in GLIDES if g['start'][1] > 0.5]
REFUSED = [g for g in GLIDES if g['refused']]
# Owner decision D88, verbatim: lay the arm forward, never stand it up.
TOWER_GLIDE_REFUSAL_D88_DE = (
    '[FEHLER] Grundstellungs-Fahrt entfällt: aus dieser Lage würde der Arm dabei dem Kameraturm zu nahe kommen. '
    'Der Arm wird in seiner Stellung gehalten (er ist NICHT weich). Bitte den Arm auf der Startseite lockern, '
    'nach vorne legen und festsetzen – die Animation dort zeigt, wie.'
)
STAND_UP_RE = r'(?i)aufricht|aufstell|stell\w* (ihn|den arm) auf|richte\w* (ihn|den arm) auf'


def _api(name):
    """A public name of feetech_geometry, or an assertion that says it is missing."""
    value = getattr(fg, name, None)
    if value is None:
        raise AssertionError(f'feetech_geometry.py defines no {name}')
    return value


def _decide(glide, exact=False):
    start = list(glide['start']) + [FIXTURE['claw_start']]
    return _api('tower_glide_decision')(start, TARGET, 'v1_studio', RANGE, exact=exact)


class TheFixtureIsTheAcceptanceSet(unittest.TestCase):
    """the data itself (spec 40, the revision-4 amendment of WP-T1)."""

    def test_605_of_807_refused_every_one_fallen_back(self):
        self.assertEqual(len(GLIDES), 807)
        self.assertEqual(len(REFUSED), 605)
        self.assertTrue(all(g['start'][1] <= 0.30 for g in REFUSED))
        counts = {}
        for g in REFUSED:
            counts[g['part']] = counts.get(g['part'], 0) + 1
        self.assertEqual(counts, {'column': 80, 'block': 115, 'head': 398, 'lens': 12})

    def test_none_of_the_93_placed_starts_the_smallest_margin_30_1_mm(self):
        self.assertEqual(len(PLACED), 93)
        self.assertFalse(any(g['refused'] for g in PLACED))
        self.assertEqual(round(1000 * min(g['margin_m'] for g in PLACED), 1), 30.1)

    def test_a_verdict_is_the_sign_of_its_margin(self):
        for g in GLIDES:
            self.assertEqual(g['refused'], g['margin_m'] < 0.0)


class TheGuardRefusesExactlyTheAcceptanceSet(unittest.TestCase):

    def test_every_start(self):
        refused = []
        for n, glide in enumerate(GLIDES):
            verdict, margin, part = _decide(glide)
            self.assertIn(verdict, ('clear', 'refused'), n)
            if verdict == 'refused':
                refused.append(n)
                self.assertLess(margin, 0.0)
                self.assertIn(part, T1_PARTS)
            else:
                self.assertEqual((margin, part), (None, None))
        self.assertEqual(refused, [n for n, g in enumerate(GLIDES) if g['refused']])


class TheExactMarginsAreTheServersRule(unittest.TestCase):
    """``exact=True`` finds a glide's smallest margin; it equals the server guard's (the fixture's ``margin_m``)."""

    def _check(self, glides):
        margins = []
        for glide in glides:
            verdict, margin, part = _decide(glide, exact=True)
            self.assertAlmostEqual(margin, glide['margin_m'], delta=1e-9)
            self.assertEqual(part, glide['part'])
            self.assertEqual(verdict, 'refused' if glide['refused'] else 'clear')
            margins.append(margin)
        return margins

    def test_every_placed_start_and_the_smallest_is_30_1_mm(self):
        self.assertEqual(round(1000 * min(self._check(PLACED)), 1), 30.1)

    def test_a_stride_of_the_refused_starts(self):
        self.assertTrue(all(m < 0.0 for m in self._check(REFUSED[::20])))


def _turn(axis, angle):
    norm = math.sqrt(sum(a * a for a in axis))
    x, y, z = (a / norm for a in axis)
    c, s, t = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
    return ((t * x * x + c, t * x * y - s * z, t * x * z + s * y),
            (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
            (t * x * z - s * y, t * y * z + s * x, t * z * z + c))


def _apply(rot, v, pos=(0.0, 0.0, 0.0)):
    return tuple(sum(rot[i][k] * v[k] for k in range(3)) + pos[i] for i in range(3))


def _corners(lo, hi):
    return [(hi[0] if i & 4 else lo[0], hi[1] if i & 2 else lo[1], hi[2] if i & 1 else lo[2]) for i in range(8)]


def _local_boxes(claw):
    """The driver's level-1 boxes in their links' frames at claw value ``claw``: links 1–4, the housing and both
    blades (placed here independently, from the public tables)."""
    boxes = [(k, _corners(*fg.V1_LINK_BOXES[k])) for k in range(1, 5)]
    cells = _api('V1_ARM_CELLS')
    boxes.append((5, _corners(*cells['housing']['box'])))
    for blade in cells['blades']:
        origin = blade['origin']
        rot = _mul(tuple(tuple(row[:3]) for row in origin), _turn(blade['axis'], blade['multiplier'] * claw))
        grow = blade['r_max_m'] * blade['gear_slack'] * abs(claw)
        lo = [v - grow for v in blade['box'][0]]
        hi = [v + grow for v in blade['box'][1]]
        boxes.append((5, [_apply(rot, c, [row[3] for row in origin]) for c in _corners(lo, hi)]))
    return boxes


def _in_world(q, boxes):
    frames = fg.link_frames(q)
    out = []
    for link, corners in boxes:
        rot, pos = frames[link]
        out.extend(_apply(rot, c, pos) for c in corners)
    return out


class TheDenseCount(unittest.TestCase):

    def _glides(self):
        rng = random.Random(41)
        pairs = [(g['start'], FIXTURE['home']) for g in GLIDES[::32]]
        for _ in range(12):
            a = [rng.uniform(lo, hi) for lo, hi in RANGE[:5]]
            b = [rng.uniform(lo, hi) for lo, hi in RANGE[:5]]
            pairs.append((a, b))
        return pairs

    def test_no_box_corner_moves_more_than_3_mm_between_two_samples(self):
        count = _api('tower_dense_count')
        worst = 0.0
        for claw in (0.0, 0.9, 1.5708):
            boxes = _local_boxes(claw)
            for a, b in self._glides():
                n = count(a, b)
                prev = None
                for i in range(n + 1):
                    now = _in_world([x + (y - x) * (i / n) for x, y in zip(a, b)], boxes)
                    if prev is not None:
                        worst = max(worst, max(math.dist(p, c) for p, c in zip(prev, now)))
                    prev = now
        self.assertLessEqual(worst, 0.003 + 1e-12)
        self.assertGreater(worst, 0.002)           # the samples are not needlessly dense either

    def test_the_count_is_the_joint_travel_over_3_mm(self):
        count = _api('tower_dense_count')
        rj = _api('TOWER_R_J_M')
        for a, b in self._glides():
            travel = sum(r * abs(y - x) for r, x, y in zip(rj, a, b))
            self.assertEqual(count(a, b), max(1, math.ceil(travel / 0.003)))
        self.assertEqual(count(HOME, HOME), 1)


class WhatCannotBeJudgedIsRefused(unittest.TestCase):

    def _decide(self, start, profile='v1_studio', window=RANGE, target=TARGET, exact=False):
        return _api('tower_glide_decision')(start, target, profile, window, exact=exact)

    def test_a_non_finite_or_short_reading(self):
        home = list(HOME) + [0.9]
        for bad in ([math.nan] + home[1:], home[:2] + [math.inf] + home[3:], home[:5] + [math.nan], home[:5],
                    home + [0.0], None, ['a'] * 6):
            for exact in (False, True):
                with self.subTest(start=bad, exact=exact):
                    self.assertEqual(self._decide(bad, exact=exact), ('unjudgeable', None, None))

    def test_a_target_outside_its_window(self):
        self.assertEqual(self._decide(list(HOME) + [0.9], target=list(HOME) + [1.7]), ('unjudgeable', None, None))

    def test_a_value_outside_its_eeprom_window(self):
        """joint 2 at 3.1416 is beyond tick 4095 (3.14006 rad): out of the driver's EEPROM window, while the
        fixture's own range admits it."""
        start = [0.3, 3.1416, 3.0, 0.4, 0.0, 0.0]
        self.assertEqual(self._decide(start, window=DRIVER.TOWER_WINDOW_RAD), ('unjudgeable', None, None))
        self.assertNotEqual(self._decide(start)[0], 'unjudgeable')
        self.assertEqual(self._decide(list(HOME) + [-0.01], window=DRIVER.TOWER_WINDOW_RAD),
                         ('unjudgeable', None, None))

    def test_an_unknown_robot_profile(self):
        start = list(HOME) + [0.9]
        self.assertEqual(self._decide(start)[0], 'clear')
        for profile in ('omx_full', 'omx_follower', 'edu6_studio', 'V1_STUDIO', '', None, 5):
            with self.subTest(profile=profile):
                self.assertEqual(self._decide(start, profile=profile), ('unjudgeable', None, None))


class TheClaw(unittest.TestCase):

    def test_glides_whose_claw_does_not_start_closed(self):
        decide = _api('tower_glide_decision')
        for claw in (0.35, 0.9, 1.5708):
            with self.subTest(claw=claw):
                self.assertEqual(decide(list(HOME) + [claw], TARGET, 'v1_studio', RANGE)[0], 'clear')
                self.assertEqual(decide(list(FALLEN_BACK[:5]) + [claw], TARGET, 'v1_studio', RANGE)[0], 'refused')


def _urdf_link_masses():
    """(frame 1..5, kg, CoM in that frame) from the shipped URDF: links 1–4 as they are, frame 5 = link5 +
    end_effector + both blades at the open claw (0.90), lumped."""
    root = ET.parse(_URDF).getroot()
    inertial = {}
    for link in root.findall('link'):
        node = link.find('inertial')
        if node is not None:
            inertial[link.get('name')] = (float(node.find('mass').get('value')),
                                          tuple(float(v) for v in node.find('origin').get('xyz').split()))
    joints = {j.get('name'): j for j in root.findall('joint')}
    # end_effector is fixed to link5 at no offset, so its CoM is already in frame 5
    end = joints['end_joint'].find('origin')
    assert [float(v) for v in end.get('xyz').split() + end.get('rpy').split()] == [0.0] * 6
    table = [(k, *inertial[f'link{k}']) for k in (1, 2, 3, 4)]
    mass, moment = 0.0, [0.0, 0.0, 0.0]
    for name in ('link5', 'end_effector'):
        m, c = inertial[name]
        mass += m
        moment = [moment[i] + m * c[i] for i in range(3)]
    for name, joint in (('right_finger', 'RL_joint'), ('left_finger', 'LF_joint')):
        j = joints[joint]
        mimic = j.find('mimic')
        angle = 0.90 * (float(mimic.get('multiplier')) if mimic is not None else 1.0)
        origin = j.find('origin')
        r, p, y = (float(v) for v in origin.get('rpy').split())
        rpy = _mul(_turn((0, 0, 1), y), _mul(_turn((0, 1, 0), p), _turn((1, 0, 0), r)))
        rot = _mul(rpy, _turn([float(v) for v in j.find('axis').get('xyz').split()], angle))
        m, c = inertial[name]
        com = _apply(rot, c, [float(v) for v in origin.get('xyz').split()])
        mass += m
        moment = [moment[i] + m * com[i] for i in range(3)]
    table.append((5, mass, tuple(v / mass for v in moment)))
    return table


def _mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


class WhereALimpArmFalls(unittest.TestCase):
    """``limp_fall_direction``: τ_2 from the URDF's masses at the live pose; ``forward`` from +0.15 N·m, ``back``
    from −0.15 N·m, else ``unknown`` (52 §6)."""

    POSES = {
        "D49's HOME": ((0.0, 0.64, 0.64, 0.0, 0.0), 0.934, 'forward'),
        'the observation pose': ((0.0, 0.10, 2.20, -1.61, 0.0), -2.350, 'back'),
        "today's former HOME": ((0.0, 0.64, 1.48, 0.90, 0.0), -0.182, 'back'),
        'standing': ((0.0, 1.5708, 0.0, 0.0, 0.0), 0.356, 'forward'),
        'lying forward': ((0.0, 2.6, 0.3, 0.0, 0.0), 0.287, 'forward'),
    }

    def test_the_known_poses(self):
        torques = _api('limp_gravity_torques')
        fall = _api('limp_fall_direction')
        for name, (q, tau2, direction) in self.POSES.items():
            with self.subTest(pose=name):
                self.assertAlmostEqual(torques(q)[0], tau2, delta=0.01)
                self.assertEqual(fall(q), direction)

    def test_the_threshold_is_0_15_n_m_either_way(self):
        torques = _api('limp_gravity_torques')
        fall = _api('limp_fall_direction')
        self.assertEqual(_api('LIMP_FALL_THRESHOLD_NM'), 0.15)
        # joint 2 on its back stop, joint 3 swept: τ_2 crosses zero between 0.35 and 0.70 rad (52 §6.1)
        for q3 in [i / 200 for i in range(70, 141)]:
            q = (0.0, 0.0, q3, 0.0, 0.0)
            tau2 = torques(q)[0]
            expected = 'forward' if tau2 >= 0.15 else 'back' if tau2 <= -0.15 else 'unknown'
            self.assertEqual(fall(q), expected, q3)
        self.assertIn('unknown', [fall((0.0, 0.0, i / 200, 0.0, 0.0)) for i in range(70, 141)])

    def test_an_unreadable_pose_is_unknown(self):
        fall = _api('limp_fall_direction')
        for bad in ((math.nan, 0.64, 0.64, 0.0, 0.0), (0.0, 0.64), None):
            self.assertEqual(fall(bad), 'unknown')

    def test_the_masses_are_the_urdfs(self):
        table = _api('V1_LINK_MASSES')
        expected = _urdf_link_masses()
        self.assertEqual([row[0] for row in table], [1, 2, 3, 4, 5])
        for (f, m, c), (ef, em, ec) in zip(table, expected):
            self.assertEqual(f, ef)
            self.assertAlmostEqual(m, em, delta=1e-12)
            for a, b in zip(c, ec):
                self.assertAlmostEqual(a, b, delta=1e-12)


class _Clock:
    """``time`` for the verifier: ``sleep`` advances ``monotonic`` at once."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RunHomeJudgesTheGlide(BoundedTestCase):

    def _node(self, profile, pose):
        node = make_node(profile)
        node._torque_on = True
        node._read_positions_once = lambda: list(pose)
        self.addCleanup(node._stop.set)       # ends a verifier thread the glide started
        return node

    def _run(self, profile, pose):
        node = self._node(profile, pose)
        with scene_profile(profile):
            result = node.run_home()
        return node, result

    def test_a_fallen_back_start_is_refused_and_nothing_is_written(self):
        node, (ok, message) = self._run('tower_v1', FALLEN_BACK)
        self.assertFalse(ok, 'run_home glided a fallen-back V1 past the tower')
        self.assertEqual(node._traj_gen, 0)
        self.assertEqual(node._traj_points, [])
        self.assertEqual(message, DRIVER.TOWER_GLIDE_REFUSAL_DE)
        self.assertTrue(node._torque_on)           # held where it lies, never limp
        self.assertIn(DRIVER.TOWER_GLIDE_REFUSAL_DE, node.get_logger().lines('error'))

    def test_home_adjacent_starts_glide_as_today(self):
        rng = random.Random(7)
        for _ in range(12):
            pose = [h + rng.uniform(-0.2, 0.2) for h in HOME] + [rng.uniform(0.0, 1.2)]
            pose[1] = max(pose[1], 0.0)
            with self.subTest(pose=pose):
                legacy, legacy_result = self._run('legacy', pose)
                tower, tower_result = self._run('tower_v1', pose)
                self.assertEqual(tower_result, (True, ''))
                self.assertEqual(tower_result, legacy_result)
                self.assertEqual(tower._traj_gen, 1)
                self.assertEqual(tower._traj_points, legacy._traj_points)
                self.assertEqual(tower._traj_points, DRIVER.build_boot_home(pose))

    def test_under_legacy_or_without_the_key_nothing_is_judged(self):
        for profile in ('legacy', None):
            with self.subTest(profile=profile):
                node, result = self._run(profile, FALLEN_BACK)
                self.assertEqual(result, (True, ''))
                self.assertEqual(node._traj_gen, 1)

    def test_an_unjudgeable_reading_is_refused(self):
        for claw in (math.nan, -0.05):
            with self.subTest(claw=claw):
                node, (ok, message) = self._run('tower_v1', list(HOME) + [claw])
                self.assertFalse(ok, 'run_home glided from a reading it cannot judge')
                self.assertEqual(node._traj_gen, 0)
                self.assertEqual(message, DRIVER.TOWER_GLIDE_UNJUDGEABLE_DE)

    def test_an_unknown_profile_under_the_tower_is_unjudgeable(self):
        with scene_profile('tower_v1'):
            for profile in ('omx_full', 'edu6_studio'):
                ok, message, _judged = DRIVER.boot_home_tower_decision(list(HOME) + [0.9], profile=profile)
                self.assertEqual((ok, message), (False, DRIVER.TOWER_GLIDE_UNJUDGEABLE_DE))
            self.assertTrue(DRIVER.boot_home_tower_decision(list(HOME) + [0.9])[0])

    def test_the_sentences(self):
        for text in (DRIVER.TOWER_GLIDE_REFUSAL_DE, DRIVER.TOWER_GLIDE_UNJUDGEABLE_DE):
            self.assertTrue(text.startswith('[FEHLER] Grundstellungs-Fahrt entfällt: '))
            self.assertIn('Kameraturm', text)
        self.assertIn('NICHT weich', DRIVER.TOWER_GLIDE_REFUSAL_DE)
        self.assertIn('Startseite', DRIVER.TOWER_GLIDE_REFUSAL_DE)
        self.assertIn('neu starten', DRIVER.TOWER_GLIDE_UNJUDGEABLE_DE)

    def test_the_refusal_says_lay_the_arm_forward_only(self):
        """D88: every V1 tower sentence tells the student to lay the arm FORWARD, never to stand it up (a V1
        standing straight up on the tower side is refused too)."""
        self.assertEqual(DRIVER.TOWER_GLIDE_REFUSAL_DE, TOWER_GLIDE_REFUSAL_D88_DE)
        for text in (DRIVER.TOWER_GLIDE_REFUSAL_DE, DRIVER.TOWER_GLIDE_UNJUDGEABLE_DE):
            self.assertNotRegex(text, STAND_UP_RE)


class TheVerifiersResendIsJudged(BoundedTestCase):
    """``_boot_home_verify`` re-sends the glide from where a stalled arm stands; on the tower that re-send is
    judged like the first glide (56 N-6)."""

    # joint 2 stalled 0.31 rad short of HOME with joint 1 turned toward the tower
    STALL = (1.0472, 0.33, 0.64, 0.0, 0.0, 0.90)
    # the floor half: a re-send line that would press a link into the table
    DIP = (0.039, 3.023, 0.019, -1.356, 0.554, 0.90)

    def _verify(self, profile, stall):
        node = make_node(profile)
        node._torque_on = True
        node._last_positions = list(stall)
        gen = node._replace_trajectory([(list(HOME) + [0.9], 3.0)], 0.0)
        with scene_profile(profile), mock.patch.object(DRIVER, 'time', _Clock()):
            node._boot_home_verify(gen)
        return node, gen, node.get_logger().lines('warn')

    def test_the_stall_is_one_the_verifier_re_sends_and_the_floor_passes(self):
        verdict, off = DRIVER.boot_home_verify_decision(self.STALL, HOME, DRIVER.BOOT_HOME_VERIFY_TOL_RAD, 0, 1)
        self.assertEqual((verdict, off), ('resend', [1, 2]))
        self.assertAlmostEqual(HOME[1] - self.STALL[1], 0.31, places=9)
        self.assertTrue(DRIVER.boot_home_floor_decision(self.STALL)[0])

    def test_a_refused_re_send_is_skipped_with_one_warning(self):
        node, gen, warnings = self._verify('tower_v1', self.STALL)
        self.assertEqual(node._traj_gen, gen)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith('[WARNUNG] '))
        self.assertIn('Kameraturm', warnings[0])
        self.assertIn(TOWER_GLIDE_REFUSAL_D88_DE.removeprefix('[FEHLER] '), warnings[0])
        self.assertNotRegex(warnings[0], STAND_UP_RE)

    def test_a_re_send_into_the_table_is_skipped_on_the_tower(self):
        self.assertFalse(DRIVER.boot_home_floor_decision(self.DIP)[0])
        node, gen, warnings = self._verify('tower_v1', self.DIP)
        self.assertEqual(node._traj_gen, gen)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn('Tischplatte', warnings[0])

    def test_under_legacy_the_re_send_runs_as_today(self):
        node, gen, warnings = self._verify('legacy', self.STALL)
        self.assertEqual(node._traj_gen, gen + 1)
        self.assertIn('erneuter Anlauf', warnings[0])


if __name__ == '__main__':
    unittest.main()
