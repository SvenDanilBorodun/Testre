#!/usr/bin/env python3
"""``mark_destination_callback`` driven for real — the pixel click that creates
a destination.

Audit `docs/plans/audit-G10-G12.md` §7.6 / §8.1: of the writers into
``_persisted_destinations`` this one validated NOTHING — it stored
``request.label`` verbatim and handed ``corr_x`` / ``corr_y`` to
``set_destination`` with no finiteness check, then reported „gespeichert." — and
its persist sat inside a bare ``except Exception: pass`` so any refusal was
invisible.

``physical_ai_server.py`` needs rclpy, so the callback is extracted by name with
``ast`` and exec'd onto a stub ``self`` — the deps-free pattern
``test_workshop_capture_pose.py`` established. Everything BELOW the callback is
real: a real ``cv2.FileStorage`` handeye YAML on disk, the real
``project_pixel_to_table``, the real ``motion.table_z_at`` and the real
``WorkflowManager``.
"""

from __future__ import annotations

import ast
import math
import textwrap
from pathlib import Path

import cv2
import numpy as np
import pytest

from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowManager

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load_method(name: str):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns: dict = {'math': math}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'method {name} not found in {_SERVER_PY}')


_mark_destination = _load_method('mark_destination_callback')

# Camera 0.5 m above the table at base (0.20, 0.00), looking straight down:
# the third column of R is the camera's +z in base coordinates.
_K = np.array([[600.0, 0.0, 320.0], [0.0, 600.0, 240.0], [0.0, 0.0, 1.0]])
_DIST = np.zeros((1, 5))
_T = np.array([
    [1.0, 0.0, 0.0, 0.20],
    [0.0, -1.0, 0.0, 0.00],
    [0.0, 0.0, -1.0, 0.50],
    [0.0, 0.0, 0.0, 1.0],
])
# The principal point therefore back-projects to base (0.20, 0.00).
_CENTRE_PX = (320, 240)


class _Logger:
    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


class _Response:
    def __init__(self):
        self.success = None
        self.world_x = None
        self.world_y = None
        self.world_z = None
        self.message = None


class _Request:
    def __init__(self, label, px=_CENTRE_PX[0], py=_CENTRE_PX[1], camera='scene'):
        self.label = label
        self.pixel_x = px
        self.pixel_y = py
        self.camera = camera


class _CalibManager:
    def __init__(self, path: Path):
        self._path = path
        self._intrinsics = {'scene': {'K': _K, 'dist': _DIST}}

    def has_intrinsics(self, camera):
        return camera in self._intrinsics

    def _handeye_path(self, camera):
        return self._path

    def read_verify_correction(self, camera):
        return None


class _Node:
    def __init__(self, path: Path, wfm: WorkflowManager | None):
        self._calib = _CalibManager(path)
        self._wfm = wfm

    def _get_or_create_calibration_manager(self):
        return self._calib

    def _get_or_create_workflow_manager(self):
        return self._wfm

    def get_logger(self):
        return _Logger()


def _write_handeye(tmp_path: Path, z_table: float,
                   table_plane: tuple[float, float, float] | None = None,
                   board_table_z: float | None = None) -> Path:
    path = tmp_path / 'scene_handeye.yaml'
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
    fs.write('transform', _T)
    fs.write('z_table', float(z_table))
    fs.write('board_table_z',
             float(board_table_z if board_table_z is not None else z_table))
    if table_plane is not None:
        fs.write('table_plane', np.array([float(v) for v in table_plane]))
    fs.release()
    return path


def _wfm() -> WorkflowManager:
    return WorkflowManager(publisher=lambda _p: None, load_destinations=lambda: {})


# ── the happy path still works end to end ───────────────────────────────────

def test_a_clean_click_projects_persists_and_reports_success(tmp_path):
    wfm = _wfm()
    node = _Node(_write_handeye(tmp_path, 0.0125), wfm)
    resp = _mark_destination(node, _Request('Ablage'), _Response())
    assert resp.success is True
    assert 'gespeichert' in resp.message
    assert resp.world_x == pytest.approx(0.20, abs=1e-6)
    assert resp.world_y == pytest.approx(0.00, abs=1e-6)
    assert resp.world_z == pytest.approx(0.0125, abs=1e-9)
    stored = wfm.get_destinations()['Ablage']
    assert stored['x'] == pytest.approx(0.20, abs=1e-6)
    assert stored['z'] == pytest.approx(0.0125, abs=1e-9)


# ── §7.6 — the name is now fenced, and the refusal is REPORTED ──────────────

@pytest.mark.parametrize('label', ['A!', 'A/B', '日本', 'A\nB', 'A]B', 'x' * 41,
                                   '   '])
