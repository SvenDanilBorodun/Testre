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

# edu6_studio geometry (follower_arm_modified_final1.urdf; derivation record in
# docs/plans/edu6-studio-arm.md). HOME stands the arm UP over its own base:
# re-measured 2026-07-27, the fingertip TCP sits at world (+0.0499, 0, +0.4545),
# i.e. 28.6 mm from the joint-1 axis but 0.4545 m TALL — 91.8 % of the maximum
# straight-up reach, with the 2R chain 98.4 % extended. It is compact in PLAN,
# not in ELEVATION; the older "compact over-base fold" wording invited the wrong
# mental picture, and "tip within 39 mm" / "min mesh gap 32.3 mm" measure 28.6 mm
# and 31.4 mm against the shipped meshes. Lowest link point +10.1 mm over the
# table; θ5 = +0.7 is a non-degenerate seed in the working (relieved) branch.
# CONSEQUENCE worth knowing: HOME is OUTSIDE the solver's image (solve() only
# emits strict-vertical poses and HOME would need q5 = 3.2708 rad), so no
# Cartesian machinery — reach check, workspace floor, reroute vias — can reason
# about it. That is why the table-floor guard on the way to it is joint-space
# (workflow/arm_geometry.py + workflow/home_planner.py). The
# gripper channel is the end_gear servo angle in RADIANS: 0 = jaws closed …
# 1.75 = open command (physical stop ≈ 1.7857; jaw ≈ 25.2 mm/rad). Joint names
# are URDF-native so /joint_states, the sim publisher and the web twin (whose
# <mimic> fingers key off end_gear_joint) agree on one name set.
_EDU6_HOME_JOINTS_RAD = (0.0, 0.70, -2.40, 0.0, 0.70, 0.0)
_EDU6_JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                     'joint6', 'end_gear_joint')

# edu1_studio geometry (5dof_assembly_urdf2.urdf; derivation record in
# docs/plans/edu1-studio-arm.md). HOME stands the arm UP over its own base with
# the claw pointing up — the same intent as the edu6 HOME and chosen the same
# way, by measurement rather than by eye. Searched over the whole joint box for
# the pose that maximises the distance of EVERY joint from its own limit subject
# to |TCP radius| <= 20 mm, TCP height in [0.38, 0.52] m, table clearance
# >= 40 mm and self-clearance >= 20 mm; the winner sits 0.640 rad clear of the
# nearest limit (vs 0.049 rad for the best tool-down candidate), keeps the whole
# arm inside a 148 mm plan radius so it does not stand in the scene camera's
# view, and refuses the fewest boot-home glides: 1/1200 random limp-collapse
# start poses drive a link below the table on the straight line to it, against
# 5/1200 for a tool-down home (the edu6's own measured rate was ~1 in 100).
# Fingertip TCP at world (+0.018, 0, +0.519); lowest moving-link point +43.7 mm,
# which is link1's structural floor, i.e. the best this arm can do.
#
# CONSEQUENCE worth knowing, identical to the edu6's: the tool at HOME points 9.7
# degrees off straight UP, so HOME is OUTSIDE the solver's image (solve() only
# emits strict-vertical poses) and no Cartesian machinery — reach check,
# workspace floor, reroute vias — can reason about it. That is why the
# table-floor guard on the way to it is joint-space (workflow/arm_geometry.py +
# workflow/home_planner.py).
#
# The gripper channel is the claw servo angle in RADIANS, 0 = jaws closed …
# 0.90 = open. NOTE this is the OPPOSITE SIGN to the raw SolidWorks export,
# whose RL_joint runs 0 → −1.57: the shipped URDF copy flips that joint's axis
# (and its mimicking twin's) so that open > closed numerically, which every
# shared code path assumes — motion.check_grasp_held reads "held" as an achieved
# angle ABOVE the commanded close, object_catalog validates the close inside a
# [closed, open) band, and SimArm classifies a command BELOW its close threshold
# as a close. It is a pure relabelling: identical physical poses, and the
# driver's per-joint sign vector carries the flip through to the servo.
#
# Joint names are URDF-native so /joint_states, the sim publisher and the web
# twin agree on one name set. ``RL_joint`` is the CAD's own name for the driven
# claw finger (the other rides an <mimic>) — it is "Right-finger L joint", not
# reinforcement learning.
_EDU1_HOME_JOINTS_RAD = (0.0, 0.64, 1.48, 0.90, 0.0)
_EDU1_JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5',
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
    ik_backend: str = 'omx'            # build_ik dispatch ('omx' | 'edu6')
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
    gripper_mm_per_rad: Optional[float] = None   # jaw-opening display factor
    # True when the TCP's HEIGHT below the tool frame depends on the gripper
    # command — a ROTATING claw, where the fingertip swings back as the jaws
    # open. The TCP is defined at the CLOSED tip, so „Tisch vermessen" measures
    # the table too low unless the student closes the claw first, and that is a
    # student-facing INSTRUCTION, not something any guard can enforce. False on
    # every parallel-jaw arm (OMX, edu6), where the tip height is constant.
    tool_tip_tracks_gripper: bool = False
    grasp_held_margin_rad: Optional[float] = None   # None → motion 0.15
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
    # envelope. Measured 2026-07-26 on the real solvers: edu6 tops out at
    # ~0.065 m of TCP height (joint5's +110° relief is the binding limit, not the
    # 2R annulus), so ALL THREE OMX swing heights and the 0.18 m cruise are
    # unreachable there — 0/144 base-swing via candidates solved, i.e. both
    # reroute rungs were structurally dead and every blocked transit fell through
    # to the German refusal. Sizing them per profile revives base-swing
    # (64/144) and leaves OMX bit-identical (None → the module constants).
    safe_travel_z_m: Optional[float] = None
    tool_clear_m: Optional[float] = None
    swing_heights_m: Optional[tuple] = None
    swing_radii_m: Optional[tuple] = None

    def build_ik(self, urdf_string: Optional[str] = None):
        """Return the arm's IK solver — the ``ik_factory`` seam.

        Dispatches on ``ik_backend``: OMX uses the closed-form analytical
        :class:`IKSolver`; edu6 the closed-form :class:`Edu6IKSolver`; edu1
        the closed-form :class:`Edu1IKSolver`. Imported
        LAZILY so this module stays importable in the deps-free unit-test stubs
        (both solvers pull in NumPy). ``urdf_string`` is forwarded so the OMX
        solver can verify its baked constants against a locally-available
        ``robot_description`` (never blocks on a cross-node fetch)."""
        if self.ik_backend == 'edu6':
            from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
            return Edu6IKSolver(urdf_string=urdf_string)
        if self.ik_backend == 'edu1':
            from physical_ai_server.workflow.edu1_ik import Edu1IKSolver
            return Edu1IKSolver(urdf_string=urdf_string)
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
    urdf_asset_id='omx_f',
    # A follower-only Roboter-Studio kit's LONE camera is the SCENE camera, not
    # the gripper cam (perception + the config topics hang off the role name —
    # 'gripper' first broke every RS kit's single-camera auto-assign). 'gripper'
    # stays second for a 2-camera follower rig. omx_full keeps ('gripper','scene').
    camera_roles=('scene', 'gripper'),
)

