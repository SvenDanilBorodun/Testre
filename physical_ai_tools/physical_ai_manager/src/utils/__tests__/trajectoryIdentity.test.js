/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

import { describe, it, expect } from 'vitest';
import { normalizeTrajectory, trajectoryMatchesRig } from '../trajectoryIdentity';
import { LEGACY_ROBOT_TYPES } from '../../components/Workshop/sammlung/format';

const PTS = [[0, 0, 0, 0, 0, 0.8, 0], [0.1, 0, 0, 0, 0, 0.8, 0.04]];

describe('normalizeTrajectory', () => {
  it('reads the direct points + fps shape', () => {
    expect(normalizeTrajectory({ points: PTS, fps: 25, robot_profile: 'omx_f' }))
      .toEqual({ fps: 25, points: PTS, robotProfile: 'omx_f' });
  });

  it('reads a stringified points_json', () => {
    expect(normalizeTrajectory({ points_json: JSON.stringify({ fps: 30, points: PTS }) }))
      .toEqual({ fps: 30, points: PTS, robotProfile: null });
  });

  it('reads the getTrajectory row shape with a samples object', () => {
    expect(normalizeTrajectory({
      id: 't1', name: 'Winken', robot_profile: 'v1_studio', samples: { fps: 25, points: PTS },
    })).toEqual({ fps: 25, points: PTS, robotProfile: 'v1_studio' });
    // samples.fps missing → the row fps.
    expect(normalizeTrajectory({ fps: 20, samples: { points: PTS } }).fps).toBe(20);
  });

  it('returns null for empty or unusable input', () => {
    for (const bad of [null, undefined, 'x', {}, { points: [] }, { samples: { points: [] } },
      { points_json: 'kaputt' }, { samples: null }]) {
      expect(normalizeTrajectory(bad)).toBeNull();
    }
  });
});

describe('trajectoryMatchesRig', () => {
  it('compares arm-family IDS, never widths', () => {
    // Both 7-wide — only the tag separates them.
    expect(trajectoryMatchesRig('v1_studio', 'omx_f')).toBe(false);
    expect(trajectoryMatchesRig('omx_f', 'v1_studio')).toBe(false);
    expect(trajectoryMatchesRig('v1_studio', 'v1_studio')).toBe(true);
  });

  it('a recording of a retired arm (its tag kept in the cloud) never matches a V1 or an OMX rig', () => {
    for (const tag of LEGACY_ROBOT_TYPES) {
      expect(trajectoryMatchesRig(tag, 'v1_studio')).toBe(false);
      expect(trajectoryMatchesRig(tag, 'omx_f')).toBe(false);
    }
  });

  it('maps an untagged recording and an unknown rig to omx_f', () => {
    expect(trajectoryMatchesRig(null, 'omx_f')).toBe(true);
    expect(trajectoryMatchesRig('', '')).toBe(true);
    expect(trajectoryMatchesRig(undefined, 'v1_studio')).toBe(false);
  });
});
