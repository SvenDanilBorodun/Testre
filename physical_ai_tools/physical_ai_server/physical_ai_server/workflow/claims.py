#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Per-run tag CLAIM / SKIP bookkeeping for the named-object blocks.

A „Solange <Typ> sichtbar" loop only terminates because each pass removes one
instance from the unclaimed view: a successful grasp CLAIMS its tag, and a
confirmed per-instance failure (out of reach, orientation unreadable, no room to
approach from above) SKIPS it. Both sets live on the per-run
``WorkflowContext`` and are guarded by ``ctx.claim_lock``, because a
``when_object_seen`` hat thread can mutate them concurrently with the main stack.

These three helpers used to live in ``handlers.perception_blocks``. They were
moved here so ``handlers.motion`` can reach them WITHOUT a circular import —
``perception_blocks`` imports ``motion`` at module scope, so the reverse edge
cannot exist. That circularity is exactly why ``move_above``'s no-approach-
clearance refusal went un-skipped for so long: the loop swallowed the
``GraspSkip``, nothing marked the tag, ``_claim_progress_count`` never grew, and
the loop burned its three stall passes before ending on the alarming „kein
Fortschritt" instead of simply moving to the next object.

``perception_blocks`` re-exports the three public helpers under their original
private names, so its ~10 internal call sites and every test that imports them are
untouched.

Claiming and skipping also RESET the recycled-object reclaim's per-tag position
anchor (:func:`_forget_position_state`) — see
``handlers.perception_blocks._reclaim_recycled``.

Pure Python + stdlib — no ROS, no numpy.
"""

from __future__ import annotations


def excluded_ids(ctx) -> set:
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


def _forget_position_state(ctx, tag: int) -> None:
    """Drop the recycled-object reclaim's per-tag POSITION state for ``tag``.

    A fresh claim (or skip) must RE-ANCHOR. ``ctx.claim_anchor`` records where the
    robot left the object THIS time, and ``ctx.claim_unseen`` records that it has
    gone missing since THIS claim — carrying either across a new claim would
    compare the object's new resting place against a stale reference and un-claim
    it on the very next look. ``ctx.claim_pick_xy`` is deliberately KEPT: it is
    the spot the object was picked FROM, which is exactly what the PICK rule
    needs after the claim.

    getattr-guarded like everything else in this module, so a minimal unit-test
    ctx without the dicts is a no-op. The caller already holds ``claim_lock``."""
    anchors = getattr(ctx, 'claim_anchor', None)
    if anchors is not None:
        try:
            anchors.pop(tag, None)
        except Exception:  # noqa: BLE001 — bookkeeping never breaks a claim
            pass
    unseen = getattr(ctx, 'claim_unseen', None)
    if unseen is not None:
        try:
            unseen.discard(tag)
        except Exception:  # noqa: BLE001 — bookkeeping never breaks a claim
            pass


def claim_tag(ctx, tag_id) -> None:
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
            _forget_position_state(ctx, int(tag_id))
    else:
        claimed.add(int(tag_id))
        _forget_position_state(ctx, int(tag_id))


def skip_tag(ctx, tag_id) -> None:
    """Mark a tag id SKIPPED — a confirmed per-instance failure (out of reach,
    orientation unreadable) that must NOT be retried, so the „Solange sichtbar"
    loop makes progress and terminates instead of retreat→redetect→fail forever.
    Excluded from future detection alongside claimed ids (:func:`excluded_ids`).
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
            _forget_position_state(ctx, int(tag_id))
    else:
        skipped.add(int(tag_id))
        _forget_position_state(ctx, int(tag_id))
