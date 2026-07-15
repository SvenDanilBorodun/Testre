// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the robotType-wipe guard in setTaskStatus: a bare /task/status tick
// (robot_type='') — e.g. a pre-capability/old server image or the degraded-boot
// empty-identity case (collision-monitor ticks now self-stamp identity via
// _stamp_identity) — must NEVER clobber the settled robot identity in Redux.
// Before the fix, the unconditional spread wiped it to '' permanently. Mirrors
// the adopt-only-when-non-empty guard userId already has.

import tasksReducer, {
  setTaskStatus,
  clearCapabilities,
  isValidCapabilities,
} from '../taskSlice';

function init() {
  return tasksReducer(undefined, { type: '@@INIT' });
}

// The robot-type picker (and its selectRobotType action) is gone — the type now
// arrives on the /task/status wire, so seed the fixture through setTaskStatus.
function withRobot(type = 'omx') {
  return tasksReducer(init(), setTaskStatus({ robotType: type }));
}

beforeEach(() => {
  try { localStorage.clear(); } catch { /* jsdom always has it */ }
});

describe('taskSlice setTaskStatus — robotType wipe guard', () => {
  it('does NOT wipe a selected robotType when /task/status reports empty', () => {
    let state = withRobot('omx');
    expect(state.taskStatus.robotType).toBe('omx');

    // The exact failure trigger: server emits robot_type='' on a restart / a
    // bare-TaskStatus notice. Other fields must still apply.
    state = tasksReducer(state, setTaskStatus({ robotType: '', phase: 0, usedCpu: 7 }));
    expect(state.taskStatus.robotType).toBe('omx');
    expect(state.taskStatus.usedCpu).toBe(7);
  });

  it('preserves robotType on a partial update that omits the field', () => {
    let state = withRobot('omx');
    state = tasksReducer(state, setTaskStatus({ usedCpu: 5 }));
    expect(state.taskStatus.robotType).toBe('omx');
  });

  it('adopts a non-empty robotType from the server', () => {
    let state = init();
    expect(state.taskStatus.robotType).toBe('');
    state = tasksReducer(state, setTaskStatus({ robotType: 'omx' }));
    expect(state.taskStatus.robotType).toBe('omx');
  });

  it('persists a non-empty robotType to localStorage but never an empty one', () => {
    let state = init();
    state = tasksReducer(state, setTaskStatus({ robotType: 'omx' }));
    expect(localStorage.getItem('edubotics_robotType')).toBe('omx');

    // An empty tick must not overwrite the persisted value either.
    tasksReducer(state, setTaskStatus({ robotType: '' }));
    expect(localStorage.getItem('edubotics_robotType')).toBe('omx');
  });
});

// The server contract (robot_profiles.capabilities_json): all six booleans,
// always together. Anything less than the complete manifest must NOT be
// adopted — an empty-but-truthy '{}' or a partial object would fail OPEN
// (nav filter hides only on explicit false) over a settled RESTRICTIVE
// manifest (audit fix 3).
const CAPS_FULL = {
  recordable: true, editable: true, trainable: true,
  inferable: true, roboter_studio: true, has_leader: true,
};
const CAPS_FOLLOWER = {
  recordable: false, editable: false, trainable: false,
  inferable: true, roboter_studio: true, has_leader: false,
};

describe('taskSlice setTaskStatus — capabilities validation (fail-closed)', () => {
  function withCaps(caps = CAPS_FOLLOWER) {
    return tasksReducer(
      init(),
      setTaskStatus({ robotType: 'omx_f', robotProfile: 'omx_follower', capabilities: caps })
    );
  }

  it('adopts a COMPLETE all-boolean manifest', () => {
    const state = withCaps(CAPS_FOLLOWER);
    expect(state.taskStatus.capabilities).toEqual(CAPS_FOLLOWER);
    expect(state.taskStatus.robotProfile).toBe('omx_follower');
  });

  it('an empty-but-truthy {} does NOT clobber a settled restrictive manifest', () => {
    let state = withCaps(CAPS_FOLLOWER);
    state = tasksReducer(state, setTaskStatus({ capabilities: {} }));
    expect(state.taskStatus.capabilities).toEqual(CAPS_FOLLOWER);
  });

  it('a PARTIAL manifest (missing keys) is ignored — keeps the last good manifest', () => {
    let state = withCaps(CAPS_FOLLOWER);
    state = tasksReducer(state, setTaskStatus({ capabilities: { recordable: true } }));
    expect(state.taskStatus.capabilities).toEqual(CAPS_FOLLOWER);
  });

  it('non-boolean capability values are ignored', () => {
    let state = withCaps(CAPS_FOLLOWER);
    state = tasksReducer(
      state,
      setTaskStatus({ capabilities: { ...CAPS_FULL, recordable: 'yes' } })
    );
    expect(state.taskStatus.capabilities).toEqual(CAPS_FOLLOWER);
  });

  it('a partial/invalid manifest never seeds caps from the initial null either', () => {
    const state = tasksReducer(init(), setTaskStatus({ capabilities: { recordable: false } }));
    expect(state.taskStatus.capabilities).toBe(null);
  });

  it('a NEW complete manifest replaces the previous one', () => {
    let state = withCaps(CAPS_FOLLOWER);
    state = tasksReducer(state, setTaskStatus({ capabilities: CAPS_FULL }));
    expect(state.taskStatus.capabilities).toEqual(CAPS_FULL);
  });
});

describe('isValidCapabilities — the shared manifest validator', () => {
  it('accepts the complete six-boolean manifest (extra keys tolerated)', () => {
    expect(isValidCapabilities(CAPS_FULL)).toBe(true);
    expect(isValidCapabilities({ ...CAPS_FULL, future_cap: true })).toBe(true);
  });

  it('rejects null / non-objects / arrays / partial / non-boolean', () => {
    expect(isValidCapabilities(null)).toBe(false);
    expect(isValidCapabilities(undefined)).toBe(false);
    expect(isValidCapabilities('caps')).toBe(false);
    expect(isValidCapabilities([])).toBe(false);
    expect(isValidCapabilities({})).toBe(false);
    expect(isValidCapabilities({ recordable: true })).toBe(false);
    expect(isValidCapabilities({ ...CAPS_FULL, inferable: 1 })).toBe(false);
  });
});

describe('taskSlice clearCapabilities — Jetson release reset (audit fix 1b)', () => {
  it('nulls capabilities + robotProfile but keeps robotType', () => {
    let state = tasksReducer(
      init(),
      setTaskStatus({ robotType: 'omx_f', robotProfile: 'omx_follower', capabilities: CAPS_FOLLOWER })
    );
    state = tasksReducer(state, clearCapabilities());
    // Null caps = "unknown" — the nav capability filter hides NOTHING until
    // the local rig's idle identity tick re-delivers a manifest.
    expect(state.taskStatus.capabilities).toBe(null);
    expect(state.taskStatus.robotProfile).toBe('');
    expect(state.taskStatus.robotType).toBe('omx_f');
  });

  it('leaves the rest of taskStatus untouched', () => {
    let state = tasksReducer(init(), setTaskStatus({ capabilities: CAPS_FULL, usedCpu: 42 }));
    state = tasksReducer(state, clearCapabilities());
    expect(state.taskStatus.usedCpu).toBe(42);
  });
});
