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

from physical_ai_server.workflow import claims as _claims
from physical_ai_server.workflow.handlers import motion as _motion
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError

# Tag-edge sanity gate: the back-projected tag side length must be within this
# fraction of the catalog tag_size_m, else the corner geometry is untrustworthy
# and we treat the orientation as unknown (→ the instance is skipped rather than
# grasped with a wrong wrist roll). Rig-tunable; generous by default so only
# gross errors (≈2× scale) trip it. env-forwarding-guard: this var is forwarded
# in robotis_ai_setup/docker/docker-compose.yml.
#
# WHAT IT IS NOT, corrected 2026-09-07: this gate does NOT catch a mis-calibrated
# PLANE HEIGHT, which an earlier revision of this comment claimed for it. The
# back-projected edge scales with the assumed plane depth, so tripping a 0.5
# tolerance takes a plane error of roughly HALF the camera-to-table distance,
# while a plane error one order of magnitude smaller than that already ruins the
# grasp. Measured against the real ``tag_edge_length_base`` /
# ``project_pixel_to_table`` (102° HFOV / 640 px, camera 0.55 m up, pitched 25°,
# a 24 mm tag at base (0.18, 0) and (0.25, 0.10)):
#
#   plane error   edge deviation      lateral grasp offset      gate trips?
#      10 mm          1.8 %             5.1 mm /  6.6 mm            no
#      20 mm          3.6 %            10.2 mm / 13.2 mm            no
#      50 mm          9.1 %            25.5 mm / 33.1 mm            no
#     200 mm         36.4 %           101.9 mm / 132.4 mm           no
#     300 mm         54.5 %           152.8 mm / 198.7 mm          YES
#
# i.e. by the time the gate fires, the grasp point is out by more than the whole
# object. It is a ~2× WRONG-OBJECT-SCALE detector — the wrong tag size in the
# catalog, or a tag printed at the wrong scale — and it is a good one for that.
# Tightening the tolerance is NOT the fix: corner noise alone moves the recovered
# edge by percent, so a threshold small enough to see 20 mm of plane error would
# reject legitimate detections. Detecting a wrong plane height needs a second,
# independent measurement of it (the touch-off's z_table vs the extrinsic's
# board_table_z already are two such measurements, cross-checked at calibration
# time by TABLE_TOUCH's 0.12 m camera cross-check) — not a stricter edge gate.
_TAG_EDGE_TOL_FRAC = max(0.0, _motion._safe_float('EDUBOTICS_TAG_EDGE_TOL_FRAC', 0.5))

# Multi-frame yaw averaging for the grasped instance. Yaw error scales as
# pixel_noise / tag_edge_pixels, so a single frame's corner noise mis-pinches
# the wrist. We sample N live scene frames of the SAME tag, circular-mean the
# per-frame tag_yaw_base, and gate on the mean resultant length R (≈1 = tight
# agreement, low = scattered/untrustworthy → drop the yaw so the existing
# skip-on-unreadable-yaw path handles it rather than blind-grasping).
# env-forwarding-guard: both vars are forwarded in
# robotis_ai_setup/docker/docker-compose.yml.
#
# CLAMPED AT BOTH ENDS, and the ceiling is load-bearing rather than tidy.
# ``_sample_tag_yaw`` runs ``2 * _TAG_YAW_FRAMES`` detect attempts spaced
# ``_TAG_YAW_FRAME_INTERVAL_S``, measured at ≈ 0.0752 s PER FRAME (7 → 0.510 s,
# 200 → 15.03 s, 8000 → ≈ 601 s), and its only caller is ``grasp_object``, which
# holds ``ctx.motion_lock`` for the WHOLE grasp — doubled again by
# ``GRASP_RETRY + 1`` attempts. Since the motion lock lost its bound
# (``motion._hold_motion_lock``), "every holder is bounded" is the argument that
# replaces it, so an UNCAPPED frame count would be a hole in that argument, not
# a nit. 30 frames ≈ 1.8 s of sampling, already 4× the shipped default.
#
# The floor is not decoration either: ``_safe_float`` catches TypeError and
# ValueError but the surrounding ``int()`` does NOT, so
# ``EDUBOTICS_TAG_YAW_FRAMES=inf`` raised OverflowError and ``=nan`` ValueError
# AT IMPORT — taking ``handlers/__init__``'s dispatch tables down with this
# module, which is the exact cascade ``_safe_float`` exists to prevent. The
# clamp below is written over a float and only then narrowed to an int, so both
# faults die together. Out-of-range values FALL BACK loudly, mirroring the
# Feetech driver's own edge/torque tick knobs. (Their shared env-var prefix is
# deliberately NOT spelled out in this comment: ci.yml::env-forwarding-guard
# greps `EDUBOTICS_[A-Z0-9_]+` over this whole package and its match stops at a
# `*`, so a prose "…_EDU6_*" reads as a forwarded-key NAME and fails the build.)
_TAG_YAW_FRAMES_MAX = 30


def _frames_de(n: int) -> str:
    return '1 Bild' if n == 1 else f'{n} Bilder'


def _clamped_tag_yaw_frames() -> int:
    raw = _motion._safe_float('EDUBOTICS_TAG_YAW_FRAMES', 7.0)
    if not math.isfinite(raw):
        print('[WARNUNG] EDUBOTICS_TAG_YAW_FRAMES ist kein gültiger Wert — es '
              f'werden {_frames_de(7)} verwendet.', flush=True)
        return 7
    clamped = min(float(_TAG_YAW_FRAMES_MAX), max(1.0, raw))
    if clamped != raw:
        print(f'[WARNUNG] EDUBOTICS_TAG_YAW_FRAMES={raw:g} liegt außerhalb des '
              f'erlaubten Bereichs (1 bis {_TAG_YAW_FRAMES_MAX}) — es '
              f'werden {_frames_de(int(clamped))} verwendet.', flush=True)
    return int(clamped)


_TAG_YAW_FRAMES = _clamped_tag_yaw_frames()
_TAG_YAW_MIN_RESULTANT = min(
    1.0, max(0.0, _motion._safe_float('EDUBOTICS_TAG_YAW_MIN_RESULTANT', 0.9)))
