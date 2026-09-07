"""Deps-free tests for the robot activation gate.

WHAT THIS PROTECTS. Before 2026-09-07 „Umgebung starten" was itself a powered
motion — Phase 3 of entrypoint_omx.sh homed the follower or glided it onto the
leader, edu6_arm_node.py homed itself, and the leader's trajectory broadcaster
spawned at boot so a both-arms rig began teleoperating before a browser was
even open. The gate moves all of that behind one student-pressed button.

Two halves, both here:

* the arithmetic and the decisions inside activation_agent.py, extracted with
  ast so importing them needs no rclpy; and
* STRUCTURAL assertions that the four layers honouring the gate actually do —
  a green unit test over a helper nobody calls would be worthless, and three of
  the four layers are shell/launch/YAML that no unit test can otherwise reach.
"""

import ast
import io
import math
import os
import re
import subprocess
import textwrap
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
_DOCKER = os.path.join(_ROOT, 'robotis_ai_setup', 'docker', 'open_manipulator')
_AGENT = os.path.join(_DOCKER, 'activation_agent.py')
_ENTRYPOINT = os.path.join(_DOCKER, 'entrypoint_omx.sh')
_LEADER_LAUNCH = os.path.join(_DOCKER, 'overlays', 'omx_l_leader_ai.launch.py')
_DRIVER = os.path.join(_DOCKER, 'edu6_arm_node.py')
_DOCKERFILE = os.path.join(_DOCKER, 'Dockerfile')
_MOTION = os.path.join(_ROOT, 'physical_ai_tools', 'physical_ai_server',
                       'physical_ai_server', 'workflow', 'handlers', 'motion.py')
_COMPOSE = os.path.join(_ROOT, 'robotis_ai_setup', 'docker', 'docker-compose.yml')
_COMPOSE_OPI = os.path.join(_ROOT, 'robotis_ai_setup', 'docker',
                            'docker-compose.opi.yml')


def _read(path):
    with io.open(path, encoding='utf-8') as fh:
        return fh.read()


def _load_agent_namespace():
    """Exec only the module-level constants and pure functions.

    activation_agent.py imports rclpy at module top, which is not installed
    anywhere this suite runs, so the module can never simply be imported. The
    ast walk below executes Assign and FunctionDef nodes only — the class body
    and the imports are never touched. Same technique test_feetech_bus.py uses
    for the Feetech driver."""
    source = _read(_AGENT)
    tree = ast.parse(source)
    ns = {'__name__': 'activation_agent_pure', 'math': math}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                exec(compile(ast.Module([node], []), _AGENT, 'exec'), ns)  # noqa: S102
            except NameError:
                # A constant that depends on something we deliberately did not
                # execute is not a constant this suite is about.
                pass
        elif isinstance(node, ast.FunctionDef):
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, _AGENT, 'exec'), ns)  # noqa: S102
    return ns


_N = _load_agent_namespace()


class TestEnvFlag(unittest.TestCase):
    """Compose writes `KEY=` for an unset `${KEY:-}`; empty must not mean off."""

    def test_only_a_literal_zero_turns_the_gate_off(self):
        self.assertFalse(_N['env_flag']('0'))
        self.assertFalse(_N['env_flag'](' 0 '))
        for on in ('1', 'true', 'yes', 'anything'):
            self.assertTrue(_N['env_flag'](on), on)

    def test_absent_and_empty_keep_the_default(self):
        self.assertTrue(_N['env_flag'](None))
        self.assertTrue(_N['env_flag'](''))
        self.assertTrue(_N['env_flag']('   '))
        self.assertFalse(_N['env_flag'](None, default=False))


