#!/usr/bin/env python3
"""„fahre über" must SKIP an instance it cannot approach from above.

`_solve_grasp_and_approach` refuses when there is no room to descend onto the object
(`_MIN_APPROACH_CLEARANCE_M`). `grasp_object` marks that tag skipped before re-raising;
`move_above` did not, because `_skip_tag` lived in `perception_blocks`, which imports
`motion` at module scope — the import cycle CLAUDE.md documents. So a „Solange sichtbar"
loop swallowed the GraspSkip, `_claim_progress_count` never grew, and the loop burned
its three stall passes before ending on „kein Fortschritt" instead of moving on.

Uses the REAL edu6 solver and profile: the refusal bands are a property of that arm's
geometry, and a stub cannot produce them.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import threading
import types

import numpy as np
import pytest

from physical_ai_server import robot_profiles
from physical_ai_server.workflow import claims
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import GraspSkip
from physical_ai_server.workflow.object_catalog import fixed_catalog


def _ctx(profile_id='edu6_studio'):
    prof = robot_profiles.resolve(profile_id)
    n = prof.num_arm_joints
    return types.SimpleNamespace(
        ik=prof.build_ik(),
        zones=None, z_table=0.0, table_plane=None,
        num_arm_joints=n,
        roll_joint_index=prof.roll_joint_index,
        gripper_open_rad=prof.gripper_open_rad,
        gripper_closed_rad=prof.gripper_closed_rad,
        velocity_limit_rad_s=prof.velocity_limit_rad_s,
        home_joints_rad=prof.home_joints_rad,
        last_full_joints=list(prof.home_joints_rad) + [float(prof.gripper_open_rad)],
        last_arm_joints=list(prof.home_joints_rad),
        claimed_tags=set(), skipped_tags=set(), claim_lock=threading.RLock(),
        publisher=lambda _c: None,
        should_stop=lambda: False,
        log=lambda _m: None,
        object_catalog=fixed_catalog(),
        tempo=1.0,
    )


def _ziel(x, y, tag_id=20, approach=0.06):
    return types.SimpleNamespace(
        aruco_id=tag_id,
        world_xyz_m=(x, y, 0.015),
        extras={'tag_yaw': 0.0, 'approach_clear_m': approach,
                'gripper_close_rad': 1.0, 'object_type': 'wuerfel'},
    )


def _find_refusing_radius(ctx):
    """The measured edu6 no-approach-clearance bands are the inner/outer ~2 mm of
    the pick band. Locate one from the REAL solver rather than hardcoding it."""
    for k in range(0, 400):
        r = 0.030 + k * 0.0005
        try:
            motion._solve_grasp_and_approach(ctx, (r, 0.0, 0.015), 0.06, roll=0.0)
        except GraspSkip:
            return r
        except Exception:
            continue
    return None


def test_move_above_clearance_refusal_marks_the_tag_skipped():
    ctx = _ctx()
    r = _find_refusing_radius(ctx)
    assert r is not None, 'no refusing radius found — the solver or bands changed'
    with pytest.raises(GraspSkip):
        motion.move_above(ctx, {'ziel': _ziel(r, 0.0, tag_id=20)})
    assert 20 in ctx.skipped_tags, (
        'the instance must be skipped so the loop can make progress')


def test_a_reachable_target_is_not_skipped():
    ctx = _ctx()
    motion.move_above(ctx, {'ziel': _ziel(0.13, 0.0, tag_id=20)})
    assert ctx.skipped_tags == set()


def test_the_skip_survives_a_ziel_without_a_tag():
    """A Greifziel with no aruco_id must not crash the refusal path."""
    ctx = _ctx()
    r = _find_refusing_radius(ctx)
    z = _ziel(r, 0.0)
    del z.aruco_id
    with pytest.raises(GraspSkip):
        motion.move_above(ctx, {'ziel': z})
    assert ctx.skipped_tags == set()


def test_grasp_object_and_move_above_agree_on_the_skip():
    """Both split and canned paths must mark the SAME instance."""
    ctx = _ctx()
    r = _find_refusing_radius(ctx)
    with pytest.raises(GraspSkip):
        motion.move_above(ctx, {'ziel': _ziel(r, 0.0, tag_id=21)})
    assert ctx.skipped_tags == {21}


# ── the extraction itself ────────────────────────────────────────────────────

def test_claims_module_imports_neither_motion_nor_perception_blocks():
    """The whole point of the extraction: claims must be importable from motion,
    so it may not depend on either handler module."""
    src = pathlib.Path(inspect.getfile(claims)).read_text(encoding='utf-8')
    tree = ast.parse(src)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or '')
    bad = [m for m in imported
           if 'handlers' in m or 'motion' in m or 'perception' in m]
    assert not bad, f'claims must not import handler modules, found {bad}'


def test_perception_blocks_still_exposes_the_private_names():
    """~10 internal call sites and several tests import these from perception_blocks."""
    assert pb._excluded_ids is claims.excluded_ids
    assert pb._claim_tag is claims.claim_tag
    assert pb._skip_tag is claims.skip_tag


def test_the_helpers_are_behaviourally_unchanged():
    ctx = _ctx()
    claims.claim_tag(ctx, 20)
    claims.skip_tag(ctx, 21)
    assert claims.excluded_ids(ctx) == {20, 21}
    # None is a no-op on both, and a ctx without the sets must not raise.
    claims.claim_tag(ctx, None)
    claims.skip_tag(ctx, None)
    bare = types.SimpleNamespace()
    claims.claim_tag(bare, 5)
    claims.skip_tag(bare, 5)
    assert claims.excluded_ids(bare) == set()
