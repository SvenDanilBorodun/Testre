/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The preview message mapping: exactly ONE server sentence (the replay lead-in
// refusal, whose „lift the arm" advice cannot be followed in a simulator) is
// replaced; every other refusal stays verbatim. The constant is pinned against
// the server source, so a reworded server sentence cannot silently turn the
// mapping into a no-op.

import fs from 'fs';
import path from 'path';
import { DE } from '../../components/Workshop/blocks/messages_de';
import {
  PREVIEW_BLOCK_TITLES_DE,
  PREVIEW_HOLD_S,
  PREVIEW_ID_PREFIX,
  REPLAY_LEAD_IN_BELOW_TABLE_SERVER,
  SIM_ENTRY_BLOCK_TITLES_DE,
  SIM_TOGGLE_DEFAULT_TITLE_DE,
  buildDestinationPreviewProgram,
  buildRecordingPreviewProgram,
  isPreviewWorkflowId,
  previewBlockReason,
  previewLeaderGate,
  previewKeyForDestination,
  previewKeyForRecording,
  previewMessageDe,
  previewWorkflowIdForDestination,
  previewWorkflowIdForRecording,
  simEntryBlockReason,
} from '../simPreview';

describe('previewMessageDe', () => {
  test('maps exactly the lead-in sentence', () => {
    expect(previewMessageDe(REPLAY_LEAD_IN_BELOW_TABLE_SERVER)).toBe(DE.PREVIEW_LEAD_IN_BELOW_TABLE);
    expect(DE.PREVIEW_LEAD_IN_BELOW_TABLE).toBe(
      'Die Aufnahme beginnt unter dem Tisch des Simulators — im Simulator kann sie nicht abgespielt werden.',
    );
  });

  test.each([
    'Die Aufnahme führt unter die Tischebene — bitte eine neue Aufnahme oberhalb des Tisches machen.',
    'Zielpunkt liegt unter der Tischebene.',
    `${REPLAY_LEAD_IN_BELOW_TABLE_SERVER} `,
    REPLAY_LEAD_IN_BELOW_TABLE_SERVER.slice(0, -1),
    '',
  ])('returns %j unchanged', (message) => {
    expect(previewMessageDe(message)).toBe(message);
  });

  test('the seam refusal stays verbatim', () => {
    // handlers/trajectory.py::refuse_wrapped_joint_jumps, formatted for joint 4.
    const seam = 'Die Aufnahme wurde bei Gelenk 4 über die ±180°-Grenze gedreht. '
      + 'Der Motor kann diese Grenze nicht überfahren und würde beim Abspielen fast '
      + 'eine ganze Umdrehung zurückdrehen. Bitte neu aufnehmen, ohne Gelenk 4 über '
      + 'die Grenze zu drehen.';
    expect(previewMessageDe(seam)).toBe(seam);
  });
});

function readTrajectoryPy() {
  const file = path.resolve(
    process.cwd(),
    '../physical_ai_server/physical_ai_server/workflow/handlers/trajectory.py',
  );
  // A missing file FAILS — the lockstep must never pass vacuously.
  return fs.readFileSync(file, 'utf8');
}

describe('lockstep with handlers/trajectory.py', () => {
  test('the server source contains the constant once adjacent literals are joined', () => {
    const raw = readTrajectoryPy();
    const joined = raw.replace(/'\s*\n\s*'/g, '');
    expect(joined.includes(REPLAY_LEAD_IN_BELOW_TABLE_SERVER)).toBe(true);
  });
});

// ── WP9: builders, ids and the two gate ladders ──────────────────────────────

const SCENE = {
  version: 1,
  objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0.02, yaw: 0.3 }],
  zones: [{ min: [0.1, -0.05, 0], max: [0.2, 0.05, 0.1] }],
};

