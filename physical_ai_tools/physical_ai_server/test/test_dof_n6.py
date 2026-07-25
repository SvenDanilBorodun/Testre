"""PR-2 n=6 legs (§16.4): the DOF-generalised seams exercised with a
TEST-LOCAL synthetic 6-arm-joint profile (NOT the registry — the GUI↔server
lockstep test would fail on a registry entry before PR 4 lands).

Grows one section per slice (2a…2d). The n=5 identity proof is the UNTOUCHED
existing suite + test_dof_golden.py; these tests only cover the new ``n``
parameters with 6-DOF-shaped inputs (7-wide full vectors, 8-wide Contract-B
rows).
"""

from __future__ import annotations

import math

import pytest

from physical_ai_server.workflow import trajectory_builder


# The edu6-shaped synthetic: 6 arm joints + gripper = 7-wide full vectors,
# velocity limit 5.45 (the follower_arm_modified_final1 URDF value).
N6 = 6
N6_VLIMIT = 5.45


# ── slice 2a: trajectory_builder velocity_limit kwarg ────────────────────────

def test_build_segment_accepts_7_wide_vectors():
    seg = trajectory_builder.build_segment(
        [0.0] * (N6 + 1), [0.1] * (N6 + 1), 1.0, velocity_limit=N6_VLIMIT)
    assert seg
    assert all(len(q) == N6 + 1 for q, _t in seg)
    assert seg[-1][0] == pytest.approx([0.1] * (N6 + 1))


def test_velocity_floor_uses_custom_limit():
    # A swing whose floor differs between 4.8 and 5.45 rad/s proves the kwarg
    # is actually consumed (not silently ignored).
    delta = 3.0
    seg_default = trajectory_builder.build_segment(
        [0.0] * 7, [delta] + [0.0] * 6, 0.1)
    seg_n6 = trajectory_builder.build_segment(
        [0.0] * 7, [delta] + [0.0] * 6, 0.1, velocity_limit=N6_VLIMIT)
    t_default = seg_default[-1][1]
    t_n6 = seg_n6[-1][1]
    expect_default = delta * (15.0 / 8.0) / (0.6 * 4.8)
    expect_n6 = delta * (15.0 / 8.0) / (0.6 * N6_VLIMIT)
    assert t_default == pytest.approx(expect_default, rel=0.05)
    assert t_n6 == pytest.approx(expect_n6, rel=0.05)
    assert t_n6 < t_default  # higher limit → shorter floored duration


def test_velocity_floor_default_unchanged():
    # The default path must stay the OMX 4.8 (bit-identical floor arithmetic).
    import numpy as np
    d = np.array([2.0, 0.0])
    assert trajectory_builder._velocity_safe_duration(d, 0.1) == (
        trajectory_builder._velocity_safe_duration(d, 0.1, 4.8))
    assert trajectory_builder._velocity_safe_duration(d, 0.1) == pytest.approx(
        2.0 * (15.0 / 8.0) / (0.6 * 4.8))


# ── slice 2b: handlers (motion ctx accessors + Contract-B width) ─────────────

import types  # noqa: E402

from physical_ai_server.workflow.handlers import motion  # noqa: E402
from physical_ai_server.workflow.handlers.motion import (  # noqa: E402
    WorkflowError,
    _gripper_closed,
    _gripper_open,
    _home_joints,
    _n,
    _roll_idx,
    _velocity_limit,
)
from physical_ai_server.workflow.handlers.trajectory import (  # noqa: E402
    extract_points,
    resegment_trajectory,
)

_EDU6_HOME = (0.0, 0.70, -2.40, 0.0, 0.70, 0.0)


