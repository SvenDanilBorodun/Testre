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

  // Workflow runtime state (used in PR4)
  runState: 'idle',
  currentBlockId: null,
  phase: '',
  progress: 0,
  log: [],
  detections: [],
  detectionLabels: [],
  workflowError: null,

  // Editor state
  selectedWorkflowId: null,
  unsavedBlocklyJson: null,
  lastSavedAt: null,
};

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
    resetCalibProgress: (state) => {
      state.framesCaptured = 0;
      state.lastViewRms = null;
      state.methodDisagreement = null;
      state.calibError = null;
    },

    setRunState: (state, action) => {
      state.runState = action.payload;
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
      state.detectionLabels = action.payload.labels || [];
    },
    clearWorkflowLog: (state) => {
      state.log = [];
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
  },
});

export const {
  setCalibState,
  setCurrentStep,
  setCalibProgress,
  setMethodDisagreement,
  setCalibError,
  markStepComplete,
  resetCalibProgress,
  setRunState,
  setWorkflowStatus,
  setDetections,
  clearWorkflowLog,
  setSelectedWorkflowId,
  setUnsavedBlocklyJson,
  markWorkflowSaved,
} = workshopSlice.actions;

export default workshopSlice.reducer;
