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
// (UrdfTwin / JogPanel / SimScene) consumes (edu6 §4.5).

import { armGeometry } from '../armProfile';
import { reachAnnulus, REACH_INNER_M, REACH_OUTER_M } from '../../components/Workshop/simConstants';

const EDU6_CAPS = {
  recordable: false, editable: false, trainable: false, inferable: false,
  roboter_studio: true, has_leader: false,
  arm_joints: 6,
  joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6',
    'end_gear_joint'],
  urdf_asset_id: 'edu6',
  gripper_open_rad: 1.75,
  gripper_closed_rad: 0.0,
  gripper_mm_per_rad: 25.2,
  reach_inner_m: 0.09,
  reach_outer_m: 0.21,
  sim_close_threshold_rad: 1.5,
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

  it('the edu6 manifest resolves every geometry field', () => {
    const g = armGeometry(EDU6_CAPS);
    expect(g.armJoints).toBe(6);
    expect(g.jointNames[6]).toBe('end_gear_joint');
    expect(g.gripperJointName).toBe('end_gear_joint');
    expect(g.urdfAssetId).toBe('edu6');
    expect(g.gripperOpenRad).toBe(1.75);
    expect(g.gripperClosedRad).toBe(0.0);
    expect(g.gripperMmPerRad).toBe(25.2);
    expect(g.simCloseThresholdRad).toBe(1.5);
  });

  it('a joint_names length mismatch falls back to OMX names (never a torn set)', () => {
    const g = armGeometry({ ...EDU6_CAPS, joint_names: ['a', 'b'] });
    expect(g.armJoints).toBe(6);
    expect(g.jointNames).toEqual([
      'joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']);
  });
});

describe('reachAnnulus', () => {
  it('null caps → the OMX constants', () => {
    expect(reachAnnulus(null)).toEqual({ inner: REACH_INNER_M, outer: REACH_OUTER_M });
  });
  it('edu6 caps → 0.09/0.21', () => {
    expect(reachAnnulus(EDU6_CAPS)).toEqual({ inner: 0.09, outer: 0.21 });
  });
  it('a nonsense outer (≤ inner) is ignored', () => {
    expect(reachAnnulus({ reach_inner_m: 0.2, reach_outer_m: 0.1 }))
      .toEqual({ inner: 0.2, outer: REACH_OUTER_M });
  });
});
