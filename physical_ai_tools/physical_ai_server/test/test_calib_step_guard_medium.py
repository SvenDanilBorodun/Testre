"""MEDIUM ride-along — calibration_start_callback must set _active_calib_step
to 'table_touch' ONLY after start_table_touch() succeeds.

A FAILED table_touch start (e.g. the scene extrinsic isn't calibrated yet)
never torques the follower off. If _active_calib_step were left at
'table_touch' on that failure, the next /calibration/start or /calibration/cancel
would see it and wrongly fire the re-torque path (_retorque_follower_or_keep_locked)
on an arm that was never made limp.

physical_ai_server.py imports rclpy/ROS and cannot be imported in CI, so we
extract the REAL calibration_start_callback source (by name, via ast) and exec
it onto a stub self — the same approach as test_retorque_keeps_mutex.py.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load_method(name: str):
    text = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(text, node))
            ns: dict = {}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'method {name} not found in {_SERVER_PY}')


_callback = _load_method('calibration_start_callback')


class _Logger:
    def error(self, *a, **k):
        pass

    warning = error
    info = error


class _Response:
    def __init__(self):
        self.success = None
        self.message = ''
        self.reprojection_error = 0.0
        self.method_disagreement = 0.0


class _Manager:
    def __init__(self, touch_ok=True, step_ok=True):
        self._touch_ok = touch_ok
        self._step_ok = step_ok

    def start_table_touch(self):
        return (self._touch_ok,
                'Tischvermessung gestartet.' if self._touch_ok
                else 'Bitte zuerst die Szenen-Kamera ausrichten (Extrinsik).')

    def start_step(self, camera, step):
        return (self._step_ok, 'Schritt gestartet.' if self._step_ok else 'Fehler.')


class _Request:
    def __init__(self, step, camera='scene'):
        self.step = step
        self.camera = camera


class _Event:
    def clear(self):
        pass


class _StubNode:
    """Minimal self for the extracted calibration_start_callback."""

    def __init__(self, manager, no_other_active=True, torque_off_ok=True):
        self._manager = manager
        self._no_other_active = no_other_active
        self._torque_off_ok = torque_off_ok
        self._active_calib_step = None
        self._calibration_stop_event = _Event()
        self.calibration_active_calls = []
        self.retorque_calls = 0

    # --- collaborators the callback calls ---
    def _assert_no_other_active(self, mode):
        return (self._no_other_active,
                '' if self._no_other_active else 'Es läuft bereits etwas.')

    def _get_or_create_calibration_manager(self):
        return self._manager

    def _retorque_follower_or_keep_locked(self, response):
        self.retorque_calls += 1
        return True

    def _set_follower_torque(self, enabled):
        return self._torque_off_ok

    def _set_calibration_active(self, value):
        self.calibration_active_calls.append(value)

    def get_logger(self):
        return _Logger()


def test_failed_table_touch_start_leaves_step_unset():
    node = _StubNode(_Manager(touch_ok=False))
    resp = _Response()
    _callback(node, _Request('table_touch'), resp)
    assert resp.success is False
    # The crux: a failed start must NOT leave _active_calib_step armed.
    assert node._active_calib_step is None
    # And torque was never disabled / calibration never marked active.
    assert node.calibration_active_calls == []


def test_successful_table_touch_start_sets_step():
    node = _StubNode(_Manager(touch_ok=True))
    resp = _Response()
    _callback(node, _Request('table_touch'), resp)
    assert resp.success is True
    assert node._active_calib_step == 'table_touch'
    assert node.calibration_active_calls == [True]


def test_failed_touch_then_other_step_does_not_retorque():
    # Simulate the real bug sequence: a failed table_touch start, then the
    # student starts a different step. Because the step was left UNSET, the
    # re-torque path (which only fires when leaving an active table_touch) must
    # NOT run.
    node = _StubNode(_Manager(touch_ok=False))
    _callback(node, _Request('table_touch'), _Response())
    assert node._active_calib_step is None
    # Now an intrinsic step.
    node._manager = _Manager(step_ok=True)
    resp2 = _Response()
    _callback(node, _Request('intrinsic'), resp2)
    assert resp2.success is True
    assert node.retorque_calls == 0
    assert node._active_calib_step == 'intrinsic'


def test_non_touch_step_sets_step_and_marks_active():
    node = _StubNode(_Manager(step_ok=True))
    resp = _Response()
    _callback(node, _Request('handeye'), resp)
    assert resp.success is True
    assert node._active_calib_step == 'handeye'
    assert node.calibration_active_calls == [True]


def test_successful_touch_then_other_step_does_retorque():
    # Counter-check: a SUCCESSFUL table_touch (arm now limp) followed by a
    # different step MUST fire the re-torque path.
    node = _StubNode(_Manager(touch_ok=True))
    _callback(node, _Request('table_touch'), _Response())
    assert node._active_calib_step == 'table_touch'
    node._manager = _Manager(step_ok=True)
    _callback(node, _Request('intrinsic'), _Response())
    assert node.retorque_calls == 1


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
