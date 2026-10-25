/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// armGeometry — the caps→geometry resolver every profile-driven surface
// (UrdfTwin / JogPanel / SimScene) consumes.

import { armGeometry, ghostJointsFromEntry } from '../armProfile';
import { reachAnnulus, REACH_INNER_M, REACH_OUTER_M } from '../../components/Workshop/simConstants';

// A SYNTHETIC 6-joint manifest with every optional geometry key: no shipped
// profile has six arm joints or a millimetre gripper today, but the resolver
// must still carry each key through.
const SIX_JOINT_CAPS = {
  recordable: false, editable: false, trainable: false, inferable: false,
  roboter_studio: true, has_leader: false,
  arm_joints: 6,
  joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6',
    'gripper'],
  urdf_asset_id: 'synthetic',
  gripper_open_rad: 1.75,
  gripper_closed_rad: 0.0,
  gripper_mm_per_rad: 25.2,
  reach_inner_m: 0.09,
  reach_outer_m: 0.21,
  sim_close_threshold_rad: 1.5,
};

// The V1's manifest geometry (robot_profiles._V1_STUDIO). Nothing pins this
// fixture to the server, so it drifts silently — keep it in step by hand.
const V1_CAPS = {
  recordable: false, editable: false, trainable: false, inferable: false,
  roboter_studio: true, has_leader: false,
  arm_joints: 5,
  joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'RL_joint'],
  urdf_asset_id: 'v1',
  gripper_open_rad: 0.9,
  gripper_closed_rad: 0.0,
  // gripper_mm_per_rad is DELIBERATELY absent — see the test below.
  reach_inner_m: 0.1,
  reach_outer_m: 0.34,
  reach_sector_deg: [-90, 90],
  sim_close_threshold_rad: 0.5,
  tool_tip_tracks_gripper: true,
};

describe('armGeometry', () => {
  it('null / undefined / non-object caps resolve to the OMX defaults', () => {
    for (const caps of [null, undefined, 'x', [], 42]) {
      const g = armGeometry(caps);
      expect(g.armJoints).toBe(5);
      expect(g.jointNames).toEqual([
        'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']);
      expect(g.gripperJointName).toBe('gripper_joint_1');
      expect(g.urdfAssetId).toBe('omx_f');
      expect(g.gripperOpenRad).toBe(0.8);
      expect(g.gripperClosedRad).toBe(-0.5);
      expect(g.gripperMmPerRad).toBeNull();
      expect(g.simCloseThresholdRad).toBeNull();
    }
  });

  it('an OMX manifest without geometry keys stays OMX', () => {
    const g = armGeometry({
      recordable: true, editable: true, trainable: true, inferable: true,
      roboter_studio: true, has_leader: true,
    });
    expect(g.armJoints).toBe(5);
    expect(g.urdfAssetId).toBe('omx_f');
  });

  it('a 6-joint manifest resolves every geometry field', () => {
    const g = armGeometry(SIX_JOINT_CAPS);
    expect(g.armJoints).toBe(6);
    expect(g.jointNames[6]).toBe('gripper');
    expect(g.gripperJointName).toBe('gripper');
    expect(g.urdfAssetId).toBe('synthetic');
    expect(g.gripperOpenRad).toBe(1.75);
    expect(g.gripperClosedRad).toBe(0.0);
    expect(g.gripperMmPerRad).toBe(25.2);
    expect(g.simCloseThresholdRad).toBe(1.5);
  });

  it('the V1 manifest resolves every geometry field', () => {
    const g = armGeometry(V1_CAPS);
    expect(g.armJoints).toBe(5);
    expect(g.jointNames[5]).toBe('RL_joint');
    expect(g.gripperJointName).toBe('RL_joint');
    expect(g.urdfAssetId).toBe('v1');
    expect(g.gripperOpenRad).toBe(0.9);
    expect(g.gripperClosedRad).toBe(0.0);
    expect(g.simCloseThresholdRad).toBe(0.5);
  });

  it('the V1 shares the OMX arm-joint COUNT and must not be mistaken for it', () => {
    // The V1 is 5-DOF like the OMX, so armJoints alone does not identify the
    // arm. Every surface that renders geometry has to key off urdf_asset_id /
    // joint_names, never the count.
    const omx = armGeometry(null);
    const v1 = armGeometry(V1_CAPS);
    expect(v1.armJoints).toBe(omx.armJoints);
    expect(v1.urdfAssetId).not.toBe(omx.urdfAssetId);
    expect(v1.gripperJointName).not.toBe(omx.gripperJointName);
  });

  it('only a rotating-claw manifest sets toolTipTracksGripper', () => {
    // It gates the „Greifer schließen" line in the touch-off wizard. `=== true`
    // with no fallback: the server omits the key on every parallel-jaw arm, and
    // an OLD server omits it for ALL of them — which is the parallel-jaw behaviour.
    expect(armGeometry(V1_CAPS).toolTipTracksGripper).toBe(true);
    expect(armGeometry(SIX_JOINT_CAPS).toolTipTracksGripper).toBe(false);
    expect(armGeometry(null).toolTipTracksGripper).toBe(false);
    expect(armGeometry({ tool_tip_tracks_gripper: 'yes' }).toolTipTracksGripper)
      .toBe(false);
  });

  it('the V1 omits gripper_mm_per_rad, so the jog row keeps its degree display', () => {
    // A ROTATING claw: the jaw gap is not proportional to the servo angle, so
    // any single mm/rad factor would print a wrong number.
    expect(armGeometry(V1_CAPS).gripperMmPerRad).toBeNull();
  });

  it('a joint_names length mismatch falls back to OMX names (never a torn set)', () => {
    const g = armGeometry({ ...SIX_JOINT_CAPS, joint_names: ['a', 'b'] });
    expect(g.armJoints).toBe(6);
    expect(g.jointNames).toEqual([
      'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']);
  });
});

