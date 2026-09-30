// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Diese Sitzung" from the /task/status feed (spec §3.2 R1–R17). A small
// simulator plays the robot's tick sequences — 30 Hz, floored seconds — and
// the page's own intents, and the tests read the resulting episode rows and
// finish state.

import TaskPhase from '../../../constants/taskPhases';
import {
  COLLISION_END_NOTE_DE,
  EMPTY_FINISH,
  EMPTY_RECORD_SESSION,
  advanceRecordSession,
  applyRecordIntent,
  applyRegisterStatus,
  applyUploadStatus,
  dismissRecordSession,
  noteCollision,
  noteLink,
  noteRecordNotice,
} from '../recordSession';

const { READY, WARMING_UP, RESETTING, RECORDING, SAVING } = TaskPhase;
const WALL0 = 1_700_000_000_000;
const REPO = 'schule-A/omx_f_Wuerfel';

const SNAPSHOT = Object.freeze({
  taskName: 'Würfel', userId: 'schule-A', robotType: 'omx_f', pushToHub: true, privateMode: true,
  numEpisodes: 3, episodeTime: 10, warmupTime: 5, resetTime: 5, fps: 30,
});

const IDLE = Object.freeze({
  phase: READY, running: false, taskType: '', currentEpisodeNumber: 0, proceedTime: 0, totalTime: 0,
  numEpisodes: 0, episodeTime: 0, warmupTime: 0, resetTime: 0, fps: 0, pushToHub: false,
  taskName: 'idle', robotType: 'omx_f', userId: '', recordWarn: '', receivedAt: 0, receivedWallMs: WALL0,
});

function sim({ snapshot = SNAPSHOT } = {}) {
  let s = EMPTY_RECORD_SESSION;
  let prev = IDLE;
  let t = 0; // performance.now()
  const wall = () => WALL0 + t;
  const recordFields = {
    running: true, taskType: 'record', taskName: snapshot.taskName, robotType: snapshot.robotType,
    userId: snapshot.userId, pushToHub: snapshot.pushToHub, numEpisodes: snapshot.numEpisodes,
    episodeTime: snapshot.episodeTime, warmupTime: snapshot.warmupTime, resetTime: snapshot.resetTime,
    fps: snapshot.fps,
  };
  const api = {
    get s() { return s; },
    get t() { return t; },
    wall,
    advance(ms) { t += ms; },
    /** One tick; `fields` merge over the last status like the reducer does. */
    tick(fields, dt = 33) {
      t += dt;
      const next = { ...prev, recordWarn: '', ...fields, receivedAt: t, receivedWallMs: wall() };
      s = advanceRecordSession(s, prev, next);
      prev = next;
      return s;
    },
    rec(fields, dt) { return api.tick({ ...recordFields, ...fields }, dt); },
    /** A phase that lasts `seconds` at 30 Hz, floored like proceed_time. */
    phase(phase, seconds, extra = {}) {
      const total = extra.totalTime ?? seconds;
      const n = Math.max(1, Math.round(seconds * 30));
      for (let i = 0; i < n; i += 1) {
        api.rec({ phase, proceedTime: Math.floor(i / 30), totalTime: total, ...extra });
      }
    },
    saving(count, n = 2) {
      for (let i = 0; i < n; i += 1) api.rec({ phase: SAVING, proceedTime: 0, totalTime: 0, currentEpisodeNumber: count });
    },
    ready(fields = {}) {
      return api.tick({ ...recordFields, running: false, phase: READY, proceedTime: 0, totalTime: 0, ...fields });
    },
    idle() { return api.tick({ ...IDLE }); },
    intent(i) { s = applyRecordIntent(s, { at: wall(), ...i }); return s; },
    start() { return api.intent({ kind: 'start', snapshot }); },
    collide() {
      s = noteCollision(s, prev, { active: true, wasActive: false, receivedAt: t, receivedWallMs: wall() });
      return s;
    },
    notice(n) { s = noteRecordNotice(s, { at: wall(), ...n }); return s; },
    upload(u) { s = applyUploadStatus(s, { at: wall(), ...u }); return s; },
    register(r) { s = applyRegisterStatus(s, { at: wall(), ...r }); return s; },
    link(h) { s = noteLink(s, h); return s; },
    dismiss(running = false) { s = dismissRecordSession(s, { running }); return s; },
    get prev() { return prev; },
  };
  return api;
}