# Spacing between consecutive yaw-sampling frames (~30 ms, plan W3b).
_TAG_YAW_FRAME_INTERVAL_S = 0.03

# „Wenn <Typ> gesehen" ABSENCE GRACE: how long a tag must be continuously unseen
# before the object hat believes it is really gone. ``workflow_manager.py``
# imports THIS constant as ``_HAT_ABSENT_GRACE_S`` (rather than defining a second
# one that would drift), and is now its ONLY consumer — do not delete it.
#
# IT IS NO LONGER A RECLAIM KNOB. The recycled-object reclaim below reasons about
# POSITION, not absence, because the absence clock is sampled at LOOP-PASS
# cadence, not frame cadence: WHILE_EMPTY_SECONDS alone is 5.0 s and a
# grasp-bearing „Solange sichtbar" pass measures 8–12 s, so "absent for ONE poll"
# ALWAYS exceeded 1.5 s. Measured 2026-09-07 with ONE missed AprilTag look right
# after a grasp: the SAME cube was grasped twice, at t = 0.28 s and t = 9.11 s,
# with „wurde zurückgelegt — wird erneut gegriffen." printed although nobody had
# touched it. The hat is a different question with a different sampling rate (it
# polls several times a second), so the same seconds are right THERE.
# env-forwarding-guard: this var is forwarded in
# robotis_ai_setup/docker/docker-compose.yml.
_RECLAIM_ABSENT_S = max(0.0, _motion._safe_float('EDUBOTICS_RECLAIM_ABSENT_S', 1.5))

# Recycled-object reclaim (#1): how far (metres, base-frame table plane) a
# claimed/skipped object must have MOVED before it counts as "a person put this
# somewhere else" and it is un-claimed/un-skipped so it is grabbed again. See
# ``_reclaim_recycled`` for the THREE rules (ANCHOR / PICK / SKIP anchoring);
# the first two act on a SIGHTING, never on an absence, so a missed AprilTag
# look cannot reclaim at any miss rate.
#
# WHY 20 mm. Position noise, measured over 200 frames of a STATIONARY tag through
# the shipped ``Perception`` + ``projection.project_pixel_to_table``: σ ≤ 0.06 mm,
# worst single-frame deviation 0.164 mm, holding across pixel noise σ 3→30, motion
# blur, and apparent tag sizes 16–45 px. The systematic terms (intrinsics,
# extrinsics, assumed plane height) are common-mode between two observations of
# the same tag and cancel in the DIFFERENCE, so only that noise floor applies
# here. 20 mm is therefore ~120× the worst observed noise, while still well inside
# the 30 mm cube — a student who nudges an object by less than two-thirds of its
# own width is not asking for it to be picked up again.
#
# ``EDUBOTICS_RECLAIM_MOVE_M=0`` (or negative) DISABLES the reclaim outright — the
# one-variable rollback, and a strictly safer one than the knob it replaces: with
# it set, nothing can ever re-grasp an object the program already placed.
# env-forwarding-guard: forwarded in robotis_ai_setup/docker/docker-compose.yml +
# docker-compose.opi.yml.
_RECLAIM_MOVE_M = _motion._safe_float('EDUBOTICS_RECLAIM_MOVE_M', 0.02)

# Rule §2 — the grasp height follows the MEASURED table plane at the object's
# own (x, y). The knob, the measurement and the shared helper all live in
# handlers/motion.py (``GRASP_Z_FROM_PLANE`` / ``table_z_at``) because a SECOND
# site — physical_ai_server.py::mark_destination_callback — needs exactly the
# same computation and exactly the same rollback, and two copies of a plane
# evaluation are how this defect appeared in the first place.


def _safe_log(ctx, message: str) -> None:
    """Log a German line, swallowing any sink error — a diagnostic must never
    break a run (the same contract every ``try: ctx.log(...)`` in this module
    already uses, in one place)."""
    try:
        ctx.log(message)
    except Exception:  # noqa: BLE001 — a diagnostic never breaks the run
        pass


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