class TestSequenceShape(unittest.TestCase):

    def test_feetech_families_are_the_case_split_the_entrypoint_makes(self):
        self.assertTrue(_N['is_feetech']('edu6_studio'))
        self.assertTrue(_N['is_feetech']('edu1_studio'))
        self.assertFalse(_N['is_feetech']('omx_full'))
        self.assertFalse(_N['is_feetech']('omx_follower'))
        self.assertFalse(_N['is_feetech'](None))

    def test_every_configuration_homes(self):
        """The whole point of the feature: no rig activates without homing."""
        for robot_type in ('omx_full', 'omx_follower', 'edu6_studio', 'edu1_studio'):
            for follower_only in (True, False):
                steps = _N['activation_steps'](robot_type, follower_only)
                self.assertEqual(steps[0], 'home', (robot_type, follower_only))

    def test_only_a_leader_rig_syncs_and_starts_teleop(self):
        self.assertEqual(_N['activation_steps']('omx_full', False),
                         ('home', 'sync', 'teleop'))
        self.assertEqual(_N['activation_steps']('omx_full', True), ('home',))
        self.assertEqual(_N['activation_steps']('omx_follower', True), ('home',))
        # A Feetech arm has no leader even if someone mis-sets the flag.
        self.assertEqual(_N['activation_steps']('edu6_studio', False), ('home',))

    def test_the_helper_is_load_bearing_not_decorative(self):
        """The sequence must be DRIVEN by activation_steps(), not merely
        described by it.

        It shipped decorative: `_run_sequence` re-decided the shape with its
        own `if self._feetech` / `if self._leader` chain while this helper's
        docstring claimed to be the single source, and every test in this class
        was green over code nobody called. A fifth profile added here alone
        would then have shipped a broken sequence with a green suite."""
        source = _read(_AGENT)
        body = source[source.index('def _run_sequence'):]
        body = body[:body.index('\n    # ── legs')]
        self.assertIn('activation_steps(self._robot_type, self._follower_only)',
                      body)
        self.assertIn('for step in steps:', body)
        # …and it must not have grown a second copy of the decision.
        self.assertNotIn('if self._leader:', body)
        # every id the helper can emit has a branch, and nothing else does
        emitted = set()
        for robot_type in ('omx_full', 'omx_follower', 'edu6_studio', 'edu1_studio'):
            for follower_only in (True, False):
                emitted |= set(_N['activation_steps'](robot_type, follower_only))
        for step in emitted:
            self.assertIn(f"step == '{step}'", body, step)
        self.assertIn('unknown activation step', body)

    def test_teleop_order_puts_home_before_the_broadcaster(self):
        """home → sync → teleop, never teleop → home.

        The broadcaster republishes the leader's live pose at 100 Hz with
        time_from_start = 0; spawning it while the follower sits at HOME and
        the leader lies collapsed is a step command into a 0.10 rad goal
        tolerance."""
        steps = _N['activation_steps']('omx_full', False)
        self.assertLess(steps.index('home'), steps.index('sync'))
        self.assertLess(steps.index('sync'), steps.index('teleop'))

    def test_has_leader_matches_the_steps(self):
        self.assertTrue(_N['has_leader']('omx_full', False))
        self.assertFalse(_N['has_leader']('omx_full', True))
        self.assertFalse(_N['has_leader']('edu6_studio', False))


class TestQuintic(unittest.TestCase):

    def test_endpoints_and_rest_to_rest(self):
        start = [0.0, 0.5, -0.5, 0.0, 0.0, 0.0]
        target = [0.1, -1.5707963267948966, 1.5707963267948966, 0.2, -0.3, 0.8]
        pts = _N['quintic_waypoints'](start, target)
        self.assertEqual(len(pts), _N['MOVE_POINTS'])
        last_pos, last_vel, last_acc, last_t = pts[-1]
        for got, want in zip(last_pos, target):
            self.assertAlmostEqual(got, want, places=12)
        for v in last_vel:
            self.assertAlmostEqual(v, 0.0, places=12)
        for a in last_acc:
            self.assertAlmostEqual(a, 0.0, places=12)
        self.assertAlmostEqual(last_t, _N['MOVE_DURATION_S'], places=12)

    def test_positions_are_monotone_between_the_endpoints(self):
        start = [0.0] * 6
        target = [1.0] * 6
        pts = _N['quintic_waypoints'](start, target)
        first = [p[0][0] for p in pts]
        self.assertEqual(first, sorted(first))
        self.assertGreater(first[0], 0.0)
        self.assertLessEqual(first[-1], 1.0 + 1e-12)

    def test_it_is_the_same_polynomial_the_entrypoint_published(self):
        # s(t) = 10t³ − 15t⁴ + 6t⁵, sampled at (i+1)/N. A transcription slip
        # here changes how a physical arm accelerates.
        start, target = [0.0], [1.0]
        pts = _N['quintic_waypoints'](start, target, duration_s=3.0, count=50)
        for i, (pos, _v, _a, _t) in enumerate(pts):
            t = (i + 1) / 50
            self.assertAlmostEqual(
                pos[0], 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5, places=12)

    def test_it_refuses_a_mismatched_or_degenerate_request(self):
        with self.assertRaises(ValueError):
            _N['quintic_waypoints']([0.0], [0.0, 1.0])
        with self.assertRaises(ValueError):
            _N['quintic_waypoints']([0.0], [1.0], duration_s=0.0)
        with self.assertRaises(ValueError):
            _N['quintic_waypoints']([0.0], [1.0], count=0)