def _n6_ctx(**kw):
    """A TEST-LOCAL synthetic 6-arm-joint profile ctx (NOT the registry)."""
    base = dict(
        num_arm_joints=N6,
        home_joints_rad=_EDU6_HOME,
        gripper_open_rad=1.75,
        gripper_closed_rad=0.0,
        velocity_limit_rad_s=N6_VLIMIT,
        last_full_joints=list(_EDU6_HOME) + [1.75],
        last_arm_joints=None,
        motion_lock=None,
        should_stop=lambda: False,
        zones=None,
        ik=None,
        tempo=1.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_ctx_accessors_default_to_omx():
    empty = types.SimpleNamespace()
    assert _n(empty) == 5
    assert _roll_idx(empty) == 4
    assert _home_joints(empty) == list(motion.HOME_JOINTS_RAD)
    assert _gripper_open(empty) == motion.GRIPPER_OPEN_RAD
    assert _gripper_closed(empty) == motion.GRIPPER_CLOSED_RAD
    assert _velocity_limit(empty) == 4.8


def test_ctx_accessors_read_the_synthetic_profile():
    ctx = _n6_ctx()
    assert _n(ctx) == 6
    assert _roll_idx(ctx) == 5
    assert _home_joints(ctx) == list(_EDU6_HOME)
    assert _gripper_open(ctx) == 1.75
    assert _gripper_closed(ctx) == 0.0
    assert _velocity_limit(ctx) == N6_VLIMIT


def test_home_handler_emits_7_wide_vectors():
    published: list = []
    ctx = _n6_ctx(publisher=lambda chunk: published.extend(chunk))
    ctx.last_full_joints = [0.1, 0.9, -2.0, 0.05, 0.9, 0.1, 0.4]  # 7-wide live
    motion.home(ctx, {})
    assert ctx.last_full_joints == list(_EDU6_HOME) + [0.4]  # gripper carried
    assert ctx.last_arm_joints == list(_EDU6_HOME)
    assert all(len(q) == 7 for q, *_ in published)
    assert published[-1][0] == pytest.approx(list(_EDU6_HOME) + [0.4])


def test_gripper_handlers_use_profile_values():
    published: list = []
    ctx = _n6_ctx(publisher=lambda chunk: published.extend(chunk))
    ctx.last_full_joints = list(_EDU6_HOME) + [0.3]
    motion.open_gripper(ctx, {})
    assert ctx.last_full_joints == list(_EDU6_HOME) + [1.75]
    motion.close_gripper(ctx, {})
    assert ctx.last_full_joints == list(_EDU6_HOME) + [0.0]
    assert ctx.last_commanded_close_rad == 0.0


def test_check_grasp_held_reads_index_n():
    ctx = types.SimpleNamespace(
        num_arm_joints=N6,
        get_follower_joints=lambda: list(_EDU6_HOME) + [0.9],
        last_commanded_close_rad=None,
    )
    # 7-wide readback: index 6 is the gripper. With the OMX default threshold
    # (env override unset → GRASP_HELD_MAX_RAD −0.35), 0.9 > −0.35 → held.
    import os
    os.environ.pop('EDUBOTICS_GRASP_HELD_MAX_RAD', None)
    prev = motion.GRASP_SETTLE_S
    motion.GRASP_SETTLE_S = 0.0
    try:
        assert motion.check_grasp_held(ctx) is True
        # a 6-wide readback on a 6-arm-joint rig is TOO SHORT → None
        ctx.get_follower_joints = lambda: list(_EDU6_HOME)
        assert motion.check_grasp_held(ctx) is None
    finally:
        motion.GRASP_SETTLE_S = prev


_B8 = [
    [0.00, 0.70, -2.40, 0.00, 0.70, 0.00, 1.75, 0.00],
    [0.05, 0.80, -2.20, 0.02, 0.80, 0.05, 1.50, 0.30],
    [0.10, 0.90, -2.00, 0.04, 0.90, 0.10, 1.20, 0.60],
]


def test_extract_points_8_wide_for_n6():
    rows = extract_points({'fps': 25, 'points': _B8}, num_arm_joints=N6)
    assert rows == _B8


def test_extract_points_exact_width_rail_both_directions():
    # 7-wide row on a 6-DOF rig → refuse (was: silent acceptance impossible,
    # short refusal existed); 8-wide row on a 5-DOF rig → refuse (was: silent
    # TRUNCATION — the §16.4 rail closes exactly this).
    with pytest.raises(WorkflowError):
        extract_points({'fps': 25, 'points': [[0.0] * 7, [0.0] * 7]},
                       num_arm_joints=N6)
    with pytest.raises(WorkflowError):
        extract_points({'fps': 25, 'points': [[0.0] * 8, [0.0] * 8]},
                       num_arm_joints=5)


def test_resegment_n6_full_stream_is_7_wide():
    out = resegment_trajectory(
        [list(r) for r in _B8], speed=1.0,
        lead_in_from=[0.0, 0.6, -2.5, 0.0, 0.6, 0.0, 1.75],
        num_arm_joints=N6, velocity_limit=N6_VLIMIT)
    assert out
    assert all(len(q) == 7 for q, _t in out)
    assert out[-1][0] == pytest.approx(_B8[-1][:7])


# ── slice 2c: path_guard / workflow_manager / sim_arm ────────────────────────

from physical_ai_server.workflow.sim_arm import SimArm  # noqa: E402
from physical_ai_server.workflow.workflow_manager import (  # noqa: E402
    WorkflowContext,
    WorkflowManager,
)


class _FakeIK6:
    """A 6-joint IK stub for width plumbing (no real edu6 solver until PR 3)."""

    def num_joints(self):
        return N6

    def fk(self, joints):
        assert len(joints) == N6, f'fk got {len(joints)} joints'
        import numpy as np
        return np.eye(3), np.array([0.2, 0.0, 0.1])

    def link_points(self, joints, samples_per_link=5):
        assert len(joints) == N6
        import numpy as np
        return [np.array([0.0, 0.0, 0.1]), np.array([0.2, 0.0, 0.1])]


def test_sim_arm_n6_home_and_gripper_index():
    home7 = list(_EDU6_HOME) + [1.75]
    arm = SimArm(num_arm_joints=N6, home_full_joints=home7)
    assert arm.get_joints() == pytest.approx(home7)
    # A 7-wide publish caches 7-wide.
    arm.publish([(list(_EDU6_HOME) + [0.5], 0.5)])
    assert len(arm.get_joints()) == 7


def test_sim_arm_n6_held_override_hits_index_6():
    ik = _FakeIK6()
    arm = SimArm(ik=ik, num_arm_joints=N6,
                 home_full_joints=list(_EDU6_HOME) + [1.75],
                 objects=[{'type': 'wuerfel', 'tag_id': 7, 'x': 0.2, 'y': 0.0,
                           'yaw': 0.0}])
    # A negative close on index 6 with an object at the fk XY → blocked readback
    # (unit semantics stay OMX-shaped until PR 7 — the INDEX is what 2c fixes).
    arm.publish([(list(_EDU6_HOME) + [-0.5], 0.5)])
    q = arm.get_joints()
    assert len(q) == 7
    assert q[6] > -0.5  # gripper channel overridden, arm joints untouched
    assert q[:6] == pytest.approx(list(_EDU6_HOME))


def test_path_guard_segment_blocked_slices_n_joints():
    from physical_ai_server.workflow.path_guard import segment_blocked
    ik = _FakeIK6()
    zones = [{'min': [1.0, 1.0, 1.0], 'max': [1.1, 1.1, 1.1]}]  # far away
    q_a = list(_EDU6_HOME) + [1.75]
    q_b = [v + 0.05 for v in _EDU6_HOME] + [0.0]
    # The _FakeIK6 asserts fk/link_points receive EXACTLY 6 joints.
    assert segment_blocked(ik, q_a, q_b, zones) is False


def test_workflow_manager_stamps_profile_onto_ctx():
    class _Profile:
        num_arm_joints = N6
        roll_joint_index = 5
        home_joints_rad = _EDU6_HOME
        observe_pose_joints = None
        gripper_open_rad = 1.75
        gripper_closed_rad = 0.0
        velocity_limit_rad_s = N6_VLIMIT

    mgr = WorkflowManager(publisher=lambda chunk: None, arm_profile=_Profile())
    assert mgr._num_arm_joints == N6
    assert mgr._home_full_joints == pytest.approx(list(_EDU6_HOME) + [1.75])
    # Default (no profile) stays OMX.
    mgr5 = WorkflowManager(publisher=lambda chunk: None)
    assert mgr5._num_arm_joints == 5
    assert mgr5._home_full_joints[1] == pytest.approx(-1.5707963267948966)


def test_workflow_context_profile_defaults_are_omx():
    ctx = WorkflowContext(publisher=lambda chunk: None)
    assert ctx.num_arm_joints == 5
    assert ctx.roll_joint_index is None
    assert ctx.home_joints_rad is None
    assert _n(ctx) == 5 and _roll_idx(ctx) == 4


# ── slice 2d: node/Communicator + the permanent grep rail ────────────────────

from pathlib import Path  # noqa: E402


def test_grep_guard_no_width_literals_in_migrated_modules():
    """§16.4 permanent rail: hardcoded width literals must not creep back into
    the DOF-migrated modules. Bracket slices/indices ``[:5]/[:6]/[:7]`` &
    ``[4]/[5]/[6]/[7]``, ``range(5|6|7)``, and list-replication width
    multiplications ``* 5 / * 6 / * 7``.

    Extended (edu6 audit finding 7) from the original ``[:5]/[:6]/[5]/[6]/[4]/
    range(5|6)`` to (a) the ``* N`` multiplication family and the ``[:7]/[7]``
    (edu6 n+1 / n+2) family, and (b) two more migrated modules —
    ``handlers/perception_blocks.py`` and ``communication/communicator.py``.

    Two allowed survivors, each width-correct-by-construction and documented at
    its site: a motion docstring mention and the OMX ``WorkflowContext``
    dataclass default (the manager overrides it width-correct at construction).

    ``physical_ai_server.py`` is DELIBERATELY NOT scanned. Its jog/manual/hand-
    guide width sites are proven BEHAVIOURALLY (test_golden_jog +
    test_jog_compute_target_n6_profile ast-extract and run them at n=5 and n=6),
    and a 5500-line node dense with unrelated numeric literals makes the ``* N``
    rule brittle — a future unrelated ``* 5`` would false-trip. It currently has
    zero hits, but the behavioural tests are the real guard there."""
    import re
    root = Path(__file__).resolve().parents[1] / 'physical_ai_server'
    files = [
        root / 'workflow' / 'handlers' / 'motion.py',
        root / 'workflow' / 'handlers' / 'trajectory.py',
        root / 'workflow' / 'handlers' / 'perception_blocks.py',
        root / 'workflow' / 'path_guard.py',
        root / 'workflow' / 'workflow_manager.py',
        root / 'workflow' / 'sim_arm.py',
        root / 'communication' / 'communicator.py',
    ]
    # \b on the multiply so a multi-digit ``* 50`` / ``* 512`` does not match —
    # only a bare int width literal (``[0.0] * 6``) trips it.
    pattern = re.compile(r'\[:[567]\]|\[[4567]\]|range\([567]\)|\*\s*[567]\b')
    # Regex self-check: the canonical bad literals are caught, benign multi-digit
    # multiplies are not (guards the pattern itself against a bad edit).
    assert pattern.search('foo[:5]') and pattern.search('q[7]')
    assert pattern.search('[0.0] * 6') and pattern.search('range(7)')
    assert not pattern.search('timeout * 50')
    allowed = (
        "``ctx.last_full_joints[5]``",                # motion docstring
        "field(default_factory=lambda: [0.0] * 6)",    # documented OMX default
    )
    offenders = []
    for f in files:
        for i, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if pattern.search(line) and not any(a in line for a in allowed):
                offenders.append(f'{f.name}:{i}: {stripped}')
    assert offenders == [], (
        'width literals crept back into DOF-migrated modules:\n'
        + '\n'.join(offenders))


def test_communicator_follower_joint_order_is_profile_settable():
    # The class default stays the OMX order; a ctor-passed profile order
    # shadows it on the instance (and empties are ignored). Source-level
    # assertions — the communicator imports rclpy/geometry_msgs (ROS-only).
    src = (Path(__file__).resolve().parents[1] / 'physical_ai_server'
           / 'communication' / 'communicator.py').read_text(encoding='utf-8')
    assert "follower_joint_order: Optional[tuple] = None" in src
    assert "self.FOLLOWER_JOINT_ORDER = names" in src
    assert ("FOLLOWER_JOINT_ORDER = ('joint1', 'joint2', 'joint3', 'joint4', "
            "'joint5', 'gripper_joint_1')") in src


def test_communicator_reorders_7_joint_message_for_edu6_order():
    """Behavioral (object.__new__ + module-stub, per test_follower_joint_order_c2):
    a Communicator carrying a 7-name edu6 follower order reorders a PERMUTED
    7-joint JointState back to canonical order — returning exactly n+1 = 7 values
    (the §16.4 width choke point) — and fails LOUD (None) on a partial message.
    Exercises the real get_latest_follower_joints reorder, not just its source."""
    from test_follower_joint_order_c2 import _JointStateMsg, _load_communicator
    Comm = _load_communicator()
    order = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6',
             'end_gear_joint')

    # Publish order permuted (gripper-first, joints shuffled); canonical output
    # is the ascending sentinels 0.1..0.7.
    perm_names = ['end_gear_joint', 'joint2', 'joint6', 'joint1', 'joint4',
                  'joint3', 'joint5']
    perm_pos = [0.7, 0.2, 0.6, 0.1, 0.4, 0.3, 0.5]
    canonical = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]

    c = object.__new__(Comm)
    c.FOLLOWER_JOINT_ORDER = order
    c.follower_topic_msgs = {'follower': _JointStateMsg(perm_names, perm_pos)}
    out = c.get_latest_follower_joints()
    assert out == canonical
    assert len(out) == N6 + 1

    # Partial message (joint6 absent) → None, never a silent zero-fill.
    partial_names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                     'end_gear_joint']
    partial_pos = [0.1, 0.2, 0.3, 0.4, 0.5, 0.7]
    c_partial = object.__new__(Comm)
    c_partial.FOLLOWER_JOINT_ORDER = order
    c_partial.follower_topic_msgs = {
        'follower': _JointStateMsg(partial_names, partial_pos)}
    assert c_partial.get_latest_follower_joints() is None