# edu6_studio — the 6-DOF Feetech follower-only Roboter-Studio arm (D1..D8 in
# docs/plans/edu6-studio-arm.md). MUST stay a literal-kwarg call: the GUI↔server
# lockstep test parses this with ast and requires literal profile_id= /
# follower_only= / capabilities=Capabilities(..., has_leader=<literal>).
_EDU6_STUDIO = ArmProfile(
    profile_id='edu6_studio',
    display_name_de='EduBotics 6-Achs – Roboter Studio',
    help_de=(
        'Der 6-Achs-Arm von EduBotics mit einer Szenen-Kamera. Nur für '
        'Roboter Studio (Greifen & Programmieren).'
    ),
    # NEW namespace literal, never 'omx_f': init_ros_params reads the
    # config-YAML top-level key equal to data_robot_type — leaving it omx_f
    # silently inherits the OMX camera/joint lists.
    data_robot_type='edu6_studio',
    follower_only=True,
    capabilities=Capabilities(
        recordable=False,
        editable=False,
        trainable=False,
        inferable=False,
        roboter_studio=True,
        has_leader=False,
    ),
    home_joints_rad=_EDU6_HOME_JOINTS_RAD,
    # No separate collision safe-home: the teleop e-stop is disabled by
    # construction on this arm (collision_enabled=False below), so the field
    # mirrors HOME as unconsumed seam data.
    safe_home_arm_rad=_EDU6_HOME_JOINTS_RAD,
    gripper_open_rad=1.75,
    gripper_closed_rad=0.0,
    num_arm_joints=6,
    joint_names=_EDU6_JOINT_NAMES,
    urdf_asset_id='edu6',
    ik_backend='edu6',
    roll_joint_index=5,
    velocity_limit_rad_s=5.45,
    collision_enabled=False,
    tool_length_m=0.1724,
    torque_service='/edu6/set_torque',
    camera_roles=('scene',),
    reach_inner_m=0.09,
    reach_outer_m=0.21,
    gripper_mm_per_rad=25.2,
    grasp_held_margin_rad=0.12,
    sim_close_threshold_rad=1.5,
    sim_held_block_offset_rad=0.19,
    # INERT BY CONSTRUCTION on this arm, and kept anyway — deliberately, with the
    # proof, rather than left looking like a tuned rig value (2026-07-26 audit F7).
    # ``sim_arm.get_joints`` reports a blocked jaw as
    #     max(held_floor, commanded + held_block_offset)
    # and ``_simulate_held`` only treats a command as a CLOSE when it is below
    # ``sim_close_threshold_rad`` (1.5). This gripper's legal band is 0.0…1.75
    # (``gripper_closed_rad``…``gripper_open_rad``, and ``close_on_object`` clamps
    # into exactly that band), so every value that can reach the max() is in
    # [0.0, 1.5) → commanded + 0.19 ∈ [0.19, 1.69) → strictly greater than 0.05
    # for EVERY legal close. The floor can therefore never win the max().
    # It stays because the FIELD is live on the OMX (cube close −0.5 + 0.25 =
    # −0.25, floored to −0.1) and dropping edu6's override would inherit that
    # OMX −0.1 — numerically just as inert, but a negative number on a gripper
    # that never goes negative, i.e. actively misleading to the next reader.
    # ``test_sim_held_floor_is_inert_for_every_legal_edu6_close`` pins the proof,
    # so widening the band or lowering the close threshold surfaces it instead of
    # silently making a never-reviewed number load-bearing.
    sim_held_floor_rad=0.05,
    # Reroute-ladder geometry, MEASURED through the real solver (2026-07-26).
    # Max solvable TCP height is ~0.065 m (at r≈0.10-0.14); joint5's +110° relief
    # binds long before the 2R annulus does, so the OMX 0.18 m cruise and its
    # (0.10, 0.14, 0.16) swing heights are ALL unreachable on this arm.
    safe_travel_z_m=0.06,
    # 0.0 is exact, not a shortcut: this arm's EE frame IS the fingertip TCP
    # (_L_TOOL), and edu6_ik.link_points appends NOTHING below it — unlike the OMX
    # solver, which projects two extra samples _TOOL_TIP_EXT_M (0.04 m) past its
    # EE along the tool axis, which is what the 0.05 m default models. Measured:
    # OMX EE→lowest tool point = 40.0 mm, edu6 = 0.0 mm. Keeping the OMX 0.05 here
    # would demand 50 mm of headroom this arm does not have; the 0.05 m zone
    # inflation + _CLEAR_EPS_M still provide the clearance margin.
    tool_clear_m=0.0,
    # Heights/radii that actually solve (mid / far / near, same intent as the OMX
    # ordering): 64/144 candidates reachable vs 0/144 with the OMX grid.
    swing_heights_m=(0.03, 0.05, 0.02),
    swing_radii_m=(0.14, 0.18, 0.10),
)