const outcomes = (s) => s.episodes.map((e) => `${e.n}:${e.outcome}`);

// A full normal episode: warm-up once, then run → save → count+1.
function normalEpisode(r, count, { E = 10, Z = 5, last = false } = {}) {
  r.phase(RECORDING, E, { currentEpisodeNumber: count - 1 });
  r.saving(count - 1);
  if (last) {
    r.saving(count, 3);
  } else {
    r.phase(RESETTING, Z, { currentEpisodeNumber: count });
  }
}

describe('R1 start', () => {
  it('a start intent then the first record tick opens a session started here', () => {
    const r = sim();
    r.start();
    expect(r.s.pendingStart.snapshot).toEqual(SNAPSHOT);
    expect(r.s.active).toBe(false);
    r.phase(WARMING_UP, 1, { totalTime: 5 });
    expect(r.s).toMatchObject({ id: 1, active: true, startedHere: true, adopted: false, pendingStart: null });
    expect(r.s.snapshot).toEqual(SNAPSHOT);
    expect(r.s.startedWallMs).toBe(WALL0 + 33);
  });

  it('a session found running is adopted from the wire (userId only via robotNamesMe)', () => {
    const r = sim();
    r.rec({ phase: RECORDING, proceedTime: 4, totalTime: 10, currentEpisodeNumber: 2, userId: '' });
    expect(r.s).toMatchObject({ active: true, startedHere: false, adopted: true, savedCount: 2, adoptedSavedCount: 2 });
    expect(r.s.snapshot).toMatchObject({ taskName: 'Würfel', userId: '', pushToHub: true, privateMode: null });
    expect(r.s.run).toMatchObject({ episode: 3, resolved: false });
  });

  it('inference ticks never open a record session', () => {
    const r = sim();
    r.tick({ running: true, taskType: 'inference', phase: TaskPhase.INFERENCING });
    expect(r.s).toBe(EMPTY_RECORD_SESSION);
  });

  it('carries an upload of the SAME repo into the new session, replaces another', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    expect(r.s.finish.state).toBe('uploading');
    r.dismiss();
    expect(r.s.finish).toMatchObject({ state: 'uploading', dismissed: true, expectedRepoId: REPO });
    r.start();
    r.rec({ phase: WARMING_UP, proceedTime: 0, totalTime: 5, currentEpisodeNumber: 0 });
    expect(r.s.finish.state).toBe('uploading');

    const q = sim({ snapshot: { ...SNAPSHOT, taskName: 'Anders' } });
    q.start();
    q.phase(RECORDING, 10);
    q.saving(0);
    q.saving(1);
    q.ready({ currentEpisodeNumber: 1 });
    q.dismiss();
    q.intent({ kind: 'start', snapshot: SNAPSHOT });
    q.rec({ phase: WARMING_UP, proceedTime: 0, totalTime: 5, taskName: 'Würfel' });
    expect(q.s.finish).toEqual(EMPTY_FINISH);
  });
});

