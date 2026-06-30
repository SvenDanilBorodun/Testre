// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Pure reducer tests for live run-highlighting (2026-06-30).
//
// Regression: Roboter Studio blocks didn't light up live while the workflow
// ran — the highlight only appeared while stopped/in the debugger. Root cause:
// subscribeToWorkflowStatus dispatches setWorkflowStatus(...) THEN
// setRunState('running') on EVERY per-block 'running' WorkflowStatus message,
// and setRunState's 'running' branch unconditionally nulled currentBlockId. So
// every running tick wiped the block id setWorkflowStatus had just adopted, and
// RunControls' highlight effect (runState==='running' && currentBlockId) saw a
// null id during execution. Only 'done'/'paused' ticks (no setRunState) kept
// the highlight, which is why it showed when stopped/in debug.
//
// The fix gates the clear on the idle→running transition (preserving #L1: no
// flash of the prior run's block) while letting the live id survive per tick.

import reducer, { setRunState, setWorkflowStatus } from '../workshopSlice';

const initial = reducer(undefined, { type: '@@INIT' });

// Reproduce ONE per-block status message exactly as the subscription handles
// it: setWorkflowStatus first (adopts the block id), then setRunState('running')
// for phase==='running'.
const applyRunningTick = (state, blockId) => {
  let s = reducer(state, setWorkflowStatus({
    current_block_id: blockId,
    phase: 'running',
    progress: 0,
    error: '',
    log_message: '',
  }));
  s = reducer(s, setRunState('running'));
  return s;
};

describe('workshopSlice live run-highlighting', () => {
  it('keeps the current block id through every per-block running tick', () => {
    // Start a run: handleStart dispatches setRunState('running') from idle.
    let state = reducer({ ...initial, runState: 'idle', currentBlockId: 'old-run-block' },
      setRunState('running'));
    // #L1: the idle→running transition clears the prior run's stale block id.
    expect(state.currentBlockId).toBeNull();
    expect(state.runState).toBe('running');

    // First moving block.
    state = applyRunningTick(state, 'block-1');
    expect(state.currentBlockId).toBe('block-1');
    expect(state.runState).toBe('running');

    // Its 'done' tick (no setRunState) keeps the id, then the next block's
    // running tick must ADVANCE the highlight — not wipe it to null.
    state = reducer(state, setWorkflowStatus({
      current_block_id: 'block-1', phase: 'done', progress: 0, error: '', log_message: '',
    }));
    expect(state.currentBlockId).toBe('block-1');

    state = applyRunningTick(state, 'block-2');
    expect(state.currentBlockId).toBe('block-2');
    expect(state.runState).toBe('running');
  });

  it('clears the highlight again on the next idle→running transition', () => {
    let state = applyRunningTick({ ...initial, runState: 'running' }, 'block-9');
    expect(state.currentBlockId).toBe('block-9');

    // Run ends.
    state = reducer(state, setRunState('finished'));
    expect(state.currentBlockId).toBeNull();
    expect(state.runState).toBe('idle');

    // A fresh run from idle clears any leftover id (defence-in-depth for #L1).
    state = reducer({ ...state, currentBlockId: 'leftover' }, setRunState('running'));
    expect(state.currentBlockId).toBeNull();
  });
});
