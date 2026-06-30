#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Perception block handlers.

Detect/count/wait blocks return values to the interpreter; they are
called via ``_eval_value`` rather than ``_exec_statement``. The
returned objects are ``Detection`` instances (or counts / booleans);
motion handlers' ``_resolve_target`` knows how to read
``world_xyz_m`` from them.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

import numpy as np

from physical_ai_server.workflow.handlers import motion as _motion
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError

# Tag-edge sanity gate: the back-projected tag side length must be within this
# fraction of the catalog tag_size_m, else the corner geometry / plane height is
# untrustworthy and we treat the orientation as unknown (→ the instance is
# skipped rather than grasped with a wrong wrist roll). Rig-tunable; generous by
# default so only gross errors (≈2× scale) trip it. env-forwarding-guard: this
# var is forwarded in robotis_ai_setup/docker/docker-compose.yml.
_TAG_EDGE_TOL_FRAC = max(0.0, _motion._safe_float('EDUBOTICS_TAG_EDGE_TOL_FRAC', 0.5))

# Multi-frame yaw averaging for the grasped instance. Yaw error scales as
# pixel_noise / tag_edge_pixels, so a single frame's corner noise mis-pinches
# the wrist. We sample N live scene frames of the SAME tag, circular-mean the
# per-frame tag_yaw_base, and gate on the mean resultant length R (≈1 = tight
# agreement, low = scattered/untrustworthy → drop the yaw so the existing
# skip-on-unreadable-yaw path handles it rather than blind-grasping).
# env-forwarding-guard: both vars are forwarded in
# robotis_ai_setup/docker/docker-compose.yml.
_TAG_YAW_FRAMES = max(1, int(_motion._safe_float('EDUBOTICS_TAG_YAW_FRAMES', 7.0)))
_TAG_YAW_MIN_RESULTANT = min(
    1.0, max(0.0, _motion._safe_float('EDUBOTICS_TAG_YAW_MIN_RESULTANT', 0.9)))
# Spacing between consecutive yaw-sampling frames (~30 ms, plan W3b).
_TAG_YAW_FRAME_INTERVAL_S = 0.03

# Recycled-object reclaim (#1): a claimed/skipped tag that has been continuously
# ABSENT for ≥ this many seconds and then reappears was removed and put back, so
# it is un-claimed/un-skipped and grabbed again. Generous default so a tag merely
# occluded for a frame or two is NOT reclaimed. env-forwarding-guard: this var is
# forwarded in robotis_ai_setup/docker/docker-compose.yml.
_RECLAIM_ABSENT_S = max(0.0, _motion._safe_float('EDUBOTICS_RECLAIM_ABSENT_S', 1.5))


def _ensure_perception(ctx):
    if ctx.perception is None:
        raise WorkflowError(
            'Wahrnehmung ist nicht initialisiert — bitte zuerst die Kalibrierung abschließen.'
        )


def _scene_frame(ctx):
    """Fetch a FRESH scene-camera frame, or raise a German error. Rejects a
    frozen/stale frame (camera down mid-workflow) — not just a never-arrived
    one — via the optional ctx.get_scene_frame_age getter."""
    getter = getattr(ctx, 'get_scene_frame', None)
    bgr = getter() if getter else None
    if bgr is None:
        raise WorkflowError(
            'Kein aktuelles Szenenbild verfügbar — bitte die Szenen-Kamera prüfen.'
        )
    age_getter = getattr(ctx, 'get_scene_frame_age', None)
    if callable(age_getter):
        try:
            age = age_getter()
        except Exception:  # noqa: BLE001 — age is advisory
            age = None
        if age is not None and age > _SCENE_FRAME_MAX_AGE_S:
            raise WorkflowError(
                'Die Szenen-Kamera liefert kein aktuelles Bild — bitte die '
                'Kamera prüfen.'
            )
    return bgr


# A scene frame older than this is treated as stale (camera stalled / down).
_SCENE_FRAME_MAX_AGE_S = 1.0


def _require_marker_detector(ctx):
    if not ctx.perception.apriltag_available():
        raise WorkflowError(
            'Marker-Erkennung ist auf diesem Gerät nicht verfügbar.'
        )


