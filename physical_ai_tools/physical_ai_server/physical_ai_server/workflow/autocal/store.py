#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The stored tower calibration (spec 40 S4): ``<calib dir>/calibration/arm_check.json``.

* :meth:`Store.write` writes the arm check's result atomically (temp file,
  fsync, ``os.replace``) and bumps the process-wide :func:`version`, so a
  reader that caches (the arm model) compares the counter on each call and a
  new arm check takes effect without a restart.
* :meth:`Store.read` answers ``Read(status, data)``: ``'ok'`` with the dict,
  ``'none'`` when there is no readable file, ``'contract_changed'`` when the
  file was made under another fixture, contract hash, arm profile or image
  size (it then reads as absent).
* ``mat_check_last.json`` keeps the last mat check's result beside it.

The legacy YAML files are never read and are left on disk. The calibration
directory is the caller's (the service resolves it); nothing here reads the
environment. Pure standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import tempfile
import threading

from physical_ai_server.workflow import scene_fixture as _sf

SUBDIR = 'calibration'
ARM_CHECK_FILE = 'arm_check.json'
MAT_CHECK_LAST_FILE = 'mat_check_last.json'
FORMAT_VERSION = 1

# Keys every stored arm check carries (spec 40 S4).
REQUIRED_KEYS = (
    'v', 'fixture_id', 'contract_hash', 'profile_id', 'camera_profile_id', 'image_size', 'created_utc', 'kb',
    'print_scale', 'joint_offsets_rad', 'axis_tilts_rad', 'tilt', 'mat_pose', 'camera_pose_mat', 'lens_w',
    'fixture_xy_mat', 'fixture_px', 'mount', 'droop_envelope_rad', 'tower_pose', 'tower_fit_rms_m',
    'target_removed', 'quality',
)

_lock = threading.Lock()
_version = 0


def version() -> int:
    """Bumped by every successful write of any :class:`Store` in this process."""
    return _version


def _bump():
    global _version
    with _lock:
        _version += 1


@dataclass(frozen=True)
class Read:
    status: str            # 'ok' | 'none' | 'contract_changed'
    data: dict | None = None


def _finite(value) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(v) for v in value)
    return False


def _atomic_write_json(path: str, data: dict) -> None:
    text = json.dumps(data, indent=1, sort_keys=True, allow_nan=False)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _read_json(path: str):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Store:
    """``calib_dir`` is ``$EDUBOTICS_CALIB_DIR`` (the service passes it)."""

    def __init__(self, calib_dir: str) -> None:
        self._dir = os.path.join(str(calib_dir), SUBDIR)

    @property
    def path(self) -> str:
        return os.path.join(self._dir, ARM_CHECK_FILE)

    @property
    def mat_check_path(self) -> str:
        return os.path.join(self._dir, MAT_CHECK_LAST_FILE)

    def write(self, data: dict) -> None:
        """Write a complete arm check (every :data:`REQUIRED_KEYS`, finite
        numbers) atomically and bump :func:`version`. Raises ``ValueError``
        for an incomplete or non-finite dict; nothing is written then."""
        missing = [k for k in REQUIRED_KEYS if k not in data]
        if missing:
            raise ValueError(f'arm check incomplete: {missing}')
        if data['v'] != FORMAT_VERSION or not _finite(data):
            raise ValueError('arm check malformed')
        _atomic_write_json(self.path, data)
        _bump()

    def read(self, profile_id: str, image_size=None) -> Read:
        """The stored arm check for ``profile_id`` (and ``image_size`` when given)."""
        data = _read_json(self.path)
        if data is None or any(k not in data for k in REQUIRED_KEYS) or data.get('v') != FORMAT_VERSION:
            return Read('none')
        if (data.get('fixture_id') != _sf.FIXTURE_ID
                or data.get('contract_hash') != _sf.contract_hash()
                or data.get('profile_id') != profile_id):
            return Read('contract_changed')
        if image_size is not None and list(data.get('image_size') or ()) != [int(v) for v in image_size]:
            return Read('contract_changed')
        return Read('ok', data)

    def set_target_removed(self, profile_id: str, removed: bool) -> bool:
        """Set the stored arm check's ``target_removed`` flag (the button, or a
        target tag seen later). ``False`` when there is no valid arm check."""
        r = self.read(profile_id)
        if r.status != 'ok':
            return False
        data = dict(r.data)
        data['target_removed'] = bool(removed)
        self.write(data)
        return True

    def write_mat_check_last(self, result: dict) -> None:
        _atomic_write_json(self.mat_check_path, result)

    def read_mat_check_last(self):
        return _read_json(self.mat_check_path)
