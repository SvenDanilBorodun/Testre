"""Regression tests for the 2026-09-07 Roboter-Studio audit, Implementer B half.

Every test here pins a defect that was REPRODUCED by running the real handlers,
the real IK solvers for all three shipped arm profiles (``omx_full`` /
``edu6_studio`` / ``edu1_studio``) and the real catalogs, and the observed
"before" numbers are quoted in the docstring or the code comment of the fix.

The suite is deliberately profile-parameterised wherever the defect was: five of
these were invisible on the OMX and only appeared on the two Feetech arms whose
ONLY capability is Roboter Studio.
"""

from __future__ import annotations

import math
import threading
import time
import types

import numpy as np
import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow import path_guard
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import destinations as dst
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import output as out
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers import trajectory as traj
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError
from physical_ai_server.workflow.object_catalog import fixed_catalog


PROFILE_IDS = ('omx_full', 'edu6_studio', 'edu1_studio')
# A radius inside each arm's own pick band, at the grasp plane.
BAND_R = {'omx_full': 0.20, 'edu6_studio': 0.14, 'edu1_studio': 0.20}


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    """Skip ``chunked_publish``'s 1 s inter-chunk wall-clock pacing; the
    published WAYPOINTS are byte-identical to production."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(
        trajectory_builder, 'time',
        types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None))
    yield


class Ctx:
    """A WorkflowContext stand-in stamped from a REAL ArmProfile, exactly the
    way ``WorkflowManager.start`` stamps one."""

    def __init__(self, profile_id, z_table=0.0, zones=None, tempo=1.0):
        self.profile = rp.resolve(profile_id)
        p = self.profile
        self.ik = p.build_ik()
        self.published: list[list] = []
        self.logs: list[str] = []
        self.z_table = z_table
        self.board_table_z = z_table
        self.table_plane = None
        self.scene_intrinsics = None
        self.scene_extrinsics = None
        self.motion_lock = threading.RLock()
        self.claim_lock = threading.RLock()
        self.var_lock = threading.RLock()
        self.should_stop = lambda: False
        self.destinations: dict = {}
        self.trajectories: dict = {}
        self.zones = zones
        self.tempo = tempo
        self.claimed_tags: set = set()
        self.skipped_tags: set = set()
        # The reclaim's position state (G9 replaced the absence clock).
        # `absent_since` no longer exists in production.
        self.claim_anchor: dict = {}
        self.claim_pick_xy: dict = {}
        self.claim_unseen: set = set()
        self.last_commanded_close_rad = None
        self.last_arm_joints = None
        self.num_arm_joints = p.num_arm_joints
        self.roll_joint_index = p.roll_joint_index
        self.home_joints_rad = p.home_joints_rad
        self.observe_pose_joints = p.observe_pose_joints
        self.gripper_open_rad = p.gripper_open_rad
        self.gripper_closed_rad = p.gripper_closed_rad
        self.velocity_limit_rad_s = p.velocity_limit_rad_s
        self.grasp_held_margin_rad = p.grasp_held_margin_rad
        self.safe_travel_z_m = p.safe_travel_z_m
        self.tool_clear_m = p.tool_clear_m
        self.swing_heights_m = p.swing_heights_m
        self.swing_radii_m = p.swing_radii_m
        self.object_catalog = fixed_catalog(profile_id)
        self.last_full_joints = list(p.home_joints_rad) + [p.gripper_open_rad]
        self.gripper_readback = p.gripper_open_rad

    # -- sinks ------------------------------------------------------------
    def publisher(self, chunk):
        self.published.append(list(chunk))

    def log(self, msg):
        self.logs.append(msg)

    def emit_detections(self, dets):
        pass

    def get_follower_joints(self):
        return list(self.last_full_joints[:self.num_arm_joints]) + [
            self.gripper_readback]

    # -- helpers ----------------------------------------------------------
    @property
    def commanded(self):
        assert self.published, 'no motion was published'
        return self.published[-1][-1][0]

    def park_at(self, xyz, roll=None, gripper=None):
        """Put the arm at a solvable Cartesian pose (so a block starts there)."""
        q = self.ik.solve(target_xyz=xyz, seed=list(self.home_joints_rad),
                          roll=motion.GRASP_ROLL_RAD if roll is None else roll)
        assert q is not None, f'test premise: {xyz} must be reachable'
        g = self.gripper_open_rad if gripper is None else gripper
        self.last_full_joints = list(q) + [g]
        self.last_arm_joints = list(q)
        return list(q)


def greifziel(ctx, xyz, tag_yaw=0.0, tag=20, close=None, approach=0.06):
    """A Greifziel with the same shape ``find_object`` returns."""
    rec = ctx.object_catalog.recipe_for_type('wuerfel')
    d = types.SimpleNamespace()
    d.world_xyz_m = tuple(float(v) for v in xyz)
    d.aruco_id = tag
    d.corners_px = None
    d.extras = {
        'tag_yaw': tag_yaw,
        'gripper_close_rad': rec.gripper_close_rad if close is None else close,
        'approach_clear_m': approach,
    }
    return d


def band_xyz(ctx, z=0.015, dy=0.0):
    r = BAND_R[ctx.profile.profile_id]
    return (ctx.ik.base_axis_x + r, dy, z)


# ══════════════════════════════════════════════════════════════════════════
# RS-06 — „Greifer hält etwas?" was CONSTANT TRUE on both Feetech arms
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_an_empty_full_close_reads_MISS_on_every_profile(profile_id, monkeypatch):
    """The fallback threshold must live INSIDE each arm's gripper band.

    ``GRASP_HELD_MAX_RAD`` (−0.35) is an OMX-band number
    (``gripper_closed_rad + GRASP_HELD_MARGIN_RAD`` = −0.5 + 0.15). Measured
    2026-09-07 with it used verbatim on every profile, no close commanded:

        omx_full     band [−0.50, 0.80]  OPEN True  CLOSED-EMPTY False  ok
        edu6_studio  band [ 0.00, 1.75]  OPEN True  CLOSED-EMPTY TRUE   wrong
        edu1_studio  band [ 0.00, 0.90]  OPEN True  CLOSED-EMPTY TRUE   wrong

    i.e. an EMPTY FULL CLOSE read as a successful grasp on the only two profiles
    whose sole capability is Roboter Studio.
    """
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ctx = Ctx(profile_id)
    ctx.last_commanded_close_rad = None            # fresh run

    # OPEN jaws, nothing commanded → the documented open-gripper behaviour.
    ctx.gripper_readback = ctx.gripper_open_rad
    assert motion.check_grasp_held(ctx) is True

    # An EMPTY close reaches the arm's own closed angle → MISS, on every arm.
    ctx.gripper_readback = ctx.gripper_closed_rad
    assert motion.check_grasp_held(ctx) is False

    threshold = motion._held_threshold_rad(ctx)
    lo = min(ctx.gripper_closed_rad, ctx.gripper_open_rad)
    hi = max(ctx.gripper_closed_rad, ctx.gripper_open_rad)
    assert lo <= threshold <= hi, (
        f'{profile_id}: the fallback threshold {threshold} is outside the '
        f'gripper band [{lo}, {hi}] and therefore decides nothing')


def test_the_omx_fallback_threshold_is_byte_identical_to_the_old_constant():
    ctx = Ctx('omx_full')
    ctx.last_commanded_close_rad = None
    assert motion._grasp_held_max(ctx) == pytest.approx(motion.GRASP_HELD_MAX_RAD)


def test_a_profileless_ctx_still_gets_the_omx_constant():
    """Every non-Roboter-Studio path and every plain test double."""
    bare = types.SimpleNamespace(last_commanded_close_rad=None)
    assert motion._grasp_held_max(bare) == pytest.approx(motion.GRASP_HELD_MAX_RAD)


def test_the_env_override_still_wins_everywhere(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '-0.35')
    ctx = Ctx('edu6_studio')
    ctx.last_commanded_close_rad = None
    assert motion._held_threshold_rad(ctx) == pytest.approx(motion.GRASP_HELD_MAX_RAD)


# ══════════════════════════════════════════════════════════════════════════
# RS-07 (Rule §2) — the grasp height must follow the MEASURED table plane
# ══════════════════════════════════════════════════════════════════════════

def _tilt_ctx(deg, centroid_x=0.18):
    """A ctx whose touch-off measured a plane tilted ``deg`` about +x, with
    ``z_table`` sampled at the tap centroid (which is what solve_table_plane
    does)."""
    a = math.tan(math.radians(deg))
    c = -a * centroid_x                       # z_table == 0 at the centroid
    ctx = Ctx('omx_full', z_table=0.0)
    ctx.table_plane = (a, 0.0, c)
    return ctx


@pytest.mark.parametrize('deg,expect_mm', [(0, 0.0), (4, 8.39), (8, 16.86), (11, 23.33)])
def test_grasp_height_follows_the_tilted_plane_at_the_objects_own_xy(deg, expect_mm):
    """Measured 2026-09-07 (tap centroid (0.18, 0), object 12 cm further out,
    commanded grasp z fixed at +15.00 mm by the scalar path):

        tilt   surface at the object   clearance   refused?
         4°          +8.39 mm           +6.61 mm     no
         8°         +16.86 mm           −1.86 mm     no   ← INTO the table
        11°         +23.33 mm           −8.33 mm     no   ← INTO the table
        14°         +29.92 mm          −14.92 mm    YES

    a ~10 mm window with the tool under the local surface and no guard objecting.
    """
    ctx = _tilt_ctx(deg)
    ox = 0.30
    surface = motion.table_z_at(ctx, ox, 0.0)
    assert surface * 1000 == pytest.approx(expect_mm, abs=0.02)
    # …and the grasp z the perception layer now derives sits ABOVE it by the
    # recipe's own band (object_height − grasp_depth = 15 mm), for every tilt.
    rec = ctx.object_catalog.recipe_for_type('wuerfel')
    grasp_z = surface + rec.object_height_m - rec.grasp_depth_m
    assert grasp_z - surface == pytest.approx(0.015, abs=1e-9)


def test_the_grasp_height_uses_the_same_plane_the_floor_guard_uses():
    ctx = _tilt_ctx(8)
    for x in (0.18, 0.24, 0.30):
        assert motion.table_z_at(ctx, x, 0.0) == pytest.approx(
            motion._floor_z_at(ctx, x, 0.0))


def test_grasp_z_from_plane_rollback_restores_the_scalar_height(monkeypatch):
    """``EDUBOTICS_GRASP_Z_FROM_PLANE=0`` — ONE knob for BOTH sites."""
    ctx = _tilt_ctx(8)
    monkeypatch.setattr(motion, 'GRASP_Z_FROM_PLANE', False)
    assert motion.table_z_at(ctx, 0.30, 0.0) == pytest.approx(0.0)
    # …while the FLOOR guard stays plane-aware, knob or no knob.
    assert motion._floor_z_at(ctx, 0.30, 0.0) == pytest.approx(0.016865, abs=1e-5)


def test_table_z_at_is_duck_typed_for_the_node_call_site():
    """``physical_ai_server.py::mark_destination_callback`` has no
    WorkflowContext — it must be able to pass any holder of the two fields."""
    holder = types.SimpleNamespace(z_table=0.0, table_plane=(0.14054, 0.0, -0.0253))
    assert motion.table_z_at(holder, 0.30, 0.0) == pytest.approx(0.016862, abs=1e-5)
    assert motion.table_z_at(
        types.SimpleNamespace(z_table=0.02, table_plane=None), 0.3, 0.0) == 0.02
    assert motion.table_z_at(
        types.SimpleNamespace(z_table=None, table_plane=None), 0.3, 0.0) is None


def test_a_malformed_plane_falls_back_to_the_scalar_rather_than_failing_open():
    holder = types.SimpleNamespace(z_table=0.05, table_plane=('x', 1, 2))
    assert motion.table_z_at(holder, 0.3, 0.0) == pytest.approx(0.05)


# ══════════════════════════════════════════════════════════════════════════
# RS-19 (Rule §2) — the object-agnostic „aufnehmen" squeeze
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', [
    'omx_full', 'omx_follower', 'edu6_studio', 'edu1_studio',
])
def test_pickup_commands_the_full_hardware_close_on_every_profile(profile_id):
    """OWNER DECISION 2026-09-08 — RS-19 was REVERTED to the full close.

    This test previously pinned the opposite. The measured defect is real and
    unchanged; what changed is which side of the trade-off ships.

    The defect (measured 2026-09-07), catalog close vs what ``pickup`` commands:

        omx_full      catalog −0.50   pickup −0.50   (identical, no-op)
        edu6_studio   catalog  1.00   pickup  0.00   (1.00 rad DEEPER)
        edu1_studio   catalog  0.10   pickup  0.00   (0.10 rad deeper)

    On edu6 that is a ≈1.19 rad position error against a 30 mm cube that already
    blocks the jaws — saturated PWM at Max_Torque 150, sustained through the
    close, the lift, the whole carry and the descend, because ``drop_at`` carries
    at ``last_commanded_close_rad``.

    Why the owner chose it anyway: „aufnehmen" is OBJECT-AGNOSTIC and has no
    catalog to consult, so a bounded close silently stops gripping anything
    thinner than the jaw gap at that angle — ≈20-25 mm on edu6, ≈13 mm on edu1
    (measured off the shipped finger meshes; the ≈2 mm first reported came from
    using CLAUDE.md's 21 mm zero-offset as a slope, and the doc's affine model
    does not reproduce on the meshes at all). Pens, cards and thin blocks would
    just fail to grip, with no message. „Greife" and the split blocks are
    unaffected either way — they command the recipe's own close.

    NEITHER SIDE IS HARDWARE-MEASURED. The revert restores an unmeasured stall;
    the fix shipped an unmeasured grip. Rig gate before either is trusted.

    To opt a rig into the bounded squeeze, set EDUBOTICS_PICKUP_CLOSE_RAD to that
    profile's catalog close — NEVER to 0, which means "fully closed" on the
    Feetech arms but OPENS an OMX gripper by 0.5 rad.
    """
    ctx = Ctx(profile_id)
    assert motion._pickup_close(ctx) == pytest.approx(ctx.gripper_closed_rad)


def test_the_omx_generic_close_is_still_its_hardware_close():
    ctx = Ctx('omx_full')
    assert motion._pickup_close(ctx) == pytest.approx(ctx.gripper_closed_rad)


@pytest.mark.parametrize('profile_id', ('edu6_studio', 'edu1_studio'))
def test_pickup_close_rollback_restores_the_full_close(profile_id, monkeypatch):
    """The env override still works — it is now the way to opt IN to the bounded
    squeeze, since the shipped default is the full close.

    ``0`` is deliberately exercised here because it is the value an operator
    types: on the Feetech arms it happens to equal ``gripper_closed_rad``, which
    is why it looks harmless — but on an OMX rig 0 is 0.5 rad OPEN. Use the
    profile's catalog close, never 0."""
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', '0')
    monkeypatch.setattr(motion, 'PICKUP_CLOSE_RAD', 0.0)
    ctx = Ctx(profile_id)
    assert motion._pickup_close(ctx) == pytest.approx(ctx.gripper_closed_rad)


