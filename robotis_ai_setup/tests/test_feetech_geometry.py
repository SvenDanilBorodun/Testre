"""The DRIVER's whole-link geometry (``docker/open_manipulator/feetech_geometry.py``)
and the boot-home table-floor pre-check that consumes it.

Deps-free by design: ``feetech_geometry`` is pure Python so this suite can
import it directly, and ``feetech_arm_node`` is read with ast (it imports
rclpy).

The drift test is the load-bearing one. The box table and the kinematic chain
exist TWICE — here and in the server's ``workflow/arm_geometry.py`` /
``workflow/v1_ik.py`` — because the driver runs in a different container and
cannot import the server package. Same discipline ``test_feetech_bus.py``
already applies to ``HOME_JOINTS_RAD``.
"""

import ast
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_DRIVER_DIR = os.path.join(_REPO, 'robotis_ai_setup', 'docker',
                           'open_manipulator')
_NODE = os.path.join(_DRIVER_DIR, 'feetech_arm_node.py')
_GEO = os.path.join(_DRIVER_DIR, 'feetech_geometry.py')
_SERVER = os.path.join(_REPO, 'physical_ai_tools', 'physical_ai_server',
                       'physical_ai_server')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fg = _load('feetech_geometry_under_test', _GEO)


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _module_literal(path, name):
    """Value of a module-level literal assignment, without importing."""
    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not a module-level literal in {path}')


_HOME = _module_literal(_NODE, 'HOME_JOINTS_RAD')

# The 'swept dip the endpoints miss' witness: shoulder folded right back, elbow
# nearly straight — a pose a LIMP arm collapses into. Above the table, as is
# HOME, yet the straight joint-space line between them drives a link far UNDER
# it. Found by sampling the box model.
_DIP_START = [0.039, 3.023, 0.019, -1.356, 0.554]


class TestBoxesAndChainDoNotDrift(unittest.TestCase):
    """The driver's copy of the model must equal the server's, exactly.

    Two copies of a safety-relevant constant table is a real cost; this is what
    makes it survivable. Fails the moment either side is edited alone — at which
    point the driver would be judging the boot glide with the wrong arm.
    """

    def test_link_boxes_match_the_server(self):
        server = _module_literal(
            os.path.join(_SERVER, 'workflow', 'arm_geometry.py'),
            'V1_LINK_BOXES')
        self.assertEqual(tuple(fg.V1_LINK_BOXES), tuple(server))

    def test_kinematic_constants_match_the_server(self):
        ik = os.path.join(_SERVER, 'workflow', 'v1_ik.py')
        for driver_name, server_name in (
                ('_V1_J1_XYZ', '_J1_XYZ'), ('_V1_J1_RPY', '_J1_RPY'),
                ('_V1_J2_XYZ', '_J2_XYZ'), ('_V1_J2_RPY', '_J2_RPY'),
                ('_V1_J3_XYZ', '_J3_XYZ'), ('_V1_J3_RPY', '_J3_RPY'),
                ('_V1_J4_XYZ', '_J4_XYZ'), ('_V1_J4_RPY', '_J4_RPY'),
                ('_V1_J5_XYZ', '_J5_XYZ'), ('_V1_J5_RPY', '_J5_RPY'),
                ('_V1_AXES', '_AXES')):
            self.assertEqual(
                _module_literal(_GEO, driver_name),
                _module_literal(ik, server_name),
                f'{driver_name} drifted between the driver and the server')

    def test_home_pose_matches_the_server_profile(self):
        server = _module_literal(os.path.join(_SERVER, 'robot_profiles.py'),
                                 '_V1_HOME_JOINTS_RAD')
        self.assertEqual(tuple(_HOME), tuple(server))


