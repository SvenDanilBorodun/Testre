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

import { createSlice, isDraft, original } from '@reduxjs/toolkit';
import TaskPhase from '../../constants/taskPhases';
import { signedOut } from '../session/sessionActions';
import { nextPhaseAnchor } from './phaseAnchor';
import {
  EMPTY_RECORD_SESSION,
  advanceRecordSession,
  applyRecordIntent,
  applyRegisterStatus,
  applyUploadStatus,
  dismissRecordSession,
  noteCollision,
  noteLink,
  noteRecordNotice,
} from './recordSession';

const savedRobotType = (() => {
  try { return localStorage.getItem('edubotics_robotType') || ''; }
  catch { return ''; }
})();

// Benutzer-ID (the HF account/org the student records under). Persisted like
// robotType so it survives a full page reload (e.g. the GUI WebView reload on
// restart), not just tab switches. `undefined` when never set, so the
// Aufnahme page's auto-select (useRecordController) can still pick the first
// account on first run.
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
  // `undefined`, not '': useRecordController's auto-select tests for it, so
  // the next student gets the first Benutzer-ID offered rather than a blank
  // field.
  userId: undefined,
  fps: 30,
  tags: [],
  warmupTime: 5,
  episodeTime: 20,
  resetTime: 5,
  numEpisodes: 5,
  token: '',
  pushToHub: true,
  // User-selectable via the „Sichtbarkeit“ switch, which is rendered ONLY by
  // the Aufnahme page's TaskCard (under „Erweitert“, wired through
  // useRecordController) — an earlier version of this comment also named
  // InferencePanel.js, which has never referenced privateMode.
  // Sent on the wire as TaskInfo.private_mode and threaded through the
  // server-side data_manager overlay → HfApiWorker → create_repo(private=…).
  //
  // Starts CHECKED (private) since 2026-08-31. This REVERSES the 2026-08-07
  // owner decision to ship it unchecked, and the reason is the default
  // ACTION rather than the default opinion: a 13-year-old who presses record
  // and touches nothing else would publish their classmates' faces and voices
  // to a world-readable HuggingFace repo. Nothing about the classroom setup
  // makes that recoverable — the upload has happened by the time anyone
  // notices, and the people in the video did not choose it. Public stays one
  // click away („Sichtbarkeit“: „Privat“ / „Öffentlich“ under „Erweitert“, and
  // the save-name line says ÖFFENTLICH in capitals), so the student still
  // makes the call; they just have to make it on purpose.
  //
  // This is NOT the same knob as TaskInfo.msg's `bool private_mode true`, and
  // they now AGREE rather than being deliberately opposite. This value is
  // what React SENDS, always explicitly (useRosServiceCaller sends
  // Boolean(taskInfo.privateMode) on every start). The .msg default only
  // applies to a client that OMITS the field, which React never does — it
  // exists so a hand-crafted rosbridge call cannot publish a classroom
  // recording to a public repo by saying nothing. Leave the .msg default
  // alone: it guards a different caller and is already `true`.
  //
  // The other half of this default lives in useRosTopicSubscription. The ROS
  // node holds `task_info` for the life of a task, so it survives a handover
  // — an incoming /task/status tick from the PREVIOUS student's task would
  // otherwise silently un-tick this box before the next student ever presses
  // record. Adoption of `private_mode` is therefore gated on `robotNamesMe`,
  // exactly like `user_id`.
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
    // Aufnahme 2.0 (spec §3.2): the running task's own plan, off the wire
    // (`task_info.*`, 0 / '' outside a task), the stripped text of a record
    // tick's `[WARNUNG]`, and when the tick arrived (`performance.now()` for
    // the phase clock, `Date.now()` for ordering). `error` stays '' for every
    // dispatched tick — warnings travel in `recordWarn` / `recordNotice`.
    taskType: '',
    fps: 0,
    numEpisodes: 0,
    episodeTime: 0,
    warmupTime: 0,
    resetTime: 0,
    pushToHub: false,
    recordWarn: '',
    receivedAt: null,
    receivedWallMs: null,
  },
  // Where the current phase instance stood on the robot's floored clock — the
  // RIG's, like taskStatus, so a sign-out leaves it alone (phaseAnchor.js).
  phaseAnchor: null,
  // The student's recording session („Diese Sitzung" + the finish card) and
  // the last notice derived from a /task/status error. Both are the
  // STUDENT's: reset on session/signedOut.
  recordSession: EMPTY_RECORD_SESSION,
  recordNotice: null,
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