class TestArrivalDecision(unittest.TestCase):
    """Both halves of the Audit-E3 verifier, because either alone passes
    vacuously."""

    def test_arrived(self):
        start = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        target = [0.0, -1.5, 1.5, 0.0, 0.0, 0.8]
        ok, stale, err, motion = _N['arrival_decision'](start, target, target)
        self.assertTrue(ok)
        self.assertEqual(stale, [])
        self.assertAlmostEqual(err, 0.0)

    def test_a_frozen_feed_that_matches_the_target_is_NOT_arrival(self):
        # The pre-E3 bug: /joint_states stops mid-move, the snapshot happens to
        # equal the target, and the check passes though the arm never moved.
        start = [0.0, -1.5, 1.5, 0.0, 0.0, 0.8]
        target = [0.0, 0.0, 0.0, 0.0, 0.0, 0.8]
        current = list(target)
        # It DID move here, so this is the control: same numbers, arm moved.
        ok, stale, _e, _m = _N['arrival_decision'](start, current, target)
        self.assertTrue(ok)
        # …and now the stale case: still at start, target far away.
        ok, stale, _e, _m = _N['arrival_decision'](start, list(start), target)
        self.assertFalse(ok)
        self.assertIn(1, stale)
        self.assertIn(2, stale)

    def test_the_gripper_is_exempt_from_both_halves(self):
        """The leader's trigger sits near -0.7 rad while the follower's gripper
        starts open: a ~1.4 rad error that is expected, not a dropout."""
        start = [0.0] * 6
        target = [0.0, 0.0, 0.0, 0.0, 0.0, -0.7]
        current = [0.0, 0.0, 0.0, 0.0, 0.0, 0.8]
        ok, stale, _e, _m = _N['arrival_decision'](start, current, target)
        self.assertTrue(ok)
        self.assertEqual(stale, [])

    def test_short_of_the_target_on_an_arm_joint_fails(self):
        start = [0.0] * 6
        target = [0.0, -1.5, 0.0, 0.0, 0.0, 0.0]
        current = [0.0, -1.0, 0.0, 0.0, 0.0, 0.0]   # 0.5 rad out, tol 0.30
        ok, _s, err, _m = _N['arrival_decision'](start, current, target)
        self.assertFalse(ok)
        self.assertAlmostEqual(err, 0.5, places=9)

    def test_a_small_commanded_delta_is_not_judged_for_motion(self):
        # |commanded| <= tol: the arm legitimately may not move measurably.
        start = [0.0] * 6
        target = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
        ok, stale, _e, _m = _N['arrival_decision'](start, list(start), target)
        self.assertTrue(ok)
        self.assertEqual(stale, [])


class TestJointPositions(unittest.TestCase):

    def test_reorders_into_our_command_order(self):
        names = ['gripper_joint_1', 'joint5', 'joint4', 'joint3', 'joint2', 'joint1']
        pos = [0.8, 0.5, 0.4, 0.3, 0.2, 0.1]
        self.assertEqual(_N['joint_positions'](names, pos),
                         [0.1, 0.2, 0.3, 0.4, 0.5, 0.8])

    def test_a_partial_message_is_refused_not_padded(self):
        """A trajectory built from a padded message commands a joint to a value
        nobody measured."""
        names = ['joint1', 'joint2', 'joint3']
        self.assertIsNone(_N['joint_positions'](names, [0.0, 0.0, 0.0]))

    def test_a_truncated_position_array_is_refused(self):
        names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
        self.assertIsNone(_N['joint_positions'](names, [0.0, 0.0]))

    def test_empty_and_none_are_refused(self):
        self.assertIsNone(_N['joint_positions']([], [1.0]))
        self.assertIsNone(_N['joint_positions'](['joint1'], None))


