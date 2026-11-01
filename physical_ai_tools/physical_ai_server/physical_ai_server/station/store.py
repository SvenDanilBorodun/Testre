#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The one atomic station YAML (plan v6.13 §6.5): read, validate, write, restore.

``BASE = $EDUBOTICS_CALIB_DIR`` (default ``/root/.cache/edubotics/calibration``)::

    BASE/station/station.yaml        the one active station calibration
    BASE/station/station.prev.yaml   the previous valid one (explicit restore only)
    BASE/station/evidence/<id>/      captured JPEGs + report.json; newest 3 kept
    BASE/station/.staging-<id>/      a commission's staging dir (deleted at boot)

OpenCV ``FileStorage`` YAML, the format of the scene YAMLs. Lengths in metres.
Nothing is ever used partially: an invalid file means "station rig, not
configured" with the German reason.
"""

import collections
import datetime
import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path

import numpy as np

from physical_ai_server.station import layout_data
from physical_ai_server.station import texts_de as T

SCHEMA = 6
LAYOUT_ID_RE = re.compile(r'^[A-Z0-9-]{1,32}$')
ORTHO_TOL = 1e-6
EVIDENCE_KEEP = 3

StationCalibration = collections.namedtuple(
    'StationCalibration', 'digest calibration polygon_base fields layout_id')

# field → expected numpy shape (None: a scalar); strings handled apart.
_MATRICES = {
    'ref_centres': (4, 2), 'ref_corners': (16, 2), 'ref_corners_commissioned': (16, 2),
    'dots': (4, 2), 'camera_matrix': (3, 3), 'distortion_coefficients': (1, 8),
    'T_cam_mat': (4, 4), 'T_cam_mat_commissioned': (4, 4), 'T_cam_base': (4, 4),
    'dot_tips': (4, 3),
}
_SCALARS = ('schema', 'image_width', 'image_height', 'lens_fit_rms_px',
            'lens_heldout_rms_px', 'board_table_z', 'reg_theta_rad', 'reg_tx_m',
            'reg_ty_m', 'z_table', 'table_a', 'table_b', 'table_c', 'world_rms_mm',
            'world_max_mm')
_STRINGS = ('layout_id', 'layout_sha256', 'commission_id', 'committed_at', 'robot_profile')
_VARIABLE = ('ref_ids', 'marks', 'zone_polygon')
_OPTIONAL_STRINGS = ('camera_resolved_at',)


def calib_base():
    return Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'))


def station_dir(base=None):
    return Path(base if base is not None else calib_base()) / 'station'


def yaml_path(base=None):
    return station_dir(base) / 'station.yaml'


def prev_path(base=None):
    return station_dir(base) / 'station.prev.yaml'


def staging_dir(commission_id, base=None):
    return station_dir(base) / f'.staging-{commission_id}'


# ── layouts ───────────────────────────────────────────────────────────────────
def layout_entry(layout_id, layouts=None):
    layouts = layout_data.LAYOUTS if layouts is None else layouts
    return layouts.get(layout_id)


def is_released(layout):
    if not layout or layout.get('layout_state') != 'released':
        return False
    poly = (layout.get('zone') or {}).get('accepted_polygon_mm') or []
    try:
        return len(poly) >= 3 and all(
            len(v) == 2 and all(math.isfinite(float(c)) for c in v) for v in poly)
    except (TypeError, ValueError):
        return False


def released_layout(layout_id, layouts=None):
    """The layout if it names a released one, else None (§6 Layout)."""
    if not isinstance(layout_id, str) or not LAYOUT_ID_RE.match(layout_id):
        return None
    layout = layout_entry(layout_id, layouts)
    return layout if is_released(layout) else None


def board_to_mat(layout):
    """4×4 board→mat transform from the layout: columns axis_x, axis_y, x×y;
    origin in metres. The manual rig's _board_to_base_transform with the same
    numbers gives the same matrix (Rx(180°) at (0.18, 0.075, 0))."""
    b = layout['board']
    ax = np.asarray(b['axis_x'], dtype=np.float64)
    ay = np.asarray(b['axis_y'], dtype=np.float64)
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2] = ax, ay, np.cross(ax, ay)
    M[:3, 3] = np.asarray(b['origin_mm'], dtype=np.float64) / 1000.0
    return M


# ── file I/O ──────────────────────────────────────────────────────────────────
def _fsync_file(path):
    with open(path, 'rb') as f:
        os.fsync(f.fileno())


def _fsync_dir(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_fs(path, fields):
    import cv2
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
    try:
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, str):
                fs.write(key, value)
            elif isinstance(value, (int, float, np.floating, np.integer)) and \
                    not isinstance(value, bool):
                fs.write(key, float(value) if isinstance(value, (float, np.floating))
                         else int(value))
            else:
                fs.write(key, np.asarray(value, dtype=np.float64))
    finally:
        fs.release()


def _read_fs(path):
    import cv2
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
    if not fs.isOpened():
        raise ValueError('unreadable')
    out = {}
    try:
        root = fs.root()
        for key in root.keys():
            node = fs.getNode(key)
            if node.isString():
                out[key] = node.string()
            elif node.isReal() or node.isInt():
                out[key] = node.real()
            else:
                out[key] = node.mat()
    finally:
        fs.release()
    return out


def write_atomic(path, fields):
    """temp + fsync + os.replace + directory fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.stem}.tmp-{os.getpid()}.yaml')
    _write_fs(tmp, fields)
    _fsync_file(tmp)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _copy_atomic(src, dst):
    dst = Path(dst)
    tmp = dst.with_name(f'.{dst.stem}.tmp-{os.getpid()}.yaml')
    shutil.copyfile(src, tmp)
    _fsync_file(tmp)
    os.replace(tmp, dst)


