#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ArmProfile registry — the server-side source of truth for a robot type.

``EDUBOTICS_ROBOT_TYPE`` (a MANAGED ``.env`` key hardset by the GUI at
"Umgebung starten") names a *profile id*. This module resolves that id to an
immutable :class:`ArmProfile` carrying:

* the server data ``robot_type`` (``omx_f`` for BOTH OMX profiles — the dataset
  repo id is ``{user}/{robot_type}_{task}``, so this MUST stay constant or every
  new dataset repo name shifts);
* the initial ``follower_only`` flag (the GUI derives ``EDUBOTICS_FOLLOWER_ONLY``
  from it; on ``omx_full`` it stays runtime-toggleable via the LeaderToggle);
* a capability manifest (which React tabs / hardware actions are available),
  serialized to ``TaskStatus.capabilities_json`` for the React tab filter;
* per-robot HOME / gripper / joint-name / URDF geometry — this round OMX values,
  byte-identical to the still-authoritative constants in
  ``workflow/handlers/motion.py`` + ``workflow/sim_arm.py`` + the node's
  ``_SIM_JOINT_NAMES`` (a no-drift test locks the mirror). Most of this is
  UNCONSUMED seam data this round (``safe_home_arm_rad``, ``urdf_asset_id``,
  ``num_arm_joints``, ``joint_names``) — carried so a genuinely new arm can
  supply its own without a DOF-agnostic refactor;
* an IK factory (:meth:`ArmProfile.build_ik`) — the ONE geometry seam actually
  consumed this round (via ``physical_ai_server._build_ik_solver``). A new arm
  plugs its solver in here.

The two OMX profiles share every geometry value and differ ONLY in
``profile_id`` / ``display_name_de`` / ``follower_only`` / ``capabilities``.

English code / comments throughout; German only in the user-facing
``[WARNUNG]`` log string.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Optional


_logger = logging.getLogger(__name__)


# OMX-F geometry. These MIRROR the authoritative constants — the no-drift test
# (test_robot_profiles.py) locks them against workflow/handlers/motion.py
# (HOME_JOINTS_RAD, GRIPPER_OPEN_RAD, GRIPPER_CLOSED_RAD), workflow/sim_arm.py
# (_SIM_HOME_FULL_JOINTS) and the node's _SIM_JOINT_NAMES. Values are
# byte-identical; NEVER change a number here without changing the authoritative
# module in the SAME commit (a HOME/gripper change is ask-first, Rule §2).
_OMX_HOME_JOINTS_RAD = (0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0)
# SEPARATE, more-folded safe-home pose (±pi/4). UNCONSUMED seam data this round:
# collision_monitor.SAFE_HOME_ARM, jetson_agent.SAFE_HOME_JOINTS and
# entrypoint_omx.sh keep their OWN hardcoded copies (the ±pi/2 vs ±pi/4
# divergence is preserved byte-identical, not reconciled — a recovery-pose
# change is ask-first). Carried so a future arm can supply its own.
_OMX_SAFE_HOME_ARM_RAD = (0.0, -0.785398, 0.785398, 0.0, 0.0)
_OMX_GRIPPER_OPEN_RAD = 0.8
_OMX_GRIPPER_CLOSED_RAD = -0.5
# Full sim / follower joint vector (5 arm joints + gripper), matches the node's
# _SIM_JOINT_NAMES and Communicator.FOLLOWER_JOINT_ORDER.
_OMX_JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                    'gripper_joint_1')