class TestPublishIsSerialised(unittest.TestCase):
    """seq order and WIRE order must be the same order.

    Allocating seq under the lock but serialising and publishing OUTSIDE it let
    the 1 Hz timer and the worker's transitions interleave, so a subscriber
    could see a stale state AFTER a newer one — measured at 0.02 % under
    hammering, including messages landing after an `active` that carried an
    older seq. Asserted structurally (ast, not a substring scan, so the
    docstring explaining the bug cannot satisfy the test)."""

    def test_seq_serialise_and_publish_all_sit_inside_the_lock(self):
        tree = ast.parse(_read(_AGENT))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == '_publish_state')
        withs = [n for n in fn.body if isinstance(n, ast.With)]
        self.assertEqual(len(withs), 1, 'expected exactly one `with self._lock:`')
        inside = ast.dump(ast.Module(withs[0].body, []))
        for needed in ("attr='_seq'", "attr='dumps'", "attr='publish'"):
            self.assertIn(needed, inside, needed)
        # …and nothing of substance is left outside it.
        outside = [n for n in fn.body
                   if not isinstance(n, (ast.With, ast.Expr))]
        self.assertEqual(outside, [], 'statements outside the lock')


class TestFreshPose(unittest.TestCase):
    """A cached joint sample becomes a COMMANDED TRAJECTORY, so its age is not
    a detail.

    The caches used to have none: a feed that delivered one sample at boot and
    then died still built the quintic from that pose, and the only symptom was
    the soft „did not quite arrive" warning at the end. MSG_NO_JOINTS could
    fire only when joints had NEVER arrived."""

    def test_a_fresh_sample_is_returned(self):
        self.assertEqual(_N['fresh_pose'](([1.0, 2.0], 100.0), 100.5),
                         [1.0, 2.0])

    def test_a_stale_sample_is_refused(self):
        self.assertIsNone(_N['fresh_pose'](([1.0], 100.0), 100.0 + 2.001))

    def test_the_boundary_is_inclusive_of_the_window(self):
        age = _N['JOINT_MAX_AGE_S']
        self.assertIsNotNone(_N['fresh_pose'](([1.0], 0.0), age))
        self.assertIsNone(_N['fresh_pose'](([1.0], 0.0), age + 0.001))

    def test_no_sample_at_all_is_None(self):
        self.assertIsNone(_N['fresh_pose'](None, 1.0))

    def test_zero_disables_the_check_which_is_the_old_behaviour(self):
        self.assertEqual(
            _N['fresh_pose'](([1.0], 0.0), 1e9, max_age_s=0), [1.0])

    def test_the_window_is_wide_enough_for_a_100_Hz_feed(self):
        # /joint_states runs at 100 Hz, so the window must absorb an ordinary
        # hiccup; too tight and a healthy rig refuses to activate.
        self.assertGreaterEqual(_N['JOINT_MAX_AGE_S'], 1.0)


class TestStatePayload(unittest.TestCase):

    def test_the_wire_keys_are_the_react_contract(self):
        # hooks/useRobotActivation.js reads exactly these. Renaming one without
        # touching that file leaves the Startseite on „unbekannt" forever.
        payload = _N['state_payload']('idle', '', 'msg', 'omx_full', True, True, 3)
        self.assertEqual(
            set(payload),
            {'state', 'step', 'message', 'robot_type', 'has_leader',
             'required', 'seq'})
        self.assertIs(payload['has_leader'], True)
        self.assertIs(payload['required'], True)
        self.assertEqual(payload['seq'], 3)

    def test_none_step_and_message_serialise_as_empty_strings(self):
        payload = _N['state_payload']('active', None, None, None, False, False, 1)
        self.assertEqual(payload['step'], '')
        self.assertEqual(payload['message'], '')
        self.assertEqual(payload['robot_type'], '')