# edu1_studio — the 5-DOF Feetech follower-only Roboter-Studio arm ("Edu:1",
# docs/plans/edu1-studio-arm.md). MUST stay a literal-kwarg call: the GUI↔server
# lockstep test parses this with ast and requires literal profile_id= /
# follower_only= / capabilities=Capabilities(..., has_leader=<literal>).
_EDU1_STUDIO = ArmProfile(
    profile_id='edu1_studio',
    display_name_de='Edu:1 – Roboter Studio',
    help_de=(
        'Der 5-Achs-Arm Edu:1 von EduBotics mit einer Szenen-Kamera. '
        'Nur für Roboter Studio (Greifen & Programmieren).'
    ),
    # NEW namespace literal, never 'omx_f': init_ros_params reads the
    # config-YAML top-level key equal to data_robot_type. It is ALSO the id the
    # cloud stamps on a saved „Bewegung" (workflow_trajectories.robot_profile,
    # migration 039) — and on this arm that tag is doing MORE work than it does
    # for the edu6: edu1 has 5 arm joints, so its Contract-B point width is 7,
    # the SAME width omx_f uses. Width alone therefore cannot tell an Edu:1
    # recording from an OMX one; only this id can.
    data_robot_type='edu1_studio',
    follower_only=True,
    capabilities=Capabilities(
        recordable=False,
        editable=False,
        trainable=False,
        inferable=False,
        roboter_studio=True,
        has_leader=False,
    ),
    home_joints_rad=_EDU1_HOME_JOINTS_RAD,
    # No separate collision safe-home: the teleop e-stop is disabled by
    # construction on this arm (collision_enabled=False below), so the field
    # mirrors HOME as unconsumed seam data.
    safe_home_arm_rad=_EDU1_HOME_JOINTS_RAD,
    # Claw servo band. 0.90 rad of opening is a ~99 mm jaw gap at the blades'
    # widest point, comfortably past the 30 mm shipped cube while keeping the
    # open→close swing short; the physical stop is 1.57.
    gripper_open_rad=0.90,
    gripper_closed_rad=0.0,
    num_arm_joints=5,
    joint_names=_EDU1_JOINT_NAMES,
    urdf_asset_id='edu1',
    ik_backend='edu1',
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
    tool_length_m=0.08625,
    torque_service='/edu1/set_torque',
    camera_roles=('scene',),
    # Student-facing table ring, measured through the real solver at the grasp
    # plane z = 0.015 m: r ∈ [0.082, 0.363]. Rounded INWARD on both ends so the
    # drawn ring never promises a placement the solver then refuses.
    reach_inner_m=0.09,
    reach_outer_m=0.35,
    # gripper_mm_per_rad is deliberately OMITTED (→ the jog row keeps its degree
    # display). This is a ROTATING claw: the jaw gap is affine in the servo
    # angle, ≈21 mm already at 0 rad plus ≈85 mm/rad, so a pure mm/rad factor
    # would print a number that is wrong by the whole 21 mm offset at every
    # opening. A degree read-out is honest; a fabricated millimetre is not.
    # The claw ROTATES: its tip sits 86.25 mm below the tool frame closed but
    # 68.8 mm at 0.9 rad open. The touch-off must therefore be taught with the
    # claw CLOSED (rig gate E8) — this flag is what puts that sentence in front
    # of the student, and only on the arm it is true for.
    tool_tip_tracks_gripper=True,
    grasp_held_margin_rad=0.10,
    # Sim grasp classifier. A close command is anything below 0.5, which sits
    # between the catalog close (0.10) and open (0.90); a 30 mm cube blocks the
    # real jaws at ≈0.25 rad, so commanded 0.10 + 0.15 reproduces that.
    sim_close_threshold_rad=0.5,
    sim_held_block_offset_rad=0.15,
    # INERT BY CONSTRUCTION here, exactly as on the edu6, and kept for the same
    # reason. ``sim_arm.get_joints`` reports a blocked jaw as
    #     max(held_floor, commanded + held_block_offset)
    # and only a command below ``sim_close_threshold_rad`` (0.5) counts as a
    # close, so every value that can reach the max() lies in [0.0, 0.5) →
    # commanded + 0.15 ∈ [0.15, 0.65) → always above 0.05. Dropping the override
    # would inherit the OMX −0.1: numerically just as inert, but a NEGATIVE
    # number on a gripper that never goes negative, i.e. actively misleading.
    sim_held_floor_rad=0.05,
    # Reroute-ladder geometry, MEASURED through the real solver (2026-09-05).
    # This arm's strict-vertical TCP ceiling is ~0.100 m (joint4's ±90° window
    # binds through q4 = q2 − q3 − π/2, not the 2R annulus), so the OMX 0.18 m
    # cruise and its (0.10, 0.14, 0.16) swing heights are ALL unreachable:
    # 0/72 base-swing candidates solve with the OMX grid. The values below give
    # 32/72 — the same fraction the edu6 grid achieves, and the ceiling here is
    # the ±90° base yaw (only 4 of the 8 candidate azimuths are in FRONT of the
    # arm), not the height/radius choice.
    safe_travel_z_m=0.075,
    # 0.0 is exact, not a shortcut: ``edu1_ik.link_points`` ends AT the
    # fingertip TCP and appends nothing below it, unlike the OMX solver, which
    # projects two extra samples 0.04 m past its EE along the tool axis — which
    # is what the 0.05 m module default models.
    tool_clear_m=0.0,
    # Mid / high / low and mid / far / near, the same ordering intent as the OMX
    # and edu6 grids. Every one of the nine (height, radius) pairs is inside the
    # annulus at its own height — the annulus NARROWS with height (r ∈ [0.090,
    # 0.354] at z = 0.03 but only [0.128, 0.316] at z = 0.07), so a radius chosen
    # off the low row alone leaves a dead row in the grid. 36/72 candidates
    # solve; the ceiling is joint1's ±90° (only 4 of the 8 candidate azimuths
    # are in FRONT of the arm), not this choice.
    swing_heights_m=(0.05, 0.07, 0.03),
    swing_radii_m=(0.18, 0.28, 0.14),
)

# Registry keyed by profile id. Keep ids + follower_only in lockstep with the
# GUI thin descriptor (gui/app/constants.py::ROBOT_PROFILES) — cross-boundary
# contract, tested each side.
ROBOT_PROFILES: dict = {
    _OMX_FULL.profile_id: _OMX_FULL,
    _OMX_FOLLOWER.profile_id: _OMX_FOLLOWER,
    _EDU6_STUDIO.profile_id: _EDU6_STUDIO,
    _EDU1_STUDIO.profile_id: _EDU1_STUDIO,
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
    GEOMETRY keys (edu6, additive) feed the profile-driven React surfaces
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
    }
    for key, value in (
        ('reach_inner_m', profile.reach_inner_m),
        ('reach_outer_m', profile.reach_outer_m),
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