class TestTheSpec(unittest.TestCase):

    def test_the_spec_has_one_box_per_joint_plus_the_base(self):
        self.assertEqual(fg.V1.n_joints, 5)
        self.assertEqual(len(fg.V1.boxes), 6)

    def test_the_spec_lookup_falls_back_to_v1(self):
        """An unknown / absent robot type must resolve to the one Feetech arm,
        not to nothing."""
        self.assertIs(fg.spec_for('v1_studio'), fg.V1)
        for bad in (None, '', '   ', 'omx_full', 'garbage'):
            self.assertIs(fg.spec_for(bad), fg.V1)
        self.assertEqual(set(fg.SPECS), {'v1_studio'})

    def test_every_public_function_defaults_to_the_v1_spec(self):
        home = list(_HOME)
        self.assertEqual(fg.lowest_z(home), fg.lowest_z(home, fg.V1))
        self.assertEqual(fg.swept_lowest_z(home, home),
                         fg.swept_lowest_z(home, home, fg.V1))
        self.assertEqual(fg.link_frames(home), fg.link_frames(home, fg.V1))

    def test_a_short_joint_vector_is_unevaluable_per_spec(self):
        """The length check follows the spec, not a hardcoded count."""
        self.assertIsNotNone(fg.lowest_z([0.0] * 5, fg.V1))
        self.assertIsNone(fg.lowest_z([0.0] * 4, fg.V1))


class TestLowestZ(unittest.TestCase):

    def test_home_clears_the_table_by_the_structural_maximum(self):
        """43.7 mm is link1's own box floor (0.0502 m joint origin minus its
        6.5 mm underhang) — it does not depend on the pose at all, so HOME is
        as clear as this arm can be. A different number means the boxes or the
        chain moved."""
        self.assertAlmostEqual(fg.lowest_z(list(_HOME)), 0.0437, places=4)

    def test_the_static_base_is_excluded(self):
        """base_link's underside IS the table (its mesh spans z from 0), so
        counting it would peg every reading at ~0 and the check would judge
        nothing. HOME must therefore read clearly positive."""
        self.assertGreater(fg.lowest_z(list(_HOME)), 0.005)

    def test_a_pose_that_presses_into_the_table_reads_negative(self):
        # Shoulder leaned back with the forearm swung down and forward — the
        # family a limp arm collapses into.
        depth = fg.lowest_z([0.0, 2.6, 0.4, 0.0, 0.0])
        self.assertIsNotNone(depth)
        self.assertLess(depth, 0.0)

    def test_a_short_or_nonfinite_pose_is_unevaluable_not_zero(self):
        """``None`` means UNKNOWN. Returning 0.0 would read as 'exactly at the
        table', which a caller could act on."""
        self.assertIsNone(fg.lowest_z([0.0, 0.0, 0.0]))
        self.assertIsNone(fg.lowest_z([0.0] * 4))
        self.assertIsNone(fg.lowest_z([float('nan')] + [0.0] * 4))
        self.assertIsNone(fg.lowest_z([float('inf')] + [0.0] * 4))

    def test_the_swept_check_catches_a_dip_the_endpoints_miss(self):
        """The whole reason the glide is judged SWEPT rather than at its ends:
        both endpoints are above the table, the line between them is ~225 mm
        under it."""
        self.assertGreater(fg.lowest_z(_DIP_START), 0.0)
        self.assertGreater(fg.lowest_z(list(_HOME)), 0.0)
        self.assertLess(fg.swept_lowest_z(_DIP_START, list(_HOME)), -0.15)

    def test_the_swept_value_is_a_minimum_over_the_line(self):
        swept = fg.swept_lowest_z(_DIP_START, list(_HOME))
        self.assertLessEqual(swept, fg.lowest_z(_DIP_START) + 1e-12)
        self.assertLessEqual(swept, fg.lowest_z(list(_HOME)) + 1e-12)

    def test_home_to_home_is_the_home_clearance(self):
        self.assertAlmostEqual(fg.swept_lowest_z(list(_HOME), list(_HOME)),
                               fg.lowest_z(list(_HOME)), places=9)

    def test_a_big_swing_is_sampled_far_more_finely_than_a_small_one(self):
        """The tunneling argument: a straight JOINT-space move traces ARCS, so
        the sample count is sized by the worst-case arc, not by a fixed number."""
        near = list(_HOME[:4]) + [_HOME[4] + 0.02]
        # a 1-degree roll change must not cost the same as a half-revolution
        self.assertAlmostEqual(fg.swept_lowest_z(list(_HOME), near),
                               min(fg.lowest_z(list(_HOME)), fg.lowest_z(near)),
                               places=4)