class TestHomeConstantNoDrift(unittest.TestCase):

    def test_home_equals_motion_HOME_JOINTS_RAD_plus_gripper_open(self):
        """The agent commands the arm; motion.py is where HOME is defined for
        the workflow. Two copies of a pose the arm physically drives to must
        not drift."""
        tree = ast.parse(_read(_MOTION))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in (
                            'HOME_JOINTS_RAD', 'GRIPPER_OPEN_RAD'):
                        found[t.id] = eval(  # noqa: S307 — literal math only
                            compile(ast.Expression(node.value), _MOTION, 'eval'),
                            {'math': math})
        self.assertIn('HOME_JOINTS_RAD', found)
        self.assertIn('GRIPPER_OPEN_RAD', found)
        expected = list(found['HOME_JOINTS_RAD']) + [found['GRIPPER_OPEN_RAD']]
        self.assertEqual(len(_N['HOME_JOINTS_RAD']), len(expected))
        for got, want in zip(_N['HOME_JOINTS_RAD'], expected):
            self.assertAlmostEqual(got, want, places=12)

    def test_home_is_the_boot_family_not_the_collision_safe_home(self):
        """Two HOME families exist on purpose: ±pi/2 here, the more-folded
        ±pi/4 for collision recovery and the Jetson. Reconciling them is an
        ask-first safety change, so this pins which one the agent drives."""
        self.assertAlmostEqual(_N['HOME_JOINTS_RAD'][1], -math.pi / 2, places=12)
        self.assertAlmostEqual(_N['HOME_JOINTS_RAD'][2], math.pi / 2, places=12)


class TestTeleopOwnsTheRailRefusal(unittest.TestCase):
    """A second activation on a leader rig is refused, and it is a CORRECTNESS
    refusal, not politeness.

    Once the broadcaster is spawned it republishes the leader's pose onto
    /leader/joint_trajectory at 100 Hz. A home trajectory on the same rail is
    overwritten within 10 ms and the follower snaps straight back to the
    leader — so the „re-home" would look like a twitch, not a home. Homing a
    teleoperating rig means moving the LEADER."""

    def test_the_refusal_exists_and_names_the_leader_arm(self):
        source = _read(_AGENT)
        self.assertIn('MSG_TELEOP_ALREADY_RUNNING', source)
        msg = _N['MSG_TELEOP_ALREADY_RUNNING']
        self.assertIn('Leader-Arm', msg)
        self.assertIn('Grundstellung', msg)
        # literal umlauts, never transliterations (CLAUDE.md Rule §1)
        for bad in ('ue', 'ae', 'oe'):
            self.assertNotIn(bad, msg.replace('neu', ''))

    def test_it_is_checked_before_the_state_flips_to_activating(self):
        source = _read(_AGENT)
        body = source[source.index('def _activate_cb'):]
        body = body[:body.index('\n    def ')]
        guard = body.index('self._leader and self._teleop_started')
        flip = body.index('self._state = STATE_ACTIVATING')
        self.assertLess(guard, flip)
        # …and it is inside the lock, with the busy check
        self.assertLess(body.index('with self._lock:'), guard)

    def test_a_follower_only_rig_is_never_refused_this_way(self):
        """`_teleop_started` is True from construction when the gate is OFF, so
        a bare `self._teleop_started` check would have blocked the re-home on
        every rollback rig — including follower-only ones, where nothing owns
        the rail. The `self._leader and` half is what stops that."""
        source = _read(_AGENT)
        self.assertIn('if self._leader and self._teleop_started:', source)


_REACT_HOOK = os.path.join(_ROOT, 'physical_ai_tools', 'physical_ai_manager',
                           'src', 'hooks', 'useRobotActivation.js')


def _shell_gate_verdict(value):
    """Run the ENTRYPOINT's own two lines, from the file, under real bash.

    Not a re-implementation: the lines are extracted from entrypoint_omx.sh so
    that editing them changes what this test measures."""
    text = _read(_ENTRYPOINT)
    m = re.search(
        r'^(REQUIRE_ACTIVATION="\$\{EDUBOTICS_REQUIRE_ACTIVATION:-1\}"\n'
        r'REQUIRE_ACTIVATION=.*\n'
        r'if \[ "\$REQUIRE_ACTIVATION" = "0" \]; then\n'
        r'.*\nelse\n.*\nfi)$',
        text, re.M)
    if not m:
        raise AssertionError('the entrypoint gate block moved — update this test')
    script = m.group(1) + '\necho "$REQUIRE_ACTIVATION"\n'
    env = dict(os.environ)
    if value is None:
        env.pop('EDUBOTICS_REQUIRE_ACTIVATION', None)
    else:
        env['EDUBOTICS_REQUIRE_ACTIVATION'] = value
    out = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                         env=env, check=True)
    return out.stdout.strip() == '1'


