"""Vormachen robot preview: the client's drive-window numbers vs the server.

``teach/teachGates.js`` keeps the Vormachen overlay in its ``vorschau`` state
(teaching keys ignored) until the follower is seen to stop AND at least
``replayDriveEstimateMs(rows)`` has passed since the first motion. That estimate
mirrors ``handlers/trajectory.py::resegment_trajectory`` at the SLOWEST shipped
velocity limit, and the no-motion hint mirrors the pre-drive chain of
``_run_replay``. None of those server numbers can be imported by the SPA, so a
server change that lengthens a drive would silently shorten the client's window
over a moving arm. This test reads TEXT only (the server package pulls in
rclpy/NumPy and must never be imported here) and fails on exactly that drift.
"""

import ast
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MANAGER = _ROOT / 'physical_ai_tools' / 'physical_ai_manager'
_GATES_JS = _MANAGER / 'src' / 'components' / 'Workshop' / 'teach' / 'teachGates.js'
_SERVER_PKG = _ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
_PROFILES = _SERVER_PKG / 'robot_profiles.py'
_BUILDER = _SERVER_PKG / 'workflow' / 'trajectory_builder.py'
_TRAJECTORY = _SERVER_PKG / 'workflow' / 'handlers' / 'trajectory.py'
_SERVER = _SERVER_PKG / 'physical_ai_server.py'


def _text(path):
    return path.read_text(encoding='utf-8')


def _js_number(name):
    match = re.search(
        r'^export const ' + re.escape(name) + r'\s*=\s*([0-9][0-9_.]*)\s*;',
        _text(_GATES_JS), re.MULTILINE)
    if match is None:
        raise AssertionError(f'{name} not found as a numeric literal in {_GATES_JS}')
    return float(match.group(1).replace('_', ''))


def _py_module_constant(path, name):
    """A module-level ``NAME = <literal expression>`` evaluated by ast."""
    tree = ast.parse(_text(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return eval(compile(ast.Expression(node.value), str(path), 'eval'))  # noqa: S307
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == name and node.value is not None):
            return eval(compile(ast.Expression(node.value), str(path), 'eval'))  # noqa: S307
    raise AssertionError(f'{name} not found at module level in {path}')


def _server_method(name):
    source = _text(_SERVER)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f'method {name} not found in {_SERVER}')


class TeachPreviewVelocityFloorTest(unittest.TestCase):

    def test_client_floor_is_at_most_every_profile_velocity_limit(self):
        floor = _js_number('TEACH_REPLAY_VELOCITY_FLOOR_RAD_S')
        limits = []
        for node in ast.walk(ast.parse(_text(_PROFILES))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == 'velocity_limit_rad_s' and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, (int, float)):
                        limits.append(float(kw.value.value))
        self.assertTrue(limits, 'no literal velocity_limit_rad_s= kwarg found')
        for limit in limits:
            self.assertLessEqual(floor, limit)
        default = re.search(r'^JOINT_VELOCITY_LIMIT_RAD_S\s*=\s*([0-9.]+)',
                            _text(_BUILDER), re.MULTILINE)
        self.assertIsNotNone(default)
        self.assertLessEqual(floor, float(default.group(1)))

    def test_the_resegment_constants_the_estimate_mirrors(self):
        self.assertEqual(_py_module_constant(_BUILDER, '_VELOCITY_SAFETY_FRACTION'), 0.6)
        self.assertEqual(_py_module_constant(_BUILDER, '_QUINTIC_PEAK_VELOCITY_FACTOR'), 15 / 8)
        self.assertEqual(_py_module_constant(_BUILDER, 'DEFAULT_CHUNK_DURATION_S'), 1.0)
        self.assertEqual(_py_module_constant(_TRAJECTORY, 'DEFAULT_LEAD_IN_S'), 1.5)
        self.assertEqual(_py_module_constant(_TRAJECTORY, '_MIN_PAIR_DT_S'), 0.001)

    def test_the_client_record_cap_mirrors_the_server(self):
        self.assertEqual(_js_number('TEACH_RECORD_MAX_S'),
                         _py_module_constant(_SERVER, 'RECORD_MAX_S'))

    def test_the_lead_in_bound_covers_a_full_turn_at_the_floor(self):
        floor = _js_number('TEACH_REPLAY_VELOCITY_FLOOR_RAD_S')
        lead_in_ms = _js_number('TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS')
        worst_s = max(1.5, 2 * 3.141592653589793 * (15 / 8) / (0.6 * floor))
        self.assertGreater(lead_in_ms, 1000 * worst_s)

    def test_the_no_motion_hint_covers_the_pre_drive_chain(self):
        self.assertIn('self._manual_lock.acquire(timeout=30.0)', _server_method('_run_replay'))
        self.assertIn('for _ in range(3):', _server_method('_retorque_follower_or_keep_locked'))
        torque = _server_method('_set_follower_torque')
        self.assertIn('wait_for_service(timeout_sec=2.0)', torque)
        self.assertIn('time.monotonic() + 3.0', torque)
        settle = _py_module_constant(_SERVER, '_TORQUE_ON_HOLD_SETTLE_S')
        hold_t = _py_module_constant(_SERVER, '_TORQUE_ON_HOLD_TIME_FROM_START_S')
        margin = _py_module_constant(_SERVER, '_TORQUE_ON_HOLD_CYCLE_MARGIN_S')
        match_wait = _py_module_constant(_SERVER, '_COMMAND_RAIL_MATCH_WAIT_S')
        self.assertEqual(settle, 0.2)
        self.assertEqual(hold_t, 0.05)
        self.assertEqual(margin, 0.02)
        self.assertEqual(match_wait, 1.0)
        chain_ms = 1000 * (30 + 3 * (2 + 2 * match_wait + settle + hold_t + margin + 3))
        self.assertAlmostEqual(chain_ms, 51810.0)
        self.assertGreaterEqual(_js_number('TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS'), chain_ms)

    def test_stopp_bumps_the_exit_generation_before_waiting_for_the_lock(self):
        # „Stopp" in `vorschau` is hand_guide(false): it must bump _manual_exit_gen
        # BEFORE it waits for _manual_lock, so a replay that has not started yet
        # re-checks the generation and aborts without driving.
        source = _server_method('workshop_hand_guide_callback')
        bump = source.find('self._manual_exit_gen += 1')
        wait = source.find('self._manual_lock.acquire(timeout=6.0)')
        self.assertGreaterEqual(bump, 0)
        self.assertGreaterEqual(wait, 0)
        self.assertLess(bump, wait)


if __name__ == '__main__':
    unittest.main()