# v1_studio geometry (the V1 CAD export with Base_part.2 as the base; shipped as
# physical_ai_manager/public/v1-urdf/v1.urdf). HOME stands the arm UP over its
# own base with the claw pointing up, chosen by measurement rather than by eye:
# it keeps every joint at least 0.640 rad from its own limit (the binding joint
# is joint2), the lowest moving-link box 43.7 mm above the table, the box model's
# self-clearance at 22.0 mm and the whole arm inside a 146.9 mm plan radius at
# every claw opening, so it does not stand in the scene camera's view. Fingertip
# TCP at world (+0.0125, 0, +0.554). The pose that maximises the joint margins
# under the same constraints refuses the same share of boot glides from a limp
# collapse, so HOME is not a different pose.
#
# CONSEQUENCE worth knowing: the tool at HOME points 9.7 degrees off straight
# UP, so HOME is OUTSIDE the solver's image (solve() only emits strict-vertical
# poses) and no Cartesian machinery — reach check, workspace floor, reroute
# vias — can reason about it. That is why the table-floor guard on the way to it
# is joint-space (workflow/arm_geometry.py + workflow/home_planner.py).
#
# The gripper channel is the claw servo angle in RADIANS, 0 = jaws closed …
# 0.90 = open, so open > closed numerically, which every shared code path
# assumes — motion.check_grasp_held reads "held" as an achieved angle ABOVE the
# commanded close, object_catalog validates the close inside a [closed, open)
# band, and SimArm classifies a command BELOW its close threshold as a close.
#
# Joint names are URDF-native so /joint_states, the sim publisher and the web
# twin agree on one name set. ``RL_joint`` is the CAD's own name for the driven
# claw finger (the other rides an <mimic>) — it is "Right-finger L joint", not
# reinforcement learning.
_V1_HOME_JOINTS_RAD = (0.0, 0.64, 1.48, 0.90, 0.0)
_V1_JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                   'RL_joint')


@dataclass(frozen=True)
class Capabilities:
    """Which student-facing surfaces a robot type exposes. Serialized to
    ``TaskStatus.capabilities_json`` (see :func:`capabilities_json`) and consumed
    by the React capability tab-filter (``omx_full`` = all True hides nothing;
    ``omx_follower`` hides Aufnahme/Daten/Training)."""

    recordable: bool
    editable: bool
    trainable: bool
    inferable: bool
    roboter_studio: bool
    has_leader: bool