def test_a_bad_label_is_refused_in_german_and_nothing_is_stored(tmp_path, label):
    wfm = _wfm()
    node = _Node(_write_handeye(tmp_path, 0.0125), wfm)
    resp = _mark_destination(node, _Request(label), _Response())
    assert resp.success is False, label
    assert 'Ungültiger Ziel-Name' in resp.message
    assert 'Bindestrich' in resp.message
    assert wfm.get_destinations() == {}
    # The refusal must not echo a log sentinel or a line break back at the UI.
    assert '[' not in resp.message and ']' not in resp.message
    assert '\n' not in resp.message


def test_the_refusal_zeroes_the_coordinates_so_no_client_uses_them(tmp_path):
    node = _Node(_write_handeye(tmp_path, 0.0125), _wfm())
    resp = _mark_destination(node, _Request('A!'), _Response())
    assert (resp.world_x, resp.world_y, resp.world_z) == (0.0, 0.0, 0.0)


def test_a_padded_label_is_stored_under_the_trimmed_key(tmp_path):
    """React's field validator trims, so the block carries 'Ablage'."""
    wfm = _wfm()
    node = _Node(_write_handeye(tmp_path, 0.0125), wfm)
    resp = _mark_destination(node, _Request('  Ablage  '), _Response())
    assert resp.success is True
    assert list(wfm.get_destinations()) == ['Ablage']


# ── §8.1 — a non-finite coordinate can no longer be stored as a success ─────

def test_a_non_finite_projection_is_refused_rather_than_stored(tmp_path):
    """No ordinary trigger — `project_pixel_to_table` returns None, not NaN, on
    every degenerate pixel the audit could drive. It needs a NaN-bearing
    calibration YAML, which is exactly the defence-in-depth case: a corrupt
    `transform` used to reach `set_destination` and be reported as saved."""
    bad_T = _T.copy()
    bad_T[0, 3] = float('nan')
    path = tmp_path / 'scene_handeye.yaml'
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
    fs.write('transform', bad_T)
    fs.write('z_table', 0.0125)
    fs.write('board_table_z', 0.0125)
    fs.release()
    wfm = _wfm()
    node = _Node(path, wfm)
    resp = _mark_destination(node, _Request('Ablage'), _Response())
    assert resp.success is False
    assert 'Koordinaten' in resp.message
    assert wfm.get_destinations() == {}


# ── the pre-existing refusals are untouched ────────────────────────────────

def test_an_unmeasured_table_still_refuses_with_the_touch_off_sentence(tmp_path):
    path = tmp_path / 'scene_handeye.yaml'
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
    fs.write('transform', _T)
    fs.release()
    node = _Node(path, _wfm())
    resp = _mark_destination(node, _Request('Ablage'), _Response())
    assert resp.success is False
    assert 'Tisch vermessen' in resp.message


def test_a_missing_workflow_manager_stays_best_effort(tmp_path):
    """The projection IS the primary output; only the persist is a bonus."""
    node = _Node(_write_handeye(tmp_path, 0.0125), None)
    resp = _mark_destination(node, _Request('Ablage'), _Response())
    assert resp.success is True
    assert resp.world_z == pytest.approx(0.0125, abs=1e-9)


# ── the click is PLANE-TRACKED, so a later touch-off moves it ──────────────

def test_the_click_is_recorded_as_a_reading_off_the_plane_not_a_measurement(
        tmp_path):
    """A camera click is a point ON THE TABLE: its z was read off the plane the
    touch-off had drawn, so it is a cached answer, not a measurement. „Position
    merken" is the opposite and stays verbatim."""
    wfm = _wfm()
    node = _Node(_write_handeye(tmp_path, 0.0125), wfm)
    _mark_destination(node, _Request('Ablage'), _Response())
    assert wfm.get_destinations()['Ablage']['plane_tracked'] is True


def test_a_pin_clicked_before_a_re_touch_off_follows_the_new_plane(tmp_path):
    """The behavioural half of the flag, and the whole point of §9.2(a):
    `_persisted_destinations` is written only by `set_destination` and never
    cleared, while EDUBOTICS_FORCE_RECALIBRATION re-runs „Tisch vermessen" every
    lesson. The click below happened on a table believed level."""
    wfm = _wfm()
    node = _Node(_write_handeye(tmp_path, 0.0), wfm)
    resp = _mark_destination(node, _Request('Ablage'), _Response())
    assert resp.success is True
    stored = wfm.get_destinations()['Ablage']
    assert stored['z'] == pytest.approx(0.0, abs=1e-9)

    # Next lesson: the touch-off finds the table tilted 11 deg about +x with
    # z_table 0 at the tap centroid (0.10, 0). The pin sits at x = 0.20.
    a = math.tan(math.radians(11.0))
    later = _Ctx(table_plane=(a, 0.0, -a * 0.10), z_table=0.0)
    resolved_mm = M.resolve_destination_z(later, stored) * 1000.0
    assert resolved_mm == pytest.approx(19.44, abs=0.02), resolved_mm


class _Ctx:
    def __init__(self, table_plane, z_table):
        self.table_plane = table_plane
        self.z_table = z_table
