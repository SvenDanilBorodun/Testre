/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  // Calibration wizard state
  calibState: 'idle',
  currentStep: 'gripper_intrinsic',
  framesCaptured: 0,
  framesRequired: 12,
  lastViewRms: null,
  methodDisagreement: null,
  calibError: null,
  hasIntrinsicGripper: false,
  hasIntrinsicScene: false,
  hasHandeyeGripper: false,
  hasHandeyeScene: false,
  hasColorProfile: false,
  // Phase-2 calibration UX additions
  // 16-cell coverage map: array of length 16, each cell is the count
  // of captured frames whose board centroid landed in that cell.
  coverageMosaic: Array(16).fill(0),
  // Per-frame quality history: ['good' | 'ok' | 'poor'] in capture order.
  qualityHistory: [],
  // ChArUco corner preview (live overlay during capture)
  charucoPreview: { detected: false, corners: [] },
  // "Jetzt prüfen" reprojection result, set after /calibration/verify.
  verifyResult: null,
  // Calibration history — most recent N saved calibrations per camera.
  calibHistory: [],

  // Workflow runtime state
  runState: 'idle',
  currentBlockId: null,
  phase: '',
  progress: 0,
  log: [],
  // Each detection: {cx, cy, w, h, label, confidence}. The pre-audit
  // shape used parallel detections[] + detectionLabels[] arrays driven
  // by a geometry_msgs/Point that didn't carry width/height; the
  // Detection.msg switch (audit §1.6) collapsed them.
  detections: [],
  workflowError: null,
  // Phase-2 debugger state
  paused: false,
  breakpoints: [],     // array of block IDs
  debuggerVisible: false,
  debuggerWarnings: [], // per-block IK pre-check warnings: [{block_id, message}]
  // SensorSnapshot.msg payload, refreshed @ 5 Hz
  sensorSnapshot: {
    follower_joints: [],
    gripper_opening: 0,
    visible_apriltag_ids: [],
    color_counts: [0, 0, 0, 0],
    visible_object_classes: [],
    ts: 0,
  },
  // Variable inspector — Map-like {name: {value, ts}}
  variables: {},

  // Editor state
  selectedWorkflowId: null,
  unsavedBlocklyJson: null,
  lastSavedAt: null,
  // Phase-3 tutorial / skillmap state
  activeTutorialId: null,
  activeTutorialStep: 0,
  // restrictedBlocks: array of block type strings, or null for unrestricted
  restrictedBlocks: null,
  // Phase-3 cloud-vision toggle. When true, open-vocab detect blocks
  // can burst to OWLv2 on Modal for German prompts not in the local
  // synonym dict. False keeps the workflow offline-only.
  // Audit F29: persist across page reloads so the student doesn't
  // re-enable on every refresh.
  cloudVisionEnabled: _readCloudVisionPersisted(),
};

function _readCloudVisionPersisted() {
  try {
    if (typeof localStorage !== 'undefined') {
      return localStorage.getItem('edubotics_cloud_vision') === 'true';
    }
  } catch (_) {
    /* private mode / disabled storage */
  }
  return false;
}

function classifyQuality(score) {
  if (score === undefined || score === null) return 'ok';
  if (score >= 3) return 'good';
  if (score >= 2) return 'ok';
  return 'poor';
}