describe('preview program builders (spec §4.4)', () => {
  test('recording preview deep-equals the documented JSON, scene verbatim', () => {
    const program = buildRecordingPreviewProgram({
      name: 'Greifen links',
      fps: 25,
      points: [[0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8, 0.0]],
      simScene: SCENE,
      tempo: 2.0,
    });
    expect(program).toEqual({
      blocks: { languageVersion: 0, blocks: [
        { type: 'edubotics_replay_trajectory', id: 'vorschau-1',
          fields: { NAME: 'Greifen links' },
          next: { block: { type: 'edubotics_wait_seconds', id: 'vorschau-2',
            inputs: { SECONDS: { shadow: { type: 'math_number', fields: { NUM: 1.5 } } } } } } },
      ] },
      sim: { enabled: true, objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0.02, yaw: 0.3 }] },
      zones: [{ min: [0.1, -0.05, 0], max: [0.2, 0.05, 0.1] }],
      tempo: 2.0,
      trajectories: { 'Greifen links': { fps: 25, points: [[0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8, 0.0]] } },
    });
    expect(Object.keys(program)).toEqual(['blocks', 'sim', 'zones', 'tempo', 'trajectories']);
  });

  test('destination preview deep-equals the documented JSON and sends only that entry', () => {
    const entry = {
      id: 'd_4f1c9a2e', name: 'Ablage', kind: 'pin', source: 'camera', robot_type: 'omx_f',
      x: 0.182, y: -0.064, z: 0.012,
    };
    const program = buildDestinationPreviewProgram({ entry, simScene: SCENE, tempo: 2.0 });
    expect(program).toEqual({
      blocks: { languageVersion: 0, blocks: [
        { type: 'edubotics_move_to', id: 'vorschau-1',
          inputs: { DESTINATION: { block: { type: 'edubotics_destination_ref', id: 'vorschau-2',
            fields: { NAME: 'Ablage' } } } },
          next: { block: { type: 'edubotics_wait_seconds', id: 'vorschau-3',
            inputs: { SECONDS: { shadow: { type: 'math_number', fields: { NUM: 1.5 } } } } } } },
      ] },
      sim: { enabled: true, objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0.02, yaw: 0.3 }] },
      zones: [{ min: [0.1, -0.05, 0], max: [0.2, 0.05, 0.1] }],
      tempo: 2.0,
      destinations: [{ name: 'Ablage', kind: 'pin', x: 0.182, y: -0.064, z: 0.012 }],
    });
  });

  test('an absent scene sends empty objects and zones (never undefined)', () => {
    const program = buildRecordingPreviewProgram({ name: 'A', fps: 25, points: [], simScene: null, tempo: 1.0 });
    expect(program.sim).toEqual({ enabled: true, objects: [] });
    expect(program.zones).toEqual([]);
    expect(PREVIEW_HOLD_S).toBe(1.5);
  });
});

describe('preview ids', () => {
  test('recording workflow id keeps the first 8 hex digits, lower-case', () => {
    expect(previewWorkflowIdForRecording('3f9a1c0d-77aa-4bbb-8ccc-0123456789ab')).toBe('vorschau-aufnahme-3f9a1c0d');
    expect(previewWorkflowIdForRecording('3F9A-1C0D-FFFF')).toBe('vorschau-aufnahme-3f9a1c0d');
    expect(previewWorkflowIdForDestination('d_4f1c9a2e')).toBe('vorschau-ziel-d_4f1c9a2e');
  });

  test('result keys and the prefix test', () => {
    expect(previewKeyForRecording('t3')).toBe('rec:t3');
    expect(previewKeyForDestination('d_1')).toBe('dest:d_1');
    expect(PREVIEW_ID_PREFIX).toBe('vorschau-');
    expect(isPreviewWorkflowId('vorschau-ziel-d_1')).toBe(true);
    expect(isPreviewWorkflowId('wf-1')).toBe(false);
    expect(isPreviewWorkflowId(null)).toBe(false);
    expect(isPreviewWorkflowId(42)).toBe(false);
  });
});

// Every gate open: a recording of the rig's own arm, a saved workflow.
const OPEN = Object.freeze({
  heartbeatStatus: 'connected', runState: 'idle', paused: false, teachOpen: false,
  jogHandGuideOn: false, simMode: false, activeTutorialId: null, rsLeaderOn: false,
  asset: { kind: 'recording', id: 't1', name: 'A', robotProfile: 'omx_f' },
  robotType: 'omx_f', workflowId: 'wf-1', inFlight: false,
});