def _python_gate_verdict(path, pattern, value):
    """Evaluate one of the two in-tree Python gate expressions, as written."""
    text = _read(path)
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f'gate expression not found in {path}')
    environ = {} if value is None else {'EDUBOTICS_REQUIRE_ACTIVATION': value}
    return bool(eval(m.group(1),  # noqa: S307 — an in-tree literal expression
                     {'os': type('os', (), {'environ': environ})}))


class TestGateParserLockstep(unittest.TestCase):
    """FOUR readers decide whether the arm may move at boot, in four languages.
    They must agree on EVERY value, and the failure is not symmetric.

    The bug this pins (found in review, 2026-09-07): the shell was the only one
    that did not strip whitespace, and `config_generator.upsert_env_var` ALWAYS
    quotes what it writes — so a hand-set `EDUBOTICS_REQUIRE_ACTIVATION="0 "`
    reached the container verbatim and the readers SPLIT: the shell kept the
    gate ON (no boot home, no leader sync) while omx_l_leader_ai.launch.py
    turned it OFF (broadcaster back in the boot spawner). Teleop then went live
    at container boot with the follower at an arbitrary power-up pose and the
    leader wherever gravity left it — a 100 Hz step command, i.e. strictly
    worse than either the pre-gate or the post-gate design, and with the
    Startseite showing a green pill because the agent read `active`."""

    # ON means „the gate holds": nothing may move at boot.
    VALUES = [
        (None, True), ('', True), ('1', True), ('true', True), ('yes', True),
        ('00', True), ('false', True), ('  ', True),
        ('0', False), (' 0 ', False), ('0\n', False), ('\t0', False),
    ]

    def _verdicts(self, value):
        return {
            'shell': _shell_gate_verdict(value),
            'launch': _python_gate_verdict(
                _LEADER_LAUNCH,
                r'_require_activation = \(\s*(os\.environ\.get\([^\n]*?\)\s*'
                r'\.strip\(\) != .0.)\)', value),
            'driver': _python_gate_verdict(
                _DRIVER,
                r"if (os\.environ\.get\('EDUBOTICS_REQUIRE_ACTIVATION', '1'\)"
                r"\.strip\(\) == '0'):", value),
            'agent': _N['env_flag'](value),
        }

    def test_all_four_readers_agree_on_every_value(self):
        for value, expected_on in self.VALUES:
            v = self._verdicts(value)
            # the driver's expression asks the INVERSE question ("is it off?")
            v['driver'] = not v['driver']
            with self.subTest(value=value):
                self.assertEqual(
                    set(v.values()), {expected_on},
                    f'readers disagree on {value!r}: {v}')

    def test_the_shell_normalises_whitespace_like_the_other_three(self):
        # The specific regression, called out by name so a "simplification"
        # that drops the tr has to argue with this test.
        self.assertIn("tr -d '[:space:]'", _read(_ENTRYPOINT))