describe('reachAnnulus', () => {
  it('null caps → the OMX constants', () => {
    expect(reachAnnulus(null)).toEqual({ inner: REACH_INNER_M, outer: REACH_OUTER_M });
  });
  it('full-ring caps → their radii (0.09/0.21)', () => {
    expect(reachAnnulus(SIX_JOINT_CAPS)).toEqual({ inner: 0.09, outer: 0.21 });
  });
  it('V1 caps → 0.10/0.34 over ±90°', () => {
    expect(reachAnnulus(V1_CAPS)).toEqual({ inner: 0.1, outer: 0.34, sector: { minDeg: -90, maxDeg: 90 } });
  });
  it('a nonsense outer (≤ inner) is ignored', () => {
    expect(reachAnnulus({ reach_inner_m: 0.2, reach_outer_m: 0.1 }))
      .toEqual({ inner: 0.2, outer: REACH_OUTER_M });
  });
});

describe('ghostJointsFromEntry — the ghost arm draws only a snapshot that fits this arm', () => {
  const OMX_NAMES = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
  const POSE = {
    id: 'd_00000001', name: 'Oben', kind: 'pose', x: 0.1, y: 0, z: 0.1,
    joints: [0, -0.9, 1.1, 0.3, 0, 0.8], joint_names: OMX_NAMES,
  };

  it('returns {names, positions} copies for a fitting OMX Position (robot_type absent or equal)', () => {
    const g = ghostJointsFromEntry(POSE, null, 'omx_f');
    expect(g).toEqual({ names: OMX_NAMES, positions: POSE.joints });
    expect(g.names).not.toBe(POSE.joint_names);
    expect(ghostJointsFromEntry({ ...POSE, robot_type: 'omx_f' }, null, 'omx_f')).not.toBeNull();
  });

  it('refuses a Ziel, a joint-less entry (older server), another arm and a stranger joint name', () => {
    expect(ghostJointsFromEntry({ ...POSE, kind: 'pin' }, null, 'omx_f')).toBeNull();
    expect(ghostJointsFromEntry({ ...POSE, joints: undefined, joint_names: undefined }, null, 'omx_f')).toBeNull();
    expect(ghostJointsFromEntry({ ...POSE, robot_type: 'v1_studio' }, null, 'omx_f')).toBeNull();
    expect(ghostJointsFromEntry(POSE, V1_CAPS, 'v1_studio')).toBeNull();
    expect(ghostJointsFromEntry({ ...POSE, joints: [0, Number.NaN] , joint_names: ['joint1', 'joint2'] }, null, 'omx_f')).toBeNull();
    expect(ghostJointsFromEntry({ ...POSE, joint_names: OMX_NAMES.slice(0, 5) }, null, 'omx_f')).toBeNull();
    expect(ghostJointsFromEntry(null, null, 'omx_f')).toBeNull();
  });

  it('accepts a V1 snapshot under V1 caps', () => {
    const names = V1_CAPS.joint_names;
    const entry = { ...POSE, robot_type: 'v1_studio', joints: [0, 0.64, 1.48, 0.9, 0, 0.9], joint_names: names };
    expect(ghostJointsFromEntry(entry, V1_CAPS, 'v1_studio')).toEqual({ names, positions: entry.joints });
  });
});