def _poll_until(ctx, predicate, timeout_s: float, label: str) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout_s`` elapses.

    On timeout, raises ``WorkflowError`` so the workflow halts with a
    German message — this is symmetric with the rest of the perception
    handlers and matches what students expect when a "Warte bis …"
    block sees nothing. The previous implementation had a dead
    ``on_timeout='continue'`` branch reading from a block field that
    never existed; if that affordance is wanted later, expose a
    dropdown on the wait_until_* blocks first.

    Audit S1: this poll is pure perception (no motion). When called from
    inside a hat-block handler, the surrounding ``with ctx.motion_lock``
    pinned the lock for up to ``timeout_s``, blocking every other
    motion thread including the recovery routine's 2s acquire. Recovery
    then proceeded **without** the lock, allowing a recovered home
    trajectory to race the still-running hat handler's body. Release
    the motion lock around the wait so it acts as a "wait barrier" only,
    not a "block-everyone-else barrier"; reacquire on exit so the hat
    handler resumes with the same locking invariants it had before.
    """
    deadline = time.monotonic() + timeout_s
    motion_lock = getattr(ctx, 'motion_lock', None)
    released = False
    if motion_lock is not None:
        try:
            motion_lock.release()
            released = True
        except RuntimeError:
            # Lock wasn't held by this thread — fine, just don't try to
            # reacquire in finally. This happens when _poll_until is
            # called from a non-hat path (e.g. test harness).
            released = False
    try:
        while time.monotonic() < deadline:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            if predicate():
                return True
            time.sleep(0.2)
        raise WorkflowError(f'Timeout: {label} nicht erkannt.')
    finally:
        if released and motion_lock is not None:
            # Audit fix #9: bounded reacquire. The previous unbounded
            # acquire() could hang forever if another thread held the
            # lock and never released it (e.g. a runaway motion handler
            # in another hat). 10 s is generous for a single motion
            # chunk to finish; past that we'd rather raise a clear
            # German error so the caller's `with motion_lock` __exit__
            # has SOMETHING to release. The exception propagates out
            # through whatever wrapped the _poll_until call.
            if not motion_lock.acquire(timeout=10.0):
                raise WorkflowError(
                    'Bewegung-Sperre konnte nicht zurückgewonnen werden.'
                )


# ------------------------------------------------------------------
# Named-object detection + grasp (Roboter Studio AprilTag grasping)
# ------------------------------------------------------------------
# A printed object carries a unique AprilTag whose id maps (via the teacher's
# object_catalog.json on ctx) to a TYPE + grasp recipe. The named-object blocks
# detect by TYPE, attach the exact base-frame grasp point + tag yaw, and grasp
# top-down with a live tag-derived wrist roll. See workflow/object_catalog.py +
# workflow/tag_pose.py.
def _require_catalog(ctx):
    """Return the loaded ObjectCatalog from ctx, or raise the German load error
    (catalog is loaded tolerantly at workflow start — a non-named workflow runs
    fine without it; a named block fails loud here)."""
    cat = getattr(ctx, 'object_catalog', None)
    if cat is None:
        err = getattr(ctx, 'object_catalog_error', None)
        raise WorkflowError(
            err or 'Objekt-Katalog ist nicht geladen — bitte „object_catalog.json" '
            'im Kalibrier-Ordner anlegen.'
        )
    return cat


def _recipe_for(cat, type_name):
    """Look up a grasp recipe, translating the catalog's ObjectCatalogError
    (German) into a WorkflowError so the runtime surfaces it as a clean student
    message instead of the generic "Interner Fehler"."""
    from physical_ai_server.workflow.object_catalog import ObjectCatalogError
    try:
        return cat.recipe_for_type(type_name)
    except ObjectCatalogError as e:
        raise WorkflowError(str(e))


def label_for(ctx, type_name) -> str:
    """The German display label for an object type (catalog ``label_de``), or the
    raw type name as a best-effort fallback. Used by the while-visible loop's
    per-pass feedback (#7) so messages read „Banane" not „banane"; never raises
    (the loop's own gate already surfaced a bad/missing catalog)."""
    try:
        return _recipe_for(_require_catalog(ctx), type_name).label_de
    except Exception:
        return str(type_name or 'Objekt')


def _excluded_ids(ctx) -> set:
    """The set of tag ids to skip in detection: CLAIMED (already grasped) ∪
    SKIPPED (confirmed-failed, future heuristic). Read under claim_lock so a
    concurrent grasp in a hat thread can't tear the set (§24.3)."""
    lock = getattr(ctx, 'claim_lock', None)
    claimed = getattr(ctx, 'claimed_tags', None) or set()
    skipped = getattr(ctx, 'skipped_tags', None) or set()
    if lock is not None:
        with lock:
            return set(claimed) | set(skipped)
    return set(claimed) | set(skipped)


def _claim_tag(ctx, tag_id) -> None:
    """Mark a tag id CLAIMED after a successful grasp so the loop never
    re-grabs a placed object and terminates. No-op if claim state is absent
    (e.g. a unit-test ctx without the sets)."""
    if tag_id is None:
        return
    claimed = getattr(ctx, 'claimed_tags', None)
    if claimed is None:
        return
    lock = getattr(ctx, 'claim_lock', None)
    if lock is not None:
        with lock:
            claimed.add(int(tag_id))
    else:
        claimed.add(int(tag_id))


def _skip_tag(ctx, tag_id) -> None:
    """Mark a tag id SKIPPED — a confirmed per-instance failure (out of reach,
    orientation unreadable) that must NOT be retried, so the „Solange sichtbar"
    loop makes progress and terminates instead of retreat→redetect→fail forever.
    Excluded from future detection alongside claimed ids (``_excluded_ids``).
    No-op if skip state is absent (e.g. a unit-test ctx without the sets)."""
    if tag_id is None:
        return
    skipped = getattr(ctx, 'skipped_tags', None)
    if skipped is None:
        return
    lock = getattr(ctx, 'claim_lock', None)
    if lock is not None:
        with lock:
            skipped.add(int(tag_id))
    else:
        skipped.add(int(tag_id))


def _reclaim_recycled(ctx, recipe, visible_type_ids) -> None:
    """Un-claim / un-skip a RECYCLED object so it is grabbed again (#1).

    Given the set of currently-visible tag ids OF THIS TYPE, update the per-tag
    absence tracker and, for each claimed/skipped tag of the type:
      * visible now AND continuously absent ≥ ``_RECLAIM_ABSENT_S`` → it was
        removed and put back: drop it from claimed/skipped and clear its absence
        (it gets grabbed again);
      * visible now but not long-absent → clear its absence;
      * not visible now → mark it absent (monotonic) if not already.
    Mutates claimed_tags/skipped_tags/absent_since under claim_lock. No-op when
    the claim state is absent (e.g. a unit-test ctx without the sets)."""
    claimed = getattr(ctx, 'claimed_tags', None)
    skipped = getattr(ctx, 'skipped_tags', None)
    absent_since = getattr(ctx, 'absent_since', None)
    if claimed is None or skipped is None or absent_since is None:
        return
    type_ids = {int(i) for i in recipe.tag_ids}
    visible = {int(i) for i in visible_type_ids}
    now = time.monotonic()
    lock = getattr(ctx, 'claim_lock', None)

    def _update():
        for tag in ((set(claimed) | set(skipped)) & type_ids):
            if tag in visible:
                started = absent_since.get(tag)
                if started is not None and (now - started) >= _RECLAIM_ABSENT_S:
                    claimed.discard(tag)
                    skipped.discard(tag)
                    absent_since.pop(tag, None)
                    try:
                        ctx.log(
                            f'„{recipe.label_de}" ({tag}) wurde zurückgelegt — '
                            'wird erneut gegriffen.'
                        )
                    except Exception:
                        pass
                else:
                    # Visible but not (yet) long-absent: reset the absence clock.
                    absent_since.pop(tag, None)
            else:
                # Not visible now: start the absence clock if not running.
                if tag not in absent_since:
                    absent_since[tag] = now

    if lock is not None:
        with lock:
            _update()
    else:
        _update()


def _detect_named_unclaimed(ctx, type_name) -> list:
    """``_detect_named`` minus the per-run claimed/skipped ids — the basis for
    the loop gate + see/count/wait blocks (a placed object is not re-counted).
    Runs the recycled-object reclaim first (on the full set of visible type ids),
    so a removed-then-replaced object is un-claimed and grabbed again (#1)."""
    return _detect_named(ctx, type_name, reclaim=True)


def count_unclaimed_visible(ctx, type_name) -> int:
    """Number of currently-visible UNCLAIMED instances of ``type_name``. The
    gate the interpreter's ``edubotics_while_visible`` loop polls each pass."""
    return len(_detect_named_unclaimed(ctx, type_name))


def _apply_xy_correction(ctx, x: float, y: float) -> tuple[float, float]:
    """Apply the optional ground-truth XY correction stored on ctx.

    Another agent (W5 calibration verify) populates ``ctx.xy_correction`` with a
    2x3 affine (numpy array) mapping detected base XY → corrected base XY, fit
    from known-position tags. Consumed SAFELY with getattr so this code works
    before/after that attribute exists (absent → identity, no correction)."""
    M = getattr(ctx, 'xy_correction', None)
    if M is None:
        return (float(x), float(y))
    try:
        Marr = np.asarray(M, dtype=np.float64).reshape(2, 3)
        v = Marr @ np.array([float(x), float(y), 1.0], dtype=np.float64)
        cx, cy = float(v[0]), float(v[1])
        if not (math.isfinite(cx) and math.isfinite(cy)):
            return (float(x), float(y))
        return (cx, cy)
    except Exception:
        # A malformed correction must never break grasping — fall back to raw.
        return (float(x), float(y))


def _apply_yaw_bias(ctx, yaw: Optional[float]) -> Optional[float]:
    """Add the optional ground-truth yaw bias (radians) stored on ctx and
    re-wrap. ``ctx.yaw_bias_rad`` is populated by the W5 calibration verify;
    consumed SAFELY (absent / None → 0.0, no change)."""
    if yaw is None:
        return None
    try:
        yb = float(getattr(ctx, 'yaw_bias_rad', 0.0) or 0.0)
    except (TypeError, ValueError):
        yb = 0.0
    if yb == 0.0:
        return float(yaw)
    from physical_ai_server.workflow.tag_pose import _wrap
    return _wrap(float(yaw) + yb)


def _attach_named_world(ctx, detections: list, recipe) -> list:
    """Attach ``world_xyz_m`` (grasp column x, y + grasp z) and ``tag_yaw`` to
    each named-object detection.

    Three things make this the AprilTag grasp projection:
    (a) the TAG CENTER is projected to the TAG-TOP plane (board_z +
    object_height), not the surface — the tag sits on top of the object, so
    projecting it to the surface would offset (x, y) by parallax on a tilted
    camera; (b) grasp z = ``z_table + object_height − grasp_depth`` (the body
    band below the top), NOT z_table; (c) the tag's base-frame yaw is computed
    for the live grasp roll. Calibration-incomplete → world_xyz_m left unset (so
    grasp_object surfaces the precise German calib error); visibility-only blocks
    (see/count) still work."""
    if not detections:
        ctx.emit_detections([])
        return detections
    board_z = getattr(ctx, 'board_table_z', None)
    if (ctx.scene_intrinsics is None or ctx.scene_extrinsics is None
            or board_z is None or ctx.z_table is None):
        ctx.emit_detections(detections)
        return detections
    from physical_ai_server.workflow.projection import project_pixel_to_table
    from physical_ai_server.workflow.tag_pose import tag_edge_length_base, tag_yaw_base
    K = ctx.scene_intrinsics['K']
    dist = ctx.scene_intrinsics['dist']
    T = ctx.scene_extrinsics
    tag_plane_z = float(board_z) + float(recipe.object_height_m)
    grasp_z = (float(ctx.z_table) + float(recipe.object_height_m)
               - float(recipe.grasp_depth_m))
    cat = getattr(ctx, 'object_catalog', None)
    expected_edge = float(getattr(cat, 'tag_size_m', 0.0) or 0.0)
    for d in detections:
        cx, cy = d.centroid_px
        point = project_pixel_to_table(cx, cy, K, dist, T, tag_plane_z)
        if point is None:
            continue
        # W5-apply: ground-truth XY correction (identity when unset).
        wx, wy = _apply_xy_correction(ctx, float(point[0]), float(point[1]))
        d.world_xyz_m = (wx, wy, grasp_z)
        yaw = None
        if getattr(d, 'corners_px', None) is not None:
            yaw = tag_yaw_base(d.corners_px, K, dist, T, tag_plane_z)
            # Tag-edge sanity gate: the back-projected side must be within
            # tolerance of the catalog tag size, else the corner geometry /
            # plane height is off and the recovered yaw is untrustworthy — drop
            # the orientation (grasp_object then SKIPS this instance instead of
            # committing a wrong-roll grasp). Position (from the robust centre
            # projection) is kept for visibility-only blocks.
            if yaw is not None and expected_edge > 0.0 and _TAG_EDGE_TOL_FRAC > 0.0:
                edge = tag_edge_length_base(d.corners_px, K, dist, T, tag_plane_z)
                if edge is None or abs(edge - expected_edge) > _TAG_EDGE_TOL_FRAC * expected_edge:
                    ctx.log(
                        f'[WARNUNG] Tag {d.aruco_id}: gemessene Tag-Größe weicht '
                        'zu stark ab — Ausrichtung verworfen.'
                    )
                    yaw = None
        # W5-apply: ground-truth yaw bias (no-op when unset). Single-frame here
        # for see/count display; grasp_object re-samples the GRASPED instance
        # over N frames (circular mean) before committing the wrist roll.
        d.extras['tag_yaw'] = _apply_yaw_bias(ctx, yaw)
        d.extras['gripper_close_rad'] = float(recipe.gripper_close_rad)
        d.extras['object_type'] = recipe.type_name
    ctx.emit_detections(detections)
    return detections


def _detect_named(ctx, type_name, exclude_ids=None, reclaim=False) -> list:
    """All catalog tags of ``type_name`` currently visible (minus
    ``exclude_ids``), with world_xyz_m + tag_yaw + close_rad attached. Reusable
    by grasp_object / see_object / count_object and the P2 while-visible loop.

    With ``reclaim=True`` (the unclaimed-view path), the FULL set of visible type
    ids is known here — before any claimed/skipped filter — so the recycled-object
    reclaim runs first (it may un-claim/un-skip a removed-then-replaced object),
    and the effective exclude set is read AFTER the reclaim. ``exclude_ids`` is
    ignored in that mode (the live claimed/skipped sets are used instead)."""
    _ensure_perception(ctx)
    _require_marker_detector(ctx)
    cat = _require_catalog(ctx)
    if not type_name:
        raise WorkflowError('Kein Objekt ausgewählt.')
    recipe = _recipe_for(cat, type_name)               # German on unknown type
    type_ids = set(recipe.tag_ids)
    if reclaim:
        # Detect unconditionally — the reclaim needs the full set of visible type
        # ids even when every instance is currently claimed/skipped.
        bgr = _scene_frame(ctx)
        detections = ctx.perception.detect(
            bgr, camera='scene', mode='apriltag', aruco_id=None)
        visible_type_ids = {d.aruco_id for d in detections if d.aruco_id in type_ids}
        _reclaim_recycled(ctx, recipe, visible_type_ids)
        wanted = type_ids - {int(i) for i in _excluded_ids(ctx)}
        kept = [d for d in detections if d.aruco_id in wanted]
        return _attach_named_world(ctx, kept, recipe)
    wanted = set(type_ids)
    if exclude_ids:
        wanted -= {int(i) for i in exclude_ids}
    if not wanted:
        ctx.emit_detections([])
        return []
    bgr = _scene_frame(ctx)
    detections = ctx.perception.detect(bgr, camera='scene', mode='apriltag', aruco_id=None)
    kept = [d for d in detections if d.aruco_id in wanted]
    return _attach_named_world(ctx, kept, recipe)


def _select_nearest_reachable(ctx, detections):
    """Nearest (base-frame distance) reachable detection with a known grasp
    point; tie-break by tag id for determinism. Returns the Detection or None."""
    candidates = []
    for d in detections:
        if d.world_xyz_m is None:
            continue
        if ctx.ik is not None and not ctx.ik.in_workspace(d.world_xyz_m):
            continue
        candidates.append(d)
    if not candidates:
        return None

    def _key(d):
        x, y, _z = d.world_xyz_m
        return (math.hypot(float(x), float(y)),
                d.aruco_id if d.aruco_id is not None else 0)

    candidates.sort(key=_key)
    return candidates[0]


def _multiframe_tag_yaw(ctx, recipe, tag_id, fallback_yaw):
    """Robust grasp yaw for ONE tag id: sample ``_TAG_YAW_FRAMES`` live scene
    frames (~30 ms apart, object static pre-grasp), compute ``tag_yaw_base`` per
    frame (same back-projection + edge gate as ``_attach_named_world``),
    circular-mean them, and gate on the mean resultant length R.

    Returns the circular-mean yaw (rad, with ``yaw_bias_rad`` applied) when
    R ≥ ``_TAG_YAW_MIN_RESULTANT`` and ≥2 valid frames were read; otherwise
    returns ``None`` so ``grasp_object`` SKIPS the instance (never blind-grasps).
    Yaw error scales as pixel_noise / tag_edge_pixels, so averaging tightens the
    wrist roll and R rejects an unstable / occluded tag.

    Called only for the SINGLE instance being grasped (held under motion_lock by
    the caller) — never for every detection — so the N detects are cheap. On any
    setup failure (missing calibration / corners) falls back to ``fallback_yaw``
    (the single-frame value already on the detection)."""
    board_z = getattr(ctx, 'board_table_z', None)
    if (ctx.scene_intrinsics is None or ctx.scene_extrinsics is None
            or board_z is None or ctx.z_table is None or tag_id is None):
        return fallback_yaw
    if _TAG_YAW_FRAMES <= 1:
        return fallback_yaw
    from physical_ai_server.workflow.tag_pose import (
        circular_mean_resultant,
        tag_edge_length_base,
        tag_yaw_base,
    )
    K = ctx.scene_intrinsics['K']
    dist = ctx.scene_intrinsics['dist']
    T = ctx.scene_extrinsics
    tag_plane_z = float(board_z) + float(recipe.object_height_m)
    cat = getattr(ctx, 'object_catalog', None)
    expected_edge = float(getattr(cat, 'tag_size_m', 0.0) or 0.0)
    yaws: list[float] = []
    want = int(tag_id)
    for i in range(_TAG_YAW_FRAMES):
        if i > 0:
            time.sleep(_TAG_YAW_FRAME_INTERVAL_S)
        try:
            bgr = _scene_frame(ctx)
        except WorkflowError:
            # A stale/absent frame mid-burst: stop sampling and decide on what
            # we have (the resultant/count gate below catches too-few frames).
            break
        dets = ctx.perception.detect(
            bgr, camera='scene', mode='apriltag', aruco_id=want)
        d = next((x for x in dets if x.aruco_id == want), None)
        if d is None or getattr(d, 'corners_px', None) is None:
            continue
        y = tag_yaw_base(d.corners_px, K, dist, T, tag_plane_z)
        if y is None:
            continue
        # Same edge sanity gate as the single-frame path.
        if expected_edge > 0.0 and _TAG_EDGE_TOL_FRAC > 0.0:
            edge = tag_edge_length_base(d.corners_px, K, dist, T, tag_plane_z)
            if edge is None or abs(edge - expected_edge) > _TAG_EDGE_TOL_FRAC * expected_edge:
                continue
        yaws.append(float(y))
    if len(yaws) < 2:
        return None
    res = circular_mean_resultant(yaws)
    if res is None:
        return None
    mean, R = res
    if R < _TAG_YAW_MIN_RESULTANT:
        ctx.log(
            f'[WARNUNG] Tag {want}: Ausrichtung über {len(yaws)} Bilder zu '
            f'unstabil (R={R:.2f}) — Ausrichtung verworfen.'
        )
        return None
    return _apply_yaw_bias(ctx, mean)


def _note_grasp_check_unavailable(ctx) -> None:
    """Log ONCE per workflow that the grasp-success check is unavailable (no
    follower-joint readback), so the fall-back-to-claim behaviour is visible
    without spamming the log on every grasp."""
    if getattr(ctx, '_grasp_check_warned', False):
        return
    try:
        ctx._grasp_check_warned = True
    except Exception:
        pass
    try:
        ctx.log(
            '[WARNUNG] Greif-Erfolgskontrolle nicht verfügbar (keine Gelenkdaten) '
            '— Objekt wird nach dem Greifen als erledigt markiert.'
        )
    except Exception:
        pass


def grasp_object(ctx, args: dict[str, Any]) -> None:
    """Detect the nearest reachable UNCLAIMED instance of the chosen object type
    and grasp it top-down with the live tag-derived wrist roll + per-object
    gripper close, verify the grasp actually HELD, then mark that tag CLAIMED.

    Grasp-success check + retry (#2): after the gripper closes,
    ``motion.check_grasp_held`` reads the achieved gripper angle — an empty close
    reaches ≈ GRIPPER_CLOSED_RAD, a held object stops the jaws partway open. On a
    MISS the whole detect→select→grasp retries up to ``GRASP_RETRY`` times
    (retreating to the observation pose between tries so the arm doesn't occlude
    the object); on a miss after the last try the instance is SKIPPED (never
    claimed) and ``GraspSkip`` is raised. When the joint readback is unavailable
    (``None``) the prior claim-on-completion behaviour is kept (no regression).
    detect→select→grasp is held under motion_lock so a concurrent when_object_seen
    hat can't grab the same instance between the detect and the pickup (TOCTOU,
    §12).

    Recoverable per-instance failures (nothing visible right now, every instance
    out of reach, the orientation couldn't be read, or the grab failed after
    retries) mark the offending tag(s) SKIPPED and raise ``GraspSkip`` — the loop
    swallows it and continues on the rest, a standalone „greife" fails loud
    (``GraspSkip`` IS a ``WorkflowError``). A HARD failure (missing calibration)
    raises the base ``WorkflowError`` so the loop ABORTS instead of spinning
    forever."""
    type_name = args.get('object_type')
    if not type_name:
        raise WorkflowError('Kein Objekt ausgewählt.')
    recipe = _recipe_for(_require_catalog(ctx), type_name)
    lock = getattr(ctx, 'motion_lock', None)
    if lock is not None:
        lock.acquire()
    try:
        attempts = _motion.GRASP_RETRY + 1
        for attempt in range(attempts):
            detections = _detect_named(ctx, type_name, exclude_ids=_excluded_ids(ctx))
            if not detections:
                # The object vanished between the loop's count and this grasp (or a
                # standalone „greife" with nothing in view). Recoverable: the loop
                # re-detects next pass; standalone fails loud.
                raise GraspSkip(
                    f'Kein „{recipe.label_de}" sichtbar — bitte das Objekt in den '
                    'markierten Greifbereich legen.'
                )
            target = _select_nearest_reachable(ctx, detections)
            if target is None:
                # Calibration incomplete (world_xyz_m unset) → _resolve_target
                # raises the precise German calib message — a HARD WorkflowError so
                # the loop aborts (calibration won't fix itself mid-run).
                if all(d.world_xyz_m is None for d in detections):
                    _motion._resolve_target(detections[0], ctx)   # raises calib error
                # Calibrated but every visible instance is out of reach. SKIP them
                # all so the loop terminates (next pass excludes them) instead of
                # retreat→redetect→fail forever; standalone fails loud.
                for d in detections:
                    _skip_tag(ctx, d.aruco_id)
                raise GraspSkip(
                    f'„{recipe.label_de}" gesehen, aber außerhalb des Greifbereichs — '
                    'bitte näher in den markierten Bereich legen.'
                )
            _motion._require_seeded_start_pose(ctx)
            x, y, z = target.world_xyz_m
            # Refine the SELECTED target's yaw over N frames (circular mean + R
            # gate) before committing the wrist roll — single-frame corner noise
            # mis-pinches the jaw. Multi-sample only this one instance (object
            # static pre-grasp), not every detection. R below threshold / <2 valid
            # frames → yaw None → the skip-on-unreadable-yaw path below handles it.
            tag_yaw = _multiframe_tag_yaw(
                ctx, recipe, target.aruco_id, target.extras.get('tag_yaw'))
            if tag_yaw is None:
                # Orientation couldn't be recovered (bad corners / failed the
                # tag-edge sanity gate). Do NOT commit a blind fixed-roll grasp —
                # on an elongated object that pinches the wrong (long) axis and
                # topples it. SKIP this instance so the loop tries the next one.
                _skip_tag(ctx, target.aruco_id)
                raise GraspSkip(
                    f'„{recipe.label_de}" erkannt, aber die Ausrichtung konnte nicht '
                    'bestimmt werden — bitte den Tag flach und gut sichtbar aufkleben.'
                )
            roll = _motion.compute_grasp_roll(ctx, x, y, tag_yaw)
            close_rad = float(target.extras.get('gripper_close_rad', _motion.GRIPPER_CLOSED_RAD))
            _motion._execute_pickup(
                ctx, (x, y, z), float(recipe.approach_clear_m), roll, close_rad)
            held = _motion.check_grasp_held(ctx)
            if held is False:
                # The jaws closed empty — the object slipped or was mis-pinched.
                if attempt < attempts - 1:
                    ctx.log(
                        f'[WARNUNG] „{recipe.label_de}" nicht gegriffen — neuer '
                        'Versuch.'
                    )
                    # Retreat out of the scene-cam view before re-detecting so the
                    # arm doesn't occlude the object on the retry frame.
                    _motion.go_to_observation_pose(ctx)
                    continue
                # Retries exhausted: SKIP this instance so the loop makes progress;
                # a standalone „greife" fails loud (GraspSkip IS a WorkflowError).
                # Do NOT claim a missed grab.
                _skip_tag(ctx, target.aruco_id)
                raise GraspSkip(
                    f'„{recipe.label_de}" konnte nicht gegriffen werden — bitte das '
                    'Objekt prüfen und neu in den Greifbereich legen.'
                )
            # held is True (object held) OR None (no readback → keep the prior
            # claim-on-completion behaviour). Claim either way so the loop makes
            # progress and never re-grabs this instance.
            if held is None:
                _note_grasp_check_unavailable(ctx)
            _claim_tag(ctx, target.aruco_id)
            # Per-pass positive feedback (#7): a brief success line so the student
            # sees progress (the loop only logged warnings before).
            try:
                ctx.log(f'„{recipe.label_de}" gegriffen.')
            except Exception:
                pass
            return
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


def see_object(ctx, args: dict[str, Any]) -> bool:
    """True when at least one UNCLAIMED instance of the chosen type is currently
    visible (visibility only — does not require grasp calibration)."""
    return bool(_detect_named_unclaimed(ctx, args.get('object_type')))


def count_object(ctx, args: dict[str, Any]) -> int:
    """Number of currently-visible UNCLAIMED instances of the chosen type."""
    return len(_detect_named_unclaimed(ctx, args.get('object_type')))


def wait_until_object_seen(ctx, args: dict[str, Any]) -> bool:
    """Poll until an UNCLAIMED instance of the chosen type is visible, or raise a
    German timeout. ``timeout`` seconds, default 10."""
    timeout_s = float(args.get('timeout', 10))
    type_name = args.get('object_type')
    return _poll_until(
        ctx,
        lambda: bool(_detect_named_unclaimed(ctx, type_name)),
        timeout_s,
        f'Objekt {type_name}',
    )


def wait_until_held(ctx, args: dict[str, Any]) -> bool:
    """„warte bis Greifer hält (max N s)" — VALUE (Boolean): poll until the
    gripper has closed on an object, or raise a German timeout. ``timeout``
    seconds, default 10.

    Mirrors ``grasp_held``'s no-silent-False contract: when the follower-joint
    readback is unavailable, ``motion.check_grasp_held`` returns ``None`` — raise
    the German „keine Gelenkdaten" error (the poll surfaces it on the first
    tick) instead of silently looping to the timeout on every rig without
    follower-joint feedback.

    LIMITATION (position-only sensing, document-only fix 2026-06-30):
    ``check_grasp_held`` reads the achieved gripper angle, which is ABOVE
    ``GRASP_HELD_MAX_RAD`` whenever the gripper is OPEN (the rest state) — so
    this block returns True immediately if the gripper is open. It is only
    meaningful directly AFTER a close (e.g. „Greifer schließen" or a grasp).
    The block tooltip states this; tightening the shared ``check_grasp_held``
    band was deliberately deferred to avoid touching the rig-validated
    ``grasp_object`` path."""
    timeout_s = float(args.get('timeout', 10))

    def _held() -> bool:
        result = _motion.check_grasp_held(ctx)
        if result is None:
            raise WorkflowError(
                'Greif-Erfolgskontrolle ist nicht verfügbar (keine Gelenkdaten).'
            )
        return result is True

    return _poll_until(ctx, _held, timeout_s, 'Greifer hält etwas')


# ------------------------------------------------------------------
# Grasp-split value + claim blocks (Phase 1)
# ------------------------------------------------------------------
# The one-block ``grasp_object`` is decomposed so students compose the pick. A
# ``finde`` VALUE block yields a Greifziel (the selected Detection with refined
# yaw baked in); the motion blocks (handlers/motion.py) consume it; ``mark_done``
# claims its tag for the loop. The detector runs ONCE in ``find_object`` — the
# taught pattern latches the result into a variable and reuses it.
def find_object(ctx, args: dict[str, Any]):
    """„finde <Typ>" — VALUE: the nearest reachable UNCLAIMED instance of the
    chosen type as a Greifziel (a ``Detection`` with ``world_xyz_m`` +
    ``extras['tag_yaw']`` + ``extras['approach_clear_m']`` attached), refining its
    orientation over several frames before returning.

    Returns ``None`` when nothing graspable is currently visible (so the student
    null-checks: „falls Ziel …"). Raises the PRECISE German calibration error
    (not the generic one) when instances ARE visible but their world position is
    unset — mirroring ``grasp_object``'s disambiguation so the student fixes the
    right step. The detector runs ONCE here; latch the result into a variable and
    reuse it across the motion blocks rather than calling „finde" in each."""
    type_name = args.get('object_type')
    if not type_name:
        raise WorkflowError('Kein Objekt ausgewählt.')
    recipe = _recipe_for(_require_catalog(ctx), type_name)
    detections = _detect_named_unclaimed(ctx, type_name)
    if not detections:
        return None
    target = _select_nearest_reachable(ctx, detections)
    if target is None:
        # _select_nearest_reachable returns None for BOTH "calibration incomplete"
        # (world_xyz_m unset) AND "every instance out of reach". Disambiguate so an
        # uncalibrated rig gets the precise calib message instead of a silent None
        # the student would misread as "nothing there".
        if all(d.world_xyz_m is None for d in detections):
            _motion._resolve_target(detections[0], ctx)   # raises the precise calib error
        # Calibrated but every visible instance is out of reach: SKIP them all
        # (exactly like grasp_object, perception_blocks.py grasp branch) so a
        # „Solange sichtbar" loop's gate drops to 0 and it ends cleanly with
        # „nichts mehr sichtbar — fertig", instead of re-selecting them every pass
        # until the 3-pass stall guard trips with the scary „kein Fortschritt".
        for d in detections:
            _skip_tag(ctx, d.aruco_id)
        try:
            ctx.log(
                f'[WARNUNG] „{recipe.label_de}" gesehen, aber außerhalb des '
                'Greifbereichs — bitte näher in den markierten Bereich legen.'
            )
        except Exception:
            pass
        return None
    tag_yaw = _multiframe_tag_yaw(
        ctx, recipe, target.aruco_id, target.extras.get('tag_yaw'))
    if tag_yaw is None:
        # Orientation unreadable — SKIP this instance (exactly like grasp_object)
        # so a „Solange sichtbar" loop excludes it next pass and makes progress on
        # the rest, instead of re-selecting it every pass until the stall guard
        # trips and ends the whole loop. Returns None so a standalone „finde"
        # null-checks cleanly.
        _skip_tag(ctx, target.aruco_id)
        try:
            ctx.log(
                f'[WARNUNG] „{recipe.label_de}" erkannt, aber die Ausrichtung '
                'konnte nicht bestimmt werden — wird übersprungen.'
            )
        except Exception:
            pass
        return None
    target.extras['tag_yaw'] = tag_yaw
    target.extras['approach_clear_m'] = float(recipe.approach_clear_m)
    return target


def object_position(ctx, args: dict[str, Any]):
    """„Position von <Ziel>" — VALUE: the Greifziel's base-frame ``{x, y, z}`` so a
    found object's location can drive ``bewege zu`` / ``lege ab bei``. Raises the
    precise German calib error when the position isn't known yet."""
    ziel = args.get('ziel')
    if ziel is None:
        raise WorkflowError('Kein Greifziel — bitte zuerst „finde …" benutzen.')
    xyz = getattr(ziel, 'world_xyz_m', None)
    if xyz is None:
        _motion._resolve_target(ziel, ctx)   # raises the precise calib error
        raise WorkflowError('Position des Objekts ist unbekannt.')
    x, y, z = xyz
    return {'x': float(x), 'y': float(y), 'z': float(z)}


def grasp_held(ctx, args: dict[str, Any]) -> bool:
    """„Greifer hält etwas?" — VALUE (Boolean): True when the gripper closed on an
    object, False on an empty close. Raises a German error when the joint readback
    is unavailable — no silent False (that would mis-report on every rig without
    follower-joint feedback)."""
    result = _motion.check_grasp_held(ctx)
    if result is None:
        raise WorkflowError(
            'Greif-Erfolgskontrolle ist nicht verfügbar (keine Gelenkdaten).'
        )
    return bool(result)


def mark_done(ctx, args: dict[str, Any]) -> None:
    """„merke <Ziel> als erledigt" — CLAIM the Greifziel's tag so a
    „Solange <Typ> sichtbar" loop counts it done and makes progress (the split
    grasp path doesn't auto-claim like the one-block ``Greife``)."""
    ziel = args.get('ziel')
    if ziel is None:
        # Recoverable skip (consistent with the split motion blocks) so an
        # unguarded loop body moves on instead of aborting; standalone fails loud.
        raise GraspSkip(
            'Kein Greifziel — bitte „finde …" benutzen, das Ergebnis in einer '
            'Variable speichern und mit „falls" prüfen.'
        )
    tag_id = getattr(ziel, 'aruco_id', None)
    if tag_id is None:
        raise WorkflowError('Greifziel hat keine Marker-ID.')
    _claim_tag(ctx, tag_id)
