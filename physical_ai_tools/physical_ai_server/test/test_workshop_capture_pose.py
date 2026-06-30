"""Phase-2 "Position merken" — the /workshop/capture_pose service callback.

Captures the follower arm's CURRENT base-frame gripper position (forward
kinematics) and persists it as a named destination — the hand-guide
counterpart to ``mark_destination_callback``'s pixel-click pinning.

``physical_ai_server.py`` imports rclpy/ROS and cannot be imported in CI, so
we extract the REAL ``capture_pose_callback`` source (by name, via ``ast``) and
exec just that function onto a stub ``self`` — the same deps-free pattern as
``test_retorque_keeps_mutex.py``. The WorkflowManager used for the
persistence assertion is the REAL class (constructed via ``__new__`` so no ROS
deps are touched), so ``set_destination`` / ``get_destinations`` are the shipped
methods, not a re-implementation.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from physical_ai_server.workflow.workflow_manager import WorkflowManager

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load_method(name: str):
    """Return the named method of PhysicalAIServer as a standalone function,
    extracted verbatim from the source file (no module import)."""
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns: dict = {}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'method {name} not found in {_SERVER_PY}')


_capture_pose = _load_method('capture_pose_callback')


class _Logger:
    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


class _Response:
    """Mirror of the WorkshopCapturePose response — every field the callback
    is required to populate on every return path."""

    def __init__(self):
        self.success = None
        self.world_x = None
        self.world_y = None
        self.world_z = None
        self.message = None


class _Request:
    def __init__(self, name):
        self.name = name


def _real_wfm():
    """A real WorkflowManager with just the destination store wired up — no
    ROS publisher / IK / perception needed for set_destination/get_destinations."""
    wfm = WorkflowManager.__new__(WorkflowManager)
    wfm._persisted_destinations = {}
    return wfm


class _StubNode:
    """Minimal `self` for the extracted callback."""

    def __init__(self, *, gate=(True, ''), communicator=object(), xyz=(0.0, 0.0, 0.0),
                 wfm=None):
        self._gate = gate
        self.communicator = communicator
        self._xyz = xyz
        self._wfm = wfm if wfm is not None else _real_wfm()
        self.gate_modes = []  # records every requested_mode the callback passed

    def _assert_no_other_active(self, requested_mode):
        self.gate_modes.append(requested_mode)
        return self._gate

    def _get_current_gripper_xyz(self):
        return self._xyz

    def _get_or_create_workflow_manager(self):
        return self._wfm

    def get_logger(self):
        return _Logger()


def _assert_zeros(resp):
    assert resp.world_x == 0.0
    assert resp.world_y == 0.0
    assert resp.world_z == 0.0


# ---------------------------------------------------------------------------
# Gate: refuse while another mode owns the arm.
# ---------------------------------------------------------------------------

def test_busy_refuses_with_german_message():
    node = _StubNode(gate=(False, 'Aufnahme läuft gerade — bitte zuerst stoppen.'))
    resp = _capture_pose(node, _Request('Ablage'), _Response())
    assert resp.success is False
    assert resp.message == 'Aufnahme läuft gerade — bitte zuerst stoppen.'
    _assert_zeros(resp)
    # The gate is consulted with the 'manual' mode (refuses on every owner).
    assert node.gate_modes == ['manual']


# ---------------------------------------------------------------------------
# Name validation (reuses _DESTINATION_NAME_RE).
# ---------------------------------------------------------------------------

def test_empty_name_rejected():
    node = _StubNode()
    resp = _capture_pose(node, _Request(''), _Response())
    assert resp.success is False
    assert 'ältig' in resp.message or 'Ungültig' in resp.message
    _assert_zeros(resp)


def test_whitespace_only_name_rejected():
    node = _StubNode()
    resp = _capture_pose(node, _Request('   '), _Response())
    assert resp.success is False
    _assert_zeros(resp)


def test_invalid_char_name_rejected():
    # '*' is outside the canonical destination alphabet.
    node = _StubNode()
    resp = _capture_pose(node, _Request('Ablage*'), _Response())
    assert resp.success is False
    assert 'Ungültiger Ziel-Name.' == resp.message
    _assert_zeros(resp)


def test_newline_injection_name_rejected():
    # A control-char / log-spoofing name must be refused before anything is stored.
    node = _StubNode()
    resp = _capture_pose(node, _Request('A\n[FEHLER] x'), _Response())
    assert resp.success is False
    _assert_zeros(resp)
    assert node._wfm.get_destinations() == {}


def test_umlaut_name_accepted():
    node = _StubNode(xyz=(0.1, 0.2, 0.3))
    resp = _capture_pose(node, _Request('Ablage über Tisch'), _Response())
    assert resp.success is True
    assert 'Ablage über Tisch' in node._wfm.get_destinations()


# ---------------------------------------------------------------------------
# FK-None handling: communicator missing vs. pose unknown — distinct messages.
# ---------------------------------------------------------------------------

def test_no_communicator_distinct_message():
    node = _StubNode(communicator=None)
    resp = _capture_pose(node, _Request('Ablage'), _Response())
    assert resp.success is False
    # German "pick the robot first" hint, distinct from "position unknown".
    assert 'auswählen' in resp.message
    _assert_zeros(resp)
    assert node._wfm.get_destinations() == {}


def test_pose_unknown_message():
    node = _StubNode(communicator=object(), xyz=None)
    resp = _capture_pose(node, _Request('Ablage'), _Response())
    assert resp.success is False
    assert 'unbekannt' in resp.message
    _assert_zeros(resp)
    assert node._wfm.get_destinations() == {}


# ---------------------------------------------------------------------------
# Happy path: persisted + retrievable via the REAL set_destination/get_destinations,
# FK Z stored verbatim (NOT snapped to z_table).
# ---------------------------------------------------------------------------

def test_capture_persists_and_is_retrievable():
    node = _StubNode(xyz=(0.234, -0.012, 0.187))
    resp = _capture_pose(node, _Request('Ablage'), _Response())
    assert resp.success is True
    assert resp.world_x == pytest.approx(0.234)
    assert resp.world_y == pytest.approx(-0.012)
    assert resp.world_z == pytest.approx(0.187)
    assert 'Ablage' in resp.message
    assert 'gespeichert' in resp.message
    # Retrievable through the WorkflowManager's own getter.
    stored = node._wfm.get_destinations()
    assert stored['Ablage'] == {
        'x': pytest.approx(0.234),
        'y': pytest.approx(-0.012),
        'z': pytest.approx(0.187),
        'label': 'Ablage',
    }


def test_fk_z_stored_verbatim_not_snapped():
    # A height well above any plausible z_table proves the callback never
    # snaps the FK Z to the measured grasp height (product decision).
    node = _StubNode(xyz=(0.05, 0.05, 0.42))
    resp = _capture_pose(node, _Request('Hoch'), _Response())
    assert resp.success is True
    assert resp.world_z == pytest.approx(0.42)
    assert node._wfm.get_destinations()['Hoch']['z'] == pytest.approx(0.42)


def test_persist_failure_fails_loud():
    class _BoomWfm:
        def set_destination(self, *a, **k):
            raise RuntimeError('disk full')

        def get_destinations(self):
            return {}

    node = _StubNode(xyz=(0.1, 0.2, 0.3), wfm=_BoomWfm())
    resp = _capture_pose(node, _Request('Ablage'), _Response())
    # Persistence IS the operation here — a store failure must NOT report success.
    assert resp.success is False
    assert resp.message == 'Position konnte nicht gespeichert werden.'
    _assert_zeros(resp)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