def _extract_function(path, name, namespace):
    """Compile ONE module-level function out of a file that cannot be imported
    (``feetech_arm_node`` pulls in rclpy) and bind it in ``namespace``.

    Deliberately the REAL function, not a re-implementation of its docstring: a
    test that restates the contract in its own code proves only that the test
    agrees with itself, and this one gates whether the arm drives into a table.
    """
    tree = ast.parse(_read(path))
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert node is not None, f'{name} not a module-level def in {path}'
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, path, 'exec'), namespace)   # noqa: S102
    return namespace[name]


_DECIDE_NS = {
    'fg': fg,
    'HOME_JOINTS_RAD': _HOME,
    'BOOT_HOME_FLOOR_TOL_M': _module_literal(
        _NODE, '_DEFAULT_BOOT_HOME_FLOOR_TOL_M'),
    # The driver's floor pre-check slices by the arm's joint count and passes
    # its box-model spec.
    'N_ARM_JOINTS': len(_HOME),
    'GEOMETRY_SPEC': fg.V1,
}
_boot_home_floor_decision = _extract_function(
    _NODE, 'boot_home_floor_decision', _DECIDE_NS)


class TestBootHomeFloorDecision(unittest.TestCase):
    """The driver's OWN pure decision function, compiled straight out of
    feetech_arm_node.py (the module imports rclpy, so it cannot be imported)."""

    @staticmethod
    def _decide(current, tol):
        return _boot_home_floor_decision(current, tol)

    def test_the_driver_uses_the_v1_spec_and_its_joint_count(self):
        src = _read(_NODE)
        self.assertIn('GEOMETRY_SPEC = fg.V1', src)
        self.assertIn('N_ARM_JOINTS = len(JOINT_NAMES) - 1', src)

    def test_the_default_tolerance_is_twenty_millimetres(self):
        self.assertEqual(
            _module_literal(_NODE, '_DEFAULT_BOOT_HOME_FLOOR_TOL_M'), 0.020)

    def test_the_default_is_used_when_no_tolerance_is_passed(self):
        self.assertFalse(_boot_home_floor_decision(_DIP_START)[0])

    def test_a_pressing_start_is_refused(self):
        ok, depth = self._decide(_DIP_START, 0.020)
        self.assertFalse(ok)
        self.assertLess(depth, -0.020)

    def test_home_itself_is_allowed(self):
        ok, _depth = self._decide(list(_HOME), 0.020)
        self.assertTrue(ok)

    def test_zero_tolerance_disables_the_check(self):
        """The one-variable rollback: with the knob at 0 even a deep press is
        allowed through."""
        ok, depth = self._decide(_DIP_START, 0.0)
        self.assertTrue(ok)
        self.assertIsNone(depth)

    def test_an_unevaluable_pose_is_allowed_through(self):
        """Fail-OPEN here on purpose: refusing to home an arm whose geometry we
        merely failed to compute would strand a healthy rig, and every other
        boot guard has already passed by this point."""
        ok, depth = self._decide([0.0, 0.0, 0.0], 0.020)
        self.assertTrue(ok)
        self.assertIsNone(depth)


