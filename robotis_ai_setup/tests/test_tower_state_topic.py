# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-T1: ``/edubotics/tower_state``, the V1 driver's tower verdict for the
arm's live pose (spec 40 WP-T1, 52 §5.1).

The driver publishes, latched (reliable, transient-local, depth 1) and again
every 0.5 s from a thread of its own, ``{"v": 1, "glide", "fall", "part",
"gen"}``: the activation glide's verdict from the pose the read loop last
published (``feetech_geometry.tower_glide_decision``), where a limp arm would
fall (``limp_fall_direction``), the binding tower part of a refusal and a count
of the messages. Only the V1 driver on the tower publishes it: under ``legacy``,
an absent key or any other value the topic does not exist, and on the OMX
profiles the driver itself never runs (``entrypoint_omx.sh`` starts it for
``v1_studio`` alone), so neither does the topic.

``feetech_arm_node.py`` imports rclpy at module top, so it is imported here
under ``ros_stubs``' fake ROS modules, completed with the message and service
types the driver needs. :func:`load_driver` is shared with
``test_feetech_tower_check.py``.
"""

import contextlib
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from unittest import mock

import ros_stubs
from timeout_guard import BoundedTestCase

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_ARM_DIR = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator')
_DRIVER = os.path.join(_ARM_DIR, 'feetech_arm_node.py')
_ENTRYPOINT = os.path.join(_ARM_DIR, 'entrypoint_omx.sh')

KEY = 'EDUBOTICS_SCENE_CAMERA_PROFILE'
TOPIC = '/edubotics/tower_state'
HOME = (0.0, 0.64, 0.64, 0.0, 0.0)
# a V1 lying back toward the tower (the first refused start of the acceptance
# set), the claw closed
FALLEN_BACK = (0.9581949445618942, 0.0, 1.6189467825699932, -0.8224370210135715, 0.0, 0.0)


class Logger:
    """Records ``(level, text)`` for every line, ``error`` included."""

    def __init__(self):
        self.records = []

    def lines(self, level):
        return [text for lvl, text in self.records if lvl == level]

    def info(self, text):
        self.records.append(('info', text))

    def warning(self, text):
        self.records.append(('warn', text))

    warn = warning

    def error(self, text):
        self.records.append(('error', text))


class Node(ros_stubs.Node):
    """``ros_stubs.Node`` plus the subscriptions and services the driver creates."""

    def __init__(self, node_name, *args, **kwargs):
        super().__init__(node_name, *args, **kwargs)
        self._logger = Logger()
        self.subscriptions = []
        self.services = []

    def create_subscription(self, msg_type, topic, callback, qos, **kwargs):
        sub = types.SimpleNamespace(msg_type=msg_type, topic=topic, callback=callback, qos=qos)
        self.subscriptions.append(sub)
        return sub

    def create_service(self, srv_type, name, callback, **kwargs):
        srv = types.SimpleNamespace(srv_type=srv_type, name=name, callback=callback)
        self.services.append(srv)
        return srv

    def publishers_on(self, topic):
        return [pub for pub in self.publishers if pub.topic == topic]


class JointState:

    def __init__(self):
        self.header = ros_stubs.Header()
        self.name = []
        self.position = []
        self.velocity = []
        self.effort = []


def _fake_ros():
    mods = ros_stubs.modules()
    mods['rclpy.node'].Node = Node
    mods['rclpy'].node = mods['rclpy.node']
    mods['sensor_msgs.msg'].JointState = JointState
    std_srvs = types.ModuleType('std_srvs')
    std_srvs_srv = types.ModuleType('std_srvs.srv')
    std_srvs_srv.SetBool = type('SetBool', (), {})
    std_srvs_srv.Trigger = type('Trigger', (), {})
    std_srvs.srv = std_srvs_srv
    trajectory_msgs = types.ModuleType('trajectory_msgs')
    trajectory_msgs_msg = types.ModuleType('trajectory_msgs.msg')
    trajectory_msgs_msg.JointTrajectory = type('JointTrajectory', (), {})
    trajectory_msgs.msg = trajectory_msgs_msg
    mods.update({'std_srvs': std_srvs, 'std_srvs.srv': std_srvs_srv,
                 'trajectory_msgs': trajectory_msgs, 'trajectory_msgs.msg': trajectory_msgs_msg})
    return mods


@contextlib.contextmanager
def scene_profile(value):
    """``EDUBOTICS_SCENE_CAMERA_PROFILE`` set to ``value`` (``None``: unset) for the block."""
    with mock.patch.dict(os.environ):
        os.environ.pop(KEY, None)
        if value is not None:
            os.environ[KEY] = value
        yield


def load_driver():
    """The REAL ``feetech_arm_node`` module, imported under fake ROS modules (its siblings ``feetech_bus``,
    ``feetech_geometry`` and ``scene_split`` are the real ones)."""
    with mock.patch.dict(sys.modules, _fake_ros()), mock.patch.object(sys, 'path', [_ARM_DIR] + sys.path):
        spec = importlib.util.spec_from_file_location('feetech_arm_node_under_test', _DRIVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


DRIVER = load_driver()


def make_node(profile):
    """A driver node built under the scene profile ``profile``; nothing on a bus is touched (its threads are
    not started)."""
    with scene_profile(profile):
        return DRIVER.FeetechArmNode(bus=object())


def tower_thread(node):
    return getattr(node, '_tower_thread', None)


class TheTopicExistsOnlyForTheV1OnTheTower(BoundedTestCase):

    def test_on_the_tower_it_is_latched_and_re_sent_at_2_hz(self):
        node = make_node('tower_v1')
        pubs = node.publishers_on(TOPIC)
        self.assertEqual(len(pubs), 1, 'no /edubotics/tower_state publisher on the tower')
        qos = pubs[0].qos
        self.assertEqual(qos.durability, 'transient_local')
        self.assertEqual(qos.reliability, 'reliable')
        self.assertEqual(qos.depth, 1)
        thread = tower_thread(node)
        self.assertIsNotNone(thread)
        self.assertEqual(thread.name, 'feetech-tower-state')
        self.assertTrue(thread.daemon)
        self.assertFalse(thread.is_alive())             # started with the bus loops (start())
        self.assertEqual(DRIVER.TOWER_STATE_PERIOD_S, 0.5)

    def test_the_thread_publishes_every_period_until_shutdown(self):
        node = make_node('tower_v1')
        node._last_positions = list(HOME) + [0.90]
        waits = []

        def wait(timeout):
            waits.append(timeout)
            return len(waits) > 3                       # three ticks, then shutdown

        with scene_profile('tower_v1'), mock.patch.object(node._stop, 'wait', wait):
            node._tower_state_loop()
        self.assertEqual(waits, [0.5] * 4)
        self.assertEqual(len(node.publishers_on(TOPIC)[0].published), 3)

    def test_quoted_and_mixed_case_reads_as_the_tower(self):
        self.assertEqual(len(make_node(' "Tower_V1" ').publishers_on(TOPIC)), 1)

    def test_legacy_absent_or_any_other_value_has_no_topic(self):
        for value in ('legacy', None, '', 'tower', 'TOWER_V2'):
            with self.subTest(value=value):
                node = make_node(value)
                self.assertEqual(node.publishers_on(TOPIC), [])
                self.assertIsNone(tower_thread(node))

    def test_the_omx_profiles_never_run_the_driver(self):
        """On ``omx_full`` and ``omx_follower`` the entrypoint never starts ``feetech_arm_node.py`` (its family
        ``case`` sends ``v1_studio`` alone to it), and no other script of the arm container names the topic."""
        with open(_ENTRYPOINT, encoding='utf-8') as handle:
            src = handle.read()
        case = re.search(r'case "\$\{EDUBOTICS_ROBOT_TYPE:-omx_full\}" in\n(.*?)\nesac', src, re.S)
        self.assertIsNotNone(case)
        arms = re.findall(r'^\s*([^\s)]+)\)\s*FEETECH_ARM=(\d)', case.group(1), re.M)
        self.assertEqual(arms, [('v1_studio', '1'), ('*', '0')])
        starts = [m.start() for m in re.finditer(r'python3 /usr/local/bin/feetech_arm_node\.py', src)]
        self.assertEqual(len(starts), 1)
        self.assertGreater(starts[0], src.index('if [ "$FEETECH_ARM" = "1" ]; then'))
        for name in os.listdir(_ARM_DIR):
            path = os.path.join(_ARM_DIR, name)
            if name != 'feetech_arm_node.py' and os.path.isfile(path):
                with open(path, encoding='utf-8', errors='replace') as handle:
                    self.assertNotIn(TOPIC, handle.read(), name)


class ThePayload(BoundedTestCase):

    def _publish(self, node, positions):
        node._last_positions = None if positions is None else list(positions)
        with scene_profile('tower_v1'):
            node._publish_tower_state()
        return json.loads(node.publishers_on(TOPIC)[0].published[-1].data)

    def test_home_is_clear_and_falls_forward(self):
        node = make_node('tower_v1')
        state = self._publish(node, HOME + (0.90,))
        self.assertEqual(state, {'v': 1, 'glide': 'clear', 'fall': 'forward', 'part': None, 'gen': 1})

    def test_a_fallen_back_arm_is_refused_names_its_part_and_falls_back(self):
        node = make_node('tower_v1')
        state = self._publish(node, FALLEN_BACK)
        self.assertEqual(state['glide'], 'refused')
        self.assertEqual(state['fall'], 'back')
        self.assertEqual(state['part'], 'column')
        self.assertEqual(list(state), ['v', 'glide', 'fall', 'part', 'gen'])

    def test_no_reading_or_a_bus_fault_is_unjudgeable(self):
        node = make_node('tower_v1')
        self.assertEqual(self._publish(node, None)['glide'], 'unjudgeable')
        node._bus_fault = True
        state = self._publish(node, HOME + (0.90,))
        self.assertEqual((state['glide'], state['fall'], state['part']), ('unjudgeable', 'unknown', None))

    def test_every_message_counts_up(self):
        node = make_node('tower_v1')
        gens = [self._publish(node, pose)['gen'] for pose in (HOME + (0.90,), HOME + (0.90,), FALLEN_BACK)]
        self.assertEqual(gens, [1, 2, 3])

    def test_a_reading_is_judged_again_once_it_moved_more_than_two_ticks(self):
        node = make_node('tower_v1')
        tick = DRIVER.RAD_PER_TICK
        self.assertEqual(self._publish(node, FALLEN_BACK)['glide'], 'refused')
        judged = node._tower_judged
        dither = [v + (tick if k % 2 else -tick) for k, v in enumerate(FALLEN_BACK)]
        self._publish(node, dither)
        self.assertIs(node._tower_judged, judged)       # a dithering reading is not judged again
        self.assertEqual(self._publish(node, HOME + (0.90,))['glide'], 'clear')
        self.assertIsNot(node._tower_judged, judged)

    def test_the_verdict_is_the_drivers_own_rule(self):
        """the same function run_home judges with, on the same pose and target."""
        node = make_node('tower_v1')
        state = self._publish(node, FALLEN_BACK)
        verdict, _margin, part = DRIVER.fg.tower_glide_decision(
            FALLEN_BACK, HOME + (DRIVER.GRIPPER_OPEN_RAD,), DRIVER.ARM_PROFILE, DRIVER.TOWER_WINDOW_RAD)
        self.assertEqual((state['glide'], state['part']), (verdict, part))
        self.assertEqual(state['fall'], DRIVER.fg.limp_fall_direction(FALLEN_BACK[:5]))


def _path(start, end, steps):
    """``steps`` readings from ``start`` to ``end`` (both included), every one more than two ticks from the last."""
    return [tuple(a + (b - a) * k / (steps - 1) for a, b in zip(start, end)) for k in range(steps)]


class TheGlideIsJudgedAgainOnlyAtRest(BoundedTestCase):
    """X-C2 (verifier C-2): one judgement of the glide is tens of milliseconds of pure Python (more on an Orange
    Pi), and a program motion moves the pose by more than two ticks every period, so a thread that re-judged
    whenever the arm moved took the interpreter from the 50 Hz read and write loops for as long as a program ran.
    While a trajectory executes (torque on and the command rail holding one) the thread keeps the verdict it
    judged at rest and publishes only the cheap ``fall`` anew; at rest again it judges once. A limp or hand-guided
    arm (torque off) is judged as before. The activation glide's refusal never reads this state: run_home judges
    the pose it reads at the press (``test_feetech_tower_check.RunHomeJudgesTheGlide``)."""

    HOME_OPEN = HOME + (0.90,)

    def _publish(self, node, positions):
        node._last_positions = None if positions is None else list(positions)
        with scene_profile('tower_v1'):
            node._publish_tower_state()
        return json.loads(node.publishers_on(TOPIC)[0].published[-1].data)

    @staticmethod
    def _program_motion(node):
        node._torque_on = True
        node._traj_points = [(list(TheGlideIsJudgedAgainOnlyAtRest.HOME_OPEN), 0.0), (list(FALLEN_BACK), 3.0)]

    @staticmethod
    def _judge():
        return mock.patch.object(DRIVER.fg, 'tower_glide_decision', wraps=DRIVER.fg.tower_glide_decision)

    def test_no_judgement_while_a_trajectory_executes_and_the_fall_stays_live(self):
        node = make_node('tower_v1')
        self.assertEqual(self._publish(node, self.HOME_OPEN)['glide'], 'clear')    # judged at rest
        self._program_motion(node)
        poses = _path(self.HOME_OPEN, FALLEN_BACK, 8)[1:]
        with self._judge() as judge:
            states = [self._publish(node, pose) for pose in poses]
        self.assertEqual(judge.call_count, 0, 'the glide was judged while a trajectory executed')
        self.assertEqual({s['glide'] for s in states}, {'clear'})                   # the verdict judged at rest
        self.assertEqual({s['part'] for s in states}, {None})
        self.assertEqual([s['fall'] for s in states],
                         [DRIVER.fg.limp_fall_direction(list(p)[:5]) for p in poses])
        self.assertEqual(states[-1]['fall'], 'back')                                # computed for the live pose
        self.assertEqual([s['gen'] for s in states], list(range(2, 2 + len(poses))))

    def test_at_rest_again_it_judges_once(self):
        node = make_node('tower_v1')
        self._publish(node, self.HOME_OPEN)
        self._program_motion(node)
        for pose in _path(self.HOME_OPEN, FALLEN_BACK, 6)[1:]:
            self._publish(node, pose)
        node._traj_points = []                          # the write loop dropped the finished trajectory
        with self._judge() as judge:
            first = self._publish(node, FALLEN_BACK)
            again = self._publish(node, FALLEN_BACK)
        self.assertEqual(judge.call_count, 1)
        self.assertEqual((first['glide'], first['part']), ('refused', 'column'))
        self.assertEqual(again, dict(first, gen=first['gen'] + 1))

    def test_a_limp_or_holding_arm_is_judged_as_before(self):
        """torque off (hand-guided; a re-torque clears the rail) or torque on with an empty rail."""
        for torque_on in (False, True):
            with self.subTest(torque_on=torque_on):
                node = make_node('tower_v1')
                node._torque_on = torque_on
                poses = _path(self.HOME_OPEN, FALLEN_BACK, 4)
                with self._judge() as judge:
                    states = [self._publish(node, pose) for pose in poses]
                self.assertEqual(judge.call_count, len(poses))
                self.assertEqual(states[-1]['glide'], 'refused')

    def test_the_first_reading_is_judged_even_while_moving(self):
        """nothing judged yet (the thread's first tick, the EDUBOTICS_REQUIRE_ACTIVATION=0 boot glide): one
        judgement, then none until the motion ends."""
        node = make_node('tower_v1')
        self._program_motion(node)
        with self._judge() as judge:
            states = [self._publish(node, pose) for pose in _path(self.HOME_OPEN, FALLEN_BACK, 5)]
        self.assertEqual(judge.call_count, 1)
        self.assertEqual({s['glide'] for s in states}, {'clear'})

    def test_a_lost_reading_reads_unjudgeable_at_once_even_while_moving(self):
        node = make_node('tower_v1')
        self._publish(node, self.HOME_OPEN)
        self._program_motion(node)
        node._bus_fault = True
        state = self._publish(node, self.HOME_OPEN)
        self.assertEqual((state['glide'], state['fall'], state['part']), ('unjudgeable', 'unknown', None))


if __name__ == '__main__':
    unittest.main()
