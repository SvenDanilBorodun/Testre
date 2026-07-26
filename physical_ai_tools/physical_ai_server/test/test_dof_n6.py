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


def test_sim_arm_n6_held_override_lands_on_index_n_and_touches_nothing_else():
    """The gripper channel of a 7-wide vector is index n=6, and a held override
    must touch ONLY it.

    MIGRATED off ``_FakeIK6`` to the REAL solver + the REAL edu6 gripper band
    (2026-07-26). As written before, this test could not fail: it ran on the stub,
    whose ``fk`` returns a CONSTANT (0.2, 0.0, 0.1) for every pose, and it
    published an OMX-style NEGATIVE close of −0.5 against the OMX default
    threshold — a value **outside edu6's legal 0…1.75 band** (the catalog
    validator refuses a negative close for this family, and the driver clamps a
    commanded −0.5 to 0.0), so it could never reach a real edu6 arm. Its own
    comment deferred that to "PR 7", which has since shipped.

    Measured: with the real solver the OLD body FAILS — the cube sat at the stub's
    constant fk, 150 mm from the real HOME fingertip against a 60 mm capture
    radius. It was green only because the stub pretended the arm stood on the
    object. Geometry/held semantics live in
    ``test_sim_arm_grasp_held_with_the_REAL_edu6_solver``; THIS test keeps the
    narrow index/width contract, honestly."""
    from physical_ai_server import robot_profiles
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    profile = robot_profiles.resolve('edu6_studio')
    solver = profile.build_ik()
    close_rad = fixed_catalog('edu6_studio').recipe_for_type(
        'wuerfel').gripper_close_rad
    # Put the object where the arm's fingertip ACTUALLY is for the pose we publish.
    ox, oy = solver.base_axis_x + 0.16, 0.0
    grasp = solver.solve((ox, oy, 0.015),
                         roll=solver.base_yaw(ox, oy) + motion.GRASP_ROLL_RAD)
    assert grasp is not None and len(grasp) == N6
    arm = SimArm(ik=solver, num_arm_joints=N6,
                 home_full_joints=list(profile.home_joints_rad)
                 + [profile.gripper_open_rad],
                 objects=[{'type': 'wuerfel', 'tag_id': 20, 'x': ox, 'y': oy,
                           'yaw': 0.0}],
                 close_threshold_rad=profile.sim_close_threshold_rad,
                 held_block_offset_rad=profile.sim_held_block_offset_rad,
                 held_floor_rad=profile.sim_held_floor_rad)
    arm.publish([(list(grasp) + [close_rad], 0.5)])
    q = arm.get_joints()
    assert len(q) == N6 + 1                       # gripper index == n
    assert q[N6] != pytest.approx(close_rad)      # index n WAS overridden
    assert q[:N6] == pytest.approx(list(grasp))   # ...and nothing else was


def test_path_guard_segment_blocked_slices_n_joints():
    """WIDTH probe — deliberately keeps ``_FakeIK6``.

    This is the one place the stub is the RIGHT tool: it ASSERTS that
    ``fk``/``link_points`` receive exactly 6 joints, which is the whole point of
    the test. The real solver cannot do that — it returns ``None`` for a short
    vector, and ``segment_blocked`` treats ``None`` as "not blocked", so swapping
    the real solver in here would silently WEAKEN the check to a tautology.
    Real edu6 geometry through ``segment_blocked`` is covered separately by
    ``test_segment_blocked_is_world_framed_with_the_REAL_edu6_solver``."""
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