class TestTheRefusalLeavesTheArmTorqued(unittest.TestCase):
    """The single most dangerous way to get this wrong.

    A refusal that de-energises a BACKDRIVING arm converts a guard into a
    collapse. The refusal path must not touch torque at all, and must say so to
    the student.
    """

    def setUp(self):
        self.src = _read(_NODE)
        tree = ast.parse(self.src)
        # start_boot_home was split by the activation gate into boot_energise
        # (torque on, no motion — all that boot does now) and run_home (the
        # glide, reached from /v1/home when a logged-in student presses
        # „Roboter aktivieren"). The floor pre-check guards the GLIDE, so it
        # moved with it; this class follows it rather than the name.
        self.fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == 'run_home'),
            None)
        self.assertIsNotNone(self.fn, 'run_home not found')
        self.composite = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == 'start_boot_home'),
            None)
        self.assertIsNotNone(self.composite, 'start_boot_home not found')

    def test_the_rollback_path_still_goes_through_the_precheck(self):
        """EDUBOTICS_REQUIRE_ACTIVATION=0 restores boot homing, and it must
        restore it THROUGH this guard — not around it."""
        body = ast.get_source_segment(self.src, self.composite)
        self.assertIn('self.boot_energise()', body)
        self.assertIn('self.run_home(', body)
        self.assertNotIn('_replace_trajectory', body)

    def test_the_precheck_runs_before_the_trajectory_is_installed(self):
        body = ast.get_source_segment(self.src, self.fn)
        self.assertIn('boot_home_floor_decision', body)
        self.assertLess(body.index('boot_home_floor_decision'),
                        body.index('_replace_trajectory'),
                        'the pre-check must gate the glide, not follow it')

    def test_the_refusal_branch_never_switches_torque_off(self):
        body = ast.get_source_segment(self.src, self.fn)
        after = body[body.index('boot_home_floor_decision'):]
        refusal = after[:after.index('_replace_trajectory')]
        self.assertNotIn('set_torque(False)', refusal)
        self.assertNotIn('_torque_off', refusal)
        self.assertNotIn('TORQUE_OFF', refusal)

    def test_the_german_message_says_the_arm_is_held_and_names_hand_guiding(self):
        msg = _module_literal(_NODE, 'BOOT_HOME_FLOOR_REFUSAL_DE')
        self.assertIn('NICHT weich', msg)
        self.assertIn('Handführung', msg)
        self.assertIn('Tischplatte', msg)
        self.assertTrue(msg.startswith('[FEHLER]'))
        # literal umlauts, never transliterations (ci.yml::german-strings-lint)
        for bad in ('ue', 'ae', 'oe'):
            self.assertNotIn(bad, msg.replace('neu', '').replace('erneut', ''))


class TestPackaging(unittest.TestCase):
    """A plain-COPY driver file only reaches the image if it is registered in
    three places. Missing one ships an ImportError at boot."""

    def test_the_dockerfile_copies_strips_and_chmods_it(self):
        src = _read(os.path.join(_DRIVER_DIR, 'Dockerfile'))
        self.assertIn(
            'COPY feetech_geometry.py /usr/local/bin/feetech_geometry.py', src)
        strip_and_chmod = [line for line in src.splitlines()
                           if '/usr/local/bin/feetech_arm_node.py' in line
                           and 'COPY' not in line]
        self.assertTrue(strip_and_chmod)
        for line in strip_and_chmod:
            self.assertIn('/usr/local/bin/feetech_geometry.py', line)

    def test_image_source_parity_byte_verifies_it(self):
        src = _read(os.path.join(_REPO, '.github', 'scripts',
                                 'image_source_parity.sh'))
        self.assertIn('feetech_geometry.py', src)

    def test_the_knob_is_forwarded_on_both_composes(self):
        """ci.yml::env-forwarding-guard unions four compose files, so the
        student compose alone would pass while the Pi silently lacks the knob."""
        for name in ('docker-compose.yml', 'docker-compose.opi.yml'):
            path = os.path.join(_REPO, 'robotis_ai_setup', 'docker', name)
            src = _read(path)
            self.assertIn('EDUBOTICS_HOME_FLOOR_TOL_M=', src, name)


if __name__ == '__main__':
    unittest.main()
