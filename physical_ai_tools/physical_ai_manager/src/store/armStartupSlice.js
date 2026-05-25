// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// On-demand robot-arm startup state. Mirrors the JSON payload published by
// the container's arm_startup_node.py on /edubotics/arm_state. `armReady` is
// the single gate the student app uses to unlock Aufnahme / Inferenz /
// Roboter Studio — it flips true ONLY when the follower↔leader sync verifies,
// so a structurally mis-synced arm can never reach recording.

import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  // 'idle' | 'homing' | 'reading_leader' | 'syncing' | 'verifying' | 'ready' | 'error'
  phase: 'idle',
  percent: 0,
  message: '',
  error: '',
  perJointErr: [],
  armReady: false,
  // True while a startup sequence is in flight (button shows a spinner and
  // is disabled). Derived from phase so we don't depend on click-side state.
  busy: false,
  lastUpdate: 0,
};

const IN_FLIGHT = ['homing', 'reading_leader', 'syncing', 'verifying'];

const armStartupSlice = createSlice({
  name: 'armStartup',
  initialState,
  reducers: {
    setArmStartupStatus: (state, action) => {
      const p = action.payload || {};
      state.phase = p.phase || 'idle';
      state.percent = typeof p.percent === 'number' ? p.percent : 0;
      state.message = p.message || '';
      state.error = p.error || '';
      state.perJointErr = Array.isArray(p.perJointErr) ? p.perJointErr : [];
      state.armReady = state.phase === 'ready' && !state.error;
      state.busy = IN_FLIGHT.includes(state.phase);
      state.lastUpdate = Date.now();
    },
    // Optimistic local flip the moment the student clicks, before the first
    // /edubotics/arm_state message arrives — keeps the button responsive.
    markArmStarting: (state) => {
      state.phase = 'homing';
      state.percent = 0;
      state.error = '';
      state.perJointErr = [];
      state.armReady = false;
      state.busy = true;
      state.lastUpdate = Date.now();
    },
    resetArmStartup: () => ({ ...initialState }),
  },
});

export const { setArmStartupStatus, markArmStarting, resetArmStartup } =
  armStartupSlice.actions;
export default armStartupSlice.reducer;
