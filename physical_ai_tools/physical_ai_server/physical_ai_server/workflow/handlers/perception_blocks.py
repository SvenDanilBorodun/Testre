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


# Audit fix #12: classroom palette accepted by the German UI. Defined
# here (rather than imported from color_profile.py) so a perception
# handler isn't coupled to the calibration manager's import surface.
# Keep in sync with overlays/workflow/color_profile.py:DEFAULT_COLORS.
_ALLOWED_COLORS: frozenset[str] = frozenset({'rot', 'gruen', 'blau', 'gelb'})


def _validate_color(color: Any) -> str:
    """Validate a color string against the allowed set, raising a
    German WorkflowError on miss. Returns the normalized lower-case
    color. The Perception backend silently returns [] when the colour
    profile isn't loaded for an unknown color — that's a UX trap the
    student can't debug, so we fail loudly here instead.
    """
    if color is None:
        raise WorkflowError('Keine Farbe angegeben.')
    if not isinstance(color, str):
        raise WorkflowError(f'Ungültige Farbe: {color}.')
    normalized = color.strip().lower()
    if normalized not in _ALLOWED_COLORS:
        raise WorkflowError(
            f'Unbekannte Farbe: {color}. Erlaubt: rot, gruen, blau, gelb.'
        )
    return normalized


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


def _require_object_detector(ctx):
    if not ctx.perception.yolox_available():
        raise WorkflowError(
            'Objekt-Erkennung ist auf diesem Gerät nicht verfügbar '
            '(Erkennungs-Modell fehlt) — bitte Farb- oder Marker-Erkennung '
            'verwenden.'
        )


def _require_marker_detector(ctx):
    if not ctx.perception.apriltag_available():
        raise WorkflowError(
            'Marker-Erkennung ist auf diesem Gerät nicht verfügbar.'
        )


def _attach_world_xyz(ctx, detections: list) -> list:
    """Project pixel centroids of detections to base-frame XYZ on the
    table plane and push the bounding-box list to the WorkflowStatus
    publisher (so the editor can render an overlay).

    Failures are surfaced as ``WorkflowError`` rather than swallowed —
    the motion handlers' ``_resolve_target`` would otherwise read
    ``d.world_xyz_m == None`` and raise the generic "Ziel-Wert konnte
    nicht ausgewertet werden" message instead of pointing the student
    at the missing calibration step (audit §3.1).
    """
    if not detections:
        ctx.emit_detections([])
        return detections
    # Recover the object's (x, y) by intersecting the camera ray with the table
    # SURFACE plane (board_table_z), NOT the gripper grasp height (z_table). The
    # object sits ON the surface; z_table is ~a finger-length ABOVE it, so
    # intersecting the ray with z_table puts (x, y) at the wrong distance along
    # the ray — a 15-26 mm lateral error on a tilted camera (invisible for a
    # perfectly vertical camera).
    #
    # The world_xyz_m Z, however, stays z_table: motion.pickup derives its
    # grasp-descend target from world_xyz_m[2] (target[2] + clearance), and the
    # workspace-floor refusal compares against z_table / table_plane — both must
    # keep seeing the MEASURED grasp height, not the surface. So: (x, y) from the
    # surface projection, z = z_table. Both calibration values must be present
    # for a graspable detection; otherwise leave world_xyz_m unset so motion
    # raises the German "Tisch vermessen zuerst" error.
    board_z = getattr(ctx, 'board_table_z', None)
    if (ctx.scene_intrinsics is None or ctx.scene_extrinsics is None
            or board_z is None or ctx.z_table is None):
        # Push the detections so the bbox overlay still renders, but
        # leave world_xyz_m unset; downstream motion handlers will
        # raise a clear German error if they try to act on these.
        ctx.emit_detections(detections)
        return detections
    try:
        from physical_ai_server.workflow.projection import project_pixel_to_table
        K = ctx.scene_intrinsics['K']
        dist = ctx.scene_intrinsics['dist']
        T = ctx.scene_extrinsics
        grasp_z = float(ctx.z_table)
        # Flat surface plane only for the (x, y) recovery: the touch-off
        # table_plane (a, b, c) is the MEASURED EE-frame tilt, offset above the
        # surface, so projecting objects onto it would reintroduce the lateral
        # error this fix removes. It is reserved for motion's descend/floor.
        for d in detections:
            cx, cy = d.centroid_px
            point = project_pixel_to_table(cx, cy, K, dist, T, board_z)
            if point is not None:
                d.world_xyz_m = (float(point[0]), float(point[1]), grasp_z)
    except Exception as e:
        raise WorkflowError(
            f'Projektion fehlgeschlagen — bitte Kalibrierung prüfen: {e}'
        )
    ctx.emit_detections(detections)
    return detections


