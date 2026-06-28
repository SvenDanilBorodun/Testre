// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the robotType-wipe guard in setTaskStatus: an idle / post-restart
// /task/status tick (robot_type='') must NEVER clobber the student's selected
// robot type in Redux. Before the fix, the unconditional spread wiped it to ''
// permanently (no steady idle status re-set it), which forced a manual
// re-select on the Start page AND silently defeated useRobotTypeRehydrate
// (which reads this Redux value). Mirrors the adopt-only-when-non-empty guard
// userId already has.

import tasksReducer, {
  setTaskStatus,
  selectRobotType,
} from '../taskSlice';

function init() {
  return tasksReducer(undefined, { type: '@@INIT' });
}

function withRobot(type = 'omx') {
  return tasksReducer(init(), selectRobotType(type));
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