class TestWireContractLockstep(unittest.TestCase):
    """activation_agent.py and hooks/useRobotActivation.js are two halves of
    ONE contract, and each side's own suite pins only its own literals.

    A rename on either side leaves BOTH suites green and puts the Startseite on
    „unbekannt" forever with the button live — the one state the design says
    must never be reachable. This is the test that reads both files, the way
    enum_parity.py and test_rosbridge_origin_gate.py already do for their
    contracts."""

    def setUp(self):
        self.js = _read(_REACT_HOOK)

    def _js_const(self, name):
        m = re.search(rf"export const {name} = '([^']+)'", self.js)
        self.assertIsNotNone(m, f'{name} not found in useRobotActivation.js')
        return m.group(1)

    def test_the_topic_and_service_names_match(self):
        self.assertEqual(self._js_const('ACTIVATION_TOPIC'), _N['STATE_TOPIC'])
        self.assertEqual(self._js_const('ACTIVATION_SERVICE'),
                         _N['ACTIVATE_SERVICE'])

    def test_the_four_state_literals_match(self):
        for js_name, py_name in (('IDLE', 'STATE_IDLE'),
                                 ('ACTIVATING', 'STATE_ACTIVATING'),
                                 ('ACTIVE', 'STATE_ACTIVE'),
                                 ('FAILED', 'STATE_FAILED')):
            self.assertEqual(self._js_const(js_name), _N[py_name], js_name)

    def test_every_key_the_agent_sends_is_read_by_the_parser(self):
        sent = set(_N['state_payload']('idle', '', '', '', True, True, 1))
        # `seq` is deliberately not read — it exists so a human tailing the
        # topic can see it is live. Everything else must be consumed.
        for key in sent - {'seq'}:
            self.assertIn(f'raw.{key}', self.js,
                          f'the agent sends {key!r} and the parser ignores it')

    def test_the_parser_reads_nothing_the_agent_does_not_send(self):
        read = set(re.findall(r'raw\.([a-z_]+)', self.js))
        sent = set(_N['state_payload']('idle', '', '', '', True, True, 1))
        self.assertEqual(read - sent, set(),
                         'the parser reads keys the agent never sends')


