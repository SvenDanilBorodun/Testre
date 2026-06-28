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
import TaskPhase from '../../constants/taskPhases';

const savedRobotType = (() => {
  try { return localStorage.getItem('edubotics_robotType') || ''; }
  catch { return ''; }
})();

// Benutzer-ID (the HF account/org the student records under). Persisted like
// robotType so it survives a full page reload (e.g. the GUI WebView reload on
// restart), not just tab switches. `undefined` when never set, so the
// InfoPanel auto-select can still pick the first account on first run.
const savedUserId = (() => {
  try { return localStorage.getItem('edubotics_userId') || undefined; }
  catch { return undefined; }
})();

const initialState = {
  taskInfo: {
    taskName: '',
    taskType: '',
    taskInstruction: [],
    policyPath: '',
    recordInferenceMode: false,
    userId: savedUserId,
    fps: 30,
    tags: [],
    warmupTime: 5,
    episodeTime: 20,
    resetTime: 5,
    numEpisodes: 5,
    token: '',
    pushToHub: true,
    // User-selectable via the "Privater Modus" toggle (InfoPanel.js /
    // InferencePanel.js). Sent on the wire as TaskInfo.private_mode and
    // threaded through the server-side data_manager overlay →
    // HfApiWorker → create_repo(private=…). Defaults true so the
    // privacy-safe option is pre-selected and a fresh recording without
    // a deliberate toggle still uploads private (faces / classroom audio).
    privateMode: true,
    useOptimizedSave: true,
    recordRosBag2: false,
  },
  taskStatus: {
    robotType: savedRobotType,
    taskName: 'idle',
    running: false,
    phase: TaskPhase.READY,
    progress: 0,
    totalTime: 0,
    proceedTime: 0,
    currentEpisodeNumber: 0,
    currentScenarioNumber: 0,
    currentTaskInstruction: '',
    userId: '',
    usedStorageSize: 0,
    totalStorageSize: 0,
    usedCpu: 0,
    usedRamSize: 0,
    totalRamSize: 0,
    error: '',
    topicReceived: false,
  },
  availableRobots: [],
  availableCameras: [],
  policyList: [],
  datasetList: [],
  heartbeatStatus: 'disconnected',
  lastHeartbeatTime: 0,
  useMultiTaskMode: false,
  multiTaskIndex: undefined,
  // Teleop force/collision e-stop. Set active when /task/status reports one of the
  // collision phases; cleared when a non-collision status arrives. `stage` mirrors the
  // server's two-step recovery: 'stopped' (phase=COLLISION — arm halted in place, student
  // removes the obstacle), 'homing' (phase=COLLISION_HOMING — safe-home glide running),
  // 'homed' (phase=COLLISION_HOMED — step 2: match the leader, resume). Drives the
  // blocking CollisionModal.
  collision: {
    active: false,
    stage: 'stopped',
    message: '',
    // Per-joint |pos - home| (rad, joint1..joint5) for the homing strip;
    // [] when the server image predates the TaskStatus field.
    jointDistToHome: [],
  },
};

const taskSlice = createSlice({
  name: 'tasks',
  initialState,
  reducers: {
    setTaskInfo: (state, action) => {
      state.taskInfo = { ...state.taskInfo, ...action.payload };
      // Persist the Benutzer-ID like robotType so it survives a full reload.
      // Only on a truthy value — never clobber the saved id with '' (the
      // /task/status handler is also guarded not to send an empty userId).
      if (action.payload.userId) {
        try { localStorage.setItem('edubotics_userId', action.payload.userId); } catch {}
      }
    },
    resetTaskInfo: (state) => {
      state.taskInfo = initialState.taskInfo;
    },
    setTaskStatus: (state, action) => {
      // Never let an idle/post-restart /task/status tick (robot_type='') wipe
      // the student's selected robot type. The server reports an empty
      // robot_type in several ordinary situations — a node restart that lost
      // its in-RAM selection, the stale-recording-session notice, the bare
      // TaskStatus() error branches in the record/inference timer — and there is
      // NO steady idle status publisher to re-set it, so an unconditional spread
      // used to clobber taskStatus.robotType to '' permanently: only a manual
      // re-select or a full reload recovered it, and it silently defeated
      // useRobotTypeRehydrate (which reads THIS Redux value). Adopt robotType
      // only when non-empty — the same guard userId already has below.
      const { robotType, ...rest } = action.payload;
      state.taskStatus = { ...state.taskStatus, ...rest };
      if (robotType) {
        state.taskStatus.robotType = robotType;
        try { localStorage.setItem('edubotics_robotType', robotType); } catch {}
      }
    },
    selectRobotType: (state, action) => {
      state.taskStatus.robotType = action.payload;
      try { localStorage.setItem('edubotics_robotType', action.payload); } catch {}
    },
    resetTaskStatus: (state) => {
      state.taskStatus = initialState.taskStatus;
    },
    setTaskType: (state, action) => {
      state.taskInfo.taskType = action.payload;
    },
    setTaskInstruction: (state, action) => {
      state.taskInfo.taskInstruction = action.payload;
    },
    setPolicyPath: (state, action) => {
      state.taskInfo.policyPath = action.payload;
    },
    setRecordInferenceMode: (state, action) => {
      state.taskInfo.recordInferenceMode = action.payload;
    },
    addTag: (state, action) => {
      if (!state.taskInfo.tags.includes(action.payload)) {
        state.taskInfo.tags.push(action.payload);
      }
    },
    removeTag: (state, action) => {
      state.taskInfo.tags = state.taskInfo.tags.filter((tag) => tag !== action.payload);
    },
    removeAllTags: (state) => {
      state.taskInfo.tags = [];
    },
    setHeartbeatStatus: (state, action) => {
      state.heartbeatStatus = action.payload;
    },
    setLastHeartbeatTime: (state, action) => {
      state.lastHeartbeatTime = action.payload;
    },
    setUseMultiTaskMode: (state, action) => {
      state.useMultiTaskMode = action.payload;
    },
    setMultiTaskIndex: (state, action) => {
      state.multiTaskIndex = action.payload;
    },
    setCollision: (state, action) => {
      state.collision = { ...state.collision, ...action.payload };
    },
  },
});

export const {
  setTaskInfo,
  resetTaskInfo,
  setTaskStatus,
  selectRobotType,
  resetTaskStatus,
  setTaskType,
  setTaskInstruction,
  setPolicyPath,
  setRecordInferenceMode,
  addTag,
  removeTag,
  removeAllTags,
  setHeartbeatStatus,
  setLastHeartbeatTime,
  setUseMultiTaskMode,
  setMultiTaskIndex,
  setCollision,
} = taskSlice.actions;

export default taskSlice.reducer;