def detect_color(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    # Audit fix #12: validate before reaching Perception so an unknown
    # German color string surfaces an actionable error instead of an
    # empty-detections result that looks like "no color visible".
    color = _validate_color(args.get('color'))
    if not ctx.perception.has_color(color):
        raise WorkflowError(
            f'Farbe „{color}" ist nicht kalibriert — bitte im Kalibrier-Schritt '
            '„Farben lernen" erfassen.'
        )
    bgr = _scene_frame(ctx)
    detections = ctx.perception.detect(bgr, camera='scene', mode='color', color=color)
    return _attach_world_xyz(ctx, detections)


def detect_object(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    _require_object_detector(ctx)
    bgr = _scene_frame(ctx)
    # Audit fix #12: color is optional on detect_object (the block lets
    # the student narrow a class to a specific color). When provided,
    # validate to fail loudly on unknown values instead of silently
    # producing empty detections.
    color = args.get('color')
    if color is not None and str(color).strip():
        color = _validate_color(color)
    detections = ctx.perception.detect(
        bgr, camera='scene', mode='yolo+color',
        coco_class=args.get('class'),
        color=color,
    )
    return _attach_world_xyz(ctx, detections)


def detect_marker(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    _require_marker_detector(ctx)
    bgr = _scene_frame(ctx)
    marker_id = args.get('marker_id')
    if marker_id is not None:
        try:
            marker_id = int(marker_id)
        except (TypeError, ValueError):
            raise WorkflowError(f'Ungültige Marker-ID: {marker_id}')
    detections = ctx.perception.detect(
        bgr, camera='scene', mode='apriltag', aruco_id=marker_id,
    )
    return _attach_world_xyz(ctx, detections)


def count_color(ctx, args: dict[str, Any]) -> int:
    return len(detect_color(ctx, args))


def count_objects_class(ctx, args: dict[str, Any]) -> int:
    return len(detect_object(ctx, args))


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


def wait_until_color(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    # Audit fix #12: validate up-front so a bad color name fails the
    # block immediately rather than polling for `timeout_s` and then
    # raising a misleading "Farbe nicht erkannt" timeout.
    color = _validate_color(args.get('color'))
    return _poll_until(
        ctx,
        lambda: bool(detect_color(ctx, {'color': color})),
        timeout_s,
        f'Farbe {color}',
    )


def wait_until_object(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    coco_class = args.get('class')
    return _poll_until(
        ctx,
        lambda: bool(detect_object(ctx, {'class': coco_class})),
        timeout_s,
        f'Objekt {coco_class}',
    )


def wait_until_marker(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    marker_id = args.get('marker_id')
    return _poll_until(
        ctx,
        lambda: bool(detect_marker(ctx, {'marker_id': marker_id})),
        timeout_s,
        f'Marker {marker_id}',
    )


# Phase-3: open-vocabulary detection. Routes German prompts through a
# small synonym dict to YOLOX/D-FINE-N closed-vocab when possible
# (cheap + offline). Falls back to OWLv2 on Modal via the cloud bridge.
#
# The cloud_vision dict on ctx supplies:
#   {
#     'enabled': bool,                          # default False
#     'cloud_burst': callable,                  # (image_bgr, prompt, should_stop=None) -> list[Detection]
#     'translate': dict[str,str]                # German prompt -> COCO class label
#   }
# The handler asks ctx.cloud_vision['translate'] first; on miss, if
# cloud_burst is callable, posts the latest scene frame to it. The
# burst receives ctx.should_stop (audit O3) so it can abort between
# JPEG encode and the 15 s HTTP wait when /workflow/stop fires.
def detect_open_vocab(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    prompt = args.get('prompt')
    if not prompt:
        raise WorkflowError('Kein Suchbegriff angegeben.')
    prompt = str(prompt).strip()
    cv = getattr(ctx, 'cloud_vision', None) or {}
    translate = (cv.get('translate') or {}) if isinstance(cv, dict) else {}
    # Audit F1: each entry is a dispatch dict
    # ``{'mode':'object'|'color', 'class':<german>, 'color':<rot|...>}``,
    # NOT a bare class string. Forwarding the whole dict to detect_object
    # crashed at ``coco_class in COCO_CLASSES`` with TypeError.
    entry = translate.get(prompt.lower())
    if isinstance(entry, dict):
        mode = entry.get('mode')
        if mode == 'object':
            return detect_object(ctx, {
                'class': entry.get('class'),
                'color': entry.get('color'),
            })
        if mode == 'color':
            return detect_color(ctx, {'color': entry.get('color')})
        # Unknown mode falls through to the cloud path — defensive only.
    elif isinstance(entry, str):
        # Legacy single-string format (older synonym dicts) — treat as
        # an object class. Kept for forward-compat if the dict moves
        # to a YAML loader.
        return detect_object(ctx, {'class': entry})
    # Cloud path. Audit F54: respect the explicit `enabled` flag —
    # decoupling "cloud_burst is bound" from "student has opted in"
    # prevents quota/cost leak once the burst is wired.
    burst = cv.get('cloud_burst') if isinstance(cv, dict) else None
    if not cv.get('enabled') or not callable(burst):
        raise WorkflowError(
            f'Begriff "{prompt}" ist lokal nicht bekannt und Cloud-Erkennung '
            'ist deaktiviert. Bitte aktivieren oder einen bekannten Begriff '
            'verwenden.'
        )
    bgr = _scene_frame(ctx)
    try:
        # Audit O3: forward ctx.should_stop so the burst can short-
        # circuit on stop between encode and the HTTP send.
        try:
            detections = burst(bgr, prompt, ctx.should_stop)
        except TypeError:
            # Backwards compat: an older burst signature without the
            # should_stop kwarg. Fall back to the 2-arg call.
            detections = burst(bgr, prompt)
    except WorkflowError:
        raise
    except NotImplementedError as e:
        # Audit F56: the stub used to re-wrap its own message into
        # "Cloud-Erkennung fehlgeschlagen: Cloud-Erkennung ist…". Pass
        # the original German message through.
        raise WorkflowError(str(e))
    except Exception:
        # Audit M6: don't f-string the raw Exception into the student-
        # facing message — it leaks Python tracebacks / requests-lib
        # internals into German UI text. Log server-side, surface a
        # fixed German message to the student.
        import traceback
        try:
            ctx.log(f'[FEHLER] Cloud-Erkennung fehlgeschlagen: {traceback.format_exc()}')
        except Exception:
            pass
        raise WorkflowError('Cloud-Erkennung fehlgeschlagen — bitte erneut versuchen.')
    return _attach_world_xyz(ctx, detections or [])


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

    Differs from ``_attach_world_xyz`` (the color/COCO path) in three ways:
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
    German timeout (mirrors wait_until_marker). ``timeout`` seconds, default 10."""
    timeout_s = float(args.get('timeout', 10))
    type_name = args.get('object_type')
    return _poll_until(
        ctx,
        lambda: bool(_detect_named_unclaimed(ctx, type_name)),
        timeout_s,
        f'Objekt {type_name}',
    )