describe('a normal session', () => {
  it('warm-up, three timed episodes, finish, upload, register', () => {
    const r = sim();
    r.start();
    r.phase(WARMING_UP, 5);
    normalEpisode(r, 1);
    expect(r.s.sawReset).toBe(true);
    normalEpisode(r, 2);
    normalEpisode(r, 3, { last: true });
    expect(outcomes(r.s)).toEqual(['1:saved', '2:saved', '3:saved']);
    expect(r.s.episodes.every((e) => !e.early)).toBe(true);
    expect(r.s.episodes[0].durationS).toBeGreaterThan(9.5);
    expect(r.s.finish.state).toBe('finalizing'); // R8
    r.ready({ currentEpisodeNumber: 3 });
    expect(r.s.active).toBe(false);
    expect(r.s.finish).toMatchObject({ state: 'uploading', expectedRepoId: REPO, endNote: null });
    expect(r.s.finish.endedAt).toBe(r.wall());

    r.upload({ repoId: REPO, status: 'Uploading', percentage: 40.4 });
    expect(r.s.finish.uploadPct).toBe(40);
    r.upload({ repoId: 'someone/else', status: 'Success' });
    expect(r.s.finish.state).toBe('uploading');
    r.upload({ repoId: REPO, status: 'Success' });
    expect(r.s.finish).toMatchObject({ state: 'registering', repoId: REPO });
    r.register({ repoId: REPO, state: 'pending' });
    expect(r.s.finish.state).toBe('registering');
    r.register({ repoId: REPO, state: 'done' });
    expect(r.s.finish).toMatchObject({ state: 'done', registerState: 'done' });
  });

  it('returns the SAME session reference for ticks that change nothing', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 2);
    const before = r.s;
    r.rec({ phase: RECORDING, proceedTime: 1, totalTime: 10 });
    expect(r.s).toBe(before);
  });
});

describe('outcomes', () => {
  it('„Jetzt speichern" saves early (R3 early flag from the R4 duration)', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    r.saving(0);
    r.phase(RESETTING, 1, { currentEpisodeNumber: 1 });
    expect(r.s.episodes[0]).toMatchObject({ n: 1, outcome: 'saved', early: true });
    expect(r.s.episodes[0].durationS).toBeCloseTo(4, 0);
  });

  it('R5: RECORDING → RESETTING without a count is a redo (another client, joystick)', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 3);
    r.phase(RESETTING, 1);
    expect(outcomes(r.s)).toEqual(['1:redo']);
    expect(r.s.episodes[0].durationS).toBeCloseTo(3, 0);
  });

  it('H15: a redo with Zurücksetzen = 0 goes RECORDING → RECORDING and is still a redo', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    r.rec({ phase: RECORDING, proceedTime: 0, totalTime: 10 });
    expect(outcomes(r.s)).toEqual(['1:redo']);
    expect(r.s.run).toMatchObject({ episode: 1, resolved: false });
  });

  it('the page\'s own redo is resolved by the acknowledgement, not by a phase change', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    const sentAt = r.wall();
    r.advance(40);
    r.intent({ kind: 'redo_ack', sentAt });
    expect(outcomes(r.s)).toEqual(['1:redo']);
    expect(r.s.awaitNewRun).toBe(true);
    expect(r.s.lastRedoSentAt).toBe(sentAt);
    // reset 0: the new run starts at 0 s in the same phase
    r.rec({ phase: RECORDING, proceedTime: 0, totalTime: 10 });
    expect(r.s.run).toMatchObject({ episode: 1, resolved: false });
    expect(r.s.awaitNewRun).toBe(false);
    expect(outcomes(r.s)).toEqual(['1:redo']);
  });

  it('an acknowledgement arriving after the new run already started leaves that run alone', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    const sentAt = r.wall();
    r.rec({ phase: RECORDING, proceedTime: 0, totalTime: 10 }); // new run (R7)
    r.intent({ kind: 'redo_ack', sentAt });
    expect(outcomes(r.s)).toEqual(['1:redo']);
    expect(r.s.run.resolved).toBe(false);
    expect(r.s.awaitNewRun).toBe(false);
  });

  it('R6: SAVING → RESETTING without a count is a frame-drop discard', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.phase(RESETTING, 1);
    expect(outcomes(r.s)).toEqual(['1:drop']);
  });

  it('every discarded row carries a duration', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 3);
    r.phase(RESETTING, 1);
    r.phase(RECORDING, 10);
    r.saving(0);
    r.phase(RESETTING, 1);
    expect(r.s.episodes.map((e) => e.durationS > 0)).toEqual([true, true]);
  });
});

