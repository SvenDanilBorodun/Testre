"""The simulator holds or misses exactly where the rig would (owner decisions
Q6, Q13; spec D6 / N3).

Two halves, and only together do they match the rig:

* the sim TABLE sits where a real touch-off puts it — ``z_table`` is the EE
  height with the fingertips on the drawn table, the solver's
  ``tool_tip_below_ee_m`` (0.04 m on the OMX, 0 on the Feetech arms, whose EE
  frame IS the closed fingertip);
* a close CAPTURES only when the fingertips are at or below the top of the
  nearest object (base z = its own ``height_m``), with no extra margin.

Before, the sim captured any object within 6 cm of the EE's (x, y) at ANY
height: a close at HOME next to a cube „hielt" it.
"""

from __future__ import annotations

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow.edu1_ik import Edu1IKSolver
from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.ik_solver import IKSolver, _TOOL_TIP_EXT_M
from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.sim_world import SimWorld

from rig_emulation import ABLAGE, PICK, B, Rig, chain, cube, grasp, program, ref, var, vget, vset


def _arm_and_world(pid, objects):
    prof = rp.ROBOT_PROFILES[pid]
    world = SimWorld([dict(o) for o in objects])
    ik = prof.build_ik()
    tip = float(ik.tool_tip_below_ee_m)
    SimPerception([dict(o) for o in objects], fixed_catalog(pid), world, table_z=tip)
    home = [float(v) for v in prof.home_joints_rad] + [float(prof.gripper_open_rad)]
    arm = SimArm(ik=ik, objects=[dict(o) for o in objects], world=world,
                 num_arm_joints=prof.num_arm_joints, home_full_joints=home,
                 close_threshold_rad=prof.sim_close_threshold_rad,
                 held_block_offset_rad=prof.sim_held_block_offset_rad,
                 held_floor_rad=prof.sim_held_floor_rad)
    return prof, ik, arm, world


def _close_at(pid, ee_z, objects=None):
    """Drive to straight above the first cube at EE height ``ee_z`` (open), then
    close the jaws there — a recording made on the rig. Returns the held key."""
    objects = objects or [cube(*PICK[0])]
    prof, ik, arm, world = _arm_and_world(pid, objects)
    x, y = objects[0]['x'], objects[0]['y']
    q = ik.solve(target_xyz=(x, y, ee_z), seed=list(prof.home_joints_rad), roll=M.GRASP_ROLL_RAD)
    assert q is not None
    g_open, g_close = float(prof.gripper_open_rad), float(prof.gripper_closed_rad)
    arm.publish([(list(q) + [g_open], 1.0)])
    arm.publish([(list(q) + [g_close], 1.0)])
    return world.held_key()


@pytest.mark.parametrize('pid,ee_z', [('omx_full', 0.055), ('edu1_studio', 0.015)])
def test_a_rig_recorded_pick_holds(pid, ee_z):
    """A pick recorded on the rig (the jaws close with the fingertips 15 mm up
    the 30 mm cube) holds the cube in the simulator too."""
    assert _close_at(pid, ee_z) == 0


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_the_composite_grasp_holds(monkeypatch, pid):
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    assert rig.run(program(grasp())) == 'finished', rig.errors()
    assert rig.world.held_key() == 0
    assert rig.grasps() == 1


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_fahre_ueber_then_schliesse_holds_nothing(monkeypatch, pid):
    """„fahre über" hovers the approach height ABOVE the cube; closing there
    grabs air on the rig, and now in the simulator too."""
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    payload = program(chain(vset('Z', B('edubotics_find_object', fields={'OBJECT_TYPE': 'wuerfel'})),
                            B('edubotics_move_above', inputs={'ZIEL': vget('Z')}),
                            B('edubotics_close_gripper')), variables=[var('Z')])
    assert rig.run(payload) == 'finished', rig.errors()
    assert rig.world.held_key() is None


def test_a_close_at_home_holds_nothing():
    """A close at the OMX Grundstellung (EE ≈ 0.14 m up) next to a cube lying
    2 cm from under the tool held it before."""
    objects = [cube(0.17, 0.0)]
    prof, ik, arm, world = _arm_and_world('omx_full', objects)
    home = [float(v) for v in prof.home_joints_rad]
    arm.publish([(home + [float(prof.gripper_closed_rad)], 1.0)])
    assert world.held_key() is None


def test_bewege_zu_tisch_ziel_then_schliesse_holds_nothing(monkeypatch):
    """B1′: „bewege zu <Tisch-Ziel am Würfel>; schließe Greifer" stops at the
    release hover (fingertips 50 mm up) and closes on air — on the rig and, with
    the fingertip table, in the simulator."""
    rig = Rig(monkeypatch, 'omx_full', objects=[cube(*PICK[0])], simulated=True)
    ziel = {'name': 'Z', 'kind': 'pin', 'x': PICK[0][0], 'y': PICK[0][1], 'z': 0.0}
    payload = program(chain(B('edubotics_move_to', inputs={'DESTINATION': ref('Z')}),
                            B('edubotics_close_gripper')), destinations=[ziel])
    assert rig.run(payload) == 'finished', rig.errors()
    assert rig.world.held_key() is None


def test_the_height_comes_from_the_resolved_sim_object():
    """M-a: the height rides the binding ``SimPerception`` makes; ``reset``
    clears it with the rest of the placement."""
    objects = [cube(*PICK[0])]
    world = SimWorld([dict(o) for o in objects])
    recipe = fixed_catalog('omx_full').recipe_for_type('wuerfel')
    SimPerception([dict(o) for o in objects], fixed_catalog('omx_full'), world, table_z=0.04)
    assert world.objects()[0]['height_m'] == pytest.approx(recipe.object_height_m)
    world.reset([dict(o) for o in objects])
    assert world.objects()[0]['height_m'] is None
    # The wire snapshot does not carry it.
    assert 'height_m' not in world.snapshot()['objects'][0]


def test_tool_tip_offsets_per_solver():
    """ONE value per solver, beside its source."""
    assert IKSolver.tool_tip_below_ee_m == _TOOL_TIP_EXT_M == 0.04
    assert Edu1IKSolver.tool_tip_below_ee_m == 0.0
    assert Edu6IKSolver.tool_tip_below_ee_m == 0.0


def test_sim_table_is_the_tool_tip_height():
    """The node's sim manager reports ``z_table`` = the fingertip height (OMX
    0.04, Edu:1 0) — read from the builder's source, which cannot be imported
    without rclpy — and its SimPerception grasps at that table."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'physical_ai_server'
           / 'physical_ai_server.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == '_get_or_create_sim_workflow_manager')
    body = ast.get_source_segment(src, fn)
    assert "tool_tip_below_ee_m" in body
    assert "load_calibration=lambda: {'z_table': sim_table_z}" in body
    assert 'table_z=sim_table_z' in body
    # The perception side of the same rule: the grasp height is table + band.
    recipe = fixed_catalog('omx_full').recipe_for_type('wuerfel')
    world = SimWorld([cube(*PICK[0])])
    per = SimPerception([cube(*PICK[0])], fixed_catalog('omx_full'), world, table_z=0.04)
    det = per.detect(None, camera='scene', mode='apriltag')[0]
    assert det.world_xyz_m[2] == pytest.approx(0.04 + recipe.object_height_m - recipe.grasp_depth_m)
