#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Tag bookkeeping of the automatic calibration (spec 40 S4, A13, A14).

* :func:`classify` splits detected ids into this arm's field tags, the other
  arm's mat tags, the two fixture tags, this arm's and the other arm's target
  tags, and object tags (everything outside the reserved range).
* :func:`match_corners` puts a detection's four corners into the model's
  order by the cyclic shift that fits the nominal view best (A14: the
  detector's order is not the model's for every view).
* :func:`median_corners` is the per-tag, per-corner median over frames.
* :func:`centre_from_corners` is where the quad's diagonals cross (A13).

What a frame carries is :class:`TagObs` per id inside :class:`Frame`.

Pure NumPy and the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np

from physical_ai_server.workflow import scene_fixture as _sf

FIXTURE_IDS = tuple(sorted(_sf.FIXTURE_TAGS_F))


class TagObs(NamedTuple):
    """One detected tag: its four corners in pixels (``(4, 2)``, the detector's
    order) and the detector's decision margin (``None`` when not known)."""

    corners: np.ndarray
    margin: float | None = None


@dataclass
class Frame:
    """One look of the scene camera at full resolution."""

    width: int
    height: int
    mean_grey: float
    tags: dict = field(default_factory=dict)   # tag id -> TagObs


def _other(arm_key):
    return 'v1' if arm_key == 'omx' else 'omx'


def classify(tag_ids, arm_key) -> dict:
    """``{'field', 'foreign_mat', 'fixture', 'target', 'foreign_target', 'object'}``
    → sorted id lists. A reserved id that belongs to nothing is left out."""
    spec, other = _sf.ARMS[arm_key], _sf.ARMS[_other(arm_key)]
    out = {k: [] for k in ('field', 'foreign_mat', 'fixture', 'target', 'foreign_target', 'object')}
    for tid in sorted(int(t) for t in tag_ids):
        if tid in spec['mat_tags_f']:
            out['field'].append(tid)
        elif tid in other['mat_tags_f']:
            out['foreign_mat'].append(tid)
        elif tid in _sf.FIXTURE_TAGS_F:
            out['fixture'].append(tid)
        elif tid in spec['target_tag_ids']:
            out['target'].append(tid)
        elif tid in other['target_tag_ids']:
            out['foreign_target'].append(tid)
        elif not _sf.is_reserved_tag_id(tid):
            out['object'].append(tid)
    return out


def match_corners(detected, nominal) -> np.ndarray:
    """``detected`` reordered by the cyclic shift ``k`` that brings its corners
    closest to ``nominal`` (both ``(4, 2)``): corner ``i`` of the answer is
    ``detected[(i + k) % 4]``. Each quad is centred first, so the choice is
    made by the orientation alone and a whole-tag offset of the nominal view
    (a camera a few millimetres off) cannot pick a wrong shift."""
    d = np.asarray(detected, dtype=np.float64).reshape(4, 2)
    n = np.asarray(nominal, dtype=np.float64).reshape(4, 2)
    dc = d - d.mean(axis=0)
    nc = n - n.mean(axis=0)
    best, best_k = None, 0
    for k in range(4):
        cost = float(np.sum(np.square(np.roll(dc, -k, axis=0) - nc)))
        if best is None or cost < best:
            best, best_k = cost, k
    return np.roll(d, -best_k, axis=0)


def median_corners(frames) -> dict:
    """``{tag_id: (median corners (4, 2), number of frames seen)}`` over a
    sequence of ``{tag_id: corners}`` mappings (``TagObs`` accepted too). The
    corners of one tag must already be in one order in every frame."""
    seen: dict = {}
    for fr in frames:
        for tid, obs in fr.items():
            c = obs.corners if isinstance(obs, TagObs) else obs
            seen.setdefault(int(tid), []).append(np.asarray(c, dtype=np.float64).reshape(4, 2))
    return {tid: (np.median(np.stack(cs), axis=0), len(cs)) for tid, cs in seen.items()}


def centre_from_corners(corners):
    """Where the diagonals (corner 0 to 2, corner 1 to 3) cross: the image of
    the tag's centre under any perspective (A13). ``None`` unless both
    diagonals cross inside the quad."""
    try:
        c = np.asarray(corners, dtype=np.float64).reshape(4, 2)
    except (ValueError, TypeError):
        return None
    if not np.all(np.isfinite(c)):
        return None
    d1 = c[2] - c[0]
    d2 = c[3] - c[1]
    w = c[1] - c[0]
    det = d1[1] * d2[0] - d1[0] * d2[1]
    if abs(det) <= 1e-9 * float(np.dot(d1, d1) + np.dot(d2, d2)):
        return None
    s = (w[1] * d2[0] - w[0] * d2[1]) / det
    t = (w[1] * d1[0] - w[0] * d1[1]) / det
    if not (0.0 <= s <= 1.0 and 0.0 <= t <= 1.0):
        return None
    return float(c[0, 0] + s * d1[0]), float(c[0, 1] + s * d1[1])
