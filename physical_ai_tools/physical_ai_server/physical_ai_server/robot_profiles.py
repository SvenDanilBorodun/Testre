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
# docs/plans/edu6-studio-arm.md). HOME is the compact over-base fold derived
# 2026-07-22 (tip within 39 mm of the joint-1 axis, min mesh gap 32.3 mm,
# θ5 = +0.7 — a non-degenerate seed in the working (relieved) branch). The
# gripper channel is the end_gear servo angle in RADIANS: 0 = jaws closed …
# 1.75 = open command (physical stop ≈ 1.7857; jaw ≈ 25.2 mm/rad). Joint names
# are URDF-native so /joint_states, the sim publisher and the web twin (whose
# <mimic> fingers key off end_gear_joint) agree on one name set.
_EDU6_HOME_JOINTS_RAD = (0.0, 0.70, -2.40, 0.0, 0.70, 0.0)
_EDU6_JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                     'joint6', 'end_gear_joint')


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
    grasp_held_margin_rad: Optional[float] = None   # None → motion 0.15
    # Sim-arm grasp classifier values (None → sim_arm OMX module constants):
    sim_close_threshold_rad: Optional[float] = None
    sim_held_block_offset_rad: Optional[float] = None
    sim_held_floor_rad: Optional[float] = None

    def build_ik(self, urdf_string: Optional[str] = None):
        """Return the arm's IK solver — the ``ik_factory`` seam.

        Dispatches on ``ik_backend``: OMX uses the closed-form analytical
        :class:`IKSolver`; edu6 the closed-form :class:`Edu6IKSolver`. Imported
        LAZILY so this module stays importable in the deps-free unit-test stubs
        (both solvers pull in NumPy). ``urdf_string`` is forwarded so the OMX
        solver can verify its baked constants against a locally-available
        ``robot_description`` (never blocks on a cross-node fetch)."""
        if self.ik_backend == 'edu6':
            from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
            return Edu6IKSolver(urdf_string=urdf_string)
        from physical_ai_server.workflow.ik_solver import IKSolver
        return IKSolver(urdf_string=urdf_string)


_OMX_FULL = ArmProfile(
    profile_id='omx_full',
    display_name_de='OMX – Voll',
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
)

# edu6_studio — the 6-DOF Feetech follower-only Roboter-Studio arm (D1..D8 in
# docs/plans/edu6-studio-arm.md). MUST stay a literal-kwarg call: the GUI↔server
# lockstep test parses this with ast and requires literal profile_id= /
# follower_only= / capabilities=Capabilities(..., has_leader=<literal>).
_EDU6_STUDIO = ArmProfile(
    profile_id='edu6_studio',
    display_name_de='EduBotics 6-Achs – Roboter Studio',
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
    sim_held_floor_rad=0.05,
)

# Registry keyed by profile id. Keep ids + follower_only in lockstep with the
# GUI thin descriptor (gui/app/constants.py::ROBOT_PROFILES) — cross-boundary
# contract, tested each side.
ROBOT_PROFILES: dict = {
    _OMX_FULL.profile_id: _OMX_FULL,
    _OMX_FOLLOWER.profile_id: _OMX_FOLLOWER,
    _EDU6_STUDIO.profile_id: _EDU6_STUDIO,
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
    }
    for key, value in (
        ('reach_inner_m', profile.reach_inner_m),
        ('reach_outer_m', profile.reach_outer_m),
        ('gripper_mm_per_rad', profile.gripper_mm_per_rad),
        ('sim_close_threshold_rad', profile.sim_close_threshold_rad),
    ):
        if value is not None:
            manifest[key] = value
    return json.dumps(manifest, separators=(',', ':'))
