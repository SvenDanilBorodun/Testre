// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Where the Inferenz page runs the model (spec §6.2): the seven rows of
// cloudTargetState, first match wins, and every branch of effectiveTarget —
// derived on every render, never stored (R-6), the Jetson always first.

import { cloudTargetState, effectiveTarget, jetsonOwnsRosbridge } from '../gating';

const CAPS = { recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true };
const base = { cloudOnly: false, jetsonStatus: 'available', capabilities: { ...CAPS, cloud_inference: true }, cloudApiConfigured: true };

describe('cloudTargetState — rows 1–7, first match wins', () => {
  it('1: cloud-only mode (no local robot) offers nothing', () => {
    expect(cloudTargetState({ ...base, cloudOnly: true })).toEqual({ offered: false, enabled: false, reason: null });
  });

  it('2: rosbridge points at the Jetson (connected, claiming, disconnecting)', () => {
    for (const s of ['connected', 'claiming', 'disconnecting']) {
      expect(cloudTargetState({ ...base, jetsonStatus: s })).toEqual({ offered: false, enabled: false, reason: null });
    }
  });

  it('3: no cloud API in this build', () => {
    expect(cloudTargetState({ ...base, cloudApiConfigured: false })).toEqual({ offered: true, enabled: false, reason: 'not_configured' });
  });

  it('4: no manifest yet', () => {
    expect(cloudTargetState({ ...base, capabilities: null })).toEqual({ offered: true, enabled: false, reason: 'no_robot' });
    expect(cloudTargetState({ ...base, capabilities: undefined })).toEqual({ offered: true, enabled: false, reason: 'no_robot' });
  });

  it('5: the robot advertises cloud_inference === true', () => {
    expect(cloudTargetState(base)).toEqual({ offered: true, enabled: true, reason: null });
  });

  it('6: the robot cannot run models', () => {
    expect(cloudTargetState({ ...base, capabilities: { ...CAPS, inferable: false, cloud_inference: false } }))
      .toEqual({ offered: true, enabled: false, reason: 'not_inferable' });
  });

  it('7: the key is absent (an older image) or not exactly true', () => {
    expect(cloudTargetState({ ...base, capabilities: { ...CAPS } })).toEqual({ offered: true, enabled: false, reason: 'old_image' });
    expect(cloudTargetState({ ...base, capabilities: { ...CAPS, cloud_inference: 'true' } })).toEqual({ offered: true, enabled: false, reason: 'old_image' });
  });

  it('the order of the rows: cloud-only and the Jetson beat a missing API and a missing manifest', () => {
    expect(cloudTargetState({ ...base, cloudOnly: true, cloudApiConfigured: false }).offered).toBe(false);
    expect(cloudTargetState({ ...base, jetsonStatus: 'connected', capabilities: null }).offered).toBe(false);
    expect(cloudTargetState({ ...base, cloudApiConfigured: false, capabilities: null }).reason).toBe('not_configured');
  });

  it('every states of the Jetson that leave rosbridge local still offer cloud', () => {
    for (const s of ['unknown', 'no_jetson', 'error', 'busy', 'available']) {
      expect(cloudTargetState({ ...base, jetsonStatus: s }).offered).toBe(true);
    }
  });
});

describe('effectiveTarget — every branch', () => {
  const on = { offered: true, enabled: true };
  const off = { offered: true, enabled: false };
  const none = { offered: false, enabled: false };

  it('the Jetson owning rosbridge wins over everything, a live run and a choice included', () => {
    for (const s of ['connected', 'claiming', 'disconnecting']) {
      expect(effectiveTarget({ jetsonStatus: s, classroomId: 'c', liveCloudSession: true, cloud: on, choice: 'cloud' })).toBe('jetson');
    }
  });

  it('a live cloud run keeps the cloud, even while discovery is pending', () => {
    expect(effectiveTarget({ jetsonStatus: 'unknown', classroomId: 'c', liveCloudSession: true, cloud: on, choice: 'jetson' })).toBe('cloud');
  });

  it('a pending discovery of a classroom Jetson is pending (it may resolve resume/reclaim)', () => {
    expect(effectiveTarget({ jetsonStatus: 'unknown', classroomId: 'c', liveCloudSession: false, cloud: on, choice: 'cloud' })).toBe('pending');
  });

  it('no classroom: discovery never runs, nothing is pending', () => {
    expect(effectiveTarget({ jetsonStatus: 'unknown', classroomId: null, liveCloudSession: false, cloud: on, choice: null })).toBe('cloud');
  });

  it('cloud not offered -> the Jetson flow', () => {
    expect(effectiveTarget({ jetsonStatus: 'available', classroomId: 'c', liveCloudSession: false, cloud: none, choice: 'cloud' })).toBe('jetson');
    expect(effectiveTarget({ jetsonStatus: 'available', classroomId: 'c', liveCloudSession: false, cloud: null, choice: null })).toBe('jetson');
  });

  it('an explicit choice wins', () => {
    expect(effectiveTarget({ jetsonStatus: 'available', classroomId: 'c', liveCloudSession: false, cloud: on, choice: 'jetson' })).toBe('jetson');
    expect(effectiveTarget({ jetsonStatus: 'available', classroomId: 'c', liveCloudSession: false, cloud: off, choice: 'cloud' })).toBe('cloud');
  });

  it('no choice: cloud is the default when enabled (owner A-Q5), the Jetson otherwise', () => {
    expect(effectiveTarget({ jetsonStatus: 'available', classroomId: 'c', liveCloudSession: false, cloud: on, choice: null })).toBe('cloud');
    expect(effectiveTarget({ jetsonStatus: 'no_jetson', classroomId: 'c', liveCloudSession: false, cloud: off, choice: null })).toBe('jetson');
    expect(effectiveTarget({ jetsonStatus: 'available', classroomId: 'c', liveCloudSession: false, cloud: on, choice: 'bogus' })).toBe('cloud');
  });

  it('jetsonOwnsRosbridge names exactly the three states', () => {
    expect(['connected', 'claiming', 'disconnecting', 'available', 'unknown', 'busy'].map(jetsonOwnsRosbridge))
      .toEqual([true, true, true, false, false, false]);
  });
});
