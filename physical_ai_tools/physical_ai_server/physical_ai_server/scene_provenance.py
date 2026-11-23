#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Scene-camera provenance of a dataset (decision D58).

A dataset never gets episodes of a second scene-camera arrangement, Daten never
merges two. A dataset recorded with the tower camera carries ONE stamp file,
``meta/edubotics_scene_camera.json``; no file means ``legacy`` (every dataset
recorded before the tower, and every legacy session, which writes none). The
file lives under ``meta/``: it is uploaded and downloaded with the dataset, part
of the meta digest, and ignored by LeRobot's loader, writer and the cloud
training (a key in ``info.json`` would be dropped by LeRobot >= 0.6).

The judge is the profile, never the ``profile_id`` (it also encodes the JPEG
quality, which differs between Windows and the Pi for one tower view).

The robot learns its live profile from ONE provider, a callable answering
``{"profile": "legacy" | "tower_v1" | None, "profile_id": <12 hex> | None,
"platform": "windows" | "pi" | None}`` (``None`` = unknown: nothing is refused,
nothing is stamped). The node wires the studio camera's provider, the arm
container's stated profile (``workflow/studio_camera.py``, decision D63; no
studio camera = no provider = unknown); :data:`LEGACY_PROVIDER` is the constant
``legacy`` answer.

Stdlib only and free of package imports: the recorder, the Daten engine, the
edit worker and the sidecar load it, some of them by file path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

STAMP_RELPATH = 'meta/edubotics_scene_camera.json'
PROFILES = ('legacy', 'tower_v1')
PROFILE_TOPIC = '/edubotics/scene_camera_profile'
STAMP_VERSION = 1
PLATFORMS = ('windows', 'pi')
# A stamp is far smaller (the writer's is below 200 bytes); a larger file is
# not ours and is never read whole.
STAMP_MAX_BYTES = 4096
_STAMP_KEYS = ('v', 'scene_camera_profile', 'profile_id', 'platform')
# fullmatch only: `$` would also accept a trailing newline.
_PROFILE_ID = re.compile(r'[0-9a-f]{12}')
# In the dataset ROOT, outside meta/: a crash between write and rename never
# leaves a file the upload sends or the meta digest hashes.
_TMP_NAME = '.edubotics_scene_camera.json.tmp'


def LEGACY_PROVIDER():  # noqa: N802 — a constant of the provider kind
    """The provider of a robot without a tower camera."""
    return {'profile': 'legacy', 'profile_id': None, 'platform': None}


def _unknown():
    return {'profile': None, 'profile_id': None, 'platform': None}


def _valid_profile_id(value) -> bool:
    return value is None or (type(value) is str and bool(_PROFILE_ID.fullmatch(value)))


def _valid_platform(value) -> bool:
    return value is None or (type(value) is str and value in PLATFORMS)


def live_profile(provider) -> dict:
    """What ``provider`` answers now, checked: its dict, or the unknown answer
    (``profile`` None) when there is no provider, it raises or it answers
    anything malformed. Never raises."""
    if provider is None:
        return _unknown()
    try:
        answer = provider()
    except Exception:  # noqa: BLE001 — a provider never breaks a recording
        return _unknown()
    if type(answer) is not dict:
        return _unknown()
    profile = answer.get('profile')
    profile_id = answer.get('profile_id')
    platform = answer.get('platform')
    if not (type(profile) is str and profile in PROFILES):
        return _unknown()
    if not (_valid_profile_id(profile_id) and _valid_platform(platform)):
        return _unknown()
    return {'profile': profile, 'profile_id': profile_id, 'platform': platform}


def resume_refused(dataset_profile, live) -> bool:
    """True only when both profiles are known and differ (refuse on proof)."""
    return dataset_profile in PROFILES and live in PROFILES and dataset_profile != live


def _meta_dir(root) -> str:
    """``<root>/meta``, refused (ValueError) when its real path is not inside
    the real path of ``root`` (a symlinked meta/ pointing elsewhere)."""
    root = os.fspath(root)
    meta = os.path.join(root, 'meta')
    real_root = Path(os.path.realpath(root))
    real_meta = Path(os.path.realpath(meta))
    if real_meta == real_root or not real_meta.is_relative_to(real_root):
        raise ValueError('meta/ lies outside the dataset')
    return meta