describe('collisions (R12, Q7, F1, F7b)', () => {
  it('a collision while RECORDING is „Kollision, verworfen" with a duration', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    r.collide();
    expect(outcomes(r.s)).toEqual(['1:collision']);
    expect(r.s.episodes[0].durationS).toBeCloseTo(4, 0);
    expect(r.s.collisionOpen).toBe(true);
    // a plain resume: reset, then the same episode again
    r.phase(RESETTING, 1);
    r.phase(RECORDING, 1);
    expect(r.s.run).toMatchObject({ episode: 1, resolved: false });
    expect(r.s.collisionOpen).toBe(false);
    expect(outcomes(r.s)).toEqual(['1:collision']);
  });

  it('a collision within the first second, then a resume with reset 0, is a new run', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 0.5);
    r.collide();
    r.rec({ phase: RECORDING, proceedTime: 0, totalTime: 10 });
    expect(r.s.run.resolved).toBe(false);
    expect(outcomes(r.s)).toEqual(['1:collision']);
  });

  it('Q7: a collision during SAVING is „Gespeichert", confirmed by the count', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0, 1);
    r.collide();
    expect(outcomes(r.s)).toEqual(['1:saved']);
    expect(r.s.episodes[0].provisional).toBe(true);
    // the server counted the committed episode before rewinding
    r.phase(RESETTING, 1, { currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:saved']);
    expect(r.s.episodes[0].provisional).toBe(false);
    expect(r.s.savedCount).toBe(1);
    // the next save gets its own row
    r.phase(RECORDING, 10, { currentEpisodeNumber: 1 });
    r.saving(1);
    r.phase(RESETTING, 1, { currentEpisodeNumber: 2 });
    expect(outcomes(r.s)).toEqual(['1:saved', '2:saved']);
  });

  it('Q7: … and corrected to „Kollision, verworfen" when the robot did not count it', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0, 1);
    r.collide();
    r.phase(RESETTING, 1, { currentEpisodeNumber: 0 });
    expect(outcomes(r.s)).toEqual(['1:collision']);
    expect(r.s.savedCount).toBe(0);
    r.phase(RECORDING, 10);
    r.saving(0);
    r.phase(RESETTING, 1, { currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:collision', '1:saved']);
  });

  it('F7b: a collision while finalizing resolves nothing; the finish decides', () => {
    const r = sim({ snapshot: { ...SNAPSHOT, numEpisodes: 1 } });
    r.start();
    r.phase(RECORDING, 4);
    r.intent({ kind: 'keep_end' });
    r.saving(0);
    r.collide();
    expect(r.s.episodes).toEqual([]);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:saved']);
    expect(r.s.episodes[0].early).toBe(true);
  });

  it('F1: a forced resume ends the session with the collision end note', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.phase(RESETTING, 1, { currentEpisodeNumber: 1 });
    r.phase(RECORDING, 3, { currentEpisodeNumber: 1 });
    r.collide();
    r.ready({ currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:saved', '2:collision']);
    expect(r.s.finish).toMatchObject({ state: 'uploading', endNote: COLLISION_END_NOTE_DE });
  });

  it('a collision outside an active session changes nothing', () => {
    const r = sim();
    const before = r.s;
    r.collide();
    expect(r.s).toBe(before);
  });
});