@dataclass(frozen=True)
class ArmProfile:
    profile_id: str            # EDUBOTICS_ROBOT_TYPE value
    display_name_de: str
    data_robot_type: str       # server config NAMESPACE key + dataset naming
    follower_only: bool        # INITIAL EDUBOTICS_FOLLOWER_ONLY value
    capabilities: Capabilities
    home_joints_rad: tuple     # OMX: (0, -pi/2, pi/2, 0, 0)
    safe_home_arm_rad: tuple   # OMX: (0, -0.785398, 0.785398, 0, 0) — SEPARATE pose
    gripper_open_rad: float = 0.8
    gripper_closed_rad: float = -0.5
    num_arm_joints: int = 5
    joint_names: tuple = _OMX_JOINT_NAMES
    urdf_asset_id: str = 'omx_f'
    # One-sentence German explanation of THIS robot, shown on the student
    # Start page under the profile name. Kept verbatim in lockstep with the
    # `help_de` strings in gui/app/constants.py::ROBOT_PROFILES and the Pi
    # twin — those two are the ones a student meets during setup, this is
    # the same sentence once the stack is running. Empty string means "no
    # sentence", which the React side renders as nothing rather than a gap.
    help_de: str = ''
    # ── DOF-generalisation seams (consumed by the §16.4 slices) ─────────────
    # None → the OMX module-constant fallbacks in the handlers' ctx accessors.
    ik_backend: str = 'omx'            # build_ik dispatch ('omx' | 'v1')
    roll_joint_index: Optional[int] = None   # None → last arm joint (OMX: 4)
    velocity_limit_rad_s: Optional[float] = None  # None → 4.8 (OMX URDF)
    collision_enabled: bool = True     # False skips the teleop e-stop wiring
    #                                    (safe ONLY for a profile that never
    #                                    publishes /collision_flag — both OMX
    #                                    profiles stay True FOREVER, §8)
    tool_length_m: Optional[float] = None    # wrist→TCP (doc/tests; IK bakes it)
    torque_service: str = '/dynamixel_hardware_interface/set_dxl_torque'
    camera_roles: tuple = ('gripper', 'scene')   # GUI camera auto-assign order
    reach_inner_m: Optional[float] = None    # React annulus (None → simConstants)
    reach_outer_m: Optional[float] = None
    # Bearing sector (degrees, base frame) the arm can reach, or None for a full
    # ring. Exported as the manifest key ``reach_sector_deg`` (a list).
    reach_sector_deg: Optional[tuple] = None
    gripper_mm_per_rad: Optional[float] = None   # jaw-opening display factor
    # True when the TCP's HEIGHT below the tool frame depends on the gripper
    # command — a ROTATING claw, where the fingertip swings back as the jaws
    # open. The TCP is defined at the CLOSED tip, so „Tisch vermessen" measures
    # the table too low unless the student closes the claw first, and that is a
    # student-facing INSTRUCTION, not something any guard can enforce. False on
    # every parallel-jaw arm (OMX), where the tip height is constant.
    tool_tip_tracks_gripper: bool = False
    # The HELD/MISS threshold used when NO gripper close has been commanded yet
    # this run is DERIVED from this margin — ``motion._grasp_held_max`` computes
    # ``gripper_closed_rad + grasp_held_margin_rad``, which is exactly −0.35 on
    # the OMX (its historical module constant, byte-identical) and 0.10 on the
    # V1. There is deliberately no `grasp_held_max_rad` field stating the number
    # outright: ``WorkflowContext`` has no such field, so it would have no
    # production reader. Do not add one without ALSO stamping it onto the
    # context.
    grasp_held_margin_rad: Optional[float] = None   # None → motion 0.15
    # Gripper close angle for the OBJECT-AGNOSTIC „aufnehmen" block, which has
    # no catalog entry to consult. ``None`` → ``gripper_closed_rad``, the
    # arm's HARDWARE-closed angle — right on the OMX (where it IS the shipped
    # catalog's close for the 30 mm cube) and a sustained stall on the V1, whose
    # closed value is ~0.2 rad deeper than the same cube blocks the claw at.
    # Rule §2; see motion.PICKUP_CLOSE_RAD for the
    # measurement, the disclosed trade-off and the one-variable rollback.
    pickup_close_rad: Optional[float] = None
    # Observation pose the „Solange sichtbar" loop retreats to between passes.
    # WorkflowContext has ALWAYS stamped this onto ctx via getattr(profile,
    # 'observe_pose_joints', None) — but the field did not exist, so the stamp was
    # a dead getattr and motion._observe_joints always fell through to HOME.
    # Declared here so the seam is real; every profile deliberately leaves it None,
    # which is byte-identical to the previous behaviour.
    observe_pose_joints: Optional[tuple] = None
    # Sim-arm grasp classifier values (None → sim_arm OMX module constants):
    sim_close_threshold_rad: Optional[float] = None
    sim_held_block_offset_rad: Optional[float] = None
    sim_held_floor_rad: Optional[float] = None
    # ── no-go-zone reroute-ladder geometry (None → path_guard OMX constants) ──
    # These are ARM-SIZED, not universal: every one is a HEIGHT or a RADIUS in
    # metres, and the shipped values were chosen for the OMX's ~0.25 m vertical
    # envelope. An arm whose strict-vertical TCP ceiling sits below those
    # heights would find both reroute rungs structurally dead and send every
    # blocked transit to the German refusal. Sizing them per profile keeps every
    # arm's ladder alive and leaves OMX bit-identical (None → the module
    # constants).
    safe_travel_z_m: Optional[float] = None
    tool_clear_m: Optional[float] = None
    swing_heights_m: Optional[tuple] = None
    swing_radii_m: Optional[tuple] = None

    def build_ik(self, urdf_string: Optional[str] = None):
        """Return the arm's IK solver — the ``ik_factory`` seam.

        Dispatches on ``ik_backend``: OMX uses the closed-form analytical
        :class:`IKSolver`; the V1 the closed-form :class:`V1IKSolver`. Imported
        LAZILY so this module stays importable in the deps-free unit-test stubs
        (both solvers pull in NumPy). ``urdf_string`` is forwarded so the OMX
        solver can verify its baked constants against a locally-available
        ``robot_description`` (never blocks on a cross-node fetch)."""
        if self.ik_backend == 'v1':
            from physical_ai_server.workflow.v1_ik import V1IKSolver
            return V1IKSolver(urdf_string=urdf_string)
        from physical_ai_server.workflow.ik_solver import IKSolver
        return IKSolver(urdf_string=urdf_string)