def test_jog_cartesian_keeps_the_wrist_with_the_REAL_edu6_solver():
    """REGRESSION GUARD (2026-07-25) — and a deliberate departure from the
    stub-based n=6 tests around it.

    ``_FakeIK6`` exists to prove WIDTH plumbing; it has no ``solve()`` at all,
    so nothing here ever exercised a real edu6 CONVENTION. That gap hid a live
    bug: the Cartesian jog passed the roll JOINT value into ``solve(roll=...)``,
    which is the identity on the OMX (``theta5 = roll``) but not on edu6
    (``q6 = fold(wrap(π − roll))``). Every X/Y/Z jog step MIRRORED the wrist —
    up to 178°, rotating a held object with it.

    So this one drives the ast-extracted jog through the REAL ``Edu6IKSolver``
    and asserts the contract a Cartesian jog actually owes the student: the tool
    moves by the requested delta, and the wrist does not move at all."""
    import ast as _ast
    import textwrap
    import numpy as np
    from physical_ai_server.workflow.edu6_ik import Edu6IKSolver

    src_path = (Path(__file__).resolve().parents[1]
                / 'physical_ai_server' / 'physical_ai_server.py')
    source = src_path.read_text(encoding='utf-8')
    tree = _ast.parse(source)
    ns: dict = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name in (
                '_compute_jog_target', '_jog_solve_floor'):
            exec(compile(textwrap.dedent(_ast.get_source_segment(source, node)),
                         str(src_path), 'exec'), ns)  # noqa: S102

    real_ik = Edu6IKSolver()

    class _P:
        num_arm_joints = N6
        roll_joint_index = 5
        gripper_open_rad = 1.75
        gripper_closed_rad = 0.0
        home_joints_rad = _EDU6_HOME

    class _Stub:
        _arm_profile = _P()

        def _build_ik_solver(self):
            return real_ik

        def _load_workflow_calibration(self):
            return {'z_table': 0.0}

    stub = _Stub()
    stub._compute_jog_target = types.MethodType(ns['_compute_jog_target'], stub)
    stub._jog_solve_floor = types.MethodType(ns['_jog_solve_floor'], stub)

    def jaw_azimuth(arm_q):
        r_world, _t = real_ik.fk(arm_q)
        v = r_world @ np.array([1.0, 0.0, 0.0])
        return math.atan2(v[1], v[0])

    # Start from a real grasp pose with a NON-ZERO wrist (zero would hide a
    # mirror: −0 == +0), including values outside the ±90° jaw-fold window,
    # which the student can reach with the joint-6 jog.
    for roll_deg in (50.0, -35.0, 120.0, -150.0):
        start = list(real_ik.solve((0.15, 0.0, 0.02),
                                   roll=math.radians(roll_deg)))
        start[5] = math.radians(roll_deg)      # a jogged wrist, verbatim
        live = start + [1.0]                   # 7-wide follower readback
        _R, t0 = real_ik.fk(start)
        az0 = jaw_azimuth(start)

        for axis, delta in ((0, 0.01), (1, 0.01), (2, 0.01)):
            req = types.SimpleNamespace(mode='cartesian', index=axis,
                                        delta=delta, target_x=0.0,
                                        target_y=0.0, target_z=0.0)
            q, world = stub._compute_jog_target('cartesian', req, live)
            assert len(q) == 7

            # 1. the wrist must not move — the whole point of a Cartesian jog
            assert q[5] == pytest.approx(start[5], abs=1e-9), (
                f'wrist moved {math.degrees(q[5] - start[5]):.1f}° on a '
                f'{"xyz"[axis]} jog from q6={roll_deg}°')
            # 2. the jaw must rotate EXACTLY with the base and not one degree
            #    more. A Y-jog re-aims joint1, and with the wrist joint held the
            #    tool swings with the arm (χ = q1 + q6 − π/2) — that is the OMX
            #    behaviour this jog mirrors, not a bug. So the sharp assertion
            #    is Δχ == Δq1: the wrist contributed NOTHING. Under the old bug
            #    q6 flipped sign, adding −2·q6 on top of Δq1.
            d = abs((jaw_azimuth(q[:6]) - az0) - (q[0] - start[0])) % math.pi
            assert min(d, math.pi - d) < 1e-9, (
                'the wrist contributed rotation of its own')
            # 3. ...and the jog must still actually move the tool
            _R2, t1 = real_ik.fk(q[:6])
            moved = np.asarray(t1) - np.asarray(t0)
            assert moved[axis] == pytest.approx(delta, abs=1e-6), (
                f'{"xyz"[axis]} step not achieved: {moved}')
            for other in (0, 1, 2):
                if other != axis:
                    assert abs(moved[other]) < 1e-6, 'jog leaked into another axis'