# ── slice 2e: the reroute ladder + sim on the REAL edu6 solver ────────────────
# §8.4 blind-spot closure (2026-07-26). ``_FakeIK6`` above has no ``solve()``, so
# every n=6 test in slices 2a-2d proves WIDTH plumbing and no CONVENTION or
# GEOMETRY. That gap already hid one live bug (the Cartesian jog wrist mirror,
# guarded by test_jog_cartesian_keeps_the_wrist_with_the_REAL_edu6_solver).
# Driving path_guard and SimArm through the REAL Edu6IKSolver + the REAL registry
# profile found two more, both edu6-only and both invisible to a green suite:
#
#   1. path_guard's four reroute-geometry constants are OMX-SIZED heights and
#      radii. edu6's top-down TCP ceiling is ~0.065 m (joint5's +110° relief
#      binds, not the 2R annulus), so ALL THREE swing heights and the 0.18 m
#      cruise are unreachable: 0/144 base-swing via candidates solved, i.e. BOTH
#      reroute rungs were structurally dead and every zone-blocked transit fell
#      through to the German refusal. Now per-profile (ArmProfile.swing_* /
#      safe_travel_z_m / tool_clear_m; None → the module constants → OMX
#      unchanged).
#   2. plan_safe_route's roll=None fallback took the roll JOINT as the solver's
#      ``roll`` argument — the same defect class as the jog mirror, in the one
#      site that fix did not reach.

_PROFILE_ID = 'edu6_studio'


