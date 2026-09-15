/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Simulator previews („▶" on a Sammlung card or in the drawer) — pure helpers.
//
// A preview is an ordinary sim run of a tiny generated program, so every
// refusal it can meet is a server sentence. Those are shown VERBATIM, with
// exactly one exception: the replay lead-in refusal tells the student to lift
// the arm first, which nobody can do to the virtual arm. That one sentence is
// mapped to a simulator-true one; everything else (the seam refusal, a
// recording that dips below the table later, width, zones) passes unchanged.

import { DE } from '../components/Workshop/blocks/messages_de';
import { trajectoryMatchesRig } from './trajectoryIdentity';

// How long the virtual arm rests on the previewed pose before the run ends.
export const PREVIEW_HOLD_S = 1.5;
// Every preview run's workflow_id starts with this, so its statuses can never be
// mistaken for (or finalize) the student's own program run.
export const PREVIEW_ID_PREFIX = 'vorschau-';
// The simulator stage mounts its twin and subscriptions on entry; a preview
// that enters sim waits this long before it starts the run.
export const SIM_ENTRY_SETTLE_MS = 600;

export function isPreviewWorkflowId(id) {
  return typeof id === 'string' && id.startsWith(PREVIEW_ID_PREFIX);
}

/** `studioAssets.lastPreviewResult` key of a recording version. */
export function previewKeyForRecording(trajectoryId) {
  return `rec:${trajectoryId}`;
}

/** `studioAssets.lastPreviewResult` key of a Ziel or a Position. */
export function previewKeyForDestination(entryId) {
  return `dest:${entryId}`;
}

export function previewWorkflowIdForRecording(trajectoryId) {
  return `${PREVIEW_ID_PREFIX}aufnahme-${String(trajectoryId).replace(/[^0-9a-f]/gi, '').slice(0, 8).toLowerCase()}`;
}

// Ziel AND Position share the prefix: both are destination-store entries.
export function previewWorkflowIdForDestination(entryId) {
  return `${PREVIEW_ID_PREFIX}ziel-${entryId}`;
}

function holdBlock(id) {
  return {
    type: 'edubotics_wait_seconds',
    id,
    inputs: { SECONDS: { shadow: { type: 'math_number', fields: { NUM: PREVIEW_HOLD_S } } } },
  };
}

// The editor's placed objects and zones ride along VERBATIM: a sim run replaces
// the server's simulated world, so a preview without them would wipe the scene.
function sceneSiblings(simScene, tempo) {
  const objects = simScene && Array.isArray(simScene.objects) ? simScene.objects : [];
  const zones = simScene && Array.isArray(simScene.zones) ? simScene.zones : [];
  return { sim: { enabled: true, objects }, zones, tempo };
}

/** „spiele Bewegung <name>" + a short hold, with the recording as its sibling. */
export function buildRecordingPreviewProgram({ name, fps, points, simScene, tempo }) {
  return {
    blocks: {
      languageVersion: 0,
      blocks: [{
        type: 'edubotics_replay_trajectory',
        id: 'vorschau-1',
        fields: { NAME: name },
        next: { block: holdBlock('vorschau-2') },
      }],
    },
    ...sceneSiblings(simScene, tempo),
    trajectories: { [name]: { fps, points } },
  };
}

/** „bewege zu Ziel <name>" + a short hold, with ONLY that entry as `destinations`. */
export function buildDestinationPreviewProgram({ entry, simScene, tempo }) {
  return {
    blocks: {
      languageVersion: 0,
      blocks: [{
        type: 'edubotics_move_to',
        id: 'vorschau-1',
        inputs: {
          DESTINATION: {
            block: { type: 'edubotics_destination_ref', id: 'vorschau-2', fields: { NAME: entry.name } },
          },
        },
        next: { block: holdBlock('vorschau-3') },
      }],
    },
    ...sceneSiblings(simScene, tempo),
    destinations: [{ name: entry.name, kind: entry.kind, x: entry.x, y: entry.y, z: entry.z }],
  };
}

/**
 * The two leader inputs of previewBlockReason, from the control-bridge probe
 * (hooks/useRsBridgeStatus) and the server-pushed capabilities.
 *
 * `useRsBridgeStatus().leaderOn` fails OPEN: a probe timeout or a Pi agent that
 * is down reads as „no leader". That is right for the run bar, but a preview is
 * a SIM run, which claims on_workflow and so disarms the teleop e-stop while a
 * live leader may still drive the real follower — and nothing server-side
 * checks for a leader on a preview. So the preview gate fails CLOSED: unless
 * the rig is PROVEN leader-less (an explicit `has_leader === false` from the
 * server or the bridge) or the bridge ANSWERED „follower only", an unanswered
 * probe is `rsLeaderUnknown`. omx_follower/edu6/edu1 carry `has_leader: false`,
 * which is what keeps a bridge-less rig previewable.
 *
 * `rsLeaderPending` (owner decision 2026-09-15, B1) splits that unknown in two:
 * while the bridge has NOT ANSWERED ONCE yet (`probed === false`) the refusal is
 * „Roboterstatus wird geprüft …" and the page disables every preview ▶; after
 * the first answer the unknown stays `rsLeaderUnknown`. Only an EXPLICIT
 * `probed === false` is pending — a bridge object without the field (an older
 * caller, a test double) keeps the pre-decision `rsLeaderUnknown` answer. The
 * two are mutually exclusive, and neither is ever set on a proven leader-less rig.
 */
export function previewLeaderGate(rsBridge, caps) {
  const bridge = rsBridge && typeof rsBridge === 'object' ? rsBridge : {};
  const rsLeaderOn = bridge.leaderOn === true;
  const provenLeaderless = (caps && caps.has_leader === false) || bridge.hasLeader === false;
  const answered = bridge.available === true;
  const undecided = !rsLeaderOn && !provenLeaderless && !answered;
  const rsLeaderPending = undecided && bridge.probed === false;
  return { rsLeaderOn, rsLeaderPending, rsLeaderUnknown: undecided && !rsLeaderPending };
}

/**
 * Why ▶ is refused right now, or null. The ORDER is the contract: the first
 * matching reason is the one the student reads. `inFlight` is silent (a double
 * click must not toast).
 */
export function previewBlockReason({
  heartbeatStatus, runState, paused, teachOpen, jogHandGuideOn, simMode,
  activeTutorialId, rsLeaderOn, rsLeaderPending, rsLeaderUnknown, asset, robotType, workflowId,
  inFlight,
}) {
  if (heartbeatStatus !== 'connected') return 'offline';
  if (runState === 'running' || paused === true) return 'running';
  if (teachOpen) return 'teach';
  if (jogHandGuideOn) return 'handguide';
  if (!simMode && activeTutorialId) return 'tutorial';
  // A sim run sets on_workflow, which gates the teleop e-stop OFF while a live
  // leader still drives the real follower (§8 B1) — so no preview then.
  if (rsLeaderOn) return 'leader';
  // …not before the bridge has answered once (owner decision 2026-09-15)…
  if (rsLeaderPending) return 'leaderPending';
  // …and no preview while that cannot be RULED OUT either (previewLeaderGate).
  if (rsLeaderUnknown) return 'leaderUnknown';
  if (asset && asset.kind === 'recording') {
    if (!trajectoryMatchesRig(asset.robotProfile, robotType)) return 'otherRobot';
    if (!workflowId) return 'unsaved';
  }
  if (inFlight) return 'inFlight';
  return null;
}

export const PREVIEW_BLOCK_TITLES_DE = Object.freeze({
  offline: DE.PREVIEW_BLOCK_OFFLINE,
  running: DE.PREVIEW_BLOCK_RUNNING,
  teach: DE.PREVIEW_BLOCK_TEACH,
  handguide: DE.PREVIEW_BLOCK_HANDGUIDE,
  tutorial: DE.PREVIEW_BLOCK_TUTORIAL,
  leader: DE.PREVIEW_BLOCK_LEADER,
  leaderPending: DE.PREVIEW_BLOCK_LEADER_PENDING,
  leaderUnknown: DE.PREVIEW_BLOCK_LEADER_UNKNOWN,
  otherRobot: DE.PREVIEW_BLOCK_OTHER_ROBOT,
  unsaved: DE.PREVIEW_BLOCK_UNSAVED,
});

/**
 * The header sim toggle and `WorkshopPage::ensureSimMode` share this ladder.
 * Leaving sim is refused only while a sim run is in flight (it would unmount
 * RunControls and its Stop button); ENTERING is refused during a tutorial (the
 * dock swap unmounts SkillmapPlayer), while Vormachen is open (it owns the real
 * arm) and while the arm is hand-guided (unmounting JogPanel ends the session).
 */
export function simEntryBlockReason({ simRunActive, simMode, activeTutorialId, teachOpen, jogHandGuideOn }) {
  if (simRunActive) return 'simRun';
  if (simMode) return null;
  if (activeTutorialId) return 'tutorial';
  if (teachOpen) return 'teach';
  if (jogHandGuideOn) return 'handguide';
  return null;
}

export const SIM_ENTRY_BLOCK_TITLES_DE = Object.freeze({
  simRun: 'Während ein Simulationslauf läuft, kann der Simulator nicht beendet werden — bitte zuerst stoppen.',
  tutorial: 'Während ein Lernpfad aktiv ist, kann der Simulator nicht gestartet werden — bitte den Lernpfad zuerst beenden.',
  teach: DE.TEACH_SIM_ENTRY_BLOCKED,
  handguide: 'Solange der Arm freigeschaltet ist, kann der Simulator nicht gestartet werden — bitte den Arm zuerst festsetzen.',
});

export const SIM_TOGGLE_DEFAULT_TITLE_DE =
  'Programm auf einem virtuellen Roboter testen — ohne echten Roboter und ohne Kalibrierung';

// `physical_ai_server/workflow/handlers/trajectory.py`, verbatim (Python's
// adjacent string literals joined). `utils/__tests__/simPreview.test.js` reads
// that file and fails if the server sentence changes without this one.
export const REPLAY_LEAD_IN_BELOW_TABLE_SERVER =
  'Der Arm ist zu nah an der Tischebene, um sicher zur Aufnahme-Startstellung zu fahren. '
  + 'Bitte den Arm zuerst anheben und erneut abspielen.';

/** The German message a PREVIEW shows for a server message. Exact match only. */
export function previewMessageDe(message) {
  return message === REPLAY_LEAD_IN_BELOW_TABLE_SERVER
    ? DE.PREVIEW_LEAD_IN_BELOW_TABLE
    : message;
}
