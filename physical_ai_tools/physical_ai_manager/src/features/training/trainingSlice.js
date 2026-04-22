/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Author: Kiwoong Park
 */

import { createSlice } from '@reduxjs/toolkit';

const savedTrainingInfo = (() => {
  try {
    const raw = localStorage.getItem('edubotics_trainingInfo');
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    // Validate shape — if old localStorage has missing keys, discard it
    if (typeof parsed !== 'object' || !('seed' in parsed) || !('steps' in parsed)) return null;
    return parsed;
  } catch { return null; }
})();

const defaultTrainingInfo = {
  datasetRepoId: undefined,
  policyType: undefined,
  outputFolderName: undefined,
  seed: 1000,
  numWorkers: 2,
  batchSize: 8,
  steps: 50000,
  evalFreq: 0,
  logFreq: 200,
  saveFreq: 10000,
};

const initialState = {
  userList: [],
  datasetList: [],
  selectedUser: undefined,
  selectedDataset: undefined,
  policyList: [],
  modelWeightList: [],
  selectedModelWeight: undefined,
  isTraining: false,
  topicReceived: false,
  lastUpdate: Date.now(),

  // Training progress (from ROS topic, kept for compatibility)
  currentStep: 0,
  updateCounter: 0,
  currentLoss: undefined,

  // Counter incremented every time a new cloud training is started — MyModels
  // listens for changes and refetches the list immediately (no 5s wait).
  cloudJobsRefreshCounter: 0,

  // Which training job the live chart is focused on. `null` means
  // "auto-select": the chart picks the newest active job, falling back to the
  // most recently terminated job. Clicking a card in MyModels pins a
  // specific id here.
  selectedTrainingId: null,

  trainingInfo: savedTrainingInfo || defaultTrainingInfo,
};

const trainingSlice = createSlice({
  name: 'training',
  initialState,
  reducers: {
    setTrainingInfo: (state, action) => {
      state.trainingInfo = action.payload;
      try { localStorage.setItem('edubotics_trainingInfo', JSON.stringify(action.payload)); } catch {}
    },
    setTopicReceived: (state, action) => {
      state.topicReceived = action.payload;
    },
    setUserList: (state, action) => {
      state.userList = action.payload;
    },
    setDatasetList: (state, action) => {
      state.datasetList = action.payload;
    },
    setSelectedUser: (state, action) => {
      state.selectedUser = action.payload;
    },
    setSelectedDataset: (state, action) => {
      state.selectedDataset = action.payload;
    },
    setDatasetRepoId: (state, action) => {
      state.trainingInfo.datasetRepoId = action.payload;
    },
    setPolicyList: (state, action) => {
      state.policyList = action.payload;
    },
    selectPolicyType: (state, action) => {
      state.trainingInfo.policyType = action.payload;
    },
    setOutputFolderName: (state, action) => {
      state.trainingInfo.outputFolderName = action.payload;
    },
    setModelWeightList: (state, action) => {
      state.modelWeightList = action.payload;
    },
    setSelectedModelWeight: (state, action) => {
      state.selectedModelWeight = action.payload;
    },
    setIsTraining: (state, action) => {
      state.isTraining = action.payload;
    },
    setSeed: (state, action) => {
      state.trainingInfo.seed = action.payload;
    },
    setNumWorkers: (state, action) => {
      state.trainingInfo.numWorkers = action.payload;
    },
    setBatchSize: (state, action) => {
      state.trainingInfo.batchSize = action.payload;
    },
    setSteps: (state, action) => {
      state.trainingInfo.steps = action.payload;
    },
    setEvalFreq: (state, action) => {
      state.trainingInfo.evalFreq = action.payload;
    },
    setLogFreq: (state, action) => {
      state.trainingInfo.logFreq = action.payload;
    },
    setSaveFreq: (state, action) => {
      state.trainingInfo.saveFreq = action.payload;
    },
    setDefaultTrainingInfo: (state) => {
      state.trainingInfo = {
        ...state.trainingInfo,
        seed: 1000,
        numWorkers: 4,
        batchSize: 8,
        steps: 100000,
        evalFreq: 20000,
        logFreq: 200,
        saveFreq: 20000,
      };
    },
    setCurrentStep: (state, action) => {
      state.currentStep = action.payload;
      state.updateCounter++;
    },
    setLastUpdate: (state, action) => {
      state.lastUpdate = action.payload;
    },
    setUpdateCounter: (state, action) => {
      state.updateCounter = action.payload;
    },
    setCurrentLoss: (state, action) => {
      state.currentLoss = action.payload;
    },
    resetTrainingProgress: (state) => {
      state.currentStep = 0;
      state.currentLoss = null;
      state.updateCounter = 0;
    },
    triggerCloudJobsRefresh: (state) => {
      state.cloudJobsRefreshCounter += 1;
    },
    setSelectedTrainingId: (state, action) => {
      state.selectedTrainingId = action.payload;
    },
  },
});

export const {
  setTrainingInfo,
  setTopicReceived,
  setUserList,
  setDatasetList,
  setSelectedUser,
  setSelectedDataset,
  setDatasetRepoId,
  setPolicyList,
  selectPolicyType,
  setOutputFolderName,
  setModelWeightList,
  setSelectedModelWeight,
  setIsTraining,
  setSeed,
  setNumWorkers,
  setBatchSize,
  setSteps,
  setEvalFreq,
  setLogFreq,
  setSaveFreq,
  setDefaultTrainingInfo,
  setCurrentStep,
  setLastUpdate,
  setUpdateCounter,
  setCurrentLoss,
  resetTrainingProgress,
  triggerCloudJobsRefresh,
  setSelectedTrainingId,
} = trainingSlice.actions;

export default trainingSlice.reducer;