_OMX_FULL = ArmProfile(
    profile_id='omx_full',
    display_name_de='OMX – Voll',
    help_de=(
        'Beide Arme: mit dem Leader-Arm führst du, der Follower-Arm '
        'fährt nach. Aufnahme, Training, Inferenz und Roboter Studio. '
        'Im Roboter Studio schaltest du den Leader-Arm bei Bedarf ab '
        'und wieder zu.'
    ),
    data_robot_type='omx_f',
    follower_only=False,
    capabilities=Capabilities(
        recordable=True,
        editable=True,
        trainable=True,
        inferable=True,
        roboter_studio=True,
        has_leader=True,
    ),
    home_joints_rad=_OMX_HOME_JOINTS_RAD,
    safe_home_arm_rad=_OMX_SAFE_HOME_ARM_RAD,
    gripper_open_rad=_OMX_GRIPPER_OPEN_RAD,
    gripper_closed_rad=_OMX_GRIPPER_CLOSED_RAD,
    num_arm_joints=5,
    joint_names=_OMX_JOINT_NAMES,
    # The reach shape the editor draws (manifest keys, N6/N11): the inner
    # radius is the editor's historic 0.10 (never widened from IK alone,
    # principle 6); the outer 0.25 m is the MINIMUM over bearings at the rig's
    # grasp height (EE z 0.055 with z_table 0.04: 0.2595 m at 0°, 0.2705 m at
    # 90°), rounded down — the drawn 0.28 promised a forward reach the arm does
    # not have. The simulator's table equals the rig's since Q13, so the same
    # number holds there.
    reach_inner_m=0.10,
    reach_outer_m=0.25,
    urdf_asset_id='omx_f',
)

_OMX_FOLLOWER = ArmProfile(
    profile_id='omx_follower',
    display_name_de='OMX – Roboter Studio (nur Follower)',
    help_de=(
        'Nur der Follower-Arm — kein Leader-Arm nötig. Für Roboter '
        'Studio (Greifen & Programmieren) und Inferenz.'
    ),
    data_robot_type='omx_f',
    follower_only=True,
    capabilities=Capabilities(
        recordable=False,
        editable=False,
        trainable=False,
        inferable=True,
        roboter_studio=True,
        has_leader=False,
    ),
    home_joints_rad=_OMX_HOME_JOINTS_RAD,
    safe_home_arm_rad=_OMX_SAFE_HOME_ARM_RAD,
    gripper_open_rad=_OMX_GRIPPER_OPEN_RAD,
    gripper_closed_rad=_OMX_GRIPPER_CLOSED_RAD,
    num_arm_joints=5,
    joint_names=_OMX_JOINT_NAMES,
    # The reach shape the editor draws (manifest keys, N6/N11): the inner
    # radius is the editor's historic 0.10 (never widened from IK alone,
    # principle 6); the outer 0.25 m is the MINIMUM over bearings at the rig's
    # grasp height (EE z 0.055 with z_table 0.04: 0.2595 m at 0°, 0.2705 m at
    # 90°), rounded down — the drawn 0.28 promised a forward reach the arm does
    # not have. The simulator's table equals the rig's since Q13, so the same
    # number holds there.
    reach_inner_m=0.10,
    reach_outer_m=0.25,
    urdf_asset_id='omx_f',
    # A follower-only Roboter-Studio kit's LONE camera is the SCENE camera, not
    # the gripper cam (perception + the config topics hang off the role name —
    # 'gripper' first broke every RS kit's single-camera auto-assign). 'gripper'
    # stays second for a 2-camera follower rig. omx_full keeps ('gripper','scene').
    camera_roles=('scene', 'gripper'),
)