class TestStructuralGate(unittest.TestCase):
    """The other three layers. No unit test can reach shell, a launch file or
    YAML, and a gate honoured by one of four layers is not a gate."""

    def test_the_entrypoint_gates_the_whole_of_phase_3(self):
        text = _read(_ENTRYPOINT)
        self.assertIn('REQUIRE_ACTIVATION="${EDUBOTICS_REQUIRE_ACTIVATION:-1}"', text)
        # The Phase-3 branch chain must START with the gate, so neither the
        # leader-sync nor the follower-only home can run when it is on.
        gate = text.index('if [ "$REQUIRE_ACTIVATION" = "1" ]; then\n'
                          '    echo "[LAUNCH] Aktivierung erforderlich')
        sync = text.index('Moving follower to match leader')
        home = text.index('moving follower to safe HOME')
        self.assertLess(gate, sync)
        self.assertLess(gate, home)

    def test_the_entrypoint_starts_the_agent_on_every_profile(self):
        text = _read(_ENTRYPOINT)
        launch = text.index('python3 /usr/local/bin/activation_agent.py')
        # After the Phase-3 gate closes, so it is outside every branch of it,
        # and before the camera phase, so a Feetech rig (which skips Phase 3
        # entirely) reaches it too.
        self.assertLess(text.index('end activation gate'), launch)
        self.assertLess(launch, text.index('--- Phase 4: Launch Cameras ---'))
        self.assertIn('PIDS="$PIDS $!"', text[launch:launch + 900])

    def test_the_agent_is_SUPERVISED_and_the_supervisor_stops_on_teardown(self):
        """Its failure is silent and total — the healthcheck proves topics
        exist, not that the rig can be activated — so unlike every other helper
        here it gets a bounded respawn loop. And the loop has to know the
        difference between a crash and a clean SIGTERM exit during teardown, or
        it respawns the agent while the container is going down."""
        text = _read(_ENTRYPOINT)
        self.assertIn('STOPPING_FLAG="/tmp/.edubotics-stopping"', text)
        # cleanup() raises the sentinel BEFORE anything is killed
        cleanup = text[text.index('cleanup() {'):text.index('trap cleanup')]
        self.assertIn(': > "$STOPPING_FLAG"', cleanup)
        self.assertLess(cleanup.index(': > "$STOPPING_FLAG"'),
                        cleanup.index('for pid in $PIDS'))
        # the loop is bounded and sentinel-gated on BOTH ends
        block = text[text.index('Aktivierungs-Agent wird gestartet'):
                     text.index('--- Phase 4: Launch Cameras ---')]
        self.assertIn('while [ ! -e "$STOPPING_FLAG" ] && [ "$attempt" -lt 5 ]',
                      block)
        self.assertIn('[ -e "$STOPPING_FLAG" ] && break', block)
        self.assertIn('[FEHLER]', block)

    def test_the_boot_leader_read_is_skipped_when_gated(self):
        """That read has no timeout; not needing it at boot removes a hang."""
        text = _read(_ENTRYPOINT)
        self.assertIn('Leader-Pose wird erst bei der Aktivierung gelesen.', text)

    def test_the_leader_launch_omits_the_broadcaster_by_default(self):
        """Teleop is held off structurally: with the controller unspawned,
        /leader/joint_trajectory has NO publisher at all."""
        source = _read(_LEADER_LAUNCH)
        tree = ast.parse(source)
        ns = {'os': os}
        boot = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == '_boot_controllers'):
                boot = eval(  # noqa: S307 — a list of string literals
                    compile(ast.Expression(node.value), _LEADER_LAUNCH, 'eval'), ns)
        self.assertEqual(boot, ['joint_state_broadcaster',
                                'trigger_position_controller'])
        # …and the rollback puts it back.
        self.assertIn("_boot_controllers.append('joint_trajectory_command_broadcaster')",
                      source)
        self.assertIn("os.environ.get('EDUBOTICS_REQUIRE_ACTIVATION', '1')", source)

    def test_the_leader_trigger_spring_is_NOT_gated(self):
        """It moves the leader's own trigger under a 300 mA cap, not the
        follower, and a JointGroupPositionController left with no command is
        its own hazard."""
        source = _read(_LEADER_LAUNCH)
        self.assertIn('trigger_position_controller', source)
        self.assertIn("'data: [-0.7]'", source)
        # the spring publish still hangs off the boot spawner's exit
        self.assertIn('target_action=robot_controller_spawner', source)

    def test_the_driver_energises_at_boot_but_does_not_home(self):
        source = _read(_DRIVER)
        self.assertIn("def boot_energise(self)", source)
        self.assertIn("def run_home(self", source)
        self.assertIn("def _home_cb(self", source)
        # main() picks energise-only unless the gate is off
        main_src = source[source.index('def main() -> int:'):]
        self.assertIn("EDUBOTICS_REQUIRE_ACTIVATION", main_src)
        gate = main_src.index("EDUBOTICS_REQUIRE_ACTIVATION")
        energise = main_src.index('node.boot_energise()')
        legacy = main_src.index('node.start_boot_home()')
        self.assertLess(gate, energise)
        self.assertLess(gate, legacy)

    def test_the_driver_exposes_the_family_neutral_home_alias(self):
        """activation_agent.py calls ONE name on either Feetech arm; the family
        branch stays in the driver."""
        source = _read(_DRIVER)
        self.assertIn("HOME_SERVICE_ALIAS = '/edubotics/home_arm'", source)
        self.assertIn("HOME_SERVICE = '/edu6/home'", source)
        self.assertIn("_EDU1_HOME_SERVICE = '/edu1/home'", source)
        self.assertIn('self.create_service(Trigger, HOME_SERVICE, self._home_cb)',
                      source)
        self.assertIn(
            'self.create_service(Trigger, HOME_SERVICE_ALIAS, self._home_cb)',
            source)
        self.assertEqual(_N['FEETECH_HOME_SERVICE'], '/edubotics/home_arm')

    def test_the_agent_is_shipped_and_parity_checked(self):
        dockerfile = _read(_DOCKERFILE)
        self.assertIn('COPY activation_agent.py /usr/local/bin/activation_agent.py',
                      dockerfile)
        # CRLF strip + exec bit, like every other plain COPY in that image
        for line in dockerfile.splitlines():
            if line.startswith("RUN sed -i 's/\\r$//'"):
                self.assertIn('/usr/local/bin/activation_agent.py', line)
                break
        else:
            self.fail('the CRLF-strip RUN line moved')
        parity = _read(os.path.join(_ROOT, '.github', 'scripts',
                                    'image_source_parity.sh'))
        self.assertIn('activation_agent.py', parity)

    def test_the_rollback_variable_is_forwarded_on_both_composes(self):
        needle = '- EDUBOTICS_REQUIRE_ACTIVATION=${EDUBOTICS_REQUIRE_ACTIVATION:-1}'
        for path in (_COMPOSE, _COMPOSE_OPI):
            text = _read(path)
            self.assertIn(needle, text, path)
            # on open_manipulator, which is the only container that reads it
            head = text.index('  open_manipulator:')
            nxt = text.index('  physical_ai_server:')
            self.assertLess(head, text.index(needle), path)
            self.assertLess(text.index(needle), nxt, path)


if __name__ == '__main__':
    unittest.main()
