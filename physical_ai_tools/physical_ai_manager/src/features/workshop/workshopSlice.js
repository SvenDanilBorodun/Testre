/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

import { createSlice, current, isDraft } from '@reduxjs/toolkit';
import { signedOut } from '../session/sessionActions';
// The [VAR:name=json] name gate, shared verbatim with the wire-side gate in
// `hooks/useRosTopicSubscription`. Imported from `utils/`, NOT from the hook:
// the hook imports `store/store`, which imports THIS file, so a slice→hook
// import would close a cycle around `configureStore`.
import { isDisplayableVariableName } from '../../utils/variableName';
// Pure (no React, no ROS): the panel's decision is the slice's too, so
// `calibrated` can never mean two things.
import { isCalibrated, isCapturing } from '../../components/Workshop/calibration/calibrationModel';

// The "no sensor data" snapshot, spelled ONCE so initialState and every reset
// path cannot drift. A FACTORY, not a shared frozen literal: `setSensorSnapshot`
// spreads into a fresh object today, but a future reducer that mutated in place
// would otherwise corrupt initialState itself. `ts: 0` is load-bearing — it is
// what SensorPanel reads as "nothing has arrived yet" (`ageMs` null), so a reset
// shows „–" rather than a freshly-stamped staleness clock over empty values.
function emptySensorSnapshot() {
  return {
    follower_joints: [],
    gripper_opening: 0,
    visible_apriltag_ids: [],
    ts: 0,
  };
}

// Values kept per variable for the Sammlung drawer's „Zuletzt" list.
export const VAR_HISTORY_LIMIT = 5;

// The key of a capped `{name: {value, ts}}` map with the oldest `ts` (FIFO
// eviction for `variables` and `counters`), or null when the map is empty.
// It reads a plain snapshot (`current`: the base object itself while the map is
// unmodified, as it is at every call site), never the draft: every entry read
// through an Immer draft becomes a proxy that is created and finalized again, so
// each dispatch past the cap paid for 256 of them — 1000 distinct names took
// 1.4 s on an idle machine and timed out at 5 s under a full test run, and a
// runaway program's [VAR:] stream paid the same per message in the browser.
function oldestKeyOf(map) {
  const plain = isDraft(map) ? current(map) : map;
  let oldestKey = null;
  let oldestTs = Infinity;
  for (const k of Object.keys(plain)) {
    const t = plain[k]?.ts ?? 0;
    if (t < oldestTs) {
      oldestTs = t;
      oldestKey = k;
    }
  }
  return oldestKey;
}

// Push {value, ts} to the FRONT of `name`'s history unless the newest entry
// already holds the same value — a loop re-setting an unchanged variable every
// pass would otherwise flush the list with five copies of one number.
function appendVariableHistory(state, name, value, ts) {
  const prev = Array.isArray(state.variableHistory[name]) ? state.variableHistory[name] : [];
  if (prev.length > 0 && JSON.stringify(prev[0].value) === JSON.stringify(value)) return;
  state.variableHistory[name] = [{ value, ts }, ...prev].slice(0, VAR_HISTORY_LIMIT);
}

const initialState = {
  // Roboter Studio's calibration (spec 40 S4, S8). `calibration` is the robot's
  // /edubotics/calibration_state, parsed (calibration/calibrationModel.js), or
  // null while nothing has been heard; the editor unlocks on the real robot
  // only while its state is `ok` (selectCalibrated). The three status flags
  // are /calibration/status's („a valid arm check exists"), hydrated by the
  // calibration panel; they no longer gate anything here.
  calibration: null,
  hasIntrinsicScene: false,
  hasHandeyeScene: false,
  hasTableTouch: false,
  // The scene-camera profile the arm container STATES (D63):
  // 'tower_v1' | 'legacy' | null (no statement yet — silence is never legacy).
  // The simulator and the twin draw the tower only for 'tower_v1'.
  sceneCameraProfile: null,

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
  sensorSnapshot: emptySensorSnapshot(),
  // Variable inspector — Map-like {name: {value, ts}}
  variables: {},
  // Sammlung drawer „Zuletzt" — {name: [{value, ts}]}, newest first, at most
  // VAR_HISTORY_LIMIT entries, a new entry only when the value CHANGED. A
  // SEPARATE map on purpose: `variables[name]` stays exactly {value, ts}, which
  // the inspector and its tests pin. Retired with `variables` (clearVariables
  // on every Start, and signedOut).
  variableHistory: {},
  // Zähler inspector — the SAME shape, a SEPARATE map. „Punkte" is the name
  // pre-filled in all three Zähler blocks and a perfectly ordinary variable
  // name too, so one shared map would have a student's variable and their
  // counter silently overwrite each other. Fed by the [CNT:name=int] sentinel.
  counters: {},

  // Editor state
  selectedWorkflowId: null,
  unsavedBlocklyJson: null,
  // Phase-3 tutorial / skillmap state
  activeTutorialId: null,
  activeTutorialStep: 0,
  // restrictedBlocks: array of block type strings, or null for unrestricted
  restrictedBlocks: null,
};