# v1_studio — the 5-DOF Feetech follower-only Roboter-Studio arm („V1").
# MUST stay a literal-kwarg call: the GUI↔server lockstep test parses this with
# ast and requires literal profile_id= / follower_only= /
# capabilities=Capabilities(..., has_leader=<literal>).
_V1_STUDIO = ArmProfile(
    profile_id='v1_studio',
    display_name_de='V1 – Roboter Studio',
    help_de=(
        'Der 5-Achs-Arm V1 von EduBotics mit einer Szenen-Kamera. '
        'Nur für Roboter Studio (Greifen & Programmieren).'
    ),
    # NEW namespace literal, never 'omx_f': init_ros_params reads the
    # config-YAML top-level key equal to data_robot_type. It is ALSO the id the
    # cloud stamps on a saved „Bewegung" (workflow_trajectories.robot_profile) —
    # and on this arm that tag does real work: the V1 has 5 arm joints, so its
    # Contract-B point width is 7, the SAME width omx_f uses. Width alone
    # therefore cannot tell a V1 recording from an OMX one; only this id can.
    data_robot_type='v1_studio',
    follower_only=True,
    capabilities=Capabilities(
        recordable=False,
        editable=False,
        trainable=False,
        inferable=False,
        roboter_studio=True,
        has_leader=False,
    ),
    home_joints_rad=_V1_HOME_JOINTS_RAD,
    # No separate collision safe-home: the teleop e-stop is disabled by
    # construction on this arm (collision_enabled=False below), so the field
    # mirrors HOME as unconsumed seam data.
    safe_home_arm_rad=_V1_HOME_JOINTS_RAD,
    # Claw servo band. 0.90 rad of opening puts the blade tips 23–26 mm above
    # the closed tip, past the 30 mm shipped cube's top at grasp height, while
    # keeping the open→close swing short; the physical stop is 1.5708.
    gripper_open_rad=0.90,
    gripper_closed_rad=0.0,
    num_arm_joints=5,
    joint_names=_V1_JOINT_NAMES,
    urdf_asset_id='v1',
    ik_backend='v1',
    # joint5 is the tool roll and the LAST arm joint, so this equals the
    # ``None`` default. Stated anyway: on this arm the roll joint is also the
    # jaw-fold joint, and a reader checking "which index does the fold touch"
    # should not have to re-derive it from num_arm_joints.
    roll_joint_index=4,
    # The SLOWEST joint's URDF limit, not the fastest: joint1/4/5 are STS3215 at
    # 4.72 rad/s while joint2/3 are STS3250 at 7.87, and the velocity floor must
    # hold for every joint on the segment.
    velocity_limit_rad_s=4.72,
    collision_enabled=False,
    tool_length_m=0.1086,
    torque_service='/v1/set_torque',
    camera_roles=('scene',),
    # Student-facing table ring. The OUTER is the intersection of the grasp
    # plane (z = 0.015: r ∈ [0.0555, 0.3665]) and the hover the catalog asks for
    # (z = 0.075: r ∈ [0.0770, 0.3415]), rounded INWARD so the drawn ring never
    # promises a placement the solver then refuses.
    #
    # The INNER is drawn at 0.10 although the solver accepts r ≥ 0.0555 (owner
    # decision): with the claw open at 0.90 the blades reach over the base tower
    # below r ≈ 0.10 (≈7 mm clearance at 0.10, contact at 0.09). The ring is
    # drawing only — nothing refuses a target inside it (rig gate VR15).
    reach_inner_m=0.10,
    reach_outer_m=0.34,
    # Joint 1 turns ±90° only: the arm cannot reach behind itself, so the editor
    # draws a sector, not a ring (N6), and snaps a tap behind into it.
    reach_sector_deg=(-90, 90),
    # gripper_mm_per_rad is deliberately OMITTED (→ the jog row keeps its degree
    # display). This is a ROTATING claw: the jaw gap is affine in the servo
    # angle, so a pure mm/rad factor would print a number that is wrong by the
    # offset at every opening. A degree read-out is honest; a fabricated
    # millimetre is not.
    # The claw ROTATES: its tip sits 108.6 mm below the tool frame closed but
    # higher when open. The touch-off must therefore be taught with the claw
    # CLOSED (rig gate VR8) — this flag is what puts that sentence in front of
    # the student, and only on the arm it is true for.
    tool_tip_tracks_gripper=True,
    # 0.00 (closed) + 0.10 = 0.10 derived, inside this arm's 0.00…0.90 band, so
    # an empty close (which reaches ≈0.05) reads as a MISS instead of as HELD.
    grasp_held_margin_rad=0.10,
    # pickup_close_rad is deliberately UNSET (→ gripper_closed_rad = the full
    # mechanical close). OWNER DECISION 2026-09-08, taken with the trade-off in
    # front of them: the generic „aufnehmen" block is object-agnostic and has no
    # catalog to consult, so a bounded close would silently stop gripping
    # anything thinner than the jaw gap at that angle. The cost: on a 30 mm cube
    # (blocking the claw at ≈0.20 rad) the unset 0.00 holds a ≈0.20 rad position
    # error through the close, the lift, the carry and the descend. „Greife"
    # and the split blocks are unaffected: they command the recipe's own close.
    # Opt in with EDUBOTICS_PICKUP_CLOSE_RAD=0.05 (the catalog close), never 0.
    # Rig gate: neither the stall nor the bounded grip is hardware-measured.
    # Sim grasp classifier. A close command is anything below 0.5, which sits
    # between the catalog close (0.05) and open (0.90); a 30 mm cube blocks the
    # real claw at 0.193–0.220 rad, so commanded 0.05 + 0.15 = 0.20 reproduces
    # that.
    sim_close_threshold_rad=0.5,
    sim_held_block_offset_rad=0.15,
    # INERT BY CONSTRUCTION, and kept anyway — deliberately, with the proof,
    # rather than left looking like a tuned rig value. ``sim_arm.get_joints``
    # reports a blocked jaw as
    #     max(held_floor, commanded + held_block_offset)
    # and only a command below ``sim_close_threshold_rad`` (0.5) counts as a
    # close, so every value that can reach the max() lies in [0.0, 0.5) →
    # commanded + 0.15 ∈ [0.15, 0.65) → always above 0.05. Dropping the override
    # would inherit the OMX −0.1: numerically just as inert, but a NEGATIVE
    # number on a gripper that never goes negative, i.e. actively misleading.
    sim_held_floor_rad=0.05,
    # Reroute-ladder geometry, MEASURED through the real solver. This arm's
    # strict-vertical TCP ceiling is ~0.146 m (joint4's LOWER limit binds
    # through q4 = q2 − q3 − π/2). Every one of the nine (height, radius) pairs
    # below is inside the annulus at its own height, which is what stops a row
    # of the grid being dead, and the cruise ring at z 0.075 is r ∈ [0.077,
    # 0.3415].
    #
    # 72/144 base-swing candidates solve, and that ceiling is joint1's ±90°
    # base yaw, not the height/radius choice: only 8 of the 16
    # ``path_guard._SWING_AZIMUTHS`` are in FRONT of the arm, 8 × 9 = 72.
    safe_travel_z_m=0.075,
    # 0.0 is exact, not a shortcut: ``v1_ik.link_points`` ends AT the
    # fingertip TCP and appends nothing below it, unlike the OMX solver, which
    # projects two extra samples 0.04 m past its EE along the tool axis — which
    # is what the 0.05 m module default models.
    tool_clear_m=0.0,
    # Mid / high / low and mid / far / near, the same ordering intent as the
    # OMX grid.
    swing_heights_m=(0.05, 0.07, 0.03),
    swing_radii_m=(0.18, 0.28, 0.14),
)