def _poll_until(ctx, predicate, timeout_s: float, label: str,
                raise_on_timeout: bool = True) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout_s`` elapses.

    ``raise_on_timeout=True`` (the default, and what every non-block caller
    wants) raises a German ``WorkflowError`` on timeout. The two „warte bis …"
    BLOCKS pass ``False``, because they declare ``output: 'Boolean'``: Blockly
    therefore lets a student drop them into „falls … sonst", and a raising
    timeout made the sonst branch UNREACHABLE — the run aborted instead of
    taking it. Measured 2026-09-07: „warte bis wuerfel sichtbar" with nothing in
    view raised „Timeout: Objekt wuerfel nicht erkannt." (which also leaked the
    raw catalog KEY where the catalog carries label_de „Würfel"). Returning
    ``False`` is what the declared Boolean type promises; the student still sees
    the timeout, as a German [WARNUNG] emitted by the caller.

    (This also retires the docstring's note about a dead ``on_timeout='continue'``
    affordance "reading from a block field that never existed" — the behaviour it
    described is now the blocks' actual, typed contract.)

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
        if raise_on_timeout:
            raise WorkflowError(f'Timeout: {label} nicht erkannt.')
        return False
    finally:
        if released and motion_lock is not None:
            # Restore the caller's invariant unconditionally. This used to be a
            # bounded acquire that RAISED „Bewegung-Sperre konnte nicht
            # zurückgewonnen werden." on timeout, claiming it did so "so the
            # caller's `with motion_lock` __exit__ has SOMETHING to release" —
            # on that branch it has precisely nothing. Driven end to end, the
            # raise left `RuntimeError: cannot release un-acquired lock`
            # escaping into `_run_hat_handler`'s OUTER bare `except Exception:
            # return`: the hat died with no message, no error count and no
            # retirement warning, run green. It also MASKED an in-flight Stop.
            # See motion._reacquire_after_release.
            _motion._reacquire_after_release(ctx)


def _check_grasp_held_locked(ctx):
    """Run ``motion.check_grasp_held`` under ``ctx.motion_lock`` (when present).

    The hat-side value blocks (``grasp_held`` / ``wait_until_held``) used to
    read the held threshold + joint readback LOCK-FREE, racing a concurrent
    close on the main stack (the threshold derives from
    ``ctx.last_commanded_close_rad``, which the close paths write under the
    lock). Serializing the whole check shrinks that race to nothing. RLock →
    nest-safe when the caller (e.g. a hat handler body, or ``grasp_object``)
    already holds the lock. Goes through the ONE acquire helper, like every
    other site (``motion._hold_motion_lock``). It used to be a raw
    ``acquire(timeout=10.0)`` that answered a Stop with a „Bewegung blockiert"
    error naming a RESTART as the remedy — i.e. a Stop reported to the student
    as a lock error, advising the one action that reproduces it identically."""
    acquired = _motion._hold_motion_lock(ctx)
    try:
        return _motion.check_grasp_held(ctx)
    finally:
        _motion._release_motion_lock(ctx, acquired)


# ------------------------------------------------------------------
# Named-object detection + grasp (Roboter Studio AprilTag grasping)
# ------------------------------------------------------------------
# A printed object carries a unique AprilTag whose id maps (via the FIXED,
# fleet-wide object set on ctx — workflow/object_catalog.py::_FIXED_CATALOG) to a
# TYPE + grasp recipe. The named-object blocks detect by TYPE, attach the exact
# base-frame grasp point + tag yaw, and grasp top-down with a live tag-derived
# wrist roll. See workflow/object_catalog.py + workflow/tag_pose.py.
def _require_catalog(ctx):
    """Return the loaded ObjectCatalog from ctx, or raise the German load error
    (catalog is loaded tolerantly at workflow start — a non-named workflow runs
    fine without it; a named block fails loud here). With the fixed hardcoded set
    this only fires on a developer typo in the constant, never on a student PC."""
    cat = getattr(ctx, 'object_catalog', None)
    if cat is None:
        err = getattr(ctx, 'object_catalog_error', None)
        raise WorkflowError(
            err or 'Objekt-Katalog konnte nicht geladen werden. '
            'Bitte wende dich an deine Lehrkraft.'
        )
    return cat


# The React dropdown's placeholder VALUE. When GetObjectCatalog has not answered
# yet (or answered empty), `_objectTypePlaceholder` / `_objectTypeEmpty` in
# blocks/perception.js both serialise this sentinel into the saved workflow, and
# all four object blocks then reached the runtime with it as their object_type —
# so the student was shown „Unbekanntes Objekt „__none__"", an internal token
# they cannot act on. Name the real situation instead.
_OBJECT_TYPE_PLACEHOLDER = '__none__'
_NO_OBJECT_TYPES_DE = (
    'Es ist noch kein Objekt ausgewählt — die Objektliste war beim Öffnen noch '
    'nicht geladen. Bitte den Block anklicken und das Objekt aus der Liste neu '
    'auswählen.'
)


def _recipe_for(cat, type_name):
    """Look up a grasp recipe, translating the catalog's ObjectCatalogError
    (German) into a WorkflowError so the runtime surfaces it as a clean student
    message instead of the generic "Interner Fehler"."""
    from physical_ai_server.workflow.object_catalog import ObjectCatalogError
    if str(type_name).strip() == _OBJECT_TYPE_PLACEHOLDER:
        raise WorkflowError(_NO_OBJECT_TYPES_DE)
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


# Per-run claim/skip bookkeeping lives in workflow.claims so handlers.motion can
# reach it without a circular import (perception_blocks imports motion at module
# scope, so the reverse edge cannot exist). Re-exported under the original private
# names: ~10 internal call sites and several tests import them from here.
_excluded_ids = _claims.excluded_ids
_claim_tag = _claims.claim_tag
_skip_tag = _claims.skip_tag


def _claim_store(ctx, name: str, factory):
    """Fetch — lazily creating once — one of the reclaim's per-tag stores on ctx.

    ``WorkflowContext`` declares all three as fields; the lazy create is what
    keeps a minimal ctx (every unit-test double in this suite, and any older
    context object) working exactly as the previous absence tracker did."""
    store = getattr(ctx, name, None)
    if store is None:
        store = factory()
        try:
            setattr(ctx, name, store)
        except Exception:  # noqa: BLE001 — a frozen ctx just gets per-call state
            pass
    return store


# The German the reclaim emits, ONE template per rule, in ONE place.
#
# It is a module-level table rather than three literals inside ``_reclaim``
# because the tests FILTER the Protokoll for these sentences: a filter that
# lists two of three variants keeps passing VACUOUSLY on the third — which is
# exactly the shape of the defect this table was introduced to fix. The test
# imports this dict, so adding a fourth rule cannot orphan the filter.
#
# „an DER alten Stelle", not „an SEINEM alten Platz": „sein/ihr" is a POSSESSIVE
# and agrees with the possessor („seinem" for der Würfel, „ihrem" for die
# Kugel), which would re-introduce the gender leak these messages exist without.
# „die Stelle" carries no information about the label at all.
_RECLAIM_TEMPLATES_DE = {
    # The object was carried away by the robot and is back on the spot it was
    # picked FROM. The rule fires on ``_back_at``, i.e. NOT elsewhere.
    'pick': '„{label}" #{tag} liegt wieder an der alten Stelle — '
            'wird noch einmal gegriffen.',
    # It was left somewhere by the robot and has since moved away from there.
    'anchor': '„{label}" #{tag} liegt jetzt woanders — '
              'wird noch einmal gegriffen.',
    # It was never carried at all (a SKIPPED object) and somebody moved it.
    'skip': '„{label}" #{tag} wurde bewegt — neuer Versuch.',
}


def _reclaim_recycled(ctx, recipe, visible_xy) -> None:
    """Un-claim / un-skip an object A PERSON moved, so it is grabbed again (#1).

    ``visible_xy`` maps every currently-visible tag id OF THIS TYPE to its
    base-frame table position ``(x, y)`` from :func:`_tag_table_xy`, or to
    ``None`` for a tag this rig cannot locate. A tag simply ABSENT from the dict
    is the third, distinct case.

    Three rules decide, and the first two both act on a SIGHTING — never on an
    absence — which is why a missed AprilTag look can no longer reclaim at ANY
    miss rate:

    * **ANCHOR** — the first sighting AFTER the claim records where the robot
      LEFT the object. Anchoring on the OBSERVED rest position rather than the
      commanded destination absorbs a bounce from ``DROP_HEIGHT_M`` for free. A
      later sighting ≥ ``_RECLAIM_MOVE_M`` away proves somebody moved it.
    * **PICK** — an object that has been unseen at least once since its claim and
      then turns up back within ``_RECLAIM_MOVE_M`` of the spot it was PICKED
      from can only have been put there by a person; the robot carried it away.
      The prior-absence precondition is what keeps a degenerate program whose
      drop point IS its pick point terminating.
    * **SKIP anchoring** — a SKIPPED object was never moved by the robot, so its
      anchor is simply where it lay when we gave up. That lets a student slide an
      out-of-reach object into reach and have it retried in the same run.

    WHY NOT ABSENCE, and the trap inside this design. The absence clock is
    sampled at LOOP-PASS cadence (8–12 s per grasp-bearing pass), so ONE missed
    look was indistinguishable from a student lifting the object — and no counter
    value separates them, because a student who SLIDES the object back is never
    absent at all. Anchoring on the last sighting BEFORE an absence re-creates
    the original defect exactly: pass k sees the object at its pick spot, the body
    places it at the drop spot, pass k+1 misses it (freezing the anchor at the
    pick spot), pass k+2 sees it at the drop spot → „moved 100 mm" → a spurious
    re-grasp. **The anchor must be observed AFTER the claim.**

    Mutates claimed_tags / skipped_tags / claim_anchor / claim_pick_xy /
    claim_unseen under claim_lock. No-op when the claim state is absent (e.g. a
    unit-test ctx without the sets), and a no-op in the simulator, which has no
    scene intrinsics, so every position is ``None`` and the reclaim fails
    closed."""
    if _RECLAIM_MOVE_M <= 0.0:
        # One-variable rollback: the reclaim is OFF. Returning here (rather than
        # letting a 0 m threshold through) matters — ``distance >= 0`` is true of
        # EVERY sighting, so a fall-through would reclaim everything.
        return
    claimed = getattr(ctx, 'claimed_tags', None)
    skipped = getattr(ctx, 'skipped_tags', None)
    if claimed is None or skipped is None:
        return
    anchors = _claim_store(ctx, 'claim_anchor', dict)
    pick_xy = _claim_store(ctx, 'claim_pick_xy', dict)
    unseen = _claim_store(ctx, 'claim_unseen', set)
    type_ids = {int(i) for i in recipe.tag_ids}
    try:
        visible = {int(k): v for k, v in dict(visible_xy).items()}
    except Exception:  # noqa: BLE001 — an unusable view reclaims nothing
        return
    lock = getattr(ctx, 'claim_lock', None)

    def _gap(a, b):
        """Distance between two table positions, or ``None`` when either side is
        unknown/malformed. ``None`` never satisfies a comparison below, so an
        unlocatable sighting fails BOTH rules closed."""
        if a is None or b is None:
            return None
        try:
            return math.hypot(float(a[0]) - float(b[0]),
                              float(a[1]) - float(b[1]))
        except Exception:  # noqa: BLE001 — a malformed position never reclaims
            return None

    def _moved(a, b) -> bool:
        gap = _gap(a, b)
        return gap is not None and gap >= _RECLAIM_MOVE_M

    def _back_at(a, b) -> bool:
        gap = _gap(a, b)
        return gap is not None and gap < _RECLAIM_MOVE_M

    def _reclaim(tag: int, rule: str) -> None:
        claimed.discard(tag)
        skipped.discard(tag)
        anchors.pop(tag, None)
        unseen.discard(tag)
        # EACH RULE STATES ITS OWN OBSERVATION, and the `rule` argument is why.
        # The two rules shared one sentence, and the PICK rule fires on
        # ``_back_at`` — i.e. having JUST ESTABLISHED the object is NOT
        # elsewhere — so „liegt jetzt woanders" printed the exact opposite of
        # what was measured, 100 % of the time, on the put-back demo this rule
        # exists for. (PICK ⇒ claimed, by construction: an unclaimed-and-
        # unskipped tag `continue`s earlier, and a skipped-not-claimed tag
        # either has its anchor set by the SKIP branch or has no pick spot at
        # all, and ``_back_at(pos, None)`` is False.)
        #
        # `rule` is PASSED, never re-derived here: this function pops the anchor
        # as its third statement, so any predicate evaluated inside it would be
        # reading state the caller has already destroyed.
        #
        # The tag id is written the way the PRINTED SHEET writes it
        # (tools/generate_apriltags.py renders „#20 Würfel"), so a student can
        # look the number up on the paper in front of them. And every sentence
        # states what was OBSERVED — never an intention nobody watched
        # („wurde zurückgelegt" claimed to know that).
        _safe_log(ctx, _RECLAIM_TEMPLATES_DE[rule].format(
            label=recipe.label_de, tag=tag))

    def _update():
        for tag in sorted(type_ids):
            is_claimed = tag in claimed
            is_skipped = tag in skipped
            if not is_claimed and not is_skipped:
                # Still up for grabs: remember where it LIES, so that once it is
                # claimed we know the spot it was picked from (the PICK rule's
                # reference point).
                pos = visible.get(tag)
                if pos is not None:
                    pick_xy[tag] = pos
                continue
            if tag not in visible:
                # An absence is evidence of NOTHING here. Record only that it
                # happened — the PICK rule's precondition — and change nothing
                # else, so no rule can ever fire off a missed look.
                unseen.add(tag)
                continue
            pos = visible.get(tag)
            if tag not in anchors and is_skipped and not is_claimed:
                # SKIP anchoring: the robot never moved this one, so it lies
                # where it lay when we gave up on it.
                pick = pick_xy.get(tag)
                if pick is not None:
                    anchors[tag] = pick
            if tag not in anchors:
                if tag in unseen and _back_at(pos, pick_xy.get(tag)):
                    _reclaim(tag, 'pick')                  # PICK rule
                    continue
                # First sighting AFTER the claim: this is where the robot left
                # it. ``None`` is stored deliberately — an unlocatable rest
                # position fails the reclaim closed for this tag from here on.
                anchors[tag] = pos
                unseen.discard(tag)
                continue
            unseen.discard(tag)
            if _moved(pos, anchors.get(tag)):
                _reclaim(tag, 'anchor' if tag in claimed else 'skip')

    if lock is not None:
        with lock:
            _update()
    else:
        _update()


def _all_instances_done(ctx, recipe) -> bool:
    """True when EVERY tag id of this type is already claimed or skipped.

    ``_detect_named``'s non-reclaim path returns ``[]`` BEFORE it ever grabs a
    frame when the wanted set is empty, so „Kein „Würfel" sichtbar — bitte das
    Objekt in den markierten Greifbereich legen." was emitted without looking at
    the camera at all. Measured 2026-09-07 with two cubes in plain view, both
    grasped earlier: that exact sentence, with the camera detect call count at
    ZERO. The remedy is wrong twice over — the objects ARE in the Greifbereich,
    and putting them back would not help, because they are marked done."""
    try:
        return not (set(int(i) for i in recipe.tag_ids)
                    - {int(i) for i in _excluded_ids(ctx)})
    except Exception:  # noqa: BLE001 — a diagnostic never breaks the run
        return False


def _nothing_to_grasp_message(ctx, recipe) -> str:
    """The right German sentence for "no graspable instance right now".

    NO ARTICLE, PRONOUN OR RELATIVE PRONOUN MAY AGREE WITH ``label_de``. The
    label is a per-type string a teacher will one day author („Eigene Objekte"),
    so anything inflected around it is wrong for some type: this read „es ist
    KEINES mehr übrig, DAS gegriffen werden könnte" (neuter, for *der Würfel* —
    the only object in the shipped catalog) and „bitte EIN „Würfel" kurz
    WEGNEHMEN" (`wegnehmen` governs the accusative, so „einen"). Both were wrong
    on the rig as shipped.

    The fix is the neuter countable noun **„Objekt"**, which is not an invention:
    ``blocks/perception.js`` already says „ein Objekt dieses Typs" and „Jedes
    Objekt wird nur einmal gegriffen" in the tooltips of these very blocks, and
    the sentence below already said „bitte das Objekt in den markierten
    Greifbereich legen". A per-type ``genus`` field was considered and rejected:
    German needs gender × CASE, ``GraspRecipe`` is a frozen dataclass whose field
    order is load-bearing, and a declension table cannot be authored by a
    twelve-year-old naming their own object.
    """
    if _all_instances_done(ctx, recipe):
        return (
            f'Alle „{recipe.label_de}" sind schon erledigt — es ist kein Objekt '
            'mehr übrig, das gegriffen werden könnte. Zum Wiederholen bitte ein '
            'Objekt kurz wegnehmen und neu hinlegen oder das Programm neu '
            'starten.'
        )
    return (
        f'Kein „{recipe.label_de}" sichtbar — bitte das Objekt in den '
        'markierten Greifbereich legen.'
    )


def _detect_named_unclaimed(ctx, type_name) -> list:
    """``_detect_named`` minus the per-run claimed/skipped ids, WITH the
    recycled-object reclaim (#1) run first on the full set of visible type ids —
    so an object a person moved is un-claimed and grabbed again.

    The „Solange sichtbar" loop gate (``count_unclaimed_visible``) is its ONLY
    caller: the reclaim is a once-per-pass decision and every VALUE block reads
    through ``_detect_named_unclaimed_readonly`` instead."""
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
    # isfinite guard, the one its sibling _apply_xy_correction already has. A
    # NaN yaw_bias_rad (a corrupt scene_handeye.yaml, which survives reboots)
    # propagates: compute_grasp_roll → solve(roll=nan) → None → „Position
    # außerhalb des Arbeitsbereichs" for EVERY grasp, permanently, with no hint
    # at the cause. Measured 2026-09-07: yaw_bias_rad=nan → _apply_yaw_bias(0.5)
    # returned nan. Treated as "no bias", the same way a malformed correction
    # matrix falls back to the raw position.
    if not math.isfinite(yb):
        yb = 0.0
    if yb == 0.0:
        return float(yaw)
    from physical_ai_server.workflow.tag_pose import _wrap
    return _wrap(float(yaw) + yb)


def _tag_table_xy(ctx, d, recipe):
    """The detection's base-frame table position ``(x, y)`` in metres, or ``None``.

    THE one projection of a tag centre onto that object's own tag-top plane:
    ``project_pixel_to_table`` followed by the ground-truth
    ``_apply_xy_correction``, in that order. ``None`` when the rig has nothing to
    project with (no scene intrinsics / extrinsics / ``board_table_z`` — the
    simulator's normal state) or when the projection itself fails.

    Single-sourced on purpose: ``_attach_named_world`` derives the grasp point
    from it and ``_reclaim_recycled`` compares two observations of it. Two copies
    of a plane evaluation are exactly how the grasp-height defect appeared (see
    the ``_GRASP_Z_FROM_PLANE`` note in handlers/motion.py), and a reclaim
    disagreeing with the grasp about where an object IS would be the same class
    of bug."""
    board_z = getattr(ctx, 'board_table_z', None)
    if (getattr(ctx, 'scene_intrinsics', None) is None
            or getattr(ctx, 'scene_extrinsics', None) is None
            or board_z is None):
        return None
    from physical_ai_server.workflow.projection import project_pixel_to_table
    try:
        cx, cy = d.centroid_px
        point = project_pixel_to_table(
            cx, cy, ctx.scene_intrinsics['K'], ctx.scene_intrinsics['dist'],
            ctx.scene_extrinsics,
            float(board_z) + float(recipe.object_height_m))
    except Exception:  # noqa: BLE001 — an unusable detection is simply unlocated
        return None
    if point is None:
        return None
    return _apply_xy_correction(ctx, float(point[0]), float(point[1]))


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
    from physical_ai_server.workflow.tag_pose import tag_edge_length_base, tag_yaw_base
    K = ctx.scene_intrinsics['K']
    dist = ctx.scene_intrinsics['dist']
    T = ctx.scene_extrinsics
    tag_plane_z = float(board_z) + float(recipe.object_height_m)
    cat = getattr(ctx, 'object_catalog', None)
    expected_edge = float(getattr(cat, 'tag_size_m', 0.0) or 0.0)
    for d in detections:
        # THE one projection + ground-truth XY correction, shared verbatim with
        # the recycled-object reclaim (_tag_table_xy) so the two can never
        # disagree about where an object is. The calibration preconditions were
        # already checked above, so a None here means the projection failed.
        xy = _tag_table_xy(ctx, d, recipe)
        if xy is None:
            # Record WHY the world position is unset so motion._resolve_target
            # can name the projection failure instead of blaming the touch-off
            # (which this rig has already completed) — see its fourth branch.
            try:
                d.extras['world_error'] = 'projection'
            except Exception:  # noqa: BLE001 — diagnosis never breaks detection
                pass
            continue
        wx, wy = xy
        # Rule §2 — the grasp height follows the MEASURED table plane at THIS
        # object's own (x, y), the same plane motion._floor_z_at judges the
        # target against. See _GRASP_Z_FROM_PLANE for the measurement and the
        # rollback; with no plane calibrated this is exactly float(ctx.z_table).
        surface = _motion.table_z_at(ctx, wx, wy)
        if surface is None or not math.isfinite(float(surface)):
            surface = float(ctx.z_table)
        grasp_z = (float(surface) + float(recipe.object_height_m)
                   - float(recipe.grasp_depth_m))
        d.extras.pop('world_error', None)
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
        # The reclaim reasons about POSITION, so it needs each visible tag's
        # table (x, y). A tag that is seen but cannot be LOCATED maps to None —
        # deliberately distinct from a tag that is absent from the dict entirely,
        # which is the only thing the reclaim reads as "not seen this time".
        visible_xy = {int(d.aruco_id): _tag_table_xy(ctx, d, recipe)
                      for d in detections if d.aruco_id in type_ids}
        _reclaim_recycled(ctx, recipe, visible_xy)
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


# ── why is it out of reach? (F4) ─────────────────────────────────────────────
# „außerhalb des Greifbereichs — bitte NÄHER legen" was emitted for every
# unreachable object, including one that is too NEAR the base (where moving it
# nearer is the exact opposite of the fix) and one behind/beside the arm (where
# no radial move helps at all — joint 1 simply cannot aim there). Rather than
# hard-code per-arm reach bounds (which would then drift from the solver), we ASK
# the solver: scan the object's OWN bearing at the object's OWN height and see
# where along that ray the arm can actually go.
#
# Cost: <= _REACH_PROBE_MAX_M / _REACH_PROBE_STEP_M closed-form solves (80), once
# per FAILED find — never on the success path, never per motion step.
_REACH_PROBE_STEP_M = 0.005
_REACH_PROBE_MAX_M = 0.40


def _out_of_reach_reason(ctx, label_de: str, xyz) -> str:
    """German sentence naming WHY ``xyz`` is out of reach for THIS arm.

    Distinguishes too-near / too-far / wrong-bearing by probing the real solver
    along the target's own bearing; falls back to the direction-neutral wording
    (true in every case) when there is no solver or the ray is unusable."""
    generic = (
        f'„{label_de}" gesehen, aber außerhalb des Greifbereichs — bitte das '
        'Objekt in den markierten Greifbereich legen (nicht zu nah am Roboter, '
        'nicht zu weit weg).'
    )
    ik = getattr(ctx, 'ik', None)
    if ik is None:
        return generic
    try:
        x, y, z = (float(v) for v in xyz)
        axis_x = float(getattr(ik, 'base_axis_x', 0.0))
        dx = x - axis_x
        r = math.hypot(dx, y)
        if r <= 1e-9:
            return generic
        ux, uy = dx / r, y / r
        reachable = []
        steps = int(_REACH_PROBE_MAX_M / _REACH_PROBE_STEP_M)
        for k in range(1, steps + 1):
            rr = k * _REACH_PROBE_STEP_M
            if ik.solve((axis_x + ux * rr, uy * rr, z)) is not None:
                reachable.append(rr)
    except Exception:  # noqa: BLE001 — a diagnostic must never break the run
        return generic
    if not reachable:
        # No radius along this bearing solves: joint 1 cannot aim here at all
        # (behind the arm / too far to the side), or this height is unreachable
        # everywhere on that ray.
        return (
            f'„{label_de}" gesehen, aber der Arm kommt in diese Richtung nicht — '
            'bitte das Objekt VOR den Roboter in den markierten Greifbereich '
            'legen.'
        )
    if r < min(reachable):
        return (
            f'„{label_de}" gesehen, aber zu nah am Roboter — bitte das Objekt '
            'etwas WEITER WEG in den markierten Greifbereich legen.'
        )
    if r > max(reachable):
        return (
            f'„{label_de}" gesehen, aber zu weit weg — bitte das Objekt '
            'NÄHER an den Roboter in den markierten Greifbereich legen.'
        )
    return generic


def _note_find_failure(ctx, reason) -> None:
    """Record (or clear, with ``None``) WHY the last „finde" produced no
    Greifziel, so the split motion blocks can say it instead of the generic
    „you never ran finde" message (see ``motion._no_greifziel_error``). Plain
    attribute on the per-run ctx — never raises."""
    try:
        ctx.last_find_failure = reason
    except Exception:  # noqa: BLE001 — a diagnostic must never break the run
        pass


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
    # DUPLICATE-FRAME REJECTION. ``get_scene_frame`` returns the CACHED latest
    # frame with no sequence or timestamp check, and the burst samples ~34 ms
    # apart against cameras configured at framerate 30.0 — so consecutive
    # samples can be the SAME image. Measured 2026-09-07 with one noisy frame
    # repeated 7×: the mean resultant length came back R = 1.000000 (maximum
    # confidence, gate passed) and the "averaged" yaw was BIT-IDENTICAL to that
    # single frame's 13.5° error. Averaging a frame with itself neither reduces
    # its error nor lets R detect it, so the gate reported certainty about the
    # one thing it exists to doubt. Independent-frame behaviour is sound (error
    # drops ≈√7) and is unchanged.
    #
    # The discriminator is the tag CORNERS: two detections of the same image
    # produce bit-identical sub-pixel corners, and a genuinely new frame never
    # does (the detector is deterministic, the sensor is not). A duplicate is
    # SKIPPED rather than counted, and the loop is allowed a bounded number of
    # extra reads so a slow camera still reaches the sample count instead of
    # silently degrading to <2 valid frames.
    seen_corners: set = set()
    duplicate_hits = 0
    attempts = 0
    max_attempts = _TAG_YAW_FRAMES * 2
    while len(yaws) < _TAG_YAW_FRAMES and attempts < max_attempts:
        if attempts > 0:
            time.sleep(_TAG_YAW_FRAME_INTERVAL_S)
        attempts += 1
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
        fingerprint = _corner_fingerprint(d.corners_px)
        if fingerprint is not None:
            if fingerprint in seen_corners:
                duplicate_hits += 1
                continue          # the SAME camera frame — not a new sample
            seen_corners.add(fingerprint)
        y = tag_yaw_base(d.corners_px, K, dist, T, tag_plane_z)
        if y is None:
            continue
        # Same edge sanity gate as the single-frame path.
        if expected_edge > 0.0 and _TAG_EDGE_TOL_FRAC > 0.0:
            edge = tag_edge_length_base(d.corners_px, K, dist, T, tag_plane_z)
            if edge is None or abs(edge - expected_edge) > _TAG_EDGE_TOL_FRAC * expected_edge:
                continue
        yaws.append(float(y))
    if len(yaws) == 1 and duplicate_hits:
        # Every extra read returned the SAME camera frame, so there was nothing
        # to average — but there IS a valid single-frame measurement, and that
        # is exactly what the single-frame path in _attach_named_world accepts
        # without any R gate. Return it and say the averaging did not run,
        # rather than pretending R = 1.0 (false confidence) or refusing a grasp
        # that used to work. This is the SIMULATOR's normal state too: its
        # camera is deterministic, so every frame really is identical.
        _safe_log(ctx, '[WARNUNG] Die Szenen-Kamera hat während der Messung '
                       'keine neuen Bilder geliefert — die Ausrichtung wurde '
                       'nur aus einem Bild bestimmt.')
        return _apply_yaw_bias(ctx, yaws[0])
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


def _corner_fingerprint(corners):
    """A hashable identity for one detection's sub-pixel corners, or ``None``
    when it cannot be formed (then the caller simply does not de-duplicate).

    Bit-exact by design: two detections of the SAME cached camera frame give
    byte-identical corners, and two genuinely different frames of a real scene
    never do. Rounding here would start rejecting real, slightly-different
    frames — which is the opposite of the intent."""
    try:
        return tuple(np.asarray(corners, dtype=np.float64).ravel().tolist())
    except Exception:  # noqa: BLE001 — de-duplication is best-effort
        return None


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
    # THE one acquire helper. This was a bare, UNBOUNDED ``lock.acquire()`` —
    # the only site that was neither bounded nor stop-polling — so pressing
    # Stop behind a 30 s holder answered in 29.70 s (0.03 s through the helper).
    acquired = _motion._hold_motion_lock(ctx)
    try:
        attempts = _motion.GRASP_RETRY + 1
        for attempt in range(attempts):
            detections = _detect_named(ctx, type_name, exclude_ids=_excluded_ids(ctx))
            if not detections:
                # The object vanished between the loop's count and this grasp (or a
                # standalone „greife" with nothing in view). Recoverable: the loop
                # re-detects next pass; standalone fails loud.
                raise GraspSkip(_nothing_to_grasp_message(ctx, recipe))
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
                # Direction-correct reason, shared with find_object (F4): the old
                # text told the student to move a TOO-NEAR object even nearer.
                raise GraspSkip(_out_of_reach_reason(
                    ctx, recipe.label_de,
                    next((d.world_xyz_m for d in detections
                          if d.world_xyz_m is not None), (0.0, 0.0, 0.0))))
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
            close_rad = float(target.extras.get('gripper_close_rad',
                                                _motion._gripper_closed(ctx)))
            try:
                _motion._execute_pickup(
                    ctx, (x, y, z), float(recipe.approach_clear_m), roll, close_rad)
            except GraspSkip:
                # The ONLY GraspSkip `_execute_pickup` can raise is the
                # no-approach-clearance refusal (F1): this instance sits at a
                # radius where the arm cannot come down on it at all. Mark it
                # SKIPPED before re-raising so a „Solange sichtbar" loop excludes
                # it next pass and TERMINATES, instead of re-selecting the same
                # cube until the 3-pass stall guard trips.
                _skip_tag(ctx, target.aruco_id)
                raise
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
        _motion._release_motion_lock(ctx, acquired)


def _detect_named_unclaimed_readonly(ctx, type_name) -> list:
    """Currently-visible UNCLAIMED instances WITHOUT running the reclaim.

    ``_detect_named_unclaimed`` runs ``_reclaim_recycled``, which MUTATES the
    per-run claim/position state. That is right for the „Solange sichtbar" loop
    gate (``count_unclaimed_visible``), which owns the loop's progress, and wrong
    for every VALUE block a student can drop anywhere — „sehe ich", „Anzahl",
    „finde" and „warte bis" are READS, and a read that silently un-claims an
    object changes what the surrounding loop does next. The visible ANSWER is
    identical on every pass where the reclaim would not have fired; where it
    would have, the loop's own gate still fires it.

    „finde" and „warte bis" joined the first two when the reclaim became
    POSITION-based, and each had its own reason:

    * „finde" is legitimately called MID-CARRY (the taught pattern is finde →
      fahre über → senke → schließe → hebe an → lege ab), and a held object's
      projected position travels with the gripper. Running the anchor rule there
      would un-claim the very object the robot is HOLDING.
    * „warte bis" polls ~5×/s where one loop pass takes 15.6 s, so a 4 s wait
      contributed ~20 observations to a rule the loop expects to advance once per
      pass.

    ACCEPTED COST, owner-approved: a „wiederhole fortlaufend" + „finde/greife"
    program (as opposed to „Solange sichtbar") no longer reclaims at all. „Solange
    sichtbar" is the block designed for re-picking, and it still does."""
    return _detect_named(ctx, type_name, exclude_ids=_excluded_ids(ctx))


def see_object(ctx, args: dict[str, Any]) -> bool:
    """True when at least one UNCLAIMED instance of the chosen type is currently
    visible (visibility only — does not require grasp calibration). A pure read:
    it never changes the per-run claim state (see
    ``_detect_named_unclaimed_readonly``)."""
    return bool(_detect_named_unclaimed_readonly(ctx, args.get('object_type')))


def count_object(ctx, args: dict[str, Any]) -> int:
    """Number of currently-visible UNCLAIMED instances of the chosen type. A
    pure read: it never changes the per-run claim state."""
    return len(_detect_named_unclaimed_readonly(ctx, args.get('object_type')))


def wait_until_object_seen(ctx, args: dict[str, Any]) -> bool:
    """„warte bis <Typ> sichtbar (max N s)" — VALUE (Boolean): poll until an
    UNCLAIMED instance of the chosen type is visible. Returns ``True`` when it
    appears and ``False`` on timeout (with a German [WARNUNG]), so the block's
    declared Boolean type is honest and „falls … sonst" works. ``timeout``
    seconds, default 10."""
    timeout_s = float(args.get('timeout', 10))
    type_name = args.get('object_type')
    label = label_for(ctx, type_name)
    seen = _poll_until(
        ctx,
        # READ-ONLY: this polls ~5×/s, and the recycled-object reclaim is a
        # once-per-loop-pass decision — see _detect_named_unclaimed_readonly.
        lambda: bool(_detect_named_unclaimed_readonly(ctx, type_name)),
        timeout_s,
        f'Objekt {label}',
        raise_on_timeout=False,
    )
    if not seen:
        # The label, never the raw catalog KEY: every neighbouring message says
        # „Würfel", this one used to say „wuerfel".
        _safe_log(ctx, f'[WARNUNG] „{label}" ist innerhalb von '
                       f'{timeout_s:g} s nicht aufgetaucht.')
    return seen


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
    ``check_grasp_held`` reads the achieved gripper angle, which is ABOVE the
    held-threshold whenever the gripper is OPEN (the rest state) — so this
    block returns True immediately if the gripper is open. It is only
    meaningful directly AFTER a close (e.g. „Greifer schließen" or a grasp).
    The block tooltip states this. (The threshold itself is per-object since
    2026-07-10 — the last COMMANDED close, ``ctx.last_commanded_close_rad``,
    + ``GRASP_HELD_MARGIN_RAD``. With no close commanded yet this run the
    fallback is ``motion._grasp_held_max``, which is PROFILE-AWARE since
    2026-09-08 and is no longer the legacy global constant: that constant
    (−0.35) sits below the whole Feetech gripper band, so on edu6/edu1 it
    reported HELD for every readable angle rather than just the open one. The
    open-gripper behaviour documented above is preserved on all three arms —
    that is the part the change deliberately kept.)

    The check runs under ``motion_lock`` (``_check_grasp_held_locked``) so a
    hat-side poll can't read the threshold + joint state mid-close; note
    ``_poll_until`` releases a caller-held lock around the wait, so the poll
    still never starves the main stack."""
    timeout_s = float(args.get('timeout', 10))

    def _held() -> bool:
        result = _check_grasp_held_locked(ctx)
        if result is None:
            raise WorkflowError(
                'Greif-Erfolgskontrolle ist nicht verfügbar (keine Gelenkdaten).'
            )
        return result is True

    held = _poll_until(ctx, _held, timeout_s, 'Greifer hält etwas',
                       raise_on_timeout=False)
    if not held:
        _safe_log(ctx, '[WARNUNG] Der Greifer hat innerhalb von '
                       f'{timeout_s:g} s nichts gegriffen.')
    return held


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
    # READ-ONLY: „finde" is legitimately called MID-CARRY, where the held
    # object's projected position travels with the gripper — running the
    # position-based reclaim here would un-claim the object the robot is HOLDING.
    # See _detect_named_unclaimed_readonly for the full reasoning + the cost.
    detections = _detect_named_unclaimed_readonly(ctx, type_name)
    if not detections:
        _note_find_failure(ctx, _nothing_to_grasp_message(ctx, recipe))
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
        # Direction-correct reason (the old text said „näher legen" even for an
        # object that was too NEAR), recorded on the ctx so „fahre über" / „senke
        # auf" / „schließe um" report it instead of „Kein Greifziel".
        reason = _out_of_reach_reason(
            ctx, recipe.label_de,
            next((d.world_xyz_m for d in detections if d.world_xyz_m is not None),
                 (0.0, 0.0, 0.0)))
        _note_find_failure(ctx, reason)
        try:
            ctx.log(f'[WARNUNG] {reason}')
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
        _note_find_failure(ctx, (
            f'„{recipe.label_de}" erkannt, aber die Ausrichtung konnte nicht '
            'bestimmt werden — bitte den Tag flach und gut sichtbar aufkleben.'))
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
    _note_find_failure(ctx, None)   # a real Greifziel — no stale reason survives
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
    follower-joint feedback). Runs under ``motion_lock``
    (``_check_grasp_held_locked``) so a hat-side read can't race a concurrent
    close on the main stack."""
    result = _check_grasp_held_locked(ctx)
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
        # Same reason-promotion as the motion blocks (F4).
        raise _motion._no_greifziel_error(ctx)
    tag_id = getattr(ziel, 'aruco_id', None)
    if tag_id is None:
        raise WorkflowError('Greifziel hat keine Marker-ID.')
    _claim_tag(ctx, tag_id)