// The pure session/anchor functions compare and return plain objects; hand
// them the base object behind an immer draft so an unchanged result is the
// very object the store already holds.
const base = (value) => (isDraft(value) ? original(value) : value);

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
      const before = state.taskStatus;
      const prevStatus = {
        phase: before.phase,
        running: before.running,
        currentEpisodeNumber: before.currentEpisodeNumber,
        proceedTime: before.proceedTime,
        receivedAt: before.receivedAt,
        receivedWallMs: before.receivedWallMs,
      };
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
      // Aufnahme 2.0: both are pure and hand back the SAME object when this
      // tick changed nothing they record, so a 30 Hz feed leaves them alone.
      const next = state.taskStatus;
      state.phaseAnchor = nextPhaseAnchor(base(state.phaseAnchor), next);
      state.recordSession = advanceRecordSession(base(state.recordSession), prevStatus, next);
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
      state.recordSession = noteLink(base(state.recordSession), action.payload);
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
      const wasActive = state.collision.active;
      state.collision = { ...state.collision, ...action.payload };
      const status = state.taskStatus; // still the pre-collision phase (the hook returns early)
      state.recordSession = noteCollision(
        base(state.recordSession),
        { phase: status.phase, episodeTime: status.episodeTime },
        { ...action.payload, wasActive },
      );
    },
    // The Aufnahme page's own intents and acknowledgements (recordSession R13).
    // `start` also retires the previous notice: a fresh attempt must not stand
    // under the last attempt's error.
    recordIntent: (state, action) => {
      state.recordSession = applyRecordIntent(base(state.recordSession), action.payload);
      if (action.payload?.kind === 'start') state.recordNotice = null;
    },
    // A notice derived from a /task/status error ({kind: 'warn'|'error', text,
    // at}). An identical notice within a second is the same notice (the robot
    // repeats a warning on consecutive ticks).
    recordNoticeSet: (state, action) => {
      const notice = action.payload;
      if (!notice) return;
      const cur = state.recordNotice;
      const duplicate = cur && cur.kind === notice.kind && cur.text === notice.text
        && Number.isFinite(cur.at) && Number.isFinite(notice.at) && notice.at - cur.at < 1000;
      if (!duplicate) state.recordNotice = { kind: notice.kind, text: notice.text, at: notice.at };
      state.recordSession = noteRecordNotice(base(state.recordSession), notice);
    },
    recordNoticeClear: (state) => {
      state.recordNotice = null;
    },
    recordUploadStatus: (state, action) => {
      state.recordSession = applyUploadStatus(base(state.recordSession), action.payload);
    },
    recordRegisterStatus: (state, action) => {
      state.recordSession = applyRegisterStatus(base(state.recordSession), action.payload);
    },
    recordSessionDismiss: (state) => {
      state.recordSession = dismissRecordSession(base(state.recordSession), {
        running: state.taskStatus.running,
      });
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
      // The session and its notice are the student's; the phase anchor is the
      // rig's clock and stays.
      state.recordSession = EMPTY_RECORD_SESSION;
      state.recordNotice = null;
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
  recordIntent,
  recordNoticeSet,
  recordNoticeClear,
  recordUploadStatus,
  recordRegisterStatus,
  recordSessionDismiss,
} = taskSlice.actions;

export default taskSlice.reducer;
