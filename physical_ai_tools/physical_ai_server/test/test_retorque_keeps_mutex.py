"""H1 regression — touch-off re-torque must keep the calibration mutex HELD
when it cannot re-assert follower torque.

The touch-off step torques the follower OFF for hand-guiding. On every
finish/cancel path the node calls ``_retorque_follower_or_keep_locked`` before
releasing ``on_calibration``. If the re-torque persistently fails the arm is
still LIMP, so the method must NOT signal "release the mutex" — otherwise a
subsequent workflow/recording/inference start drives a limp follower (the
arm_controller writes Goal Position but Torque Enable is 0 → the servos ignore
it and slump under gravity). Nothing else re-asserts follower torque before
motion (the Rule §2 collision monitor is inference-gated, off the Roboter-Studio
path).

``physical_ai_server.py`` imports rclpy/ROS and cannot be imported in CI, so we
extract the REAL ``_retorque_follower_or_keep_locked`` source (by name, via
``ast``) and exec just that function onto a stub ``self``. This tests the
literal shipped method with no ROS deps and no frankenstein whole-module import.
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
    """Return the named method of PhysicalAIServer as a standalone function,
    extracted verbatim from the source file (no module import)."""
    tree = ast.parse(_SERVER_PY.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(
                _SERVER_PY.read_text(encoding='utf-8'), node))
            ns: dict = {}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'method {name} not found in {_SERVER_PY}')


class _Logger:
    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Response:
    success = True
    message = ''


class _StubNode:
    """Minimal self for the extracted method: a scripted _set_follower_torque +
    a no-op logger."""

    def __init__(self, results):
        # results: list of bools the torque switch returns on successive calls.
        self._results = list(results)
        self.calls = 0
        self.on_calibration = True  # mutex is held going in

    def _set_follower_torque(self, enabled):
        assert enabled is True, 'the helper only ever re-torques (True)'
        self.calls += 1
        # Mirrors the real method: the reason stash is (re)written on every call,
        # and stays EMPTY unless the service answered with a non-empty message.
        self._follower_torque_last_message = getattr(self, 'next_reason', '')
        return self._results.pop(0) if self._results else False

    def get_logger(self):
        return _Logger()


_retorque = _load_method('_retorque_follower_or_keep_locked')
# F8: the German failure message now appends the DRIVER's own reason via this
# helper — extracted verbatim so these tests exercise the shipped code.
_StubNode._last_torque_reason = _load_method('_last_torque_reason')


def test_retorque_success_first_try_returns_true():
    node = _StubNode([True])
    resp = _Response()
    ok = _retorque(node, resp)
    assert ok is True
    assert node.calls == 1


def test_retorque_succeeds_within_bounded_retries():
    node = _StubNode([False, False, True])  # third attempt succeeds
    resp = _Response()
    ok = _retorque(node, resp)
    assert ok is True
    assert node.calls == 3


def test_retorque_persistent_failure_keeps_mutex_and_fails_loud():
    node = _StubNode([False, False, False])
    resp = _Response()
    ok = _retorque(node, resp)
    assert ok is False
    # The caller uses this False to SKIP _set_calibration_active(False), so the
    # mutex stays held. The method itself never flips on_calibration.
    assert node.on_calibration is True
    assert resp.success is False
    # German fail-loud, literal umlauts (Rule §1), and bounded to 3 attempts.
    assert 'verriegelt' in resp.message
    assert 'ä' in resp.message or 'ü' in resp.message or 'ö' in resp.message
    assert node.calls == 3


def test_retorque_never_exceeds_three_attempts():
    node = _StubNode([False] * 10)
    resp = _Response()
    _retorque(node, resp)
    assert node.calls == 3


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