describe('previewBlockReason — each reason when only its condition holds', () => {
  test('null when every gate is open', () => {
    expect(previewBlockReason(OPEN)).toBeNull();
    expect(previewBlockReason({ ...OPEN, asset: { kind: 'pin', id: 'd1', name: 'Z' }, workflowId: null })).toBeNull();
  });

  test.each([
    ['offline', { heartbeatStatus: 'disconnected' }],
    ['running', { runState: 'running' }],
    ['running', { paused: true }],
    ['teach', { teachOpen: true }],
    ['handguide', { jogHandGuideOn: true }],
    ['tutorial', { activeTutorialId: 'tut-1' }],
    ['leader', { rsLeaderOn: true }],
    ['leaderUnknown', { rsLeaderUnknown: true }],
    ['otherRobot', { asset: { kind: 'recording', id: 't1', name: 'A', robotProfile: 'edu6_studio' } }],
    ['unsaved', { workflowId: null }],
    ['inFlight', { inFlight: true }],
  ])('%s', (reason, over) => {
    expect(previewBlockReason({ ...OPEN, ...over })).toBe(reason);
  });

  test('a tutorial blocks only outside the simulator', () => {
    expect(previewBlockReason({ ...OPEN, activeTutorialId: 'tut-1', simMode: true })).toBeNull();
  });

  test('the order is the contract: an earlier reason wins over every later one', () => {
    const all = {
      ...OPEN, heartbeatStatus: 'x', runState: 'running', teachOpen: true, jogHandGuideOn: true,
      activeTutorialId: 't', rsLeaderOn: true, rsLeaderUnknown: true, workflowId: null, inFlight: true,
      asset: { kind: 'recording', id: 't1', name: 'A', robotProfile: 'edu6_studio' },
    };
    const order = ['offline', 'running', 'teach', 'handguide', 'tutorial', 'leader', 'leaderUnknown', 'otherRobot', 'unsaved', 'inFlight'];
    const clear = [
      { heartbeatStatus: 'connected' }, { runState: 'idle' }, { teachOpen: false }, { jogHandGuideOn: false },
      { activeTutorialId: null }, { rsLeaderOn: false }, { rsLeaderUnknown: false },
      { asset: { kind: 'recording', id: 't1', name: 'A', robotProfile: 'omx_f' } },
      { workflowId: 'wf' }, { inFlight: false },
    ];
    let state = all;
    order.forEach((reason, i) => {
      expect(previewBlockReason(state)).toBe(reason);
      state = { ...state, ...clear[i] };
    });
    expect(previewBlockReason(state)).toBeNull();
  });

  test('every toasted reason has its German title', () => {
    expect(PREVIEW_BLOCK_TITLES_DE).toEqual({
      offline: 'Keine Verbindung zum Roboter-Dienst.',
      running: 'Ein Programm läuft gerade – erst auf „Stopp" drücken.',
      teach: 'Erst Vormachen beenden.',
      handguide: 'Der Arm ist freigeschaltet – bitte zuerst festsetzen.',
      tutorial: 'Während eines Lernpfads kann der Simulator nicht geöffnet werden.',
      leader: 'Solange der Leader-Arm eingeschaltet ist, gibt es keine Vorschau. Bitte zuerst oben „Leader abschalten".',
      leaderUnknown: 'Gerade ist nicht klar, ob der Leader-Arm eingeschaltet ist – die Steuerung antwortet nicht. Bitte gleich noch einmal versuchen.',
      otherRobot: 'Diese Bewegung wurde mit einem anderen Robotertyp aufgenommen und kann hier nicht abgespielt werden.',
      unsaved: 'Bitte zuerst den Workflow speichern.',
    });
  });
});