# Registry keyed by profile id. Keep ids + follower_only in lockstep with the
# GUI thin descriptor (gui/app/constants.py::ROBOT_PROFILES) — cross-boundary
# contract, tested each side.
ROBOT_PROFILES: dict = {
    _OMX_FULL.profile_id: _OMX_FULL,
    _OMX_FOLLOWER.profile_id: _OMX_FOLLOWER,
    _V1_STUDIO.profile_id: _V1_STUDIO,
}

DEFAULT_PROFILE_ID = 'omx_full'


def resolve(profile_id: Optional[str]) -> ArmProfile:
    """Resolve an ``EDUBOTICS_ROBOT_TYPE`` value to an :class:`ArmProfile`.

    Unknown / empty / ``None`` falls back to :data:`DEFAULT_PROFILE_ID`
    (``omx_full`` = both-arms, today's behavior) with a German-safe
    ``[WARNUNG]`` log. A bad env value must NEVER crash the boot (the caller
    runs this at ``__init__`` — a raise there = a respawn crash-loop)."""
    key = (profile_id or '').strip()
    profile = ROBOT_PROFILES.get(key)
    if profile is None:
        _logger.warning(
            '[WARNUNG] Unbekannter Robotertyp %r — Standardprofil "%s" wird '
            'verwendet.', profile_id, DEFAULT_PROFILE_ID,
        )
        return ROBOT_PROFILES[DEFAULT_PROFILE_ID]
    return profile