def test_jog_compute_target_n6_profile():
    """The ast-extracted _compute_jog_target with a 6-arm-joint profile stub:
    gripper index 6, roll joint index 5, profile gripper band."""
    import ast as _ast
    import textwrap
    src_path = (Path(__file__).resolve().parents[1]
                / 'physical_ai_server' / 'physical_ai_server.py')
    source = src_path.read_text(encoding='utf-8')
    tree = _ast.parse(source)
    ns: dict = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name in (
                '_compute_jog_target', '_jog_solve_floor'):
            exec(compile(textwrap.dedent(_ast.get_source_segment(source, node)),
                         str(src_path), 'exec'), ns)  # noqa: S102

    class _P:
        num_arm_joints = N6
        roll_joint_index = 5
        gripper_open_rad = 1.75
        gripper_closed_rad = 0.0
        home_joints_rad = _EDU6_HOME

    class _Stub:
        _arm_profile = _P()

        def _build_ik_solver(self):
            return _FakeIK6()

        def _load_workflow_calibration(self):
            return {}

    stub = _Stub()
    stub._compute_jog_target = types.MethodType(ns['_compute_jog_target'], stub)
    stub._jog_solve_floor = types.MethodType(ns['_jog_solve_floor'], stub)
    live = list(_EDU6_HOME) + [1.0]  # 7-wide

    req = types.SimpleNamespace(mode='joint', index=6, delta=0.5,
                                target_x=0.0, target_y=0.0, target_z=0.0)
    q, _world = stub._compute_jog_target('joint', req, live)
    assert len(q) == 7
    assert q[6] == pytest.approx(1.5)          # gripper channel moved
    assert q[:6] == pytest.approx(list(_EDU6_HOME))

    # Gripper band: profile 0..1.75 — a delta past the top REFUSES.
    req_over = types.SimpleNamespace(mode='joint', index=6, delta=1.0,
                                    target_x=0.0, target_y=0.0, target_z=0.0)
    with pytest.raises(WorkflowError):
        stub._compute_jog_target('joint', req_over, live)

    # Joint index 6 is the gripper on n=6; index 7 is invalid.
    req_bad = types.SimpleNamespace(mode='joint', index=7, delta=0.1,
                                    target_x=0.0, target_y=0.0, target_z=0.0)
    with pytest.raises(WorkflowError):
        stub._compute_jog_target('joint', req_bad, live)


