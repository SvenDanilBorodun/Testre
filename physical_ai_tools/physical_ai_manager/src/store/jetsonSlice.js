// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import { createSlice } from '@reduxjs/toolkit';

// State machine for the classroom Jetson connection. Lives in Redux so
// the sidebar filter in StudentApp.js (hide Aufnahme + Roboter Studio
// when status === 'connected') and the JetsonAvailabilityChip can both
// read the same source of truth.
//
// Transitions:
//   unknown   → no_jetson  (GET /classrooms/{id}/jetson returns 404)
//   unknown   → available  (returns 200 with current_owner_user_id NULL)
//   unknown   → busy       (returns 200 with current_owner_user_id != me)
//   available → claiming   (user clicks "Verbinde")
//   claiming  → connected  (POST /jetson/{id}/claim returns 200)
//   claiming  → busy       (POST returns 409 P0030)
//   connected → disconnecting (user clicks "Trennen")
//   disconnecting → no_jetson_or_available (after the wipe completes server-side)
//   any       → error      (network blip / unexpected response)
const initialState = {
  status: 'unknown',
  jetsonId: null,
  mdnsName: null,
  lanIp: null,
  ownerUserId: null,
  ownerUsername: null,
  ownerFullName: null,
  online: false,
  error: null,
  // v2.3.0: transient heartbeat failure indicator. True after 2
  // consecutive non-410 heartbeat errors (network blip); the chip
  // shows "Verbindung wird wiederhergestellt…" so the student knows
  // the connection is being checked rather than silently dead. Reset
  // to false on a successful heartbeat. Distinct from `status === 'error'`
  // which is a terminal state requiring user re-action.
  heartbeatTransient: false,
};

const jetsonSlice = createSlice({
  name: 'jetson',
  initialState,
  reducers: {
    setNoJetson: (state) => {
      Object.assign(state, initialState, { status: 'no_jetson' });
    },
    setJetsonInfo: (state, action) => {
      // From GET /classrooms/{id}/jetson — full info, status derived
      // from the owner field. Caller decides what to do with it.
      const info = action.payload || {};
      state.jetsonId = info.jetson_id ?? null;
      state.mdnsName = info.mdns_name ?? null;
      state.lanIp = info.lan_ip ?? null;
      state.ownerUserId = info.current_owner_user_id ?? null;
      state.ownerUsername = info.current_owner_username ?? null;
      state.ownerFullName = info.current_owner_full_name ?? null;
      state.online = !!info.online;
      state.error = null;
    },
    setJetsonStatus: (state, action) => {
      state.status = action.payload;
      if (action.payload !== 'error') {
        state.error = null;
      }
    },
    setJetsonError: (state, action) => {
      state.error = action.payload || null;
      state.status = 'error';
    },
    setHeartbeatTransient: (state, action) => {
      state.heartbeatTransient = !!action.payload;
    },
    clearJetson: (state) => {
      Object.assign(state, initialState);
    },
  },
});

export const {
  setNoJetson,
  setJetsonInfo,
  setJetsonStatus,
  setJetsonError,
  setHeartbeatTransient,
  clearJetson,
} = jetsonSlice.actions;

export default jetsonSlice.reducer;
