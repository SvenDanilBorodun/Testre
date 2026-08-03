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
import { signedOut } from '../session/sessionActions';

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

// The cross-agent capability contract (server:
// robot_profiles.capabilities_json → six booleans). A manifest is ADOPTED only
// when it is a COMPLETE object of these keys, all boolean — a '{}', a partial
// object, or a non-boolean payload is IGNORED so it can never fail OPEN over a
// settled restrictive manifest (audit fix 3). Extra keys are tolerated so a
// future capability addition on the server doesn't break an older client.
export const CAPABILITY_KEYS = [
  'recordable', 'editable', 'trainable', 'inferable', 'roboter_studio', 'has_leader',
];

export function isValidCapabilities(caps) {
  if (!caps || typeof caps !== 'object' || Array.isArray(caps)) return false;
  return CAPABILITY_KEYS.every((k) => typeof caps[k] === 'boolean');
}

// The recording form with NO localStorage in it. `initialState.taskInfo` is
// this object HYDRATED with the persisted Benutzer-ID, so the two must stay
// separate: a reset to `initialState` would hand back the id of whoever was
// signed in when the module loaded, i.e. restore the exact value a sign-out
// just scrubbed from storage. Every reset path below rebuilds from HERE.
const defaultTaskInfo = {
  taskName: '',
  taskType: '',
  taskInstruction: [],
  policyPath: '',
  recordInferenceMode: false,
  // `undefined`, not '': InfoPanel's auto-select tests for it, so the next
  // student gets the first Benutzer-ID offered rather than a blank field.
  userId: undefined,
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
};

const initialState = {
  taskInfo: { ...defaultTaskInfo, userId: savedUserId },
  taskStatus: {
    robotType: savedRobotType,
    // Robot profile id (e.g. 'omx_full' / 'omx_follower') + the server-authored
    // capability manifest. Both arrive on the /task/status wire (robot_profile,
    // capabilities_json) and are boot-set on the server, so they self-heal on a
    // node respawn. `null` caps mean "unknown" — the nav filter hides NOTHING
    // until an explicit manifest lands.
    robotProfile: '',
    capabilities: null,
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
    setTaskStatus: (state, action) => {
      // Never let a bare /task/status tick (robot_type='') wipe the settled
      // robot identity. robot_type / robot_profile / capabilities_json are
      // boot-set on the server and stamped on EVERY tick — incl. the idle
      // identity tick AND the collision monitor's own publishes (which
      // self-stamp identity via _stamp_identity, so they no longer emit empty
      // identity fields). The empty-field guards below still matter for two
      // cases: a PRE-CAPABILITY / old server image (sends '') and the
      // degraded-boot path (communicator=None → empty identity). An
      // unconditional spread would clobber robotType/robotProfile/capabilities
      // to their empty values on those ticks. Adopt each only when present —
      // the same guard userId already has below. `capabilities` additionally
      // must be a COMPLETE manifest (isValidCapabilities): a '{}' or partial
      // object would otherwise fail OPEN over a settled restrictive manifest.
      // A cached capabilities object (D10, one per distinct capabilities_json
      // string) is re-adopted by the SAME reference, so this stays
      // identity-stable.
      const { robotType, robotProfile, capabilities, ...rest } = action.payload;
      state.taskStatus = { ...state.taskStatus, ...rest };
      if (robotType) {
        state.taskStatus.robotType = robotType;
        try { localStorage.setItem('edubotics_robotType', robotType); } catch {}
      }
      if (robotProfile) {
        state.taskStatus.robotProfile = robotProfile;
      }
      if (isValidCapabilities(capabilities)) {
        state.taskStatus.capabilities = capabilities;
      }
    },
    resetTaskStatus: (state) => {
      state.taskStatus = initialState.taskStatus;
    },
    // Drop the capability manifest + profile (used on classroom-Jetson release /
    // rosbridge re-point). Resetting caps to null makes the nav capability
    // filter hide NOTHING until the LOCAL rig re-delivers its manifest on the
    // next idle identity tick — so a stale Jetson (omx_follower) manifest can't
    // wrongly hide Aufnahme/Daten/Training the moment the lock is released.
    // robotType is left intact (it is identical on both rigs and the top-bar
    // "Roboter" chip reads it).
    clearCapabilities: (state) => {
      state.taskStatus.capabilities = null;
      state.taskStatus.robotProfile = '';
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
  extraReducers: (builder) => {
    builder.addCase(signedOut, (state) => {
      // The recording FORM is the student's. `taskStatus` is the RIG's —
      // robotType / robotProfile / capabilities are server-authored and drive
      // the nav filter, so wiping them here would hide tabs until the next idle
      // identity tick (the Redux-wipe scar). Its one student-scoped field is
      // `userId`, a mirror of the running task's owner.
      state.taskInfo = { ...defaultTaskInfo };
      state.taskStatus.userId = '';
    });
  },
});

export const {
  setTaskInfo,
  setTaskStatus,
  resetTaskStatus,
  clearCapabilities,
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