def digest_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── validation ────────────────────────────────────────────────────────────────
def _finite(a):
    return a is not None and np.all(np.isfinite(np.asarray(a, dtype=np.float64)))


def _rigid_ok(M):
    M = np.asarray(M, dtype=np.float64)
    if M.shape != (4, 4) or not _finite(M):
        return False
    R = M[:3, :3]
    if np.linalg.norm(R.T @ R - np.eye(3)) > ORTHO_TOL:
        return False
    if abs(np.linalg.det(R) - 1.0) > ORTHO_TOL:
        return False
    return np.allclose(M[3], [0.0, 0.0, 0.0, 1.0], atol=0.0)


def validate(raw, layouts=None):
    """(fields, '') when valid, else (None, German reason)."""
    bad = (None, T.GATE_NOT_CONFIGURED_DE)
    if not isinstance(raw, dict):
        return bad
    fields = {}
    for key in _STRINGS:
        v = raw.get(key)
        if not isinstance(v, str) or not v:
            return bad
        fields[key] = v
    for key in _OPTIONAL_STRINGS:
        v = raw.get(key)
        if v is not None:
            if not isinstance(v, str):
                return bad
            fields[key] = v
    for key in _SCALARS:
        v = raw.get(key)
        if isinstance(v, np.ndarray) or v is None:
            return bad
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return bad
        if not math.isfinite(fv):
            return bad
        fields[key] = fv
    for key, shape in _MATRICES.items():
        v = raw.get(key)
        if not isinstance(v, np.ndarray):
            return bad
        a = np.asarray(v, dtype=np.float64)
        if a.shape != shape or not _finite(a):
            return bad
        fields[key] = a
    for key in _VARIABLE:
        v = raw.get(key)
        if not isinstance(v, np.ndarray) or not _finite(v):
            return bad
        fields[key] = np.asarray(v, dtype=np.float64)
    if int(fields['schema']) != SCHEMA:
        return bad
    if (int(fields['image_width']), int(fields['image_height'])) != T.STATION_IMAGE_SIZE:
        return bad
    for key in ('T_cam_mat', 'T_cam_mat_commissioned', 'T_cam_base'):
        if not _rigid_ok(fields[key]):
            return bad
    ids = fields['ref_ids'].reshape(-1)
    if ids.size != 4 or len(set(int(i) for i in ids)) != 4 or \
            any(not float(i).is_integer() or not 100 <= int(i) <= 199 for i in ids):
        return bad
    fields['ref_ids'] = [int(i) for i in ids]
    poly = fields['zone_polygon']
    if poly.ndim != 2 or poly.shape[1] != 2 or poly.shape[0] < 3:
        return bad
    if fields['marks'].ndim != 2 or fields['marks'].shape[1] != 2:
        return bad
    if not LAYOUT_ID_RE.match(fields['layout_id']):
        return bad
    layout = released_layout(fields['layout_id'], layouts)
    if layout is None:
        return bad
    if fields['layout_sha256'] != layout_data.layout_sha256(layout):
        return bad
    return fields, ''