@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_pickup_records_the_generic_close_so_drop_at_carries_at_it(profile_id):
    """``_execute_pickup`` writes the commanded close into
    ``last_commanded_close_rad``, which ``drop_at`` reuses as the CARRY close —
    which is how the full close was sustained through the whole place."""
    ctx = Ctx(profile_id)
    motion.pickup(ctx, {'target': band_xyz(ctx, z=0.0)})
    assert ctx.last_commanded_close_rad == pytest.approx(motion._pickup_close(ctx))


def test_the_profile_field_is_the_single_source_of_truth():
    """The lookup seam still works and is still registry-backed — the owner's
    revert unsets the VALUE, it does not remove the mechanism.

    Pinning ``pickup_close_rad is None`` on every profile is what makes the
    revert visible: a future edit that re-adds a value has to come here and argue
    with the docstring above, rather than silently changing what „aufnehmen"
    squeezes."""
    for pid in ('edu6_studio', 'edu1_studio'):
        profile = rp.ROBOT_PROFILES[pid]
        assert motion._profile_for_ctx(Ctx(pid)) is profile
        assert profile.pickup_close_rad is None, (
            f'{pid} re-declared pickup_close_rad; RS-19 was reverted by owner '
            'decision 2026-09-08 — see the docstring above before changing this'
        )
        assert motion._pickup_close(Ctx(pid)) == pytest.approx(
            profile.gripper_closed_rad)


# ══════════════════════════════════════════════════════════════════════════
# RS-20 — „Greife" reported success when the gripper never closed
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_a_gripper_still_at_the_open_command_is_a_MISS(profile_id, monkeypatch):
    """Measured 2026-09-07 with the follower gripper PINNED OPEN: „Greife"
    returned OK, printed „„Würfel" gegriffen." and CLAIMED the tag on all three
    profiles — so a „Solange sichtbar" loop marked the object done."""
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ctx = Ctx(profile_id)
    rec = ctx.object_catalog.recipe_for_type('wuerfel')
    ctx.last_commanded_close_rad = rec.gripper_close_rad
    ctx.gripper_readback = ctx.gripper_open_rad
    assert motion.check_grasp_held(ctx) is False