const workshopSlice = createSlice({
  name: 'workshop',
  initialState,
  reducers: {
    setCalibState: (state, action) => {
      state.calibState = action.payload;
    },
    setCurrentStep: (state, action) => {
      state.currentStep = action.payload;
    },
    setCalibProgress: (state, action) => {
      const { framesCaptured, framesRequired, lastViewRms } = action.payload;
      if (framesCaptured !== undefined) state.framesCaptured = framesCaptured;
      if (framesRequired !== undefined) state.framesRequired = framesRequired;
      if (lastViewRms !== undefined) state.lastViewRms = lastViewRms;
    },
    setMethodDisagreement: (state, action) => {
      state.methodDisagreement = action.payload;
    },
    setCalibError: (state, action) => {
      state.calibError = action.payload;
    },
    markStepComplete: (state, action) => {
      const step = action.payload;
      if (step === 'gripper_intrinsic') state.hasIntrinsicGripper = true;
      else if (step === 'scene_intrinsic') state.hasIntrinsicScene = true;
      else if (step === 'gripper_handeye') state.hasHandeyeGripper = true;
      else if (step === 'scene_handeye') state.hasHandeyeScene = true;
      else if (step === 'color_profile') state.hasColorProfile = true;
    },
    setCalibrationStatus: (state, action) => {
      // Hydrate per-step badges from /calibration/status so the wizard
      // doesn't make the student redo intrinsic captures after every page
      // reload. Payload mirrors the CalibrationStatus.srv response.
      const {
        has_gripper_intrinsics,
        has_scene_intrinsics,
        has_gripper_handeye,
        has_scene_handeye,
        has_color_profile,
      } = action.payload || {};
      if (has_gripper_intrinsics !== undefined) state.hasIntrinsicGripper = !!has_gripper_intrinsics;
      if (has_scene_intrinsics !== undefined) state.hasIntrinsicScene = !!has_scene_intrinsics;
      if (has_gripper_handeye !== undefined) state.hasHandeyeGripper = !!has_gripper_handeye;
      if (has_scene_handeye !== undefined) state.hasHandeyeScene = !!has_scene_handeye;
      if (has_color_profile !== undefined) state.hasColorProfile = !!has_color_profile;
    },
    resetCalibProgress: (state) => {
      state.framesCaptured = 0;
      state.lastViewRms = null;
      state.methodDisagreement = null;
      state.calibError = null;
      state.coverageMosaic = Array(16).fill(0);
      state.qualityHistory = [];
      state.verifyResult = null;
    },
    requestRecalibration: (state) => {
      // Audit U3: drop every per-step "done" flag so the WorkshopPage's
      // `calibrated` selector flips false and the wizard re-mounts.
      // The on-host YAMLs under /root/.cache/edubotics/calibration/
      // are untouched — re-running step 1 just overwrites them. To
      // wipe them entirely the student would need `docker volume rm
      // edubotics_calib` (separate operator path; intentional, since
      // we never want to delete calibration without explicit intent).
      state.hasIntrinsicGripper = false;
      state.hasIntrinsicScene = false;
      state.hasHandeyeGripper = false;
      state.hasHandeyeScene = false;
      state.hasColorProfile = false;
      state.currentStep = 'gripper_intrinsic';
      state.framesCaptured = 0;
      state.methodDisagreement = null;
      state.calibError = null;
      state.coverageMosaic = Array(16).fill(0);
      state.qualityHistory = [];
      state.verifyResult = null;
    },
    addCoverageCell: (state, action) => {
      const { cell, quality } = action.payload || {};
      if (typeof cell === 'number' && cell >= 0 && cell < 16) {
        state.coverageMosaic[cell] = (state.coverageMosaic[cell] || 0) + 1;
      }
      if (quality !== undefined && quality !== null) {
        state.qualityHistory.push(classifyQuality(quality));
      }
    },
    setCharucoPreview: (state, action) => {
      const { detected, corners } = action.payload || {};
      state.charucoPreview = {
        detected: !!detected,
        corners: Array.isArray(corners) ? corners : [],
      };
    },
    setVerifyResult: (state, action) => {
      state.verifyResult = action.payload || null;
    },
    setCalibHistory: (state, action) => {
      state.calibHistory = Array.isArray(action.payload) ? action.payload : [];
    },

    setRunState: (state, action) => {
      state.runState = action.payload;
      if (action.payload === 'running') state.paused = false;
    },
    setPaused: (state, action) => {
      state.paused = !!action.payload;
    },
    setWorkflowStatus: (state, action) => {
      const { current_block_id, phase, progress, error, log_message } = action.payload;
      if (current_block_id !== undefined) state.currentBlockId = current_block_id;
      if (phase !== undefined) state.phase = phase;
      if (progress !== undefined) state.progress = progress;
      if (error !== undefined) state.workflowError = error;
      if (log_message) state.log.push({ ts: Date.now(), text: log_message });
      if (state.log.length > 200) state.log = state.log.slice(-200);
    },
    setDetections: (state, action) => {
      state.detections = action.payload.detections || [];
    },
    clearWorkflowLog: (state) => {
      state.log = [];
    },

    // Phase-2 debugger reducers
    toggleDebugger: (state) => {
      state.debuggerVisible = !state.debuggerVisible;
    },
    setDebuggerVisible: (state, action) => {
      state.debuggerVisible = !!action.payload;
    },
    setSensorSnapshot: (state, action) => {
      // Shallow-merge over the previous snapshot so sparse messages
      // (or future field additions) don't erase fields like
      // `gripper_opening` that this tick happens not to populate.
      // Audit round-3 §AV.
      const incoming = action.payload || {};
      state.sensorSnapshot = {
        ...(state.sensorSnapshot || {}),
        ...incoming,
        ts: Date.now(),
      };
    },
    setVariable: (state, action) => {
      // Audit round-3 §BJ — cap the number of distinct variable names
      // a workflow can pin into Redux state. A loop emitting 10 k
      // unique [VAR:i=N] sentinels would otherwise grow this slice
      // unboundedly. FIFO-evict the oldest by ts when the cap is hit.
      const VAR_LIMIT = 256;
      const NAME_RE = /^[A-Za-zÄÖÜäöüß_][A-Za-zÄÖÜäöüß0-9_]{0,63}$/;
      const { name, value } = action.payload || {};
      if (typeof name !== 'string' || !name) return;
      if (name === '__proto__' || name === 'constructor' || name === 'prototype') return;
      if (!NAME_RE.test(name)) return;
      const ts = Date.now();
      // If we're at the cap and adding a NEW name, evict the oldest
      // entry. Existing-name overwrites are free.
      const isNew = !(name in state.variables);
      if (isNew) {
        const keys = Object.keys(state.variables);
        if (keys.length >= VAR_LIMIT) {
          let oldestKey = null;
          let oldestTs = Infinity;
          for (const k of keys) {
            const t = state.variables[k]?.ts ?? 0;
            if (t < oldestTs) {
              oldestTs = t;
              oldestKey = k;
            }
          }
          if (oldestKey) delete state.variables[oldestKey];
        }
      }
      state.variables[name] = { value, ts };
    },
    clearVariables: (state) => {
      state.variables = {};
    },
    addBreakpoint: (state, action) => {
      const id = action.payload;
      if (!id || state.breakpoints.includes(id)) return;
      state.breakpoints.push(id);
    },
    removeBreakpoint: (state, action) => {
      const id = action.payload;
      state.breakpoints = state.breakpoints.filter((b) => b !== id);
    },
    clearBreakpoints: (state) => {
      state.breakpoints = [];
    },
    setDebuggerWarnings: (state, action) => {
      state.debuggerWarnings = Array.isArray(action.payload) ? action.payload : [];
    },

    setSelectedWorkflowId: (state, action) => {
      state.selectedWorkflowId = action.payload;
    },
    setUnsavedBlocklyJson: (state, action) => {
      state.unsavedBlocklyJson = action.payload;
    },
    markWorkflowSaved: (state) => {
      state.lastSavedAt = Date.now();
      state.unsavedBlocklyJson = null;
    },

    // Phase-3 tutorial reducers
    setActiveTutorial: (state, action) => {
      const { id, step } = action.payload || {};
      state.activeTutorialId = id || null;
      state.activeTutorialStep = typeof step === 'number' ? step : 0;
    },
    advanceTutorialStep: (state) => {
      state.activeTutorialStep += 1;
    },
    setRestrictedBlocks: (state, action) => {
      state.restrictedBlocks = Array.isArray(action.payload)
        ? action.payload
        : null;
    },
    setCloudVisionEnabled: (state, action) => {
      const next = !!action.payload;
      state.cloudVisionEnabled = next;
      // Audit F29: mirror to localStorage so page reloads keep the
      // toggle in sync. Inside the reducer rather than a thunk
      // because the toggle is only flipped from one place
      // (RunControls.jsx) and the persistence is the user's
      // expectation, not optional.
      try {
        if (typeof localStorage !== 'undefined') {
          localStorage.setItem('edubotics_cloud_vision', String(next));
        }
      } catch (_) {
        /* private mode / disabled storage */
      }
    },
  },
});

export const {
  setCalibState,
  setCurrentStep,
  setCalibProgress,
  setMethodDisagreement,
  setCalibError,
  markStepComplete,
  setCalibrationStatus,
  resetCalibProgress,
  requestRecalibration,
  addCoverageCell,
  setCharucoPreview,
  setVerifyResult,
  setCalibHistory,
  setRunState,
  setPaused,
  setWorkflowStatus,
  setDetections,
  clearWorkflowLog,
  toggleDebugger,
  setDebuggerVisible,
  setSensorSnapshot,
  setVariable,
  clearVariables,
  addBreakpoint,
  removeBreakpoint,
  clearBreakpoints,
  setDebuggerWarnings,
  setSelectedWorkflowId,
  setUnsavedBlocklyJson,
  markWorkflowSaved,
  setActiveTutorial,
  advanceTutorialStep,
  setRestrictedBlocks,
  setCloudVisionEnabled,
} = workshopSlice.actions;

export default workshopSlice.reducer;