# ── n=6 end-to-end (assembled WorkflowManager.start, golden harness machinery) ─

def test_n6_end_to_end_widths_through_home_move_gripper_replay(monkeypatch):
    """Modest n=6 END-TO-END on the golden harness machinery (WorkflowManager.
    start + the real interpreter / handlers / trajectory re-segmentation),
    driven by a TEST-LOCAL synthetic 6-arm-joint profile through
    home → move_to → close → open → replay of an 8-wide (n+2) Contract-B
    recording. Asserts every emitted arm command is n+1 = 7 wide throughout and
    the recording is n+2 = 8 wide (extract_points accepts it at n=6).

    A minimal fixed 6-DOF fake IK stands in for the geometry (there is no real
    edu6 solver in the test env until PR 3) — enough for move_to's single solve.
    The GAP: the grasp corridor / AprilTag perception pipeline is out of scope
    here because it needs a geometrically-consistent solver (covered separately
    by the edu6 IK oracle, test_edu6_ik.py); this test's job is the WIDTH
    plumbing across an assembled multi-block run, not grasp geometry."""
    import json
    import time

    from physical_ai_server.workflow.sim_arm import SimArm
    from physical_ai_server.workflow.workflow_manager import WorkflowManager

    # Instant inter-chunk pacing (published waypoints unchanged) — mirrors
    # test_dof_golden._fast_chunk_pacing so the assembled run finishes fast.
    _state = {'t': 0.0}

    def _mono():
        _state['t'] += 1000.0
        return _state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_mono,
                                              sleep=lambda _s: None))

    class _IK6:
        """Fixed 6-DOF IK: just enough surface for move_to's single solve."""

        def num_joints(self):
            return N6

        def solve(self, target_xyz=None, seed=None, free_yaw=True, roll=None):
            return list(_EDU6_HOME)          # a reachable fixed 6-joint pose

        def fk(self, joints):
            import numpy as np
            assert len(joints) == N6, f'fk got {len(joints)} joints'
            return np.eye(3), np.array([0.18, 0.0, 0.06])

    profile = types.SimpleNamespace(
        num_arm_joints=N6,
        home_joints_rad=_EDU6_HOME,
        roll_joint_index=5,
        gripper_open_rad=1.75,
        gripper_closed_rad=0.0,
        velocity_limit_rad_s=N6_VLIMIT,
        observe_pose_joints=None,
        grasp_held_margin_rad=0.12,
    )

    captured: list = []
    home7 = list(_EDU6_HOME) + [1.75]
    sim_arm = SimArm(joint_state_sink=captured.append, ik=_IK6(),
                     num_arm_joints=N6, home_full_joints=home7)

    program = {
        # 8-wide (n+2) Contract-B recording: [j1..j6, grip, t_s].
        'trajectories': {'T1': {'fps': 25, 'points': [list(r) for r in _B8]}},
        'blocks': {'blocks': [{
            'type': 'edubotics_home', 'id': 'h1',
            'next': {'block': {
                'type': 'edubotics_move_to', 'id': 'mv1',
                'inputs': {'DESTINATION': {'block': {
                    'type': 'edubotics_destination_ref', 'id': 'dr1',
                    'fields': {'NAME': 'Ablage'},
                }}},
                'next': {'block': {
                    'type': 'edubotics_close_gripper', 'id': 'cg1',
                    'next': {'block': {
                        'type': 'edubotics_open_gripper', 'id': 'og1',
                        'next': {'block': {
                            'type': 'edubotics_replay_trajectory', 'id': 'rp1',
                            'fields': {'NAME': 'T1'},
                        }},
                    }},
                }},
            }},
        }]},
    }

    status: list = []
    mgr = WorkflowManager(
        publisher=sim_arm.publish,
        ik_factory=lambda: _IK6(),
        load_destinations=lambda: {'Ablage': {'x': 0.18, 'y': 0.0, 'z': 0.06,
                                              'label': 'Ablage'}},
        load_calibration=lambda: {},
        emit_status=lambda ev: status.append(ev),
        on_finished=lambda phase: status.append({'_finished': phase}),
        get_current_pose_xyz=lambda: sim_arm.fk_xyz(),
        get_follower_joints=sim_arm.get_joints,
        arm_profile=profile,
    )
    ok, msg, _ = mgr.start(json.dumps(program), 'wf-n6-e2e')
    assert ok, msg
    deadline = time.monotonic() + 30.0
    done = lambda: [e for e in status  # noqa: E731
                    if isinstance(e, dict) and '_finished' in e]
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    finished = done()
    assert finished and finished[-1]['_finished'] == 'finished', (
        f'n6 e2e did not finish cleanly: {status[-3:]}')
    errors = [e for e in status
              if isinstance(e, dict) and e.get('phase') == 'error']
    assert not errors, f'n6 e2e errored: {errors}'

    # Every emitted arm command is n+1 = 7 wide (home, move, both gripper moves,
    # and the 8-wide recording re-segmented down to 7-wide playback).
    assert captured, 'no vectors emitted'
    widths = {len(q) for q in captured}
    assert widths == {N6 + 1}, f'non-7-wide vectors emitted: widths={widths}'
    assert len(captured) > 20, 'suspiciously short n6 e2e stream'
