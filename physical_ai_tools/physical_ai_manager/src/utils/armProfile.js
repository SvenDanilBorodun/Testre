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
    // True only for a ROTATING claw, whose fingertip swings back as the jaws
    // open — so the TCP's height below the tool frame depends on the gripper
    // command. `=== true` and no fallback: the server omits the key entirely on
    // every parallel-jaw arm, and an OLD server omits it for all of them, which
    // is exactly the pre-Edu:1 behaviour.
    toolTipTracksGripper: c.tool_tip_tracks_gripper === true,
  };
}

// Grasp-classifier band (front-end, idealized). The OMX-F gripper joint rests
// open ≈ +0.8 rad and any CLOSE drives it negative-ish (per-object close angles
// run ≈ -0.1 … -0.5, with no fixed floor). Classify with a wide hysteresis band
// well below the open rest: "closed" BELOW +0.2 (catches even a shallow -0.1
// close on a wide object — M1 fix; the old -0.20 threshold missed those), "open"
// again ABOVE +0.5. The 0.2…0.5 band prevents chatter; a descend (gripper held
// at +0.8) never reads as closed.
const OMX_GRIPPER_CLOSED_RAD = 0.2;
const OMX_GRIPPER_OPEN_RAD = 0.5;

// One band, two consumers: SimScene's grasp-attach (with its own hysteresis
// latch) and „Greifer merken" in inserted programs (classifyGripper below).
export function gripperBand(caps) {
  const geo = armGeometry(caps);
  if (geo.simCloseThresholdRad === null) {
    return { close: OMX_GRIPPER_CLOSED_RAD, open: OMX_GRIPPER_OPEN_RAD };
  }
  // Profile-supplied close threshold; re-open hysteresis sits halfway
  // between it and the profile's full-open command (edu6: 1.5 / 1.625).
  return {
    close: geo.simCloseThresholdRad,
    open: (geo.simCloseThresholdRad + geo.gripperOpenRad) / 2,
  };
}

// The ghost-arm pose of a stored Position (`{names, positions}`), or null. Only a
// snapshot that fits THIS arm is drawn: every name one of the profile's joints,
// and a stamped robot_type (when present) equal to the rig's. An entry with no
// joints (an older server) or from another arm draws nothing — never a guess.
export function ghostJointsFromEntry(entry, caps, robotType) {
  if (!entry || entry.kind !== 'pose') return null;
  const { joints, joint_names: names } = entry;
  if (!Array.isArray(joints) || !Array.isArray(names)) return null;
  if (joints.length === 0 || joints.length !== names.length) return null;
  if (!joints.every((v) => typeof v === 'number' && Number.isFinite(v))) return null;
  const known = new Set(armGeometry(caps).jointNames);
  if (!names.every((n) => typeof n === 'string' && known.has(n))) return null;
  if (entry.robot_type !== undefined && entry.robot_type !== null
    && entry.robot_type !== robotType) return null;
  return { names: names.slice(), positions: joints.slice() };
}

// 'closed' strictly below the band, 'open' strictly above it, null inside the
// band or for a non-finite value — an in-between gripper is UNKNOWN, never
// guessed, so no gripper block is ever emitted from it.
export function classifyGripper(value, band) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (!band || typeof band !== 'object') return null;
  if (value < band.close) return 'closed';
  if (value > band.open) return 'open';
  return null;
}

/** A Contract-B take's `{ start, end }` gripper states (each null when unknown). */
export function recordingGripperStates(rows, caps) {
  const { armJoints } = armGeometry(caps);
  const band = gripperBand(caps);
  const stateAt = (row) => (Array.isArray(row) && row.length === armJoints + 2
    ? classifyGripper(row[armJoints], band) : null);
  if (!Array.isArray(rows) || rows.length === 0) return { start: null, end: null };
  return { start: stateAt(rows[0]), end: stateAt(rows[rows.length - 1]) };
}

/**
 * A stored place's gripper state, or null. Only when the snapshot's joint at
 * the gripper index IS this profile's gripper joint — an entry captured on
 * another arm (or with no joints: an older server) stays unknown.
 */
export function placeGripperState(entry, caps) {
  if (!entry || !Array.isArray(entry.joints) || !Array.isArray(entry.joint_names)) return null;
  const { armJoints, gripperJointName } = armGeometry(caps);
  if (entry.joints.length !== entry.joint_names.length) return null;
  if (entry.joint_names[armJoints] !== gripperJointName) return null;
  return classifyGripper(entry.joints[armJoints], gripperBand(caps));
}