def _real_edu6_ctx(**kw):
    """A ctx carrying the REAL Edu6IKSolver and the REAL registry geometry.

    Deliberately NOT the synthetic ``_n6_ctx``/``_FakeIK6`` pair: the whole point
    of this slice is that the numbers and the conventions are real."""
    from physical_ai_server import robot_profiles
    p = robot_profiles.resolve(_PROFILE_ID)
    base = dict(
        ik=p.build_ik(),
        num_arm_joints=p.num_arm_joints,
        roll_joint_index=p.roll_joint_index,
        home_joints_rad=p.home_joints_rad,
        gripper_open_rad=p.gripper_open_rad,
        gripper_closed_rad=p.gripper_closed_rad,
        velocity_limit_rad_s=p.velocity_limit_rad_s,
        safe_travel_z_m=p.safe_travel_z_m,
        tool_clear_m=p.tool_clear_m,
        swing_heights_m=p.swing_heights_m,
        swing_radii_m=p.swing_radii_m,
        last_arm_joints=None,
        last_full_joints=list(p.home_joints_rad) + [p.gripper_open_rad],
        zones=None, tempo=1.0, calibration=None, destinations={},
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _omx_ctx_for_path_guard():
    """An OMX ctx with NO geometry stamps — the None→module-constant path every
    profile-less construction takes."""
    from physical_ai_server.workflow.ik_solver import IKSolver
    return types.SimpleNamespace(
        ik=IKSolver(), num_arm_joints=5, roll_joint_index=None,
        home_joints_rad=None, gripper_open_rad=None, gripper_closed_rad=None,
        velocity_limit_rad_s=None, last_arm_joints=None,
        last_full_joints=list(motion.HOME_JOINTS_RAD) + [motion.GRIPPER_OPEN_RAD],
        zones=None, tempo=1.0, calibration=None, destinations={},
    )


def _count_solvable_vias(ctx, heights, radii):
    from physical_ai_server.workflow import path_guard
    solver = ctx.ik
    ok = total = 0
    for z in heights:
        for r in radii:
            for az in path_guard._candidate_azimuths():
                total += 1
                via = path_guard._try_via(
                    ctx,
                    (solver.base_axis_x + r * math.cos(az), r * math.sin(az), z),
                    motion.GRASP_ROLL_RAD)
                if via is not None:
                    ok += 1
    return ok, total


def test_path_guard_geometry_defaults_to_the_omx_constants():
    """The accessor contract: an absent OR explicitly-None field resolves to the
    module constant, so the OMX and every profile-less run are unchanged."""
    from physical_ai_server.workflow import path_guard
    for bare in (types.SimpleNamespace(),
                 types.SimpleNamespace(safe_travel_z_m=None, tool_clear_m=None,
                                       swing_heights_m=None, swing_radii_m=None)):
        assert path_guard._safe_travel_z(bare) == path_guard.SAFE_TRAVEL_Z
        assert path_guard._tool_clear(bare) == path_guard._TOOL_CLEAR_M
        assert path_guard._swing_heights(bare) == path_guard._SWING_HEIGHTS
        assert path_guard._swing_radii(bare) == path_guard._SWING_RADII
    # ...and a profile stamp WINS.
    ctx = _real_edu6_ctx()
    assert path_guard._safe_travel_z(ctx) == pytest.approx(0.06)
    assert path_guard._tool_clear(ctx) == pytest.approx(0.0)
    assert path_guard._swing_heights(ctx) == (0.03, 0.05, 0.02)
    assert path_guard._swing_radii(ctx) == (0.14, 0.18, 0.10)


def test_base_swing_grid_is_unreachable_on_edu6_with_the_omx_heights():
    """The defect as a number: with the shipped OMX grid NOT ONE of the 3x3x16
    base-swing via candidates is solvable on edu6, so rung 3 could never return a
    route however the zones were drawn. The profile grid fixes it, and the OMX
    grid must stay fully solvable on the OMX."""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    omx_ok, total = _count_solvable_vias(
        ctx, path_guard._SWING_HEIGHTS, path_guard._SWING_RADII)
    assert omx_ok == 0, (
        f'expected the OMX grid to be wholly unreachable on edu6, got '
        f'{omx_ok}/{total} — re-derive the profile values if the arm changed')
    prof_ok, prof_total = _count_solvable_vias(
        ctx, path_guard._swing_heights(ctx), path_guard._swing_radii(ctx))
    # Tight, not merely non-zero: measured 64/144 (8 of 16 azimuths — joint 1 is
    # ±90°, so only the front half is ever reachable — × 8 solvable (z, r) pairs
    # of the 9, since z=0.05 r=0.18 solves 0/16). A loose `>= 24` would let a
    # degradation to 25 pass silently.
    assert prof_ok == 64, (
        f'the edu6 swing grid solves {prof_ok}/{prof_total}, expected 64 — '
        f're-derive the profile heights/radii against the solver if the arm or '
        f'the joint limits changed')
    # OMX control: the module constants are correct FOR THE OMX.
    omx_ctx = _omx_ctx_for_path_guard()
    ok, tot = _count_solvable_vias(
        omx_ctx, path_guard._swing_heights(omx_ctx),
        path_guard._swing_radii(omx_ctx))
    assert ok == tot, f'OMX swing grid regressed: {ok}/{tot}'


def test_lift_and_travel_cruise_needs_the_profile_tool_clearance_on_edu6():
    """Rung 2 on edu6: with the OMX 0.05 m tool clearance even a FLAT zone
    demands a 0.105 m cruise this arm cannot reach — dead for every zone. edu6's
    EE frame IS its fingertip TCP and its ``link_points`` appends nothing below it
    (measured: OMX 40 mm of appended tool-tip samples, edu6 0 mm), so the
    profile's 0.0 makes a flat zone routable again."""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    solver = ctx.ik
    flat_zone = [{'min': [solver.base_axis_x + 0.15, -0.02, 0.0],
                  'max': [solver.base_axis_x + 0.19, 0.02, 0.0]}]
    columns = [(solver.base_axis_x + 0.14, -0.05),
               (solver.base_axis_x + 0.14, 0.05)]
    clear_omx = path_guard._zone_clear_top(
        flat_zone, path_guard.ZONE_MARGIN_M, path_guard.LINK_RADIUS_M)
    clear_prof = path_guard._zone_clear_top(
        flat_zone, path_guard.ZONE_MARGIN_M, path_guard.LINK_RADIUS_M,
        path_guard._tool_clear(ctx))
    assert clear_prof == pytest.approx(clear_omx - path_guard._TOOL_CLEAR_M)
    # OMX geometry on the edu6 arm: no reachable cruise at all.
    assert path_guard._reachable_cruise_z(
        ctx, columns, clear_omx, path_guard.SAFE_TRAVEL_Z,
        motion.GRASP_ROLL_RAD) is None
    # Profile geometry: reachable, and inside the requested band.
    cruise = path_guard._reachable_cruise_z(
        ctx, columns, clear_prof, path_guard._safe_travel_z(ctx),
        motion.GRASP_ROLL_RAD)
    assert cruise is not None, 'edu6 cannot cruise over even a flat zone'
    assert clear_prof - 1e-9 <= cruise <= path_guard._safe_travel_z(ctx) + 1e-9


def test_edu6_roll_arg_vs_raw_joint_characterises_the_mirror():
    """CHARACTERISATION, not a guard — it documents the SIZE of the wrist mirror
    that the raw-joint fallback produced, so the numbers in CLAUDE.md and the
    handoff doc are pinned to something executable.

    Deliberately NOT named after the bug: it calls ``_try_via`` directly and never
    calls ``plan_safe_route``, so reverting the fallback does NOT fail it. The
    real guard for that is
    ``test_plan_safe_route_roll_none_converts_through_the_solver``, which records
    what the live ladder asks for. (Verified 2026-07-26: of 12 mutations, this
    test is killed by none of the call-site reverts — hence the rename.)"""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    solver = ctx.ik
    target = (solver.base_axis_x + 0.14, 0.04, 0.03)
    via_xyz = (solver.base_axis_x + 0.10, 0.0, 0.03)
    for wrist_deg in (50.0, -35.0, 20.0):
        q_end = solver.solve(
            target, roll=motion.GRASP_ROLL_RAD + math.radians(wrist_deg))
        assert q_end is not None and abs(q_end[5]) > 1e-3
        # what plan_safe_route now computes for roll=None
        via_fixed = path_guard._try_via(ctx, via_xyz, motion._roll_arg(ctx, q_end))
        # what it used to compute (the raw joint value)
        via_bug = path_guard._try_via(ctx, via_xyz, float(q_end[5]))
        assert via_fixed is not None and via_bug is not None
        assert via_fixed[5] == pytest.approx(q_end[5], abs=1e-9), (
            'the via wrist must match q_end so the reroute does not spin it')
        assert abs(via_bug[5] - q_end[5]) == pytest.approx(
            2 * abs(q_end[5]), abs=1e-9), (
            'expected the old fallback to MIRROR the wrist — if this no longer '
            'holds, the edu6 roll↔joint6 mapping changed')
    # OMX: roll_from_joints is the identity, so the fallback is unchanged there.
    omx_ctx = _omx_ctx_for_path_guard()
    for wrist in (-3.0, -1.0, 0.0, 1.0, 3.0):
        q_end = list(motion.HOME_JOINTS_RAD)
        q_end[4] = wrist
        assert motion._roll_arg(omx_ctx, q_end) == pytest.approx(wrist, abs=1e-12)


def test_sim_arm_grasp_held_with_the_REAL_edu6_solver():
    """SimArm's held simulation on the real arm: the real FK must land on the
    placed object's world XY (a URDF-framed FK would miss by ~2·x), the RADIAN
    gripper band must classify a close correctly (edu6 closes DOWNWARD from 1.75,
    the OMX close is NEGATIVE), and the blocked readback must clear
    ``check_grasp_held``'s per-object threshold."""
    from physical_ai_server import robot_profiles
    from physical_ai_server.workflow.sim_arm import SimArm
    p = robot_profiles.resolve(_PROFILE_ID)
    solver = p.build_ik()
    ox, oy = solver.base_axis_x + 0.16, 0.02
    grasp = solver.solve((ox, oy, 0.015),
                         roll=solver.base_yaw(ox, oy) + motion.GRASP_ROLL_RAD)
    assert grasp is not None
    # The real FK must agree with the world frame the sim objects live in.
    tcp = solver.fk(grasp)[1]
    assert math.hypot(float(tcp[0]) - ox, float(tcp[1]) - oy) < 1e-6

    def arm_with(objects):
        return SimArm(ik=solver, num_arm_joints=p.num_arm_joints,
                      home_full_joints=list(p.home_joints_rad)
                      + [p.gripper_open_rad],
                      objects=objects,
                      close_threshold_rad=p.sim_close_threshold_rad,
                      held_block_offset_rad=p.sim_held_block_offset_rad,
                      held_floor_rad=p.sim_held_floor_rad)

    on_object = [{'type': 'wuerfel', 'tag_id': 20, 'x': ox, 'y': oy, 'yaw': 0.0}]
    # Read the close out of the REAL per-family catalog rather than hardcoding
    # it: edu6 closes DOWNWARD inside a 0…1.75 rad band (1.0 for `wuerfel`),
    # where the OMX value for the same object is −0.5. A magic number here would
    # silently stop tracking the catalog.
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    close_rad = fixed_catalog(_PROFILE_ID).recipe_for_type(
        'wuerfel').gripper_close_rad
    assert 0.0 < close_rad < p.sim_close_threshold_rad, (
        f'catalog close {close_rad} is not a close command for this gripper')
    threshold = close_rad + p.grasp_held_margin_rad

    arm = arm_with(on_object)
    arm.publish([(list(grasp) + [close_rad], 0.5)])
    held = arm.get_joints()
    assert len(held) == p.num_arm_joints + 1
    assert held[6] > threshold, (
        f'blocked jaw {held[6]:.3f} does not clear the per-object held '
        f'threshold {threshold:.3f} — a sim grasp would never report HELD')
    assert held[:6] == pytest.approx(list(grasp)), 'arm joints must be untouched'

    # An OPEN command is not a close: the readback passes through verbatim.
    arm.publish([(list(grasp) + [p.gripper_open_rad], 0.6)])
    assert arm.get_joints()[6] == pytest.approx(p.gripper_open_rad)

    # A close with nothing under the jaws is a MISS, not a phantom hold. Probe
    # just OUTSIDE the capture radius rather than far away: at 0.20 m (3.3× the
    # radius) this leg would pass even if the radius were mis-set to anything
    # under 0.20, so it would not detect a widened capture disc.
    from physical_ai_server.workflow import sim_arm as _sim_arm
    radius = _sim_arm._GRASP_CAPTURE_RADIUS_M
    just_outside = arm_with([{'type': 'wuerfel', 'tag_id': 20,
                              'x': ox + radius * 1.05, 'y': oy, 'yaw': 0.0}])
    just_outside.publish([(list(grasp) + [close_rad], 0.5)])
    assert just_outside.get_joints()[6] == pytest.approx(close_rad), (
        'an object just outside the capture radius produced a phantom hold')
    # ...and just INSIDE it still holds, so the boundary is pinned from both sides.
    just_inside = arm_with([{'type': 'wuerfel', 'tag_id': 20,
                             'x': ox + radius * 0.95, 'y': oy, 'yaw': 0.0}])
    just_inside.publish([(list(grasp) + [close_rad], 0.5)])
    assert just_inside.get_joints()[6] > threshold


def test_segment_blocked_is_world_framed_with_the_REAL_edu6_solver():
    """``path_guard`` tests zone boxes against ``ik.link_points``. edu6 is solved
    in a WORLD frame that is the URDF frame rotated π about Z, so a link_points
    that forgot the rotation would check the mirrored half of the table: zones
    would be missed and phantom zones hit. The π-rotated twin box is the sharp
    assertion — it must NOT block."""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    solver = ctx.ik
    qa = solver.solve((solver.base_axis_x + 0.16, -0.06, 0.03),
                      roll=motion.GRASP_ROLL_RAD)
    qb = solver.solve((solver.base_axis_x + 0.16, 0.06, 0.03),
                      roll=motion.GRASP_ROLL_RAD)
    assert qa is not None and qb is not None
    mid = solver.fk([(a + b) / 2.0 for a, b in zip(qa, qb)])[1]
    mx, my = float(mid[0]), float(mid[1])

    def box(cx, cy):
        return [{'min': [cx - 0.02, cy - 0.02, 0.0],
                 'max': [cx + 0.02, cy + 0.02, 0.10]}]

    assert path_guard.segment_blocked(solver, qa, qb, box(mx, my)) is True
    assert path_guard.segment_blocked(solver, qa, qb, box(-0.35, 0.35)) is False
    assert path_guard.segment_blocked(solver, qa, qb, box(-mx, -my)) is False, (
        'a π-rotated (URDF-frame) box blocked the sweep — link_points is not '
        'reporting WORLD coordinates')


def test_plan_base_swing_asks_the_solver_for_the_PROFILE_heights_and_radii():
    """CALL-SITE guard for rung 3 (the numbers alone are not enough — a test that
    only checks the accessors would still pass if ``_plan_base_swing`` kept
    reading the module constants).

    Records every via candidate the real ``_plan_base_swing`` asks for and
    asserts the height/radius SET is the profile's, not the OMX module grid."""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    asked: list[tuple[float, float, float]] = []
    original = path_guard._try_via
    try:
        path_guard._try_via = lambda _c, xyz, _roll: (asked.append(tuple(xyz))
                                                     or None)
        # Every candidate refuses, so the rung exhausts its whole grid and we see
        # all of it. The return value is irrelevant here.
        path_guard._plan_base_swing(
            ctx, list(ctx.last_full_joints), list(ctx.last_full_joints), [],
            path_guard.ZONE_MARGIN_M, path_guard.LINK_RADIUS_M,
            ctx.gripper_open_rad, motion.GRASP_ROLL_RAD, 2.5)
    finally:
        path_guard._try_via = original
    assert asked, 'the rung asked for no candidates at all'
    heights = sorted({round(z, 6) for _x, _y, z in asked})
    radii = sorted({round(math.hypot(x - ctx.ik.base_axis_x, y), 6)
                    for x, y, _z in asked})
    assert heights == sorted(path_guard._swing_heights(ctx)), (
        f'_plan_base_swing used heights {heights}, not the profile grid '
        f'{sorted(path_guard._swing_heights(ctx))}')
    assert radii == sorted(path_guard._swing_radii(ctx)), (
        f'_plan_base_swing used radii {radii}, not the profile grid')
    # ...and the OMX constants must NOT be what it asked for.
    assert heights != sorted(path_guard._SWING_HEIGHTS)


def test_plan_lift_and_travel_uses_the_profile_cruise_and_tool_clearance():
    """CALL-SITE guard for rung 2: records the ``(clear_z, ceil_z)`` the real
    ``_plan_lift_and_travel`` hands ``_reachable_cruise_z``. ``ceil_z`` must be
    the profile cruise (0.06, not the OMX 0.18) and ``clear_z`` must be computed
    with the profile tool clearance (0.0, not the OMX 0.05)."""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    solver = ctx.ik
    start = solver.solve((solver.base_axis_x + 0.14, -0.05, 0.03),
                         roll=motion.GRASP_ROLL_RAD)
    end = solver.solve((solver.base_axis_x + 0.14, 0.05, 0.03),
                       roll=motion.GRASP_ROLL_RAD)
    assert start is not None and end is not None
    zone_top = 0.0
    zones = [{'min': [solver.base_axis_x + 0.15, -0.02, 0.0],
              'max': [solver.base_axis_x + 0.19, 0.02, zone_top]}]
    seen: list[tuple[float, float]] = []
    original = path_guard._reachable_cruise_z
    try:
        path_guard._reachable_cruise_z = (
            lambda _c, _cols, clear_z, ceil_z, _roll:
            seen.append((clear_z, ceil_z)) or None)
        path_guard._plan_lift_and_travel(
            ctx, list(start) + [ctx.gripper_open_rad],
            list(end) + [ctx.gripper_open_rad], zones,
            path_guard.ZONE_MARGIN_M, path_guard.LINK_RADIUS_M,
            ctx.gripper_open_rad, motion.GRASP_ROLL_RAD, 2.5)
    finally:
        path_guard._reachable_cruise_z = original
    assert len(seen) == 1, f'expected one cruise query, got {seen}'
    clear_z, ceil_z = seen[0]
    assert ceil_z == pytest.approx(0.06), (
        f'lift-and-travel asked to cruise at {ceil_z} — the OMX 0.18 is '
        f'unreachable on edu6')
    inflated_top = zone_top + path_guard.LINK_RADIUS_M + path_guard.ZONE_MARGIN_M
    assert clear_z == pytest.approx(inflated_top + path_guard._CLEAR_EPS_M), (
        f'clear_z {clear_z} still carries an OMX tool clearance')


def test_plan_safe_route_roll_none_converts_through_the_solver():
    """CALL-SITE guard for the roll fallback: drives the REAL
    ``plan_safe_route(roll=None)`` and records the ``roll`` its vias are solved
    with. It must be ``roll_from_joints(q_end)``, never ``q_end[roll_idx]``.

    A zone is drawn across the direct transit so the ladder actually reaches its
    via code; whether it ultimately routes or refuses is irrelevant — the
    assertion is on the roll it asked for."""
    from physical_ai_server.workflow import path_guard
    ctx = _real_edu6_ctx()
    solver = ctx.ik
    start = solver.solve((solver.base_axis_x + 0.14, -0.06, 0.03),
                         roll=motion.GRASP_ROLL_RAD)
    for wrist_deg in (50.0, -35.0):
        end = solver.solve(
            (solver.base_axis_x + 0.14, 0.06, 0.03),
            roll=motion.GRASP_ROLL_RAD + math.radians(wrist_deg))
        assert start is not None and end is not None
        assert abs(end[5]) > 1e-3, 'need a NON-zero wrist or a mirror hides'
        q_start = list(start) + [ctx.gripper_open_rad]
        q_end = list(end) + [ctx.gripper_open_rad]
        mid = solver.fk([(a + b) / 2.0 for a, b in zip(start, end)])[1]
        zones = [{'min': [float(mid[0]) - 0.02, float(mid[1]) - 0.02, 0.0],
                  'max': [float(mid[0]) + 0.02, float(mid[1]) + 0.02, 0.10]}]
        assert path_guard.segment_blocked(solver, q_start, q_end, zones), (
            'the scenario must block the DIRECT rung or no via is ever solved')
        rolls: list[float] = []
        original = path_guard._try_via
        try:
            path_guard._try_via = lambda _c, _xyz, roll: (rolls.append(roll)
                                                         or None)
            try:
                path_guard.plan_safe_route(ctx, q_start, q_end, zones, 2.5,
                                           roll=None)
            except motion.WorkflowError:
                pass          # refusing is fine; we only care about the roll
        finally:
            path_guard._try_via = original
        assert rolls, 'no via was solved — the ladder never reached its vias'
        expected = solver.roll_from_joints(q_end)
        assert all(r == pytest.approx(expected, abs=1e-12) for r in rolls), (
            f'vias solved with {sorted(set(rolls))}, expected {expected}')
        # And prove that is NOT the joint value (which is what the bug passed).
        assert abs(expected - float(q_end[5])) > math.radians(30), (
            'roll and joint6 must differ here or the test cannot detect the bug')


def test_workflow_manager_stamps_every_profile_field_onto_the_REAL_ctx(monkeypatch):
    """THE LAST MILE — and the one hole slice 2e did not close on its own.

    ``path_guard`` reads its per-arm geometry off ``ctx``, and the ONLY thing that
    puts it there on a real run is the ``WorkflowContext(...)`` construction in
    ``WorkflowManager.start``. Every other test in this file builds its ctx
    DIRECTLY from the profile (``_real_edu6_ctx``) or from a hand-written
    namespace, so none of them touches that line. Measured 2026-07-26: replacing
    all four new stamps with ``None``, or a ONE-CHARACTER typo in a getattr key
    (``'swing_heights_m'`` → ``'swing_height_m'``), silently reverts the entire
    reroute fix — edu6's ladder is structurally dead again — and the whole suite
    stays green at 723 passed.

    The hole was pre-existing and systemic (``velocity_limit_rad_s`` and
    ``grasp_held_margin_rad`` had it too), so this test covers EVERY
    profile-derived ctx field, not just the four new ones. **Add a row here when
    you add a stamp.**

    It drives the REAL manager with the REAL registry profile and inspects the
    ctx that was actually constructed."""
    import json
    import time as _time

    from physical_ai_server import robot_profiles
    from physical_ai_server.workflow import workflow_manager as _wm

    profile = robot_profiles.resolve(_PROFILE_ID)
    assert profile.profile_id == _PROFILE_ID, 'registry resolve fell back'

    captured: dict = {}
    real_ctx_cls = _wm.WorkflowContext

    def _recording_ctx(**kwargs):
        captured.update(kwargs)
        return real_ctx_cls(**kwargs)

    monkeypatch.setattr(_wm, 'WorkflowContext', _recording_ctx)

    finished: list = []
    mgr = _wm.WorkflowManager(
        publisher=lambda _chunk: None,
        arm_profile=profile,
        on_finished=lambda phase: finished.append(phase),
    )
    # An empty program is enough: the ctx is built BEFORE any block runs.
    ok, msg, _ = mgr.start(json.dumps({'blocks': {'blocks': []}}), 'wf-stamp')
    assert ok, f'start refused: {msg}'
    deadline = _time.monotonic() + 15.0
    while not finished and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert finished, 'the empty workflow never finished — cannot trust the ctx'

    assert captured, 'no WorkflowContext was constructed'
    # (ctx field, profile field) — every profile-derived stamp in start().
    for ctx_field, profile_field in (
        ('num_arm_joints', 'num_arm_joints'),
        ('roll_joint_index', 'roll_joint_index'),
        ('home_joints_rad', 'home_joints_rad'),
        ('observe_pose_joints', 'observe_pose_joints'),
        ('gripper_open_rad', 'gripper_open_rad'),
        ('gripper_closed_rad', 'gripper_closed_rad'),
        ('velocity_limit_rad_s', 'velocity_limit_rad_s'),
        ('grasp_held_margin_rad', 'grasp_held_margin_rad'),
        ('safe_travel_z_m', 'safe_travel_z_m'),
        ('tool_clear_m', 'tool_clear_m'),
        ('swing_heights_m', 'swing_heights_m'),
        ('swing_radii_m', 'swing_radii_m'),
    ):
        assert ctx_field in captured, (
            f'WorkflowContext was built WITHOUT {ctx_field!r} — a profile stamp '
            f'is missing from WorkflowManager.start')
        # getattr-with-default mirrors the manager's own stamping semantics: a
        # field ArmProfile does not define stamps as None. `observe_pose_joints`
        # is exactly that today — the manager reads it but the dataclass has no
        # such field, so it can only ever be None and `motion._observe_joints`
        # always falls through to EDUBOTICS_OBSERVE_POSE / HOME.
        expected = getattr(profile, profile_field, None)
        assert captured[ctx_field] == expected, (
            f'ctx.{ctx_field} = {captured[ctx_field]!r} but the profile says '
            f'{expected!r} — the stamp is wired to the wrong field (a typo in a '
            f'getattr key reads as None and silently reverts the behaviour)')

    # The four geometry values must be the REAL edu6 numbers, not None: a None
    # here resolves to path_guard's OMX module constants, which are exactly the
    # unreachable values this profile exists to override.
    assert captured['safe_travel_z_m'] is not None
    assert captured['tool_clear_m'] is not None
    assert captured['swing_heights_m'] is not None
    assert captured['swing_radii_m'] is not None

    # ...and path_guard, reading that REAL ctx, must resolve the profile values
    # rather than falling back. This is the actual end-to-end contract.
    from physical_ai_server.workflow import path_guard
    ctx = real_ctx_cls(**captured)
    assert path_guard._swing_heights(ctx) == tuple(profile.swing_heights_m)
    assert path_guard._swing_radii(ctx) == tuple(profile.swing_radii_m)
    assert path_guard._safe_travel_z(ctx) == pytest.approx(profile.safe_travel_z_m)
    assert path_guard._tool_clear(ctx) == pytest.approx(profile.tool_clear_m)
    assert path_guard._swing_heights(ctx) != path_guard._SWING_HEIGHTS

    # An OMX profile must still resolve to the module constants through the SAME
    # real path (the None-stamp branch).
    captured.clear()
    mgr_omx = _wm.WorkflowManager(
        publisher=lambda _chunk: None,
        arm_profile=robot_profiles.resolve('omx_full'),
        on_finished=lambda phase: finished.append(phase),
    )
    ok, msg, _ = mgr_omx.start(json.dumps({'blocks': {'blocks': []}}), 'wf-stamp-omx')
    assert ok, f'OMX start refused: {msg}'
    deadline = _time.monotonic() + 15.0
    while len(finished) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    omx_ctx = real_ctx_cls(**captured)
    assert captured['safe_travel_z_m'] is None
    assert path_guard._safe_travel_z(omx_ctx) == path_guard.SAFE_TRAVEL_Z
    assert path_guard._tool_clear(omx_ctx) == path_guard._TOOL_CLEAR_M
    assert path_guard._swing_heights(omx_ctx) == path_guard._SWING_HEIGHTS
    assert path_guard._swing_radii(omx_ctx) == path_guard._SWING_RADII
