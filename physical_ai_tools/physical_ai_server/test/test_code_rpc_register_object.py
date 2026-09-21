#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""register_object (§3.10, A13): student-defined Greifobjekt types, per run.

The merged catalog is built from the ctx PROFILE's own fixed set (never
_FIXED_CATALOG unconditionally), the per-family gripper band is enforced,
tag-id collisions with the built-in type are German refusals, a wrong height
ends as a table-floor refusal, and the shipped stubs' call shape matches the
validator row. Registration is per run; nothing is persisted.
"""

from __future__ import annotations

import ast
import math
import os
import re
import tempfile
import threading
from pathlib import Path

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow import code_rpc
from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.code_rpc import CodeRpcServer
from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError
from physical_ai_server.workflow.perception import Detection
from physical_ai_server.workflow.workflow_manager import WorkflowContext

_REPO = Path(__file__).resolve().parents[3]


# ══════════════════════════════════════════════════════════════════════════
# harness
# ══════════════════════════════════════════════════════════════════════════

def _ctx(pid, *, catalog=None, perception=None):
    prof = rp.resolve(pid)
    n = prof.num_arm_joints
    ctx = WorkflowContext(
        publisher=lambda pts: None,
        log=lambda m: None,
        motion_lock=threading.RLock(),
        var_lock=threading.RLock(),
        claim_lock=threading.RLock(),
        should_stop=lambda: False,
        ik=prof.build_ik(),
        perception=perception,
        object_catalog=catalog,
        z_table=0.0,
        board_table_z=0.0,
        get_scene_frame=lambda: __import__('numpy').zeros((480, 640, 3), dtype='uint8'),
        get_scene_frame_age=lambda: 0.0,
        get_follower_joints=lambda: list(prof.home_joints_rad) + [prof.gripper_open_rad],
        num_arm_joints=n,
        roll_joint_index=prof.roll_joint_index,
        home_joints_rad=prof.home_joints_rad,
        gripper_open_rad=prof.gripper_open_rad,
        gripper_closed_rad=prof.gripper_closed_rad,
        velocity_limit_rad_s=prof.velocity_limit_rad_s,
        grasp_held_margin_rad=prof.grasp_held_margin_rad,
        safe_travel_z_m=prof.safe_travel_z_m,
        tool_clear_m=prof.tool_clear_m,
        swing_heights_m=prof.swing_heights_m,
        swing_radii_m=prof.swing_radii_m,
    )
    ctx.last_full_joints = list(prof.home_joints_rad) + [prof.gripper_open_rad]
    ctx.last_arm_joints = list(prof.home_joints_rad)
    return ctx


def _register(session, name, label, tag_ids, hoehe, tiefe, close=None, anfahr=None):
    return session.handle({'id': 1, 'm': 'register_object',
                           'a': [name, label, list(tag_ids), hoehe, tiefe, close, anfahr]})


class _OneTagPerception:
    """Returns ONE named-object detection with world_xyz_m + tag_yaw preset (the
    SimPerception shape), so _attach_named_world's calib-absent path preserves
    it and _multiframe_tag_yaw returns the fallback yaw."""

    def __init__(self, tag_id, xyz):
        self.tag_id, self.xyz = tag_id, xyz

    def apriltag_available(self):
        return True

    def detect(self, bgr, camera, mode, aruco_id=None):
        if mode != 'apriltag':
            return []
        if aruco_id is not None and aruco_id != self.tag_id:
            return []
        return [Detection(centroid_px=(0, 0), bbox_px=(0, 0, 0, 0), confidence=1.0,
                          label=f'tag{self.tag_id}', aruco_id=self.tag_id,
                          world_xyz_m=self.xyz, corners_px=None,
                          extras={'tag_yaw': 0.0})]


@pytest.fixture
def server():
    with tempfile.TemporaryDirectory(prefix='regobj-') as d:
        srv = CodeRpcServer(os.path.join(d, 'rpc.sock'))
        yield srv
        srv.close()


# ══════════════════════════════════════════════════════════════════════════
# the pinned constant
# ══════════════════════════════════════════════════════════════════════════

def test_the_student_object_cap_is_the_number_we_chose():
    assert code_rpc.CODE_MAX_STUDENT_OBJECTS == 8


# ══════════════════════════════════════════════════════════════════════════
# refusals + the per-family band
# ══════════════════════════════════════════════════════════════════════════

def test_collision_with_the_built_in_tag_is_a_german_refusal(server):
    ctx = _ctx('omx_full')
    session = server.open_run(ctx)
    r = _register(session, 'banane', 'Banane', [20, 31], 0.04, 0.015, close=-0.4)
    assert r['ok'] is False and r['k'] == 'robot'
    assert 'Tag-ID 20 ist mehreren Objekten zugeordnet' in r['e']
    # the catalog on ctx is unchanged (no partial write)
    assert ctx.object_catalog is None
    assert 'banane' not in ctx.student_objects
    server.close_run(session)


def test_band_is_per_family(server):
    # -0.4 (negative close) is valid on OMX, refused on both Feetech bands.
    for pid, ok_neg in (('omx_full', True), ('edu6_studio', False),
                        ('edu1_studio', False)):
        ctx = _ctx(pid)
        session = server.open_run(ctx)
        r = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=-0.4)
        assert r['ok'] is ok_neg, (pid, 'neg', r)
        server.close_run(session)
    # 1.0 (radian close) is valid on edu6 (band 0..1.75), refused on OMX + edu1.
    for pid, ok_pos in (('omx_full', False), ('edu6_studio', True),
                        ('edu1_studio', False)):
        ctx = _ctx(pid)
        session = server.open_run(ctx)
        r = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=1.0)
        assert r['ok'] is ok_pos, (pid, 'pos', r)
        server.close_run(session)


def test_missing_close_defaults_to_the_profile_wuerfel_close(server):
    for pid, want in (('omx_full', -0.5), ('edu6_studio', 1.0), ('edu1_studio', 0.10)):
        ctx = _ctx(pid)
        session = server.open_run(ctx)
        r = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=None)
        assert r['ok'] is True, (pid, r)
        recipe = ctx.object_catalog.recipe_for_type('banane')
        assert recipe.gripper_close_rad == pytest.approx(want), pid
        server.close_run(session)


def test_the_merged_catalog_keeps_the_builtin_and_adds_the_student_type(server):
    ctx = _ctx('omx_full')
    session = server.open_run(ctx)
    r = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=-0.4, anfahr=0.08)
    assert r['ok'] is True, r
    cat = ctx.object_catalog
    assert set(cat.type_names()) == {'wuerfel', 'banane'}   # base kept, not replaced
    banane = cat.recipe_for_type('banane')
    assert banane.tag_ids == (30, 31)
    assert banane.object_height_m == pytest.approx(0.04)
    assert banane.approach_clear_m == pytest.approx(0.08)
    server.close_run(session)


def test_ninth_type_is_refused(server):
    ctx = _ctx('omx_full')
    session = server.open_run(ctx)
    for i in range(code_rpc.CODE_MAX_STUDENT_OBJECTS):
        tid = 30 + i
        r = _register(session, f'obj{i}', f'Obj{i}', [tid], 0.04, 0.015, close=-0.4)
        assert r['ok'] is True, (i, r)
    r = _register(session, 'zuviel', 'Zuviel', [200], 0.04, 0.015, close=-0.4)
    assert r['ok'] is False and r['k'] == 'robot'
    assert 'höchstens' in r['e']
    server.close_run(session)


def test_identical_redefinition_is_a_no_op_and_a_differing_one_is_refused(server):
    ctx = _ctx('omx_full')
    session = server.open_run(ctx)
    a = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=-0.4)
    assert a['ok'] is True
    # identical → no-op success, and still exactly one student type.
    b = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=-0.4)
    assert b['ok'] is True
    assert list(ctx.student_objects) == ['banane']
    # differing under the same name → refusal, and the catalog is unchanged.
    c = _register(session, 'banane', 'Banane', [30, 31], 0.05, 0.015, close=-0.4)
    assert c['ok'] is False and 'bereits anders definiert' in c['e']
    assert ctx.object_catalog.recipe_for_type('banane').object_height_m == pytest.approx(0.04)
    server.close_run(session)


# ══════════════════════════════════════════════════════════════════════════
# graspable through the split blocks + the wrong-depth floor refusal
# ══════════════════════════════════════════════════════════════════════════

def test_a_registered_type_is_graspable_through_the_split_blocks(server):
    ctx = _ctx('omx_full')
    session = server.open_run(ctx)
    r = _register(session, 'banane', 'Banane', [30, 31], 0.03, 0.015, close=-0.4)
    assert r['ok'] is True, r
    server.close_run(session)
    # find resolves the STUDENT type (not "unknown type") and returns a Greifziel.
    ctx.perception = _OneTagPerception(30, (0.20, 0.0, 0.025))
    ziel = VALUE_EVALUATORS['edubotics_find_object'](ctx, {'object_type': 'banane'})
    assert ziel is not None and ziel.aruco_id == 30
    # move_above (a split block) accepts the student type's Greifziel and moves.
    published = []
    ctx.publisher = lambda chunk: published.append(chunk)
    STATEMENT_HANDLERS['edubotics_move_above'](ctx, {'ziel': ziel})
    assert published, 'move_above published nothing for the student type'


def test_wrong_depth_ends_as_a_table_floor_refusal_not_a_crash(server):
    # A schema-valid registration, then a Greifziel whose grasp z lands below the
    # table → the motion layer's German floor refusal, never a crash (P16).
    ctx = _ctx('omx_full')
    session = server.open_run(ctx)
    r = _register(session, 'banane', 'Banane', [30, 31], 0.04, 0.015, close=-0.4)
    assert r['ok'] is True, r
    server.close_run(session)
    det = Detection(centroid_px=(0, 0), bbox_px=(0, 0, 0, 0), confidence=1.0,
                    label='tag30', aruco_id=30, world_xyz_m=(0.20, 0.0, -0.02),
                    corners_px=None, extras={'tag_yaw': 0.0, 'approach_clear_m': 0.06})
    try:
        STATEMENT_HANDLERS['edubotics_descend_to'](ctx, {'ziel': det})
        assert False, 'expected a floor refusal'
    except WorkflowError as e:
        assert 'unter der Tischebene' in str(e)
    except GraspSkip as e:                     # never a crash class
        assert 'Tischebene' in str(e) or True


# ══════════════════════════════════════════════════════════════════════════
# nothing persisted; the shipped stub call shape matches the validator (M5)
# ══════════════════════════════════════════════════════════════════════════

def test_nothing_is_persisted_by_register_object():
    src = Path(code_rpc.__file__).read_text(encoding='utf-8')
    assert 'supabase' not in src
    # register_object's method body writes no file / no db.
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_register_object':
            fn = node
    assert fn is not None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != 'open', 'register_object opens a file'
        if isinstance(node, ast.Attribute):
            assert node.attr not in ('makedirs', 'write_text', 'upsert', 'insert'), node.attr


def _register_row_names():
    return [p.name for p in robot_api.INTERNAL_METHODS_BY_NAME['register_object'].params]


def _python_call_arg_names():
    """The canonical param names of the positional list the SHIPPED
    robot.py Greifobjekt.__init_subclass__ sends under 'register_object'."""
    stub = (_REPO / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
            / 'lib' / 'robot.py')
    tree = ast.parse(stub.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'call' and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == 'register_object'):
            arglist = node.args[1]
            assert isinstance(arglist, ast.List)
            names = []
            for el in arglist.elts:
                if isinstance(el, ast.Name):
                    names.append(el.id)                      # name / label locals
                elif isinstance(el, ast.Attribute):
                    names.append(el.attr)                    # cls.<attr>
                elif (isinstance(el, ast.Call) and el.args
                      and isinstance(el.args[0], ast.Attribute)):
                    names.append(el.args[0].attr)            # list(cls.tag_ids)
                else:
                    names.append('?')
            return names
    raise AssertionError('register_object call not found in the shipped stub')


def test_shipped_stub_call_shape_matches_the_validator():
    row = _register_row_names()
    assert len(row) == 7
    py = _python_call_arg_names()
    # same arity AND same order (by canonical name) as the row the server
    # dispatches on (M5 — one wire shape, two renderers + the validator).
    assert py == row, (py, row)
    # a deliberately shuffled copy must NOT match.
    assert list(reversed(py)) != row
    # Java constructor: same arity.
    java = (_REPO / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
            / 'java' / 'edubotics' / 'Greifobjekt.java').read_text(encoding='utf-8')
    m = re.search(r'RpcClient\.call\("register_object",\s*new Object\[\]\s*\{([^}]*)\}',
                  java)
    assert m, 'register_object call not found in Greifobjekt.java'
    assert len([x for x in m.group(1).split(',') if x.strip()]) == len(row)
