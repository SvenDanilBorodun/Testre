// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Whether the Inferenz page offers „In der Cloud" (spec §6.2): the six rows of
// cloudTargetState, first match wins, derived on every render, never stored
// (R-6). The cloud is the page's only target.

import * as gating from '../gating';

const { cloudTargetState } = gating;
const CAPS = { recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true };
const base = { cloudOnly: false, capabilities: { ...CAPS, cloud_inference: true }, cloudApiConfigured: true };

describe('cloudTargetState — rows 1–6, first match wins', () => {
  it('1: cloud-only mode (no local robot) offers nothing', () => {
    expect(cloudTargetState({ ...base, cloudOnly: true })).toEqual({ offered: false, enabled: false, reason: null });
  });

  it('2: no cloud API in this build', () => {
    expect(cloudTargetState({ ...base, cloudApiConfigured: false })).toEqual({ offered: true, enabled: false, reason: 'not_configured' });
  });

  it('3: no manifest yet', () => {
    expect(cloudTargetState({ ...base, capabilities: null })).toEqual({ offered: true, enabled: false, reason: 'no_robot' });
    expect(cloudTargetState({ ...base, capabilities: undefined })).toEqual({ offered: true, enabled: false, reason: 'no_robot' });
  });

  it('4: the robot advertises cloud_inference === true', () => {
    expect(cloudTargetState(base)).toEqual({ offered: true, enabled: true, reason: null });
  });

  it('5: the robot cannot run models', () => {
    expect(cloudTargetState({ ...base, capabilities: { ...CAPS, inferable: false, cloud_inference: false } }))
      .toEqual({ offered: true, enabled: false, reason: 'not_inferable' });
  });

  it('6: the key is absent (an older image) or not exactly true', () => {
    expect(cloudTargetState({ ...base, capabilities: { ...CAPS } })).toEqual({ offered: true, enabled: false, reason: 'old_image' });
    expect(cloudTargetState({ ...base, capabilities: { ...CAPS, cloud_inference: 'true' } })).toEqual({ offered: true, enabled: false, reason: 'old_image' });
  });

  it('the order of the rows: cloud-only beats a missing API and a missing manifest', () => {
    expect(cloudTargetState({ ...base, cloudOnly: true, cloudApiConfigured: false }).offered).toBe(false);
    expect(cloudTargetState({ ...base, cloudOnly: true, capabilities: null }).offered).toBe(false);
    expect(cloudTargetState({ ...base, cloudApiConfigured: false, capabilities: null }).reason).toBe('not_configured');
  });

  it('exports nothing else: there is no target to choose between', () => {
    expect(Object.keys(gating)).toEqual(['cloudTargetState']);
  });
});