def calibration_dict(fields, digest):
    """The keys today's workflow consumers read, plus image_size and digest."""
    K = np.asarray(fields['camera_matrix'], dtype=np.float64)
    dist = np.asarray(fields['distortion_coefficients'], dtype=np.float64)
    return {
        'scene_intrinsics': {'K': K, 'dist': dist},
        'scene_extrinsics': np.asarray(fields['T_cam_base'], dtype=np.float64),
        'z_table': float(fields['z_table']),
        'board_table_z': float(fields['board_table_z']),
        'table_plane': (float(fields['table_a']), float(fields['table_b']),
                        float(fields['table_c'])),
        'xy_correction': None,
        'yaw_bias_rad': 0.0,
        'image_size': T.STATION_IMAGE_SIZE,
        'digest': digest,
    }


def polygon_to_base(fields):
    """The accepted zone polygon (mat frame) in the base frame, with the stored
    registration (R_reg, t_reg) — converted once at load, so polygon and
    projection share one frame."""
    th = float(fields['reg_theta_rad'])
    c, s = math.cos(th), math.sin(th)
    R = np.array([[c, -s], [s, c]])
    t = np.array([float(fields['reg_tx_m']), float(fields['reg_ty_m'])])
    poly = np.asarray(fields['zone_polygon'], dtype=np.float64).reshape(-1, 2)
    return poly @ R.T + t


def load(path=None, layouts=None):
    """(StationCalibration, '') or (None, German reason). Never raises."""
    path = Path(path) if path is not None else yaml_path()
    try:
        if not path.exists():
            return None, T.GATE_NOT_CONFIGURED_DE
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        raw = _read_fs(path)
    except Exception:  # noqa: BLE001 — a corrupt or truncated file is "not configured"
        return None, T.GATE_NOT_CONFIGURED_DE
    fields, reason = validate(raw, layouts)
    if fields is None:
        return None, reason
    try:
        cal = StationCalibration(digest, calibration_dict(fields, digest),
                                 polygon_to_base(fields), fields, fields['layout_id'])
    except Exception:  # noqa: BLE001
        return None, T.GATE_NOT_CONFIGURED_DE
    return cal, ''


def write_station(fields, base=None, layouts=None):
    """Commit and re-solve write: (1) copy the current VALID station.yaml to
    station.prev.yaml, (2) write the new file; each temp + fsync + os.replace,
    then the directory fsync. A crash leaves the old or the new complete file."""
    current = yaml_path(base)
    if current.exists():
        cal, _ = load(current, layouts)
        if cal is not None:
            _copy_atomic(current, prev_path(base))
    write_atomic(current, fields)


def restore_previous(base=None, layouts=None):
    """Swap the two files (§8.3): previous → station.yaml, replaced current →
    station.prev.yaml, each temp + fsync + os.replace, then the directory fsync.
    Requires a previous file that passes the full validation BEFORE the swap.
    Returns (ok, German message)."""
    prev = prev_path(base)
    cal, _ = load(prev, layouts) if prev.exists() else (None, '')
    if cal is None:
        return False, T.RESTORE_INVALID_DE
    current = yaml_path(base)
    d = station_dir(base)
    tmp_new = d / f'.restore-new-{os.getpid()}.yaml'
    tmp_old = d / f'.restore-old-{os.getpid()}.yaml'
    shutil.copyfile(prev, tmp_new)
    _fsync_file(tmp_new)
    had_current = current.exists()
    if had_current:
        shutil.copyfile(current, tmp_old)
        _fsync_file(tmp_old)
    os.replace(tmp_new, current)
    if had_current:
        os.replace(tmp_old, prev)
    _fsync_dir(d)
    return True, T.RESTORE_OK_DE


def cleanup_staging(base=None):
    """At server start: delete every leftover .staging-* (a commission cut off
    by a restart). Returns the number removed."""
    d = station_dir(base)
    n = 0
    if not d.is_dir():
        return 0
    for p in d.glob('.staging-*'):
        shutil.rmtree(p, ignore_errors=True)
        n += 1
    return n


def write_evidence(commission_id, report, jpegs=(), base=None):
    """evidence/<commission_id>/: report.json + the captured JPEGs; newest 3 kept."""
    root = station_dir(base) / 'evidence'
    target = root / str(commission_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'report.json').write_text(json.dumps(report, indent=1, default=float),
                                        encoding='utf-8')
    for name, data in jpegs:
        (target / name).write_bytes(data)
    dirs = sorted((p for p in root.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dirs[EVIDENCE_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