describe('ending (R9, R13)', () => {
  it('„Behalten und beenden" keeps the partial episode (counted by the finish)', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    r.intent({ kind: 'keep_end' });
    expect(r.s.finish).toMatchObject({ state: 'finalizing', fromIntent: true });
    expect(r.s.intent).toMatchObject({ kind: 'keep_end', episode: 1 });
    r.saving(0);
    r.ready({ currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:saved']);
    expect(r.s.episodes[0].early).toBe(true);
    expect(r.s.finish.state).toBe('uploading');
  });

  it('Q3: a run under 1 s dropped by FINISH leaves no row', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.phase(RESETTING, 1, { currentEpisodeNumber: 1 });
    r.phase(RECORDING, 0.5, { currentEpisodeNumber: 1 });
    r.intent({ kind: 'keep_end' });
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:saved']);
  });

  it('Q8: a run kept too soon after „Wiederholen" is „Beim Beenden verworfen"', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    const sentAt = r.wall();
    r.intent({ kind: 'redo_ack', sentAt });
    r.phase(RECORDING, 2);
    r.intent({ kind: 'keep_end' });
    r.saving(0);
    r.ready({ currentEpisodeNumber: 0 });
    expect(outcomes(r.s)).toEqual(['1:redo', '1:ended']);
    expect(r.s.finish.state).toBe('nothing');
  });

  it('„Verwerfen und beenden" (reset 0): the discarded run is „ended", the run FINISH dropped has no row', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.phase(RESETTING, 1, { currentEpisodeNumber: 1 });
    r.phase(RECORDING, 4, { currentEpisodeNumber: 1 });
    r.intent({ kind: 'discard_end' });
    const sentAt = r.wall();
    // the robot starts the next run before the acknowledgement arrives
    r.rec({ phase: RECORDING, proceedTime: 0, totalTime: 10, currentEpisodeNumber: 1 });
    r.intent({ kind: 'discard_ack', sentAt });
    r.rec({ phase: RECORDING, proceedTime: 0, totalTime: 10, currentEpisodeNumber: 1 });
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    expect(outcomes(r.s)).toEqual(['1:saved', '2:ended']);
    expect(r.s.finish.state).toBe('uploading');
  });

  it('„Verwerfen und beenden" (reset > 0): the ack resolves the run, RESETTING adds nothing', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    r.intent({ kind: 'discard_end' });
    const sentAt = r.wall();
    r.intent({ kind: 'discard_ack', sentAt });
    r.phase(RESETTING, 0.1);
    r.saving(0);
    r.ready({ currentEpisodeNumber: 0 });
    expect(outcomes(r.s)).toEqual(['1:ended']);
    expect(r.s.finish.state).toBe('nothing');
  });

  it('… and when RESETTING arrives before the acknowledgement it is still „ended"', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 4);
    r.intent({ kind: 'discard_end' });
    const sentAt = r.wall();
    r.phase(RESETTING, 0.1);
    r.intent({ kind: 'discard_ack', sentAt });
    expect(outcomes(r.s)).toEqual(['1:ended']);
  });

  it('a run ≥ 1 s that the end dropped without an intent is „drop"', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 3);
    r.saving(0);
    r.ready({ currentEpisodeNumber: 0 });
    expect(outcomes(r.s)).toEqual(['1:drop']);
  });

  it('„Beenden" in warm-up ends with nothing saved', () => {
    const r = sim();
    r.start();
    r.phase(WARMING_UP, 1, { totalTime: 5 });
    r.intent({ kind: 'end' });
    expect(r.s.intent).toMatchObject({ kind: 'end', episode: null });
    r.saving(0);
    r.ready();
    expect(r.s.episodes).toEqual([]);
    expect(r.s.finish.state).toBe('nothing');
  });

  it('a failed end command (clear) takes the intent\'s finalizing back', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 2);
    r.intent({ kind: 'keep_end' });
    r.intent({ kind: 'clear' });
    expect(r.s.intent).toBeNull();
    expect(r.s.finish.state).toBe('idle');
  });

  it('… but not a finalizing the robot reported itself (R8)', () => {
    const r = sim({ snapshot: { ...SNAPSHOT, numEpisodes: 1 } });
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.intent({ kind: 'clear' });
    expect(r.s.finish.state).toBe('finalizing');
  });

  it('local only: no upload', () => {
    const r = sim({ snapshot: { ...SNAPSHOT, pushToHub: false } });
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    expect(r.s.finish.state).toBe('local_done');
  });

  it('a terminating tick carrying a warning is an upload that was blocked', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.notice({ kind: 'warn', text: 'Namespace gehört dir nicht.' });
    r.ready({ currentEpisodeNumber: 1, recordWarn: 'Namespace gehört dir nicht.' });
    expect(r.s.finish).toMatchObject({ state: 'upload_failed', message: 'Namespace gehört dir nicht.', endNote: null });
  });

  it('a warning within 5 s of the end becomes the end note (low disk, frame drop)', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.notice({ kind: 'warn', text: 'Der Speicher ist fast voll.' });
    r.ready({ currentEpisodeNumber: 1 });
    expect(r.s.finish.endNote).toBe('Der Speicher ist fast voll.');

    const q = sim();
    q.start();
    q.phase(RECORDING, 10);
    q.notice({ kind: 'warn', text: 'Die Szenen-Kamera zeigt dasselbe Bild.' });
    q.advance(6000);
    q.saving(0);
    q.saving(1);
    q.ready({ currentEpisodeNumber: 1 });
    expect(q.s.finish.endNote).toBeNull();
  });

  it('an adopted session tracks the first upload it sees (repo unknown)', () => {
    const r = sim();
    r.rec({ phase: RECORDING, proceedTime: 1, totalTime: 10, currentEpisodeNumber: 1, userId: '' });
    r.saving(1);
    r.saving(2);
    r.ready({ currentEpisodeNumber: 2 });
    expect(r.s.finish).toMatchObject({ state: 'uploading', expectedRepoId: null });
    r.upload({ repoId: 'x/omx_f_a', status: 'Uploading', percentage: 10 });
    expect(r.s.finish.repoId).toBe('x/omx_f_a');
    r.upload({ repoId: 'y/other', status: 'Uploading', percentage: 90 });
    expect(r.s.finish.uploadPct).toBe(10);
  });

  it('the idle identity tick also ends an active session (link came back after the end)', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.idle();
    expect(r.s.active).toBe(false);
    expect(r.s.finish.state).toBe('uploading');
    // V2-6: the end itself was not seen, so what the upload did is unknown
    expect(r.s.finish.linkLost).toBe(true);
  });

  it('… until a status of that upload arrives: then it is known again', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.idle();
    expect(r.s.finish.linkLost).toBe(true);
    r.upload({ repoId: REPO, status: 'Uploading', percentage: 40 });
    expect(r.s.finish).toMatchObject({ linkLost: false, uploadPct: 40 });
    // a status for someone else's repo says nothing about ours
    const q = sim();
    q.start();
    q.phase(RECORDING, 10);
    q.saving(0);
    q.saving(1);
    q.idle();
    q.upload({ repoId: 'x/other', status: 'Uploading', percentage: 40 });
    expect(q.s.finish.linkLost).toBe(true);
  });

  it('the terminating record tick itself knows the end: no lost link', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    expect(r.s.finish.linkLost).toBe(false);
  });

  it('V1-2: with upload OFF a warning on the terminating tick is a failed finalize', () => {
    const r = sim({ snapshot: { ...SNAPSHOT, pushToHub: false } });
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    const why = 'Datensatz konnte nicht abgeschlossen werden — die Aufnahme ist unvollständig und muss neu aufgenommen werden.';
    r.notice({ kind: 'warn', text: why });
    r.ready({ currentEpisodeNumber: 1, recordWarn: why });
    expect(r.s.finish).toMatchObject({ state: 'finalize_failed', message: why, endNote: null });
  });

  it('… and with upload ON, the server\'s finalize sentence is a failed finalize too', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    const why = 'Datensatz konnte nicht abgeschlossen werden — die Aufnahme ist unvollständig und muss neu aufgenommen werden.';
    r.ready({ currentEpisodeNumber: 1, recordWarn: why });
    expect(r.s.finish).toMatchObject({ state: 'finalize_failed', message: why });
  });
});

