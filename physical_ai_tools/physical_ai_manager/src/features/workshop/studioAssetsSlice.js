/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Roboter Studio „Sammlung" state: the open workflow's recording list, the
// Sammlung drawer, the simulator preview in flight and its last results, the
// Vormachen overlay request, the card highlight and a split recording rename.
//
// Kept apart from `workshopSlice` on purpose: that slice is the RUN and the
// rig's calibration; this one is the student's ASSETS around one document. It
// answers `session/signedOut` itself (the eighth slice to do so) and resets
// whenever the open document changes identity.

import { createAsyncThunk, createSlice } from '@reduxjs/toolkit';
import * as workflowApi from '../../services/workflowApi';
import { signedOut } from '../session/sessionActions';
import {
  markWorkflowSaved,
  setRunState,
  setSelectedWorkflowId,
  setWorkflowStatus,
} from './workshopSlice';
import { previewMessageDe } from '../../utils/simPreview';

const PREVIEW_TEMPOS = [0.5, 1.0, 2.0];
const TERMINAL_PHASES = ['finished', 'stopped', 'error'];

const initialState = {
  trajectories: { workflowId: null, status: 'none', items: [], error: null, fetchedAt: 0 },
  // status: 'none' (no workflow id) | 'idle' | 'loading' | 'ready' | 'error'
  // item: { id, name, point_count, duration_s, fps, robot_profile, created_at, updated_at }
  drawer: { open: false, tab: 'aufnahmen', focusId: null, previewTempo: 1.0 },
  preview: null,
  // { key, kind, name, workflowId, startedAt, sawOwnStatus, lastError, unreachable, unreachableMessage }
  lastPreviewResult: {},
  // key -> { status: 'ok'|'stopped'|'refused', message, unreachable, unreachableMessage, ts }
  teach: { open: false, requested: null, mode: null, focus: null },
  highlight: null,
  renameSplit: null,
};

// A fresh deep copy — Immer never hands out the module constant for mutation,
// but a returned state object must not share nested objects with it either.
const freshState = () => JSON.parse(JSON.stringify(initialState));

export const fetchTrajectories = createAsyncThunk(
  'studioAssets/fetchTrajectories',
  async ({ accessToken, workflowId }) => workflowApi.listTrajectories(accessToken, workflowId),
);

const str = (v) => (typeof v === 'string' ? v : '');
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

function normalizeTrajectoryItems(raw) {
  return (Array.isArray(raw) ? raw : [])
    .filter((it) => it && typeof it === 'object' && typeof it.id === 'string' && it.id)
    .map((it) => ({
      id: it.id,
      name: str(it.name),
      point_count: num(it.point_count),
      duration_s: num(it.duration_s),
      fps: num(it.fps),
      robot_profile: typeof it.robot_profile === 'string' ? it.robot_profile : null,
      created_at: str(it.created_at),
      updated_at: str(it.updated_at),
    }));
}

// A preview ends here, exactly once: its result is recorded under its key and
// the in-flight marker is dropped.
function finalizePreview(state, phase) {
  const p = state.preview;
  if (!p) return;
  let status = 'ok';
  if (phase === 'stopped') status = 'stopped';
  else if (phase === 'error' || p.lastError) status = 'refused';
  state.lastPreviewResult[p.key] = {
    status,
    message: p.lastError || '',
    unreachable: !!p.unreachable,
    unreachableMessage: p.unreachableMessage || '',
    ts: Date.now(),
  };
  state.preview = null;
}