describe('simEntryBlockReason — the header toggle ladder', () => {
  const IDLE = { simRunActive: false, simMode: false, activeTutorialId: null, teachOpen: false, jogHandGuideOn: false };
  test('entry', () => {
    expect(simEntryBlockReason(IDLE)).toBeNull();
    expect(simEntryBlockReason({ ...IDLE, activeTutorialId: 't' })).toBe('tutorial');
    expect(simEntryBlockReason({ ...IDLE, teachOpen: true })).toBe('teach');
    expect(simEntryBlockReason({ ...IDLE, jogHandGuideOn: true })).toBe('handguide');
    expect(simEntryBlockReason({ ...IDLE, activeTutorialId: 't', teachOpen: true, jogHandGuideOn: true })).toBe('tutorial');
    expect(simEntryBlockReason({ ...IDLE, teachOpen: true, jogHandGuideOn: true })).toBe('teach');
  });
  test('inside the simulator only a sim run blocks leaving', () => {
    const inSim = { ...IDLE, simMode: true, activeTutorialId: 't', teachOpen: true, jogHandGuideOn: true };
    expect(simEntryBlockReason(inSim)).toBeNull();
    expect(simEntryBlockReason({ ...inSim, simRunActive: true })).toBe('simRun');
  });
  test('the titles moved out of WorkshopPage verbatim', () => {
    expect(SIM_ENTRY_BLOCK_TITLES_DE).toEqual({
      simRun: 'Während ein Simulationslauf läuft, kann der Simulator nicht beendet werden — bitte zuerst stoppen.',
      tutorial: 'Während ein Lernpfad aktiv ist, kann der Simulator nicht gestartet werden — bitte den Lernpfad zuerst beenden.',
      teach: DE.TEACH_SIM_ENTRY_BLOCKED,
      handguide: 'Solange der Arm freigeschaltet ist, kann der Simulator nicht gestartet werden — bitte den Arm zuerst festsetzen.',
    });
    expect(SIM_TOGGLE_DEFAULT_TITLE_DE).toBe(
      'Programm auf einem virtuellen Roboter testen — ohne echten Roboter und ohne Kalibrierung');
  });
});

// hooks/useRsBridgeStatus::UNAVAILABLE and a live answer, as the hook returns them.
const BRIDGE_DOWN = { available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false };
const BRIDGE_LEADER = { available: true, followerOnly: false, hasLeader: true, busy: false, leaderOn: true };
const BRIDGE_FOLLOWER = { available: true, followerOnly: true, hasLeader: true, busy: false, leaderOn: false };

describe('previewLeaderGate — the preview fails CLOSED on an unanswered bridge probe', () => {
  test.each([
    ['bridge down, omx_full caps', BRIDGE_DOWN, { has_leader: true }, { rsLeaderOn: false, rsLeaderUnknown: true }],
    ['bridge down, caps not yet pushed', BRIDGE_DOWN, null, { rsLeaderOn: false, rsLeaderUnknown: true }],
    ['bridge down, leader-less profile', BRIDGE_DOWN, { has_leader: false }, { rsLeaderOn: false, rsLeaderUnknown: false }],
    ['no bridge object at all, leader-less', undefined, { has_leader: false }, { rsLeaderOn: false, rsLeaderUnknown: false }],
    ['no bridge object at all, has leader', undefined, { has_leader: true }, { rsLeaderOn: false, rsLeaderUnknown: true }],
    ['bridge says leader on', BRIDGE_LEADER, { has_leader: true }, { rsLeaderOn: true, rsLeaderUnknown: false }],
    ['bridge says follower only', BRIDGE_FOLLOWER, { has_leader: true }, { rsLeaderOn: false, rsLeaderUnknown: false }],
    ['bridge answers has_leader false', { ...BRIDGE_FOLLOWER, hasLeader: false }, null, { rsLeaderOn: false, rsLeaderUnknown: false }],
  ])('%s', (_label, bridge, caps, expected) => {
    expect(previewLeaderGate(bridge, caps)).toEqual(expected);
  });

  test('a probe timeout on a leader rig refuses the preview in German', () => {
    const reason = previewBlockReason({ ...OPEN, ...previewLeaderGate(BRIDGE_DOWN, { has_leader: true }) });
    expect(reason).toBe('leaderUnknown');
    expect(PREVIEW_BLOCK_TITLES_DE[reason]).toBe(DE.PREVIEW_BLOCK_LEADER_UNKNOWN);
  });
});