def capabilities_json(profile: ArmProfile) -> str:
    """Compact JSON capability manifest of a profile.

    The six booleans (``recordable``, ``editable``, ``trainable``,
    ``inferable``, ``roboter_studio``, ``has_leader``) are the ORIGINAL
    cross-agent contract — the React adopt-guard requires ALL SIX or the
    manifest is ignored, and the tab filter reads only them. The additional
    GEOMETRY keys (additive) feed the profile-driven React surfaces
    (jog rows, sim reach annulus, URDF twin asset, gripper display); extras
    are tolerated by the validator on both old and new clients, and the
    ``None``-valued optionals are OMITTED rather than sent as null."""
    caps = profile.capabilities
    manifest: dict = {
        'recordable': caps.recordable,
        'editable': caps.editable,
        'trainable': caps.trainable,
        'inferable': caps.inferable,
        'roboter_studio': caps.roboter_studio,
        'has_leader': caps.has_leader,
        'arm_joints': profile.num_arm_joints,
        'joint_names': list(profile.joint_names),
        'urdf_asset_id': profile.urdf_asset_id,
        'gripper_open_rad': profile.gripper_open_rad,
        'gripper_closed_rad': profile.gripper_closed_rad,
        # IDENTITY keys (additive). React had no German name for a robot and
        # rendered the raw profile id — a student read 'omx_full'. The GUI and
        # the Pi agent have carried `display_de`/`help_de` all along; putting
        # them on the manifest that already rides /task/status is what reaches
        # the running app without a FOURTH copy of the profile registry in
        # JS. Safe on an OLD client by the validator's own contract: it checks
        # only that the six booleans are present and boolean, and tolerates
        # extras by design.
        'display_de': profile.display_name_de,
        # The camera ROLES this profile actually uses, so the student surface
        # can say "2 von 2 Kameras" instead of counting topics blind. Same
        # allowlist the GUI/Pi wizards enforce at setup time.
        'camera_roles': list(profile.camera_roles),
        # The text-program languages this image can run (Roboter Studio coding
        # suite). Additive — the six-boolean adopt-guard tolerates it — and a
        # list, not a boolean, so React's codeRunBlockReason keys on membership,
        # fail-closed on absence. Every image that ships the code_runner can run
        # both, so this is a static image capability (a run start still refuses
        # in German when the runner container is down). Kept a plain literal
        # rather than imported from workflow.code_program (which pulls the whole
        # RPC/handler stack) — code_program.CODE_LANGUAGES is the twin.
        'code_languages': ['python', 'java'],
        # Cloud-Inferenz (additive, contract C5): this robot can run a trained model on the
        # cloud GPU. Equal to `inferable` — a cloud run IS an inference run on the same rail;
        # the six-boolean adopt-guard tolerates the extra key on an old client.
        'cloud_inference': caps.inferable,
    }
    for key, value in (
        ('reach_inner_m', profile.reach_inner_m),
        ('reach_outer_m', profile.reach_outer_m),
        ('reach_sector_deg', (list(profile.reach_sector_deg)
                              if profile.reach_sector_deg is not None else None)),
        ('gripper_mm_per_rad', profile.gripper_mm_per_rad),
        ('sim_close_threshold_rad', profile.sim_close_threshold_rad),
    ):
        if value is not None:
            manifest[key] = value
    # Sent ONLY when true, so the manifest of every parallel-jaw arm is
    # byte-identical to before and the React reader's `=== true` test needs no
    # fallback of its own.
    if profile.tool_tip_tracks_gripper:
        manifest['tool_tip_tracks_gripper'] = True
    # Omitted rather than sent as '' — same rule as the None-valued optionals
    # above, so a profile without a sentence costs the wire nothing and the
    # React side renders no empty paragraph.
    if profile.help_de:
        manifest['help_de'] = profile.help_de
    return json.dumps(manifest, separators=(',', ':'))