const workshopSlice = createSlice({
  name: 'workshop',
  initialState,
  reducers: {
    setCalibrationState: (state, action) => {
      // The parsed /edubotics/calibration_state, or null (nothing heard).
      state.calibration = action.payload || null;
    },
    setCalibrationStatus: (state, action) => {
      // /calibration/status: under the tower camera all three fields mean „a
      // valid arm check exists" (spec 40 S4). Payload mirrors the
      // CalibrationStatus.srv response.
      const { has_scene_intrinsics, has_scene_handeye, has_table_plane } = action.payload || {};
      if (has_scene_intrinsics !== undefined) state.hasIntrinsicScene = !!has_scene_intrinsics;
      if (has_scene_handeye !== undefined) state.hasHandeyeScene = !!has_scene_handeye;
      if (has_table_plane !== undefined) state.hasTableTouch = !!has_table_plane;
    },
    setSceneCameraProfile: (state, action) => {
      const p = action.payload;
      state.sceneCameraProfile = p === 'tower_v1' || p === 'legacy' ? p : null;
    },

    setRunState: (state, action) => {
      const next = action.payload;
      // Terminal phases ('finished', 'stopped', 'error') need to clear
      // both `runState` AND `phase` so the RunControls `isRunning`
      // selector (state.workshop.phase === 'running') flips false even
      // when the server's last WorkflowStatus message stamped `phase`
      // before the terminal dispatch arrived. Without this the Start
      // button stayed disabled until the next page-load. Reset both
      // to 'idle' / '' synchronously so the UI snaps back.
      if (next === 'finished' || next === 'stopped' || next === 'error') {
        state.runState = 'idle';
        state.phase = '';
        state.currentBlockId = null;
        // Detections are an artifact OF the run: the server publishes
        // `active_detections` only on /workflow/status, and the whole point of
        // dispatching the empty list mid-run is that a finished perception block
        // must stop painting boxes. A run that ENDS is the same event one step
        // later — and without this the last box set stayed on screen over a
        // camera feed that keeps streaming from web_video_server independently
        // of ROS, so the boxes look live.
        state.detections = [];
      } else if (next === 'running') {
        state.paused = false;
        // Clear the previous run's highlighted block ONLY on the transition
        // INTO running (idle/stopped → running), so a fresh run doesn't briefly
        // flash the PRIOR run's block before the first new block id arrives
        // (#L1). Do NOT clear on every per-block 'running' tick:
        // subscribeToWorkflowStatus dispatches setRunState('running') on EVERY
        // running WorkflowStatus message, AFTER setWorkflowStatus has already
        // adopted that tick's current_block_id — so an unconditional null wiped
        // the live highlight on every block. The running (moving) block then
        // never lit up; only its brief 'done'-tick window (no setRunState
        // dispatch) and debugger pauses (phase='paused', no setRunState) kept
        // the highlight — exactly the "blocks only light up when stopped or in
        // debug, not live" report. Guarding on the transition preserves #L1 AND
        // restores live run-highlighting.
        if (state.runState !== 'running') {
          state.currentBlockId = null;
        }
        state.runState = 'running';
      } else {
        state.runState = next;
      }
    },
    setPaused: (state, action) => {
      state.paused = !!action.payload;
    },
    setWorkflowStatus: (state, action) => {
      const { current_block_id, phase, progress, error, log_message } = action.payload;
      // Live run-highlighting flicker fix: partial-payload emits (ctx.log
      // lines, detection updates) publish a fresh WorkflowStatus whose
      // string fields default to '' — current_block_id='' AND phase=''. A
      // bare `!== undefined` guard would blank the highlighted block (and the
      // status chip) on every mid-block log tick. Only adopt a TRUTHY value;
      // terminal phases ('' on stop/finish) are driven by setRunState, not here.
      if (current_block_id) state.currentBlockId = current_block_id;
      if (phase) state.phase = phase;
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
    // The fourth sibling of clearWorkflowLog / clearVariables /
    // setDebuggerWarnings([]) — the three things RunControls.handleStart already
    // wiped before a new run. `workflowError` was skipped, and it is the only
    // one of the four the student SEES as a red alert (it also force-opens the
    // Protokoll drawer). Nothing else could clear it in time: the server emits
    // no further status after the terminal `error` phase, so the banner
    // describing the previous run rendered over the new one until that run's
    // first tick arrived — or forever, if the new run failed to start.
    clearWorkflowError: (state) => {
      state.workflowError = null;
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
      const { name, value } = action.payload || {};
      if (typeof name !== 'string' || !name) return;
      // Prototype-pollution guard. Deliberately NOT folded into
      // `isDisplayableVariableName` (which answers "can this be shown?"):
      // these three are perfectly displayable, they are just not safe as
      // object keys. `useRosTopicSubscription` keeps the identical check at
      // its own dispatch site — two gates, because either one can be the only
      // one a future caller passes through.
      if (name === '__proto__' || name === 'constructor' || name === 'prototype') return;
      // RS-49. This used to be a SECOND, stricter copy of the wire gate —
      // /^[A-Za-zÄÖÜäöüß_][A-Za-zÄÖÜäöüß0-9_]{0,63}$/ — so widening only the
      // hook fixed nothing: of the 19 names Blockly's own `Variables.promptName`
      // really produces, this reducer silently `return`ed on 15 („meine Zahl",
      // „Anzahl Würfel", „zähler-2", „Öl-Stand", „2te_zahl", „3", „Test 123" …)
      // and the Variablen panel stayed empty while every test passed. One
      // predicate, imported — never a local regex again.
      if (!isDisplayableVariableName(name)) return;
      const ts = Date.now();
      // If we're at the cap and adding a NEW name, evict the oldest
      // entry. Existing-name overwrites are free.
      const isNew = !(name in state.variables);
      if (isNew) {
        if (Object.keys(state.variables).length >= VAR_LIMIT) {
          const oldestKey = oldestKeyOf(state.variables);
          if (oldestKey) {
            delete state.variables[oldestKey];
            // An evicted variable takes its history with it, or the history
            // map would grow past the cap the variables map enforces.
            delete state.variableHistory[oldestKey];
          }
        }
      }
      state.variables[name] = { value, ts };
      appendVariableHistory(state, name, value, ts);
    },
    clearVariables: (state) => {
      state.variables = {};
      state.variableHistory = {};
    },
    // The [CNT:name=int] twin of setVariable. Same three gates in the same
    // order — prototype names, then the SHARED display predicate, then the cap
    // — plus one the variables path cannot have: a counter is an integer, so a
    // non-integer value is refused rather than rendered as a quoted string in a
    // column the panel labels „Wert" for a whole number.
    setCounter: (state, action) => {
      const COUNTER_LIMIT = 256;
      const { name, value } = action.payload || {};
      if (typeof name !== 'string' || !name) return;
      // Prototype-pollution guard, kept HERE and not folded into
      // `isDisplayableVariableName` — for the same reason `setVariable` keeps
      // its own copy: these three are displayable, they are just unsafe as
      // object keys, and a folded check makes deleting either call site's guard
      // invisible to a test.
      if (name === '__proto__' || name === 'constructor' || name === 'prototype') return;
      // The ONE predicate, shared with setVariable and both wire gates. Never a
      // local regex — a second copy is exactly what RS-49 was.
      if (!isDisplayableVariableName(name)) return;
      // `reset` writes 0 and `add` writes int+1, and the wire frame captures
      // digits only, so anything else did not come from a counter block.
      if (!Number.isInteger(value)) return;
      const ts = Date.now();
      const isNew = !(name in state.counters);
      if (isNew) {
        if (Object.keys(state.counters).length >= COUNTER_LIMIT) {
          const oldestKey = oldestKeyOf(state.counters);
          if (oldestKey) delete state.counters[oldestKey];
        }
      }
      state.counters[name] = { value, ts };
    },
    // RETIREMENT, half one: RunControls.handleStart dispatches this next to
    // clearVariables() on every „Start", so a run never shows the previous
    // run's tally. (Half two is the `signedOut` case below — without it the
    // next student at a shared classroom PC inherits these rows.)
    clearCounters: (state) => {
      state.counters = {};
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
      const next = action.payload;
      // Breakpoints are Blockly block IDs, so they are meaningful ONLY inside
      // the document that minted them. Switching workflows left the previous
      // program's ids in place: BreakpointList renders them as raw UUIDs (its
      // `blockLabel` falls back to the id when `getBlockById` misses) that
      // cannot be alt-clicked off, and handleStart pushes them to
      // /workflow/set_breakpoints before every run.
      //
      // The guard is `prev && prev !== next`, and both halves matter. Requiring
      // a PREVIOUS id is what keeps the save-a-new-workflow path (null → id,
      // WorkshopPage.handleSave / GalleryTab) from throwing away breakpoints the
      // student just set on the very blocks they saved — the document is
      // unchanged there, only its identity is. Requiring a DIFFERENT id keeps
      // re-picking the open workflow a no-op. Every other switch — picking
      // another workflow, or creating one while holding one — does change the
      // document and does clear.
      //
      // Deliberately here and not in WorkshopPage.handlePickWorkflow: that is
      // one of three dispatchers, and a fourth added later would silently miss.
      //
      // The same event retires the run's shown VALUES (review m4): a
      // variable, its last five values and a counter belong to the program
      // that produced them, and a code document lists every name
      // `workshop.variables` holds — so the previous program's names showed
      // up in the next one's Variablen tab, „Nirgends" used. „Neu" from one
      // UNSAVED document to the next (null → null) is the one switch this
      // guard cannot see; WorkshopPage.handleNewProgram retires them there.
      const prev = state.selectedWorkflowId;
      if (prev && prev !== next) {
        state.breakpoints = [];
        state.variables = {};
        state.variableHistory = {};
        state.counters = {};
      }
      state.selectedWorkflowId = next;
    },
    // The student OPENS another document („Öffnen", a gallery clone): its
    // breakpoints and shown values leave with the one it replaces — also
    // from an UNSAVED document (null → id), which setSelectedWorkflowId must
    // not treat as a switch, because its null → id is also the first save of
    // an unsaved document, the same program getting its id (review round 2,
    // mi7). Re-opening the open document changes nothing.
    openWorkflow: (state, action) => {
      const next = action.payload;
      if (state.selectedWorkflowId !== next) {
        state.breakpoints = [];
        state.variables = {};
        state.variableHistory = {};
        state.counters = {};
      }
      state.selectedWorkflowId = next;
    },
    setUnsavedBlocklyJson: (state, action) => {
      state.unsavedBlocklyJson = action.payload;
    },
    markWorkflowSaved: (state) => {
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
  },
  extraReducers: (builder) => {
    // This slice is HALF rig and half student, so it is the one place the
    // sign-out reset has to be selective.
    //
    // KEPT: everything calibration (`calibration`, the three status flags, the
    // stated `sceneCameraProfile`). A calibration describes the CAMERA, the MAT
    // and the ARM, it is persisted server-side in the edubotics_calib volume,
    // and the robot's state topic re-sends it anyway.
    //
    // CLEARED: the student's PROGRAM and everything derived from running it.
    // `unsavedBlocklyJson` is the one that made this necessary — WorkshopPage
    // seeds the editor from it when the cloud fetch fails, so on a
    // `reload: false` sign-out the next student opened Roboter Studio holding
    // the previous student's blocks. (Their WRITES fail safe: the cloud's
    // `_assert_workflow_owned` answers 404. It is disclosure, not corruption.)
    builder.addCase(signedOut, (state) => {
      state.selectedWorkflowId = null;
      state.unsavedBlocklyJson = null;
      // A half-finished tutorial AND the toolbox restriction it imposes.
      // `setActiveTutorial({id:null})` alone does NOT clear restrictedBlocks,
      // which is how the previous enumeration left the next student's toolbox
      // locked to someone else's lesson.
      state.activeTutorialId = null;
      state.activeTutorialStep = 0;
      state.restrictedBlocks = null;
      // Output of the student's own run: the German log lines, the inspector
      // variables, the breakpoints they set in their blocks, the per-block IK
      // warnings about their destinations.
      state.log = [];
      state.variables = {};
      state.variableHistory = {};
      state.counters = {};
      state.breakpoints = [];
      state.debuggerWarnings = [];
      state.workflowError = null;
      state.currentBlockId = null;
      // The RUN itself, not just its output. These are written ONLY by a
      // /workflow/status tick or a RunControls button, and nothing clears them
      // when the socket dies — which is exactly when „Abmelden" becomes
      // reachable, because utils/signOut::logoutBlockReason opens the gate on a
      // dead rosbridge link. So the sign-out that a dropped link permits was the
      // one path that could hand the next student `runState: 'running'`. They
      // then get selectWorkflowRunning() true (their OWN sign-out blocked,
      // citing a program they never started), RunControls showing Stop instead
      // of Start, and SimScene gated as though a run were in flight.
      state.runState = 'idle';
      state.phase = '';
      state.progress = 0;
      state.paused = false;
      // The previous student's perception boxes and their arm's joint angles.
      // Both are pushed by ROS and neither has any other clear that survives the
      // stream stopping, so without this they are readable by the next student —
      // and `sensorSnapshot` is a SHALLOW MERGE, so no action could shrink
      // `follower_joints` back to [] even in principle.
      state.detections = [];
      state.sensorSnapshot = emptySensorSnapshot();
    });
  },
});

export const {
  setCalibrationState,
  setCalibrationStatus,
  setSceneCameraProfile,
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
  setCounter,
  clearCounters,
  addBreakpoint,
  removeBreakpoint,
  clearBreakpoints,
  clearWorkflowError,
  setDebuggerWarnings,
  setSelectedWorkflowId,
  openWorkflow,
  setUnsavedBlocklyJson,
  markWorkflowSaved,
  setActiveTutorial,
  advanceTutorialStep,
  setRestrictedBlocks,
} = workshopSlice.actions;

// ── Activity selectors ──────────────────────────────────────────────────────
// Roboter Studio activity lives in THIS slice, not in `tasks.taskStatus`, so a
// guard about "is the student busy" has to ask here as well. Both predicates are
// spelled out where the fields are declared rather than in the component that
// consumes them, because both need a field-level reason to be correct.

/**
 * Is a Blockly program executing? `paused` counts: a debugger breakpoint still
 * holds the server's `on_workflow` claim, so the arm is mid-program with the
 * next segment already queued. The arm check is a program too (spec 40 S4).
 */
export const selectWorkflowRunning = (state) =>
  state.workshop.runState === 'running' || state.workshop.paused === true;

/**
 * Is a calibration capture under way that leaving would throw away? True
 * while the arm check (`arm_check_running`) or the handheld lens capture
 * (`handheld_running`) runs — the robot's own state says so (spec 40 S8), so
 * nothing here latches. The name is the old one: StudentApp's „Abmelden" gate
 * reads it (ANDed with being on Roboter Studio and a live robot link).
 */
export const selectCalibrationHasUnsolvedCaptures = (state) => isCapturing(state.workshop.calibration);

/** Roboter Studio on the real robot unlocks only while the calibration state is `ok`. */
export const selectCalibrated = (state) => isCalibrated(state.workshop.calibration);

/** The arm container states the tower camera: the simulator and the twin draw the tower. */
export const selectTowerScene = (state) => state.workshop.sceneCameraProfile === 'tower_v1';

export default workshopSlice.reducer;