@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_a_genuinely_blocked_jaw_is_still_HELD(profile_id, monkeypatch):
    """The fix must only ever turn True → False, never the reverse."""
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ctx = Ctx(profile_id)
    rec = ctx.object_catalog.recipe_for_type('wuerfel')
    ctx.last_commanded_close_rad = rec.gripper_close_rad
    # A 30 mm cube stops the jaws well away from BOTH ends of the band.
    ctx.gripper_readback = 0.5 * (rec.gripper_close_rad + ctx.gripper_open_rad)
    assert motion.check_grasp_held(ctx) is True


def test_grasp_held_travel_rollback(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    monkeypatch.setattr(motion, 'GRASP_HELD_MIN_TRAVEL_FRAC', 0.0)
    ctx = Ctx('edu6_studio')
    ctx.last_commanded_close_rad = 1.0
    ctx.gripper_readback = ctx.gripper_open_rad
    assert motion.check_grasp_held(ctx) is True


# ══════════════════════════════════════════════════════════════════════════
# RS-22 / RS-39 — wrong VALUE types in the ZIEL and DESTINATION sockets
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_close_on_object_refuses_a_non_greifziel(profile_id):
    """Measured 2026-09-07 with a variable holding 42.0: err=None,
    commanded_close −0.5 / 0.0 / 0.0, logs=[] — the profile's HARDEST close,
    silently, on all three arms.

    G13: raised as a base ``WorkflowError``, NOT a ``GraspSkip``. A wrongly
    wired value is a PROGRAM error — no loop pass can turn 42.0 into a
    Greifziel — and as a GraspSkip the „Solange sichtbar" loop swallowed it,
    descended on the cube three times with an OPEN gripper (21 motion chunks)
    and still reported phase `finished`, green."""
    ctx = Ctx(profile_id)
    with pytest.raises(WorkflowError) as exc:
        motion.close_on_object(ctx, {'ziel': 42.0})
    assert not isinstance(exc.value, GraspSkip), (
        'a wrongly wired value must not be loop-swallowable')
    assert 'kein Greifziel' in str(exc.value)
    assert ctx.published == []


@pytest.mark.parametrize('profile_id', PROFILE_IDS)
@pytest.mark.parametrize('block,handler,key', [
    ('aufnehmen', motion.pickup, 'target'),
    ('bewege zu', motion.move_to, 'destination'),
    ('ablegen bei', motion.drop_at, 'destination'),
])
def test_a_greifziel_is_refused_by_the_destination_blocks(
        profile_id, block, handler, key):
    """Re-measured 2026-09-08 with the real solvers, tag yaw 1.1 rad, object ON
    each arm's +x axis at its band radius. Split blocks vs pickup(Greifziel),
    quoting the ROLL JOINT so the three rows are one quantity:

        omx_full     correct z 0.0150 q_roll +0.4708  →  z 0.0270 q_roll +1.5708
        edu6_studio  correct z 0.0150 q_roll −0.4708  →  z 0.0270 q_roll +1.5708
        edu1_studio  correct z 0.0150 q_roll −0.4708  →  z 0.0270 q_roll −1.5708

    i.e. +12.0 mm of descend height and — pickup(Greifziel) never seeing the tag
    yaw — a wrist error of exactly that yaw, 1.1 rad = 63.03°, on all three arms
    (raw joint differences 63.03 / 116.97 / 63.03°; edu6's is the same grasp
    through its jaw fold). On a 30 mm cube that grips 3 mm below its top —
    reported as success. See ``motion._refuse_greifziel`` for the two figures
    an earlier revision of both copies carried („roll −2.6708", „56–57°").
    """
    ctx = Ctx(profile_id)
    ziel = greifziel(ctx, band_xyz(ctx), tag_yaw=1.1)
    with pytest.raises(WorkflowError) as exc:
        handler(ctx, {key: ziel})
    # THE CLASS, not just the text. ``GraspSkip`` IS a ``WorkflowError`` and
    # carries the same message, so `raises(WorkflowError)` alone cannot see the
    # difference — mutating ``motion._refuse_greifziel``'s raise to ``GraspSkip``
    # survived the ENTIRE suite (1482 passed / 4 skipped, identical to baseline)
    # while being hit 9 times BY THIS TEST. The class is the behaviour here: a
    # GraspSkip is swallowed by the „Solange sichtbar" loop, so the wrong socket
    # would go on being wrong, silently, once per pass. The sibling test 25 lines
    # above already carries this line; this one is where the mutation lives.
    assert not isinstance(exc.value, GraspSkip), (
        'a wrongly wired Greifziel is a PROGRAM error, not a loop-swallowable '
        'per-instance skip')
    assert 'Greifziel' in str(exc.value)
    assert ctx.published == []


@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_a_position_dict_is_still_accepted_by_bewege_zu(profile_id):
    """„Position von <Ziel>" is the LEGITIMATE way to drive „bewege zu" from a
    found object, and it must keep working."""
    ctx = Ctx(profile_id)
    x, y, z = band_xyz(ctx)
    motion.move_to(ctx, {'destination': {'x': x, 'y': y, 'z': z}})
    assert ctx.published


# ══════════════════════════════════════════════════════════════════════════
# RS-28 — „bewege zu" / „ablegen bei" twisted a HELD object
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', ('omx_full', 'edu6_studio'))
def test_a_carried_object_keeps_its_orientation_across_a_transit(profile_id):
    """Measured 2026-09-07 after a split grasp at tag yaw 0.7 rad:
    „bewege zu" travelled the wrist +40.1° on omx_full and drove the roll JOINT
    −49.9° → +90.0° on edu6_studio, with the object in the jaws the whole way.
    „hebe an" already pinned the current roll; these two did not."""
    ctx = Ctx(profile_id)
    ziel = greifziel(ctx, band_xyz(ctx), tag_yaw=0.7)
    motion.move_above(ctx, {'ziel': ziel})
    motion.descend_to(ctx, {'ziel': ziel})
    motion.close_on_object(ctx, {'ziel': ziel})
    roll_before = motion._roll_arg(ctx, ctx.last_full_joints)
    assert ctx.last_commanded_close_rad is not None
    assert motion._is_carrying(ctx), 'test premise: the jaws are closed on it'

    x, y, _z = band_xyz(ctx)
    ctx.destinations['A'] = {'x': x, 'y': y + 0.02, 'z': 0.0}
    motion.move_to(ctx, {'destination': 'A'})
    roll_after = motion._roll_arg(ctx, ctx.last_full_joints)
    assert roll_after == pytest.approx(roll_before, abs=1e-6)


def test_an_empty_gripper_still_uses_the_fixed_jaw_constant():
    ctx = Ctx('omx_full')
    assert motion._carry_roll(ctx) == pytest.approx(motion.GRASP_ROLL_RAD)


def test_carry_roll_rollback(monkeypatch):
    monkeypatch.setattr(motion, 'CARRY_KEEP_ROLL', False)
    ctx = Ctx('omx_full')
    ctx.park_at((0.20, 0.0, 0.05), roll=0.3, gripper=-0.4)
    ctx.last_commanded_close_rad = -0.4
    assert motion._is_carrying(ctx)
    assert motion._carry_roll(ctx) == pytest.approx(motion.GRASP_ROLL_RAD)


# ══════════════════════════════════════════════════════════════════════════
# RS-29 — „ablegen bei" printed GRASP wording on a PLACE
# ══════════════════════════════════════════════════════════════════════════

def test_a_place_never_prints_grasp_wording():
    """Measured 2026-09-07 over 120 radii per arm across the pick band, counting
    successful places that printed „Anfahrhöhe … über dem Greifpunkt":

        edu6_studio  92 / 120 = 76.7 %   edu1_studio 1 / 120   omx_full 8 / 109

    The text names „Greifpunkt" and „wenn der Greifer das Objekt beim Anfahren
    berührt" — about arriving at an object that is still on the table, for an
    operation where the object is already in the jaws."""
    for pid in PROFILE_IDS:
        ctx = Ctx(pid)
        rec = ctx.object_catalog.recipe_for_type('wuerfel')
        ctx.last_commanded_close_rad = rec.gripper_close_rad
        x, y, _z = band_xyz(ctx)
        ctx.destinations['A'] = {'x': x, 'y': y, 'z': 0.0}
        try:
            motion.drop_at(ctx, {'destination': 'A'})
        except WorkflowError:
            continue
        assert not [m for m in ctx.logs if 'Greifpunkt' in m], (
            f'{pid}: a place printed grasp wording: {ctx.logs}')


def test_a_real_grasp_still_gets_the_grasp_warning():
    """The wording is suppressed for a PLACE only — the grasp path keeps it.

    The assertion used to read ``… or not ctx.logs``, which made it unable to
    fail in the direction its name claims: deleting the warning EMISSION leaves
    ``ctx.logs`` empty and the second clause True. Proved by mutation
    2026-09-08 — disabling the ``purpose == 'grasp'`` guard fails three tests in
    two OTHER files and did NOT fail this one. The tail is gone."""
    ctx = Ctx('edu6_studio')
    # An outer-band grasp whose full 60 mm hover is unreachable.
    grasp = (ctx.ik.base_axis_x + 0.205, 0.0, 0.015)
    if ctx.ik.solve(target_xyz=grasp, roll=motion.GRASP_ROLL_RAD) is None:
        pytest.skip('premise: the grasp point itself must be reachable')
    try:
        motion._solve_grasp_and_approach(ctx, grasp, 0.06,
                                         roll=motion.GRASP_ROLL_RAD)
    except GraspSkip:
        pytest.skip('this radius has no approach clearance at all')
    assert any('Greifpunkt' in m for m in ctx.logs), ctx.logs


# ══════════════════════════════════════════════════════════════════════════
# RS-30 — a projection failure was reported as „Tisch vermessen"
# ══════════════════════════════════════════════════════════════════════════

def test_a_projection_failure_names_the_camera_not_the_touch_off():
    """Measured 2026-09-07 with the calibration COMPLETE (intrinsics, extrinsics
    and board_table_z all present): „sehe ich" → True, „Anzahl" → 1, and
    „finde"/„Greife" → „bitte zuerst „Tisch vermessen" abschließen." — a
    touch-off the student had already done."""
    ctx = Ctx('omx_full')
    ctx.scene_intrinsics = {'K': np.eye(3), 'dist': np.zeros(5)}
    ctx.scene_extrinsics = np.eye(4)
    ctx.board_table_z = 0.0
    d = greifziel(ctx, (0.2, 0.0, 0.015))
    d.world_xyz_m = None
    d.extras['world_error'] = 'projection'
    with pytest.raises(WorkflowError) as exc:
        motion._resolve_target(d, ctx)
    assert 'Kamera-Ausrichtung' in str(exc.value)
    assert 'Tisch vermessen' not in str(exc.value)


def test_a_genuinely_missing_touch_off_still_says_tisch_vermessen():
    ctx = Ctx('omx_full')
    ctx.scene_intrinsics = {'K': np.eye(3), 'dist': np.zeros(5)}
    ctx.scene_extrinsics = np.eye(4)
    ctx.board_table_z = 0.0
    d = greifziel(ctx, (0.2, 0.0, 0.015))
    d.world_xyz_m = None
    with pytest.raises(WorkflowError) as exc:
        motion._resolve_target(d, ctx)
    assert 'Tisch vermessen' in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════
# RS-51 / RS-27 — „warte N Sekunden"
# ══════════════════════════════════════════════════════════════════════════

def test_wait_seconds_treats_nan_like_any_other_malformed_input():
    """Measured 2026-09-07: seconds='nan' → waited 0.000 s, logs=[]. Both guards
    are </> comparisons (False for NaN) and `monotonic() < nan` is False on the
    first test. Reachable because variables_get carries no Blockly output type."""
    ctx = Ctx('omx_full')
    t0 = time.monotonic()
    motion.wait_seconds(ctx, {'seconds': 'nan'})
    waited = time.monotonic() - t0
    assert 0.9 <= waited <= 1.5, f'NaN must fall back to the 1.0 s default, waited {waited}'


def test_wait_seconds_releases_the_motion_lock_while_it_waits():
    """Measured 2026-09-07: a hat running `with motion_lock: warte 2 s` made the
    main stack wait 1.89 s for its next move (3.22 s vs a 0.53 s baseline in a
    second run). Every OTHER wait in the runtime already releases it."""
    ctx = Ctx('omx_full')
    seen = {'free': False}

    def watcher():
        time.sleep(0.05)
        if ctx.motion_lock.acquire(timeout=0.5):
            seen['free'] = True
            ctx.motion_lock.release()

    with ctx.motion_lock:
        t = threading.Thread(target=watcher)
        t.start()
        motion.wait_seconds(ctx, {'seconds': 0.3})
        t.join()
    assert seen['free'], 'the motion lock stayed held for the whole wait'


def test_wait_seconds_from_the_main_stack_is_unchanged():
    """The main stack does not hold the lock; releasing must be a no-op there."""
    ctx = Ctx('omx_full')
    t0 = time.monotonic()
    motion.wait_seconds(ctx, {'seconds': 0.1})
    assert 0.05 <= time.monotonic() - t0 <= 0.5


# ══════════════════════════════════════════════════════════════════════════
# RS-18 — bounded, stop-aware motion-lock acquires
# ══════════════════════════════════════════════════════════════════════════

def test_the_composite_motions_wait_for_the_lock_and_say_so_once():
    """Measured 2026-09-07: three grasping hats plus a main „wiederhole 3 mal"
    ended in phase=error after 12.8 s, 5/5 runs, with a „Bewegung blockiert"
    error naming a RESTART as the remedy. The BOUNDED waiter (the student's main
    program) died while the UNBOUNDED ones won, and the remedy it named
    reproduces the failure identically.

    Both halves are gone. There is no bound — a queued motion is not an error,
    it waits — and the German is ONE [WARNUNG] promising the run carries on."""
    ctx = Ctx('omx_full')
    holder = threading.Event()

    def hold():
        with ctx.motion_lock:
            holder.set()
            time.sleep(0.6)

    t = threading.Thread(target=hold)
    t.start()
    holder.wait(1.0)
    t0 = time.monotonic()
    acquired = motion._hold_motion_lock(ctx, notice_s=0.1)
    waited = time.monotonic() - t0
    motion._release_motion_lock(ctx, acquired)
    t.join()
    assert acquired is True, 'the waiter gave up instead of waiting'
    assert waited > 0.2, 'the holder was not actually holding'
    warn = [m for m in ctx.logs if 'gleichzeitig' in m]
    assert len(warn) == 1, f'exactly one [WARNUNG], got {ctx.logs}'
    assert warn[0].startswith('[WARNUNG]') and 'es geht weiter' in warn[0]


def test_the_wrong_remedy_is_gone_from_every_module_that_takes_the_lock():
    """The eradication, asserted where it can actually be seen.

    The assertion used to be `'neu starten' not in str(exc.value)` on ONE call
    site — and it passed while TWO live copies of „Bitte Workflow neu starten."
    sat in the same package (`perception_blocks._check_grasp_held_locked`,
    `trajectory.replay_trajectory`), under a comment in `motion.py` declaring
    the string fixed. A module-set SOURCE scan is the version that could not
    have passed then, and it is deliberately not an exception-text assertion:
    the point is the copies nobody was calling in that test.

    Note „Programm neu starten" (in `_nothing_to_grasp_message`) is a DIFFERENT,
    correct sentence — restarting really does re-run the program there — which
    is why this scans for the exact wrong remedy and not for „neu starten"."""
    import inspect
    from physical_ai_server.workflow import interpreter as _interp
    from physical_ai_server.workflow import workflow_manager as _wm
    from physical_ai_server.workflow.handlers import perception_blocks as _pb
    from physical_ai_server.workflow.handlers import trajectory as _traj
    offenders = [m.__name__ for m in (motion, _pb, _traj, _interp, _wm)
                 if 'Bitte Workflow neu starten' in inspect.getsource(m)]
    assert offenders == [], (
        f'the remedy that reproduces the failure is live in {offenders}')


def test_waiting_for_the_lock_honours_stop():
    """A Stop pressed while queueing for the lock used to cost 4.04 s."""
    ctx = Ctx('omx_full')
    stop = {'v': False}
    ctx.should_stop = lambda: stop['v']
    holder = threading.Event()

    def hold():
        with ctx.motion_lock:
            holder.set()
            time.sleep(1.0)

    t = threading.Thread(target=hold)
    t.start()
    holder.wait(1.0)
    stop['v'] = True
    t0 = time.monotonic()
    with pytest.raises(WorkflowError) as exc:
        motion._hold_motion_lock(ctx)
    assert 'gestoppt' in str(exc.value)
    assert time.monotonic() - t0 < 0.5, 'Stop must not wait out the lock'
    t.join()


# ══════════════════════════════════════════════════════════════════════════
# RS-37 — a malformed `zones` payload silently disabled the guard
# ══════════════════════════════════════════════════════════════════════════

# (`path_guard.malformed_zone_count` and its test lived here. The function had
# ZERO production callers — a test was its only caller — and its docstring named
# two reporting sites, neither of which called it. A guard with no trigger is not
# a guard, and a TEST is not a caller. The three tests below are what replace its
# coverage, and they test a path a student can reach.)

def test_safe_move_warns_once_when_no_zone_could_be_read():
    """Measured 2026-09-07: zones=[{'min':'x','max':3}] → 0 boxes, run finishes
    green, NOTHING protected, and not one log line."""
    ctx = Ctx('omx_full', zones=[{'min': 'x', 'max': 3}])
    q = list(ctx.last_full_joints)
    motion.safe_move(ctx, q, q, 1.0)
    motion.safe_move(ctx, q, q, 1.0)
    warns = [m for m in ctx.logs if 'Sperrzonen konnten nicht gelesen' in m]
    assert len(warns) == 1, f'exactly one warning per run, got {ctx.logs}'


def test_valid_zones_produce_no_warning():
    ctx = Ctx('omx_full', zones=[{'min': [0.5, 0.5, 0.5], 'max': [0.6, 0.6, 0.6]}])
    q = list(ctx.last_full_joints)
    motion.safe_move(ctx, q, q, 1.0)
    assert not [m for m in ctx.logs if 'nicht gelesen' in m]


# ── E-1 — the same warning must reach the REPLAY path ────────────────────────

def _replay_ctx(profile_id, zones):
    ctx = Ctx(profile_id, zones=zones)
    n = len(ctx.last_full_joints) - 1
    q = list(ctx.last_full_joints)
    ctx.trajectories = {'Bewegung 1': {
        'fps': 25,
        'points': [q[:n] + [q[-1]] + [i * 0.04] for i in range(10)]}}
    return ctx


def test_a_replay_warns_when_no_zone_could_be_read():
    """The guard's own first act was `if not zones or ik is None …: return`, and
    an ALL-MALFORMED payload is TRUTHY — `build_zones` skips every entry
    silently, `segment_blocked` finds zero boxes, and the replay ran with NO
    protection and no word to the student. It is the worse of the two paths:
    „bewege zu" re-solves and reroutes; a replay drives the recorded joint path
    verbatim."""
    ctx = _replay_ctx('omx_full', [{'min': 'x', 'max': 3}])
    traj.replay_trajectory(ctx, {'name': 'Bewegung 1'})
    warns = [m for m in ctx.logs if 'Sperrzonen konnten nicht gelesen' in m]
    assert len(warns) == 1, f'exactly one warning, got {ctx.logs}'
    assert ctx.published, 'the replay must still run — this is a warning'


def test_a_replay_with_readable_zones_warns_nothing():
    ctx = _replay_ctx('omx_full',
                      [{'min': [0.5, 0.5, 0.5], 'max': [0.6, 0.6, 0.6]}])
    traj.replay_trajectory(ctx, {'name': 'Bewegung 1'})
    assert not [m for m in ctx.logs if 'nicht gelesen' in m]


@pytest.mark.parametrize('zones', [None, []])
def test_a_replay_with_NO_zones_warns_nothing(zones):
    """The `if zones:` gate in front of the warning, from the other side.

    Found 2026-09-08 by mutation: replacing that gate with `if True:` — i.e.
    warning unconditionally — SURVIVED the whole suite. `_warn_unreadable_zones`
    asks `build_zones(zones, 0.0)`, which is falsy for ``None`` and ``[]`` just
    as it is for an all-malformed list, so a student who drew NO Sperrzonen was
    told „der Arm fährt OHNE Sperrzonen-Schutz" about zones they never created —
    exactly the false alarm the gate exists to prevent, and the failure mode
    that makes a real warning stop being read. The two truthiness cases are
    parameterised because ``[]`` and ``None`` arrive from different places
    (`WorkflowManager._parse_zones` returns ``None`` for a non-list, the SPA
    sends ``[]`` for a cleared scene)."""
    ctx = _replay_ctx('omx_full', zones)
    traj.replay_trajectory(ctx, {'name': 'Bewegung 1'})
    assert not [m for m in ctx.logs if 'Sperrzonen' in m], ctx.logs
    assert ctx.published, 'a zone-less replay still runs'


def test_a_move_and_a_replay_in_one_run_warn_once():
    """The latch lives on the CTX, not on either function — moving it to
    per-function state would give the student two warnings for one payload."""
    ctx = _replay_ctx('omx_full', [{'min': 'x', 'max': 3}])
    q = list(ctx.last_full_joints)
    motion.safe_move(ctx, q, q, 1.0)
    traj.replay_trajectory(ctx, {'name': 'Bewegung 1'})
    warns = [m for m in ctx.logs if 'Sperrzonen konnten nicht gelesen' in m]
    assert len(warns) == 1, f'exactly one warning per RUN, got {ctx.logs}'


# ══════════════════════════════════════════════════════════════════════════
# RS-14 — the refusal now names what the student can change
# ══════════════════════════════════════════════════════════════════════════

def test_the_zone_refusal_names_the_inflated_width():
    """A 3 cm drawn box is a 13 cm obstacle to the planner. Measured over 20
    blocked, non-static edu6 transits: 2880 base-swing candidates tried, 1280
    SOLVED, 426 with leg 1 clear, 417 with leg 2 clear — and ZERO with both."""
    msg = path_guard._refusal_message(
        [{'min': [0.14, -0.015, 0.0], 'max': [0.17, 0.015, 0.005]}],
        path_guard.ZONE_MARGIN_M, path_guard.LINK_RADIUS_M)
    assert '5 cm' in msg              # the inflation
    assert '13 cm' in msg             # 3 cm drawn + 2 x 5 cm
    assert 'Sperrzone' in msg


def test_the_refusal_degrades_to_the_plain_sentence_with_no_readable_zone():
    assert path_guard._refusal_message(
        [{'min': 'x'}], path_guard.ZONE_MARGIN_M,
        path_guard.LINK_RADIUS_M) == path_guard._REFUSE_MSG


def test_the_inflation_constants_are_not_shrunk():
    """Rule §2: LINK_RADIUS_M + ZONE_MARGIN_M is already ~34 mm UNDER-covered on
    edu6. This audit did not touch it, in either direction."""
    assert path_guard.LINK_RADIUS_M == 0.03
    assert path_guard.ZONE_MARGIN_M == 0.02


# ══════════════════════════════════════════════════════════════════════════
# RS-32 / RS-52 / RS-12 — replay
# ══════════════════════════════════════════════════════════════════════════

def _traj(dt, n=5):
    return {'fps': 25,
            'points': [[0.0] * 5 + [0.0, i * dt] for i in range(n)]}


def test_the_recorded_time_column_is_bounded():
    """Measured 2026-09-07: dt=1 s → 31 waypoints, dt=1000 s → 30 001 (~6 MB),
    and a TWO-POINT payload of 97 BYTES with dt=1e9 implies ~3·10^10 waypoints
    against a 6 GB container mem_limit. The cloud validator caps the point
    count, the width, the JSON size and the fps — never t_s."""
    assert traj.extract_points(_traj(1.0))          # a real recording is fine
    with pytest.raises(WorkflowError) as exc:
        traj.extract_points(_traj(1e9, n=2))
    assert 'zu lang' in str(exc.value)


def test_a_120_second_recording_still_loads():
    """The recorder's own cap is RECORD_MAX_S = 120 s."""
    assert traj.extract_points(_traj(30.0, n=5))    # 120 s span


# --- G8.6: the endpoint span is not the bound ------------------------------
# Every payload below is LITERAL, so these keep their meaning if
# MAX_TRAJECTORY_SPAN_S ever moves. The spike rows are the audit's own rows C
# and D; each was ACCEPTED before 2026-09-08 with a span of 0.1 s.

def test_a_time_spike_is_refused_even_though_its_endpoints_are_close():
    """G8.6, measured 2026-09-07 and re-measured 2026-09-08 on this tree.

    ``extract_points`` bounded ``rows[-1][-1] - rows[0][-1]`` while
    ``resegment_trajectory`` costs ``round(dt * 30)`` waypoints PER CONSECUTIVE
    PAIR, so an internal spike had a tiny span and an enormous per-pair dt:

        row C  [0, 2000, 0.1]   114 B  span 0.1 s  ACCEPTED ->  60 002 waypoints
        row D  [0, 20000, 0.1]  115 B  span 0.1 s  ACCEPTED -> 600 002 waypoints

    scaled to 1e9 that is ~6·10^10 waypoints against the container's 6 GB
    mem_limit — the OOM-kill this cap's own docstring claims to prevent, from a
    payload under 120 bytes. Trigger: /workflow/start over rosbridge
    authenticates nobody and the cloud's validate_trajectory does not cap t_s.
    """
    for spike in (2000.0, 20000.0):
        rows = [[0.0] * 5 + [0.0, 0.0],
                [0.0] * 5 + [0.0, spike],
                [0.0] * 5 + [0.0, 0.1]]
        with pytest.raises(WorkflowError) as exc:
            traj.extract_points({'fps': 25, 'points': rows})
        # The span of this payload is 0.1 s — well under any plausible cap — so
        # only the non-decreasing rule can be refusing it.
        assert rows[-1][-1] - rows[0][-1] == pytest.approx(0.1)
        assert 'Zeitangaben' in str(exc.value)


def test_a_backwards_step_anywhere_in_the_column_is_refused():
    """A single backwards pair is enough: with any dt < 0, sum(dt) is no longer
    the endpoint span and the one cap stops bounding the stream."""
    rows = [[0.0] * 5 + [0.0, 0.0],
            [0.0] * 5 + [0.0, 1.0],
            [0.0] * 5 + [0.0, 0.9],   # <- the only defect in an honest-looking
            [0.0] * 5 + [0.0, 2.0]]   #    4-point, 2-second recording
    with pytest.raises(WorkflowError):
        traj.extract_points({'fps': 25, 'points': rows})


def test_an_ordinary_recording_is_still_accepted():
    """The acceptance half of the pair. LITERAL 25 Hz stamps from a real
    hand-guide sampler (t = time.monotonic() - start, strictly increasing);
    a repeated stamp is legal too — the sampler can emit two frames inside one
    clock tick, and dt = 0 costs 0 waypoints."""
    rows = [[0.0] * 5 + [0.0, 0.00],
            [0.1] * 5 + [0.0, 0.04],
            [0.2] * 5 + [0.0, 0.08],
            [0.3] * 5 + [0.0, 0.08],   # duplicate stamp: non-decreasing, legal
            [0.4] * 5 + [0.0, 0.12]]
    assert traj.extract_points({'fps': 25, 'points': rows}) == rows


def test_the_run_bar_tempo_reaches_replay():
    """Measured 2026-09-07: ctx.tempo 0.5 / 1.0 / 2.0 all produced 69 waypoints
    over the same span — „langsam" slowed every other motion block and not this
    one."""
    counts = {}
    for tempo in (0.5, 1.0, 2.0):
        ctx = Ctx('omx_full', tempo=tempo)
        ctx.trajectories = {'R': _traj(0.5, n=3)}
        traj.replay_trajectory(ctx, {'name': 'R'})
        counts[tempo] = sum(len(c) for c in ctx.published)
    assert counts[0.5] > counts[1.0] > counts[2.0], counts


def test_replay_refuses_to_drive_through_a_sperrzone():
    """Measured 2026-09-07, omx_full, zone {min [0.125, −0.035, 0.038], max
    [0.175, 0.035, 0.058]}: a replay published 75 waypoints of which 11
    consecutive pairs swept the zone, with NO zone log line."""
    ctx = Ctx('omx_full')
    ctx.trajectories = {'R': {'fps': 25, 'points': [
        [0.0, -1.2, 1.2, 0.0, 0.0, 0.0, 0.0],
        [0.0, -0.4, 0.9, 0.0, 0.0, 0.0, 1.0]]}}
    ctx.zones = [{'min': [0.125, -0.035, 0.038], 'max': [0.175, 0.035, 0.058]}]
    with pytest.raises(WorkflowError) as exc:
        traj.replay_trajectory(ctx, {'name': 'R'})
    assert 'Sperrzone' in str(exc.value)
    assert ctx.published == [], 'a refusal must cost zero motion'


def test_replay_without_zones_is_unchanged():
    ctx = Ctx('omx_full')
    ctx.trajectories = {'R': {'fps': 25, 'points': [
        [0.0, -1.2, 1.2, 0.0, 0.0, 0.0, 0.0],
        [0.0, -0.4, 0.9, 0.0, 0.0, 0.0, 1.0]]}}
    traj.replay_trajectory(ctx, {'name': 'R'})
    assert ctx.published


# ══════════════════════════════════════════════════════════════════════════
# RS-33 / RS-34 / RS-54 / RS-56 — output blocks
# ══════════════════════════════════════════════════════════════════════════

class _LogCtx:
    def __init__(self):
        self.msgs: list[str] = []

    def log(self, m):
        self.msgs.append(m)


@pytest.mark.parametrize('value,expected', [
    (True, 'wahr'), (False, 'falsch'), (3, '3'), (3.0, '3'), (2.5, '2.5'),
    (None, ''), ('hallo', 'hallo'),
])
def test_melde_speaks_german_not_python(value, expected):
    """Measured 2026-09-07: melde<wahr> → 'True', melde<3> → '3.0'. The
    interpreter's German-aware _to_text was used ONLY by text_join, which is why
    „verbinde(wahr, 3)" already printed „wahr3" correctly."""
    c = _LogCtx()
    out.log(c, {'message': value})
    assert c.msgs == [expected]


def test_melde_formats_a_position_dict_readably():
    """Measured: melde<Position von Ziel> → "{'x': 0.15, 'y': 0.0, 'z': 0.015}"."""
    c = _LogCtx()
    out.log(c, {'message': {'x': 0.15, 'y': 0.0, 'z': 0.015}})
    assert c.msgs == ['x=0,150 m, y=0,000 m, z=0,015 m']


def test_sage_does_not_read_a_python_repr_aloud():
    """Measured: sage<finde Würfel> → [SPEAK:Detection(centroid_px=(320, 240),
    bbox_px=…, aruco_id=20, …)] — read aloud in de-DE."""
    c = _LogCtx()
    ziel = types.SimpleNamespace(world_xyz_m=(0.15, 0.0, 0.015), aruco_id=20,
                                 extras={})
    out.speak_de(c, {'text': ziel})
    assert c.msgs == ['[SPEAK:Greifziel (Marker 20) bei x=0,150 m, y=0,000 m, z=0,015 m]']
    assert 'Detection(' not in c.msgs[0]


def test_melde_strips_newlines_so_a_fehler_line_cannot_be_forged():
    """destinations.py justifies its own name validator as preventing exactly
    this injection; „melde" was the one output block that emitted newlines
    verbatim."""
    c = _LogCtx()
    out.log(c, {'message': 'a\n[FEHLER] alles kaputt'})
    assert '\n' not in c.msgs[0]
    assert '[FEHLER]' not in c.msgs[0]


def test_the_log_cap_is_the_cap():
    """Measured: MAX_LOG_CHARS is 2000 but the emitted string was 2002."""
    c = _LogCtx()
    out.log(c, {'message': 'x' * 5000})
    assert len(c.msgs[0]) == out.MAX_LOG_CHARS


def test_sage_with_no_text_says_so_instead_of_doing_nothing():
    for empty in ('', '   ', None):
        c = _LogCtx()
        out.speak_de(c, {'text': empty})
        assert c.msgs and 'WARNUNG' in c.msgs[0], (
            f'{empty!r} produced {c.msgs!r}')


@pytest.mark.parametrize('bad', [float('nan'), float('inf')])
def test_play_tone_does_not_clamp_nan_to_the_loudest_longest_tone(bad):
    """Measured: freq=nan and freq=inf both emitted [TONE:4000.0:5.000] — a
    min/max clamp sends NaN to the UPPER limit, the same limit-seeking pattern
    CLAUDE.md flags in the edu6 driver. toast's int(round(nan)) raises and
    correctly falls back."""
    c = _LogCtx()
    out.play_tone(c, {'freq': bad, 'seconds': bad})
    assert c.msgs == ['[TONE:880.0:0.250]']


_OUTPUT_KINDS = [
    (out.log, {'message': 'x'}, 'melde'),
    (out.speak_de, {'text': 'x'}, 'sage'),
    (out.play_tone, {}, 'ton'),
    (out.play_sound, {}, 'klang'),
    (out.toast, {'text': 'x'}, 'meldung'),
]


@pytest.mark.parametrize('fn,args,kind', _OUTPUT_KINDS)
def test_a_straight_chain_of_five_output_blocks_prints_five(fn, args, kind,
                                                            monkeypatch):
    """THE headline of the token bucket, and the first program a twelve-year-old
    writes. A minimum-INTERVAL gate cannot tell a straight chain from a spin,
    because Blockly runs sequential blocks microseconds apart: five „melde"
    blocks printed ONE line, and the German blamed the student for it."""
    monkeypatch.setattr(out, 'OUTPUT_MAX_PER_S', 5.0)
    c = _LogCtx()
    for _ in range(5):
        fn(c, args)
    assert len(c.msgs) == 5, f'{kind}: {len(c.msgs)} of 5 blocks printed'
    assert not any('WARNUNG' in m for m in c.msgs), (
        'a five-block program is not "zu viele"')


@pytest.mark.parametrize('fn,args,kind', _OUTPUT_KINDS)
def test_a_sustained_loop_is_still_throttled(fn, args, kind, monkeypatch):
    """The defect the limiter exists for, unchanged. Measured 2026-09-07 inside
    „wiederhole fortlaufend", 3 s window: play_sound 17.3/s, play_tone 18.0/s,
    toast 17.7/s, speak_de 17.7/s. 200 emissions at 17/s = 11.8 s, so the bucket
    allows its 50-deep burst plus ~59 refills — nowhere near 200."""
    monkeypatch.setattr(out, 'OUTPUT_MAX_PER_S', 5.0)
    monkeypatch.setattr(out, 'OUTPUT_BURST', 50)
    clock = {'t': 0.0}
    monkeypatch.setattr(out, 'time',
                        types.SimpleNamespace(monotonic=lambda: clock['t']))
    c = _LogCtx()
    for _ in range(200):
        fn(c, args)
        clock['t'] += 1.0 / 17.0
    emitted = [m for m in c.msgs if 'WARNUNG' not in m]
    assert len(emitted) <= 110, f'{kind}: {len(emitted)} of 200 got through'
    assert any('WARNUNG' in m for m in c.msgs), 'the drop must be announced'


def test_the_shipped_output_burst_and_rate_are_fifty_and_five():
    """LITERAL pins, read from the SOURCE because both are env-derived /
    monkeypatched everywhere: measured 2026-09-08, ``OUTPUT_MAX_PER_S = 0.0``
    passed the entire suite because both of its references patch it."""
    import re
    from pathlib import Path
    src = Path(out.__file__).read_text(encoding='utf-8')
    assert re.search(r"^OUTPUT_BURST = 50$", src, re.M), (
        'OUTPUT_BURST moved off 50 — 50 free back-to-back emissions per kind is '
        'what makes a straight-line program work')
    assert re.search(
        r"^OUTPUT_MAX_PER_S = max\(0\.0, _env_float\("
        r"'EDUBOTICS_OUTPUT_MAX_PER_S', 5\.0\)\)$", src, re.M), (
        'the shipped sustained rate is 5/s, from EDUBOTICS_OUTPUT_MAX_PER_S')


def test_the_rate_limit_is_per_kind():
    c = _LogCtx()
    out.log(c, {'message': 'a'})
    out.play_sound(c, {})
    assert c.msgs == ['a', '[SOUND]']


def test_output_rate_limit_rollback(monkeypatch):
    monkeypatch.setattr(out, 'OUTPUT_MAX_PER_S', 0.0)
    c = _LogCtx()
    for _ in range(20):
        out.play_sound(c, {})
    assert len(c.msgs) == 20


# ══════════════════════════════════════════════════════════════════════════
# RS-41 / RS-42 — destination names + coordinates
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('name', ['A!', 'A/B', '日本', '😀', 'A\nB', 'A]B', 'x' * 41])
def test_a_bad_destination_name_says_which_name_and_which_alphabet(name):
    """Measured 2026-09-07: every one of these aborted the run with the bare
    „Ungültiger Ziel-Name.", naming neither the offending name nor the allowed
    characters — and the React validator has no character class at all, so they
    all first surface here, at RUN time."""
    ctx = Ctx('omx_full')
    with pytest.raises(WorkflowError) as exc:
        dst.destination_pin(ctx, {'name': name, 'x': 0.1, 'y': 0.0, 'z': 0.0})
    msg = str(exc.value)
    assert 'Ungültiger Ziel-Name' in msg
    assert 'Buchstaben' in msg and 'Bindestrich' in msg
    assert '[' not in msg and ']' not in msg, 'the echo must not carry a sentinel'
    assert '\n' not in msg


def test_a_good_destination_name_is_still_accepted():
    ctx = Ctx('omx_full')
    dst.destination_pin(ctx, {'name': 'A B-c_Ä', 'x': 0.1, 'y': 0.0, 'z': 0.0})
    assert 'A B-c_Ä' in ctx.destinations


@pytest.mark.parametrize('raw', ['NaN', 'Infinity', '1e400', '-Infinity'])
def test_a_non_finite_pin_is_refused_not_reported_as_success(raw):
    """Measured 2026-09-07: ('NaN', 0, 0) → 'Ziel "A" gespeichert (nan, 0.000,
    0.000).'; both infinity spellings → 'inf'. Reachable without a crafted
    payload — Number(NaN).toFixed(3) === "NaN", so a degenerate projection lands
    the literal string in the Blockly field."""
    ctx = Ctx('omx_full')
    with pytest.raises(WorkflowError) as exc:
        dst.destination_pin(ctx, {'name': 'A', 'x': raw, 'y': 0, 'z': 0})
    assert 'gültigen Koordinaten' in str(exc.value)
    assert 'A' not in ctx.destinations


def test_destination_current_also_refuses_a_non_finite_pose():
    ctx = Ctx('omx_full')
    ctx.get_current_pose_xyz = lambda: (float('nan'), 0.0, 0.0)
    with pytest.raises(WorkflowError):
        dst.destination_current(ctx, {'name': 'A'})
    assert 'A' not in ctx.destinations


# ══════════════════════════════════════════════════════════════════════════
# RS-38 / RS-48 — perception messages
# ══════════════════════════════════════════════════════════════════════════

class _Perception:
    def __init__(self, dets=(), count_calls=None):
        self._dets = list(dets)
        self.calls = 0

    def apriltag_available(self):
        return True

    def detect(self, *_a, **_k):
        self.calls += 1
        return list(self._dets)


def _perception_ctx(profile_id='omx_full', dets=()):
    ctx = Ctx(profile_id)
    ctx.perception = _Perception(dets)
    ctx.get_scene_frame = lambda: np.zeros((8, 8, 3), dtype=np.uint8)
    ctx.get_scene_frame_age = lambda: 0.0
    return ctx


def test_warte_bis_sichtbar_returns_false_instead_of_aborting():
    """Both blocks declare output:'Boolean', so Blockly lets a student drop them
    into „falls … sonst" — and a raising timeout made the sonst branch
    UNREACHABLE. Measured: „Timeout: Objekt wuerfel nicht erkannt." (which also
    leaked the raw catalog KEY where label_de is „Würfel")."""
    ctx = _perception_ctx()
    assert pb.wait_until_object_seen(
        ctx, {'object_type': 'wuerfel', 'timeout': 0.3}) is False
    warn = [m for m in ctx.logs if 'WARNUNG' in m]
    assert warn, ctx.logs
    assert 'Würfel' in warn[0]
    assert 'wuerfel' not in warn[0], 'the raw catalog key must not reach a student'


def test_warte_bis_greifer_haelt_returns_false_on_timeout(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ctx = _perception_ctx()
    ctx.last_commanded_close_rad = -0.5
    ctx.gripper_readback = ctx.gripper_open_rad     # a MISS, per RS-20
    assert pb.wait_until_held(ctx, {'timeout': 0.3}) is False
    assert any('WARNUNG' in m for m in ctx.logs)


def test_all_instances_done_is_not_reported_as_nothing_visible():
    """Measured 2026-09-07 with two cubes in plain view, both grasped earlier:
    „Kein „Würfel" sichtbar — bitte das Objekt in den markierten Greifbereich
    legen." with the camera detect call count at ZERO. The objects ARE in the
    Greifbereich, and putting them back would not help."""
    ctx = _perception_ctx()
    ctx.claimed_tags.update({20, 21})
    recipe = ctx.object_catalog.recipe_for_type('wuerfel')
    with pytest.raises(GraspSkip) as exc:
        pb.grasp_object(ctx, {'object_type': 'wuerfel'})
    assert 'schon erledigt' in str(exc.value)
    assert 'in den markierten Greifbereich legen' not in str(exc.value)
    assert pb._all_instances_done(ctx, recipe) is True


def test_a_genuinely_empty_scene_still_says_nothing_visible():
    ctx = _perception_ctx()
    with pytest.raises(GraspSkip) as exc:
        pb.grasp_object(ctx, {'object_type': 'wuerfel'})
    assert 'sichtbar' in str(exc.value)


def test_see_and_count_do_not_mutate_the_claim_state():
    """They are pure VALUE blocks a student can drop anywhere; a read that
    silently un-claims an object changes what the surrounding loop does next."""
    ctx = _perception_ctx()
    ctx.claimed_tags.add(20)
    # This guard used to read `ctx.absent_since`, a field that no longer exists
    # in production after G9 — so it compared {} to {} and had SILENTLY STOPPED
    # TESTING ANYTHING. Re-pointed at the three stores the reclaim mutates today.
    before_anchor = dict(ctx.claim_anchor)
    before_pick = dict(ctx.claim_pick_xy)
    before_unseen = set(ctx.claim_unseen)
    pb.see_object(ctx, {'object_type': 'wuerfel'})
    pb.count_object(ctx, {'object_type': 'wuerfel'})
    assert ctx.claimed_tags == {20}
    assert ctx.claim_anchor == before_anchor
    assert ctx.claim_pick_xy == before_pick
    assert ctx.claim_unseen == before_unseen


# ══════════════════════════════════════════════════════════════════════════
# RS-46 — what the tag-edge gate can and cannot detect
# ══════════════════════════════════════════════════════════════════════════

def test_the_tag_edge_gate_is_a_scale_detector_not_a_plane_detector():
    """Pins the CORRECTED claim. Measured 2026-09-07 against the real
    tag_edge_length_base / project_pixel_to_table (102° HFOV / 640 px, camera
    0.55 m up, pitched 25°, a 24 mm tag):

        plane error   edge deviation   lateral grasp offset   gate trips?
           20 mm          3.6 %              10.2 mm             no
           50 mm          9.1 %              25.5 mm             no
          200 mm         36.4 %             101.9 mm             no
          300 mm         54.5 %             152.8 mm            YES

    i.e. by the time it fires, the grasp point is out by more than the whole
    object. The tolerance is a ~2x wrong-object-SCALE detector.
    """
    tol = pb._TAG_EDGE_TOL_FRAC
    expected = 0.024
    # A plane error one order of magnitude larger than one that already ruins
    # the grasp still does not reach the tolerance…
    assert abs(0.02182 - expected) <= tol * expected      # 50 mm plane error
    assert abs(0.01527 - expected) <= tol * expected      # 200 mm plane error
    # …while a ~2x object-scale error does.
    assert abs(0.048 - expected) > tol * expected


# ══════════════════════════════════════════════════════════════════════════
# RS-47 — the multi-frame yaw burst must not average duplicate frames
# ══════════════════════════════════════════════════════════════════════════

def test_a_repeated_frame_is_not_counted_as_an_independent_sample(monkeypatch):
    """Measured 2026-09-07 with one noisy frame repeated 7×: R = 1.000000 (the
    gate passed at maximum confidence) and the "averaged" yaw was BIT-IDENTICAL
    to that single frame's 13.5° error. get_scene_frame returns the CACHED
    latest frame with no sequence check, at ~34 ms sampling against a 30 fps
    camera."""
    ctx = _perception_ctx()
    ctx.scene_intrinsics = {'K': np.eye(3), 'dist': np.zeros(5)}
    ctx.scene_extrinsics = np.eye(4)
    ctx.board_table_z = 0.0
    corners = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    d = types.SimpleNamespace(aruco_id=20, corners_px=corners, extras={})
    ctx.perception = _Perception([d])
    monkeypatch.setattr(pb, '_TAG_YAW_FRAME_INTERVAL_S', 0.0)
    monkeypatch.setattr(pb, 'tag_yaw_base', None, raising=False)
    monkeypatch.setattr(
        'physical_ai_server.workflow.tag_pose.tag_yaw_base',
        lambda *_a, **_k: 0.3)
    monkeypatch.setattr(
        'physical_ai_server.workflow.tag_pose.tag_edge_length_base',
        lambda *_a, **_k: 0.024)
    recipe = ctx.object_catalog.recipe_for_type('wuerfel')
    yaw = pb._multiframe_tag_yaw(ctx, recipe, 20, 0.3)
    # The single-frame value is still returned — nothing regresses, including
    # the SIMULATOR, whose camera is deterministic by construction…
    assert yaw == pytest.approx(0.3)
    # …but the false confidence is gone: the student is told the averaging did
    # not run instead of the R gate reporting 1.000000.
    assert any('keine neuen Bilder' in m for m in ctx.logs), ctx.logs


# ══════════════════════════════════════════════════════════════════════════
# RS-13 — „Heimposition" lost the Sperrzone reroute and blamed the table
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', ('edu6_studio', 'edu1_studio'))
def test_home_hands_a_zone_blocked_route_to_the_reroute_ladder(profile_id):
    """``_leg_ok`` returned ONE bool for TWO causes and rung L3 raised only the
    FLOOR message, so a zone-blocked home told the student to hand-guide the arm
    up — about a table that was never in the way — and path_guard's ladder was
    unreachable from home on every arm with box geometry. Measured 2026-09-07
    over 25 path-only blocking zones per arm:

        profile        plan_safe_route routed   home REFUSED   both
        omx_full             10 / 25                 0            0
        edu6_studio          14 / 25                20           11
        edu1_studio          19 / 25                14            9
    """
    from physical_ai_server.workflow import home_planner as hp
    ctx = Ctx(profile_id)
    n = ctx.num_arm_joints
    q_home = list(ctx.home_joints_rad) + [ctx.gripper_open_rad]
    q_start = ctx.park_at(band_xyz(ctx, z=0.03)) + [ctx.gripper_open_rad]
    # A zone ON the direct line but not on either endpoint.
    mid = [q_start[i] + 0.5 * (q_home[i] - q_start[i]) for i in range(n)]
    pts = ctx.ik.link_points(np.array(mid), samples_per_link=5)
    hit = None
    for p in pts:
        h = 0.008
        cand = [{'min': [float(p[0]) - h, float(p[1]) - h, float(p[2]) - h],
                 'max': [float(p[0]) + h, float(p[1]) + h, float(p[2]) + h]}]
        if (path_guard.segment_blocked(ctx.ik, q_start, q_home, cand)
                and path_guard._static_overlap_refusal(
                    ctx.ik, q_start, q_home, cand, path_guard.ZONE_MARGIN_M,
                    path_guard.LINK_RADIUS_M) is None):
            hit = cand
            break
    if hit is None:
        pytest.skip('no path-only blocking zone found for this start pose')
    ctx.zones = hit
    try:
        legs = hp.plan_home_route(ctx, q_start, q_home, 3.0)
    except WorkflowError as exc:
        # A refusal is allowed — but it must name the SPERRZONE, never the table.
        assert 'Sperrzone' in str(exc), (
            f'a zone-blocked home must not blame the table: {exc}')
        return
    assert len(legs) >= 1
    # Every returned leg still clears the floor: the guard is not weakened.
    from physical_ai_server.workflow import arm_geometry as ag
    geom = ag.resolve_geometry(ctx)
    floor_fn = hp._floor_fn(ctx)
    for a, b, _d in legs:
        assert hp._floor_ok(geom, a, b, floor_fn, -hp.FLOOR_TOL_M)


def test_the_floor_refusal_still_says_handfuehrung():
    """The table-floor rung's own remedy must be untouched."""
    from physical_ai_server.workflow import home_planner as hp
    assert 'Handführung' in hp._FLOOR_REFUSAL_DE
    assert 'Tischebene' in hp._FLOOR_REFUSAL_DE


# ══════════════════════════════════════════════════════════════════════════
# RS-21 — „hebe an" from HOME on the Feetech arms
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_lift_from_the_grundstellung_never_aborts_a_program(profile_id):
    """Measured 2026-09-07: fk(HOME) is outside the strict-vertical solver's
    image on BOTH Feetech arms, and „hebe an" then raised „Position außerhalb
    des Arbeitsbereichs — bitte das Objekt in den markierten Greifbereich
    legen", blaming an object for the arm's own pose and killing the run. 44 of
    120 block orderings per arm reach it."""
    ctx = Ctx(profile_id)
    motion.lift(ctx, {})            # must not raise on ANY profile
    if profile_id != 'omx_full':
        assert ctx.published == []
        assert any('hebe an' in m for m in ctx.logs), ctx.logs
        assert not any('Greifbereich' in m for m in ctx.logs)


# ══════════════════════════════════════════════════════════════════════════
# RS-56 — the React placeholder sentinel must not reach the student
# ══════════════════════════════════════════════════════════════════════════

def test_the_object_type_placeholder_is_named_not_echoed():
    """When GetObjectCatalog has not answered, `_objectTypePlaceholder` and
    `_objectTypeEmpty` in blocks/perception.js both serialise '__none__' into
    the SAVED workflow, and all four object blocks then raised „Unbekanntes
    Objekt „__none__"" — an internal token the student cannot act on."""
    cat = fixed_catalog('omx_full')
    with pytest.raises(WorkflowError) as exc:
        pb._recipe_for(cat, '__none__')
    assert '__none__' not in str(exc.value)
    assert 'Objektliste' in str(exc.value)
    # A genuinely unknown type still gets the catalog error, naming the type.
    with pytest.raises(WorkflowError) as exc:
        pb._recipe_for(cat, 'banane')
    assert 'banane' in str(exc.value)
