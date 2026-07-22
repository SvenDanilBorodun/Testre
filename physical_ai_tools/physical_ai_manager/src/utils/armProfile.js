/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// armGeometry — resolve the ADDITIVE geometry keys of the server capability
// manifest (robot_profiles.capabilities_json) into the values the profile-
// driven React surfaces consume (UrdfTwin, JogPanel, SimScene/simConstants).
//
// Every field falls back to the OMX value when the manifest is absent (cloud
// mode, pre-first-tick) or predates the edu6 keys — so an old server, a null
// caps, or a partial manifest render EXACTLY the pre-edu6 UI. Deliberately
// dependency-free (no three, no react) — it rides the entry chunk.

const OMX_JOINT_NAMES = [
  'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1',
];

export function armGeometry(caps) {
  const c = caps && typeof caps === 'object' && !Array.isArray(caps) ? caps : {};
  const armJoints = Number.isInteger(c.arm_joints) && c.arm_joints > 0
    ? c.arm_joints : 5;
  const jointNames = Array.isArray(c.joint_names)
    && c.joint_names.length === armJoints + 1
    && c.joint_names.every((n) => typeof n === 'string' && n)
    ? c.joint_names : OMX_JOINT_NAMES;
  const num = (v, fallback) => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
  return {
    armJoints,
    jointNames,
    gripperJointName: jointNames[armJoints],
    urdfAssetId: typeof c.urdf_asset_id === 'string' && c.urdf_asset_id
      ? c.urdf_asset_id : 'omx_f',
    gripperOpenRad: num(c.gripper_open_rad, 0.8),
    gripperClosedRad: num(c.gripper_closed_rad, -0.5),
    // null = "no mm mapping" → the jog gripper row keeps its degree display.
    gripperMmPerRad: typeof c.gripper_mm_per_rad === 'number'
      && Number.isFinite(c.gripper_mm_per_rad) && c.gripper_mm_per_rad > 0
      ? c.gripper_mm_per_rad : null,
    reachInnerM: num(c.reach_inner_m, null),
    reachOuterM: num(c.reach_outer_m, null),
    // Sim grasp-classifier close threshold; null → the OMX SimScene literals.
    simCloseThresholdRad: num(c.sim_close_threshold_rad, null),
  };
}
