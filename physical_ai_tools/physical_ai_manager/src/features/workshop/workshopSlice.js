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
import { signedOut } from '../session/sessionActions';

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

const initialState = {
  // Calibration wizard state
  // WS4 (2026-06-17): scene-cam-only calibration. The wizard starts on the
  // scene intrinsic step (the gripper steps were removed). The gripper flags
  // are kept (defaulted true = "not required") so any legacy gate that still
  // ANDs them never blocks the editor.
  calibState: 'idle',
  currentStep: 'scene_intrinsic',
  framesCaptured: 0,
  // Pre-capture placeholder; the backend overwrites it with the live
  // frames_required on every capture (20 since the rational-distortion /
  // wide-lens intrinsic upgrade, 2026-06-24 W4).
  framesRequired: 20,
  lastViewRms: null,
  methodDisagreement: null,
  calibError: null,
  hasIntrinsicGripper: true,
  hasIntrinsicScene: false,
  hasHandeyeGripper: true,
  hasHandeyeScene: false,
  // Touch-off table measurement (optional accuracy step, recommended).
  hasTableTouch: false,
  hasColorProfile: false,
  // W5 (2026-06-24): after the touch-off completes, route the student into the
  // OPTIONAL „Genauigkeit prüfen" step before the editor opens. `pendingVerify`
  // holds the editor closed only during that just-finished wizard session; the
  // student clears it with „Fertig" or „Überspringen". It is NOT part of the
  // `calibrated` gate — on a normal page reload it defaults false, so a
  // calibrated rig opens straight to the editor and verify never blocks use.
  pendingVerify: false,
  // Last accuracy-verify solve result (residuals / yaw bias / mirror flag),
  // shown as a quality readout. {residual_mm_mean, residual_mm_max,
  // yaw_bias_deg, mirror_detected, point_count, message}.
  accuracyResult: null,
  // True between „Kalibrierung neu starten" and the moment the student finishes
  // re-running the geometry steps. Forces the wizard to stay open even though
  // valid YAMLs still exist on disk — without it the wizard's mount-time
  // /calibration/status hydrate re-reads those YAMLs and flips `calibrated`
  // straight back to true, bouncing the student back to the editor (2026-06-23).
  recalibrating: false,
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
  sensorSnapshot: emptySensorSnapshot(),
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
};