describe('R10 / R11 notices', () => {
  it('a hard error ends the active session as stopped_error', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 3);
    r.notice({ kind: 'error', text: 'Die Kameras senden keine Bilder.' });
    expect(r.s).toMatchObject({ active: false, errorText: 'Die Kameras senden keine Bilder.' });
    expect(r.s.finish.state).toBe('stopped_error');
    // the idle tick afterwards changes nothing
    const before = r.s;
    r.idle();
    expect(r.s).toBe(before);
  });

  it('an error before the first tick only drops the pending start', () => {
    const r = sim();
    r.start();
    r.notice({ kind: 'error', text: 'Der Leader-Arm sendet keine Daten.' });
    expect(r.s.pendingStart).toBeNull();
    expect(r.s.finish.state).toBe('idle');
  });

  it('a warning while active is remembered for the end note; none while idle', () => {
    const r = sim();
    r.notice({ kind: 'warn', text: 'x' });
    expect(r.s).toBe(EMPTY_RECORD_SESSION);
    r.start();
    r.phase(RECORDING, 1);
    r.notice({ kind: 'warn', text: 'Kamera hängt.' });
    expect(r.s.lastWarn).toEqual({ text: 'Kamera hängt.', at: r.wall() });
  });

  it('start_failed drops the pending start', () => {
    const r = sim();
    r.start();
    r.intent({ kind: 'start_failed' });
    expect(r.s.pendingStart).toBeNull();
  });
});