const studioAssetsSlice = createSlice({
  name: 'studioAssets',
  initialState,
  reducers: {
    openDrawer: (state, action) => {
      const { tab, focusId } = action.payload || {};
      state.drawer.open = true;
      if (typeof tab === 'string' && tab) state.drawer.tab = tab;
      state.drawer.focusId = focusId === undefined ? null : focusId;
    },
    closeDrawer: (state) => {
      state.drawer.open = false;
    },
    setDrawerTab: (state, action) => {
      if (typeof action.payload === 'string' && action.payload) state.drawer.tab = action.payload;
    },
    setDrawerFocus: (state, action) => {
      state.drawer.focusId = action.payload === undefined ? null : action.payload;
    },
    setPreviewTempo: (state, action) => {
      const v = Number(action.payload);
      // Clamped to the three run-bar tempos: the nearest one wins.
      state.drawer.previewTempo = PREVIEW_TEMPOS.reduce(
        (best, t) => (Math.abs(t - v) < Math.abs(best - v) ? t : best),
        1.0,
      );
    },
    previewStarted: (state, action) => {
      const { key, kind, name, workflowId } = action.payload || {};
      state.preview = {
        key,
        kind,
        name,
        workflowId,
        startedAt: Date.now(),
        sawOwnStatus: false,
        lastError: '',
        unreachable: false,
        unreachableMessage: '',
      };
    },
    previewFailed: (state, action) => {
      const { key, message } = action.payload || {};
      state.lastPreviewResult[key] = {
        status: 'refused',
        message: previewMessageDe(message),
        unreachable: false,
        unreachableMessage: '',
        ts: Date.now(),
      };
      state.preview = null;
    },
    previewUnreachable: (state, action) => {
      if (!state.preview) return;
      const { message } = action.payload || {};
      state.preview.unreachable = true;
      state.preview.unreachableMessage = typeof message === 'string' ? message : '';
    },
    requestTeach: (state, action) => {
      const { focus } = action.payload || {};
      state.teach.requested = { focus: focus || null, token: Date.now() };
    },
    teachOpened: (state, action) => {
      const { mode, focus } = action.payload || {};
      state.teach.open = true;
      state.teach.mode = mode || null;
      state.teach.focus = focus || null;
      state.teach.requested = null;
    },
    teachRequestHandled: (state) => {
      state.teach.requested = null;
    },
    teachClosed: (state) => {
      state.teach.open = false;
      state.teach.mode = null;
      state.teach.focus = null;
    },
    setHighlight: (state, action) => {
      state.highlight = action.payload || null;
    },
    setRenameSplit: (state, action) => {
      state.renameSplit = action.payload || null;
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(signedOut, () => freshState())
      .addCase(setSelectedWorkflowId, (state, action) => {
        const next = action.payload === undefined ? null : action.payload;
        // Same document (re-pick, or the page's own create stamping the id the
        // list already belongs to): nothing is stale, nothing is cleared.
        if (next === state.trajectories.workflowId) return;
        state.trajectories = {
          workflowId: next,
          status: next ? 'idle' : 'none',
          items: [],
          error: null,
          fetchedAt: 0,
        };
        state.drawer.focusId = null;
        state.lastPreviewResult = {};
        state.highlight = null;
        state.renameSplit = null;
      })
      // The saved document now carries the cloud name (§ WP6 rename split).
      .addCase(markWorkflowSaved, (state) => {
        state.renameSplit = null;
      })
      // A preview is finalized ONLY by a status carrying its OWN workflow_id.
      // `/workflow/status` also delivers a previous run's late terminal, which
      // must never record this preview's result.
      .addCase(setWorkflowStatus, (state, action) => {
        const p = state.preview;
        const payload = action.payload || {};
        if (!p || !payload.workflow_id || payload.workflow_id !== p.workflowId) return;
        p.sawOwnStatus = true;
        if (typeof payload.error === 'string' && payload.error) {
          p.lastError = previewMessageDe(payload.error);
        }
        if (TERMINAL_PHASES.includes(payload.phase)) finalizePreview(state, payload.phase);
      })
      // A terminal run state (the hook's echo of a terminal status, or the local
      // Stopp) finalizes only a preview that has already seen its own status.
      .addCase(setRunState, (state, action) => {
        if (!TERMINAL_PHASES.includes(action.payload)) return;
        if (state.preview && state.preview.sawOwnStatus) finalizePreview(state, action.payload);
      })
      .addCase(fetchTrajectories.pending, (state, action) => {
        if (action.meta.arg.workflowId !== state.trajectories.workflowId) return;
        state.trajectories.status = 'loading';
      })
      .addCase(fetchTrajectories.fulfilled, (state, action) => {
        if (action.meta.arg.workflowId !== state.trajectories.workflowId) return;
        state.trajectories.status = 'ready';
        state.trajectories.items = normalizeTrajectoryItems(action.payload);
        state.trajectories.error = null;
        state.trajectories.fetchedAt = Date.now();
      })
      .addCase(fetchTrajectories.rejected, (state, action) => {
        if (action.meta.arg.workflowId !== state.trajectories.workflowId) return;
        state.trajectories.status = 'error';
        state.trajectories.error = (action.error && action.error.message) || '';
      });
  },
});

export const {
  openDrawer,
  closeDrawer,
  setDrawerTab,
  setDrawerFocus,
  setPreviewTempo,
  previewStarted,
  previewFailed,
  previewUnreachable,
  requestTeach,
  teachOpened,
  teachRequestHandled,
  teachClosed,
  setHighlight,
  setRenameSplit,
} = studioAssetsSlice.actions;

// Null-safe, per field: page tests mock partial states without this slice, or
// with only one of its fields.
const field = (s, key) => (
  s && s.studioAssets && s.studioAssets[key] !== undefined ? s.studioAssets[key] : initialState[key]
);
export const selectTrajectoryList = (s) => field(s, 'trajectories');
export const selectPreview = (s) => field(s, 'preview');
export const selectPreviewActive = (s) => !!field(s, 'preview');
export const selectTeachOpen = (s) => !!(field(s, 'teach') || {}).open;
export const selectTeachState = (s) => field(s, 'teach');
export const selectDrawer = (s) => field(s, 'drawer');
export const selectLastPreviewResult = (s) => field(s, 'lastPreviewResult');
export const selectHighlight = (s) => field(s, 'highlight');
export const selectRenameSplit = (s) => field(s, 'renameSplit');

export default studioAssetsSlice.reducer;