function classifyQuality(score) {
  // Backend sends 0 for "unknown" (RMS not yet estimable, < 4 frames) — treat
  // it like null so the first captures aren't flagged as a red "schwach" dot.
  if (score === undefined || score === null || score === 0) return 'ok';
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
      else if (step === 'table_touch') state.hasTableTouch = true;
      else if (step === 'color_profile') state.hasColorProfile = true;
      // Once all three scene steps are done again, recalibration is complete —
      // drop the override so the WorkshopPage gate returns to the editor.
      if (state.hasIntrinsicScene && state.hasHandeyeScene && state.hasTableTouch) {
        state.recalibrating = false;
      }
      // W5: completing the touch-off routes the student into the OPTIONAL
      // accuracy-verify step before the editor opens (gated by pendingVerify,
      // which is not part of `calibrated`). The student finishes or skips it.
      if (step === 'table_touch') {
        state.pendingVerify = true;
        state.currentStep = 'accuracy_verify';
        state.accuracyResult = null;
      }
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
        has_table_plane,
        has_color_profile,
      } = action.payload || {};
      if (has_gripper_intrinsics !== undefined) state.hasIntrinsicGripper = !!has_gripper_intrinsics;
      if (has_scene_intrinsics !== undefined) state.hasIntrinsicScene = !!has_scene_intrinsics;
      if (has_gripper_handeye !== undefined) state.hasHandeyeGripper = !!has_gripper_handeye;
      if (has_scene_handeye !== undefined) state.hasHandeyeScene = !!has_scene_handeye;
      if (has_table_plane !== undefined) state.hasTableTouch = !!has_table_plane;
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
      // WS4: keep the (vestigial) gripper flags satisfied; only the scene
      // artefacts gate the editor now.
      // 2026-06-24 (W1, FULL scope): the force-recalibration release made
      // intrinsics MANDATORY on every restart — the backend reports all three
      // scene flags false until the student re-runs the whole flow this boot.
      // So a manual „Kalibrierung neu starten" must also redo intrinsics:
      // reset hasIntrinsicScene too and restart at `scene_intrinsic` (the
      // first step). The earlier "intrinsics are always present, jump to the
      // extrinsic" assumption is dead (factory-default seeding was removed).
      // Force the wizard to stay open despite the on-disk YAMLs (see the
      // `recalibrating` field doc) until the steps are actually re-run.
      state.recalibrating = true;
      state.hasIntrinsicGripper = true;
      state.hasIntrinsicScene = false;
      state.hasHandeyeGripper = true;
      state.hasHandeyeScene = false;
      state.hasTableTouch = false;
      state.hasColorProfile = false;
      state.currentStep = 'scene_intrinsic';
      state.framesCaptured = 0;
      state.methodDisagreement = null;
      state.calibError = null;
      state.coverageMosaic = Array(16).fill(0);
      state.qualityHistory = [];
      state.verifyResult = null;
      state.pendingVerify = false;
      state.accuracyResult = null;
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
    // W5: store the accuracy-verify solve readout (residuals / yaw bias / mirror).
    setAccuracyResult: (state, action) => {
      state.accuracyResult = action.payload || null;
    },
    // W5: „Fertig" / „Überspringen" on the accuracy-verify step — drop the
    // editor-hold so the WorkshopPage gate opens the editor.
    finishVerify: (state) => {
      state.pendingVerify = false;
    },
    setCalibHistory: (state, action) => {
      state.calibHistory = Array.isArray(action.payload) ? action.payload : [];
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
        // of ROS, so the boxes look live. CameraFeedOverlay is also mounted by
        // BOTH calibration steps, so the stale boxes outlived Roboter Studio
        // entirely and reappeared in the calibration wizard.
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
      const prev = state.selectedWorkflowId;
      if (prev && prev !== next) state.breakpoints = [];
      state.selectedWorkflowId = next;
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
  },
  extraReducers: (builder) => {
    // This slice is HALF rig and half student, so it is the one place the
    // sign-out reset has to be selective.
    //
    // KEPT: everything calibration (`calibState`, the per-step has* flags, the
    // coverage mosaic, the history). A calibration describes the CAMERA and the
    // TABLE, it is persisted server-side in the edubotics_calib volume, and
    // clearing it here would send the next student through a 20-frame ChArUco
    // capture they do not need.
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
      state.lastSavedAt = null;
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
  setAccuracyResult,
  finishVerify,
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
  clearWorkflowError,
  setDebuggerWarnings,
  setSelectedWorkflowId,
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
 * next segment already queued.
 *
 * Deliberately NOT read: `calibState`. `setCalibState` has no dispatcher
 * anywhere in `src/`, so that field never leaves `'idle'` and a guard keyed on
 * it would be inert.
 */
export const selectWorkflowRunning = (state) =>
  state.workshop.runState === 'running' || state.workshop.paused === true;

/**
 * Are there captured calibration frames that abandoning the wizard would throw
 * away? `framesCaptured` alone is not that question — it LATCHES. Nothing
 * resets it on leaving a step or the page; only the NEXT step's mount does
 * (`resetCalibProgress` / `setCalibProgress({framesCaptured: 0})`). So after the
 * touch-off it stays at 3 for the rest of the session.
 *
 * `!calibrated` is what makes it a live question: it is the same three-flag
 * conjunction `WorkshopPage` gates its editor on, and once it holds the required
 * steps are done and the latched count describes finished work. The OPTIONAL
 * „Genauigkeit prüfen" step therefore does not block — it runs after those
 * three, and losing it costs a re-run of one measurement.
 *
 * `recalibrating` is deliberately NOT a trigger. Nothing clears it but
 * completing all three steps, so a student who pressed „Kalibrierung neu
 * starten" and changed their mind would be unable to hand the PC over at all.
 *
 * The caller must ALSO establish that the wizard is on screen — the wizard is
 * the only thing that captures frames, and its unmount cancels the server-side
 * buffer, so off the Roboter-Studio page the latched count is stale for the same
 * reason.
 */
export const selectCalibrationHasUnsolvedCaptures = (state) => {
  const w = state.workshop;
  const calibrated = w.hasIntrinsicScene && w.hasHandeyeScene && w.hasTableTouch;
  return w.framesCaptured > 0 && !calibrated;
};

export default workshopSlice.reducer;