def _stamp_bytes(root):
    """The stamp file's bytes, or None when the dataset has none. Raises
    ValueError (OSError for an I/O failure) when a file is there but is not a
    regular file inside the dataset of at most STAMP_MAX_BYTES."""
    meta = _meta_dir(root)
    path = os.path.join(meta, os.path.basename(STAMP_RELPATH))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('the stamp is not a regular file')
    if st.st_size > STAMP_MAX_BYTES:
        raise ValueError('the stamp is too large')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('the stamp is not a regular file')
        data = os.read(fd, STAMP_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > STAMP_MAX_BYTES:
        raise ValueError('the stamp is too large')
    return data


def read_stamp(root):
    """The dataset's validated stamp dict, or None when it has none. Raises
    ValueError (or OSError) when a stamp is there but unreadable, malformed, of
    an unknown ``v`` or names an unknown value."""
    data = _stamp_bytes(root)
    if data is None:
        return None
    try:
        stamp = json.loads(data.decode('utf-8'))
    except RecursionError as e:              # a deeply nested 4 KB file
        raise ValueError('the stamp is nested too deeply') from e
    if not isinstance(stamp, dict) or set(stamp) != set(_STAMP_KEYS):
        raise ValueError('the stamp has other keys')
    if type(stamp['v']) is not int or stamp['v'] != STAMP_VERSION:
        raise ValueError('unknown stamp version')
    profile = stamp['scene_camera_profile']
    if not (type(profile) is str and profile in PROFILES):
        raise ValueError('unknown scene camera profile')
    if not (_valid_profile_id(stamp['profile_id']) and _valid_platform(stamp['platform'])):
        raise ValueError('unknown profile id or platform')
    return stamp


def dataset_profile(root):
    """``'legacy'`` (no stamp), ``'tower_v1'``, or None (a stamp that cannot be
    read: malformed, too large, a symlink, an unknown ``v`` or value)."""
    try:
        stamp = read_stamp(root)
    except (OSError, ValueError):
        return None
    return 'legacy' if stamp is None else stamp['scene_camera_profile']


def _write_into_meta(root, data: bytes) -> None:
    """``data`` as ``meta/<stamp>``: written to the temp file in the dataset
    root, fsynced, renamed into meta/, the directory fsynced."""
    meta = _meta_dir(root)
    tmp = os.path.join(os.fspath(root), _TMP_NAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(meta, os.path.basename(STAMP_RELPATH)))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(meta, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_stamp(root, profile_id, platform) -> None:
    """Stamp a dataset as recorded with the tower camera (the only profile that
    is ever written): compact JSON, the keys in their order. Raises on a value
    outside the schema or an I/O failure."""
    if not (_valid_profile_id(profile_id) and _valid_platform(platform)):
        raise ValueError('profile id or platform outside the schema')
    stamp = {'v': STAMP_VERSION, 'scene_camera_profile': 'tower_v1',
             'profile_id': profile_id, 'platform': platform}
    _write_into_meta(root, json.dumps(stamp, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))


def copy_stamp_bytes(src_root, dst_root) -> bool:
    """The source's stamp file into ``dst_root`` byte for byte, unparsed (a
    later ``v`` survives a delete or a split). False when the source has none."""
    data = _stamp_bytes(src_root)
    if data is None:
        return False
    _write_into_meta(dst_root, data)
    return True


def copy_stamp(src_roots, dst_root) -> bool:
    """A Daten output carries its sources' stamp: ONE source -> its bytes
    (:func:`copy_stamp_bytes`); several (they passed the merge check
    ``scene_camera``) -> their common stamp, a field the sources do not share
    as null, or nothing when they are legacy. Raises ValueError when several
    sources do not share one readable profile. True when a stamp was written."""
    roots = list(src_roots)
    if len(roots) == 1:
        return copy_stamp_bytes(roots[0], dst_root)
    stamps = [read_stamp(r) for r in roots]
    profiles = {'legacy' if s is None else s['scene_camera_profile'] for s in stamps}
    if len(profiles) != 1:
        raise ValueError('the sources were recorded with different scene cameras')
    if profiles == {'legacy'}:
        return False

    def common(key):
        values = {s[key] for s in stamps}
        return values.pop() if len(values) == 1 else None
    write_stamp(dst_root, common('profile_id'), common('platform'))
    return True