describe('R14 upload matching', () => {
  function finished() {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    return r;
  }

  it('ignores a status older than the end (a previous upload of the same repo)', () => {
    const r = finished();
    const s = applyUploadStatus(r.s, { repoId: REPO, status: 'Success', at: r.s.finish.endedAt - 1 });
    expect(s).toBe(r.s);
  });

  it('a Failed status turns the finish into upload_failed with the server text', () => {
    const r = finished();
    r.upload({ repoId: REPO, status: 'Failed', message: 'Hochladen fehlgeschlagen: 403' });
    expect(r.s.finish).toMatchObject({ state: 'upload_failed', message: 'Hochladen fehlgeschlagen: 403' });
  });

  it('a register failure or skip still finishes, with the reason kept', () => {
    const r = finished();
    r.upload({ repoId: REPO, status: 'Success' });
    r.register({ repoId: REPO, state: 'skipped' });
    expect(r.s.finish).toMatchObject({ state: 'done', registerState: 'skipped' });
    const q = finished();
    q.upload({ repoId: REPO, status: 'Success' });
    q.register({ repoId: 'other/repo', state: 'done' });
    expect(q.s.finish.state).toBe('registering');
    q.register({ repoId: REPO, state: 'failed' });
    expect(q.s.finish).toMatchObject({ state: 'done', registerState: 'failed' });
  });

  it('R16: a link loss while uploading marks the upload state unknown', () => {
    const r = finished();
    r.link('connected');
    expect(r.s.finish.linkLost).toBe(false);
    r.link('timeout');
    expect(r.s.finish.linkLost).toBe(true);
    const before = r.s;
    r.link('disconnected');
    expect(r.s).toBe(before);
  });

  it('no link bookkeeping outside an upload', () => {
    const r = sim();
    const before = r.s;
    r.link('disconnected');
    expect(r.s).toBe(before);
  });
});

describe('R17 dismiss', () => {
  it('clears the card; an upload in flight stays tracked', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    r.dismiss();
    expect(r.s.episodes).toEqual([]);
    expect(r.s.finish).toMatchObject({ state: 'uploading', dismissed: true });
    r.upload({ repoId: REPO, status: 'Success' });
    expect(r.s.finish).toMatchObject({ state: 'registering', dismissed: true });
  });

  it('resets a settled finish to idle', () => {
    const r = sim({ snapshot: { ...SNAPSHOT, pushToHub: false } });
    r.start();
    r.phase(RECORDING, 10);
    r.saving(0);
    r.saving(1);
    r.ready({ currentEpisodeNumber: 1 });
    r.dismiss();
    expect(r.s.finish).toEqual(EMPTY_FINISH);
    expect(r.s.id).toBe(1);
  });

  it('is a no-op while a task runs', () => {
    const r = sim();
    r.start();
    r.phase(RECORDING, 1);
    const before = r.s;
    r.dismiss(true);
    expect(r.s).toBe(before);
  });
});
