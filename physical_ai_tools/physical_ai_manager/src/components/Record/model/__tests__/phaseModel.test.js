// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme phase model (spec §3.4): view rules in order, F6d episode
// numbers, the button table, the Phasenleiste, the Episodenleiste, the start
// blocks (Q5) and the form lock (F6b).

import TaskPhase from '../../../../constants/taskPhases';
import { EMPTY_RECORD_SESSION, EMPTY_FINISH } from '../../../../features/tasks/recordSession';
import RECORD_COPY from '../recordCopy';
import { STARTING_SLOW_MS, VIEW, deriveRecordView, deriveStartBlock, deriveView } from '../phaseModel';

const NOW = 1_700_000_000_000;

const FORM = Object.freeze({
  taskName: 'Würfel', taskInstruction: ['Greife.'], userId: 'schule-A', fps: 30, tags: [],
  warmupTime: 5, episodeTime: 20, resetTime: 5, numEpisodes: 3, pushToHub: true, privateMode: true,
});

const IDLE_STATUS = Object.freeze({
  phase: TaskPhase.READY, running: false, taskType: '', totalTime: 0, proceedTime: 0,
  currentEpisodeNumber: 0, numEpisodes: 0, episodeTime: 0, warmupTime: 0, resetTime: 0, fps: 0,
  pushToHub: false, topicReceived: true, capabilities: null, robotProfile: 'omx_full', robotType: 'omx_f',
});

const rec = (patch = {}) => ({
  ...IDLE_STATUS, running: true, taskType: 'record', numEpisodes: 3, episodeTime: 20, warmupTime: 5,
  resetTime: 5, fps: 30, pushToHub: true, ...patch,
});

const activeSession = (patch = {}) => ({
  ...EMPTY_RECORD_SESSION, id: 1, active: true, startedHere: true,
  snapshot: { ...FORM, robotType: 'omx_f', taskInstruction: undefined }, ...patch,
});

const model = (patch = {}) => deriveRecordView({
  heartbeat: 'connected',
  status: IDLE_STATUS,
  form: FORM,
  collision: { active: false },
  session: EMPTY_RECORD_SESSION,
  nowWallMs: NOW,
  elapsedS: 0,
  busy: false,
  disk: null,
  verdicts: null,
  bridge: null,
  activation: null,
  ...patch,
});

const ids = (m) => m.buttons.map((b) => `${b.id}${b.disabled ? '(off)' : ''}`);

describe('deriveView — rules in order', () => {
  it('collision wins over everything', () => {
    expect(deriveView({ heartbeat: 'timeout', status: rec({ phase: TaskPhase.RECORDING }), collision: { active: true } }))
      .toBe(VIEW.COLLISION);
  });

  it('a running inference is INFERENCE_BUSY', () => {
    expect(deriveView({ heartbeat: 'connected', status: rec({ taskType: 'inference', phase: TaskPhase.INFERENCING }) }))
      .toBe(VIEW.INFERENCE_BUSY);
    expect(deriveView({ heartbeat: 'connected', status: rec({ taskType: '', phase: TaskPhase.INFERENCE_LOADING }) }))
      .toBe(VIEW.INFERENCE_BUSY);
  });

  it('running maps the phase — even with the link lost', () => {
    const v = (phase, extra = {}) => deriveView({ heartbeat: 'timeout', status: rec({ phase }), ...extra });
    expect(v(TaskPhase.WARMING_UP)).toBe(VIEW.WARMUP);
    expect(v(TaskPhase.RECORDING)).toBe(VIEW.RECORDING);
    expect(v(TaskPhase.RESETTING)).toBe(VIEW.RESETTING);
    expect(v(TaskPhase.SAVING)).toBe(VIEW.SAVING);
    expect(v(TaskPhase.STOPPED)).toBe(VIEW.SAVING);
    const finalizing = activeSession({ finish: { ...EMPTY_FINISH, state: 'finalizing' } });
    expect(v(TaskPhase.SAVING, { session: finalizing })).toBe(VIEW.FINISHING);
  });

  it('then OFFLINE, CONNECTING, STARTING, FINISHING, READY', () => {
    expect(deriveView({ heartbeat: 'timeout', status: IDLE_STATUS })).toBe(VIEW.OFFLINE);
    expect(deriveView({ heartbeat: 'connected', status: { ...IDLE_STATUS, topicReceived: false } })).toBe(VIEW.CONNECTING);
    const pending = { ...EMPTY_RECORD_SESSION, pendingStart: { at: NOW, snapshot: {} } };
    expect(deriveView({ heartbeat: 'connected', status: IDLE_STATUS, session: pending })).toBe(VIEW.STARTING);
    const done = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'done' } };
    expect(deriveView({ heartbeat: 'connected', status: IDLE_STATUS, session: done })).toBe(VIEW.FINISHING);
    const nothing = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'nothing' } };
    expect(deriveView({ heartbeat: 'connected', status: IDLE_STATUS, session: nothing })).toBe(VIEW.READY);
    const dismissed = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'uploading', dismissed: true } };
    expect(deriveView({ heartbeat: 'connected', status: IDLE_STATUS, session: dismissed })).toBe(VIEW.READY);
  });
});

describe('STARTING (F2)', () => {
  const pending = (at) => ({ ...EMPTY_RECORD_SESSION, pendingStart: { at, snapshot: {} } });

  it('has no time limit and says so after 8 s', () => {
    const m = model({ session: pending(NOW - 1000) });
    expect(m.view).toBe(VIEW.STARTING);
    expect(m.pill.sub).toBe(RECORD_COPY.pill.startingSub);
    expect(ids(m)).toEqual(['start(off)']);
    expect(m.buttons[0].spinning).toBe(true);
    const slow = model({ session: pending(NOW - STARTING_SLOW_MS) });
    expect(slow.view).toBe(VIEW.STARTING);
    expect(slow.pill.sub).toBe('Dauert länger als gewohnt …');
    expect(model({ session: pending(NOW - 10 * 60 * 1000) }).view).toBe(VIEW.STARTING);
  });
});

describe('episodes (F6d)', () => {
  it('current is saved + 1 in every view — during RESETTING the NEXT episode', () => {
    const m = model({ status: rec({ phase: TaskPhase.RESETTING, currentEpisodeNumber: 1, totalTime: 5 }), session: activeSession() });
    expect(m.episode).toEqual({ current: 2, total: 3, saved: 1, isLast: false });
    expect(m.pill.sub).toBe('noch 5 s · dann Episode 2');
  });

  it('never above the total; the last episode is flagged', () => {
    const m = model({ status: rec({ phase: TaskPhase.SAVING, currentEpisodeNumber: 3 }), session: activeSession() });
    expect(m.episode.current).toBe(3);
    expect(model({ status: rec({ phase: TaskPhase.RECORDING, currentEpisodeNumber: 2, totalTime: 20 }) }).isLastEpisode).toBe(true);
  });

  it('outside a task the form decides the total', () => {
    expect(model().episode.total).toBe(3);
    expect(model().pill).toMatchObject({ title: 'Bereit', sub: '3 Episoden à 20 s' });
  });

  it('a finished session keeps its own numbers after the idle tick', () => {
    const session = { ...activeSession({ active: false, savedCount: 2 }), finish: { ...EMPTY_FINISH, state: 'uploading' } };
    const m = model({ session });
    expect(m.view).toBe(VIEW.FINISHING);
    expect(m.episode.saved).toBe(2);
    expect(m.pill).toMatchObject({ title: 'Wird abgeschlossen', sub: '2 Episoden gespeichert' });
  });
});

describe('buttons', () => {
  it('follow the table', () => {
    expect(ids(model())).toEqual(['start']);
    expect(ids(model({ status: rec({ phase: TaskPhase.WARMING_UP, totalTime: 5 }), session: activeSession() })))
      .toEqual(['skip', 'end']);
    expect(ids(model({ status: rec({ phase: TaskPhase.RECORDING, totalTime: 20 }), elapsedS: 3, session: activeSession() })))
      .toEqual(['redo', 'saveNow', 'end']);
    expect(ids(model({ status: rec({ phase: TaskPhase.SAVING }), session: activeSession() })))
      .toEqual(['redo(off)', 'saveNow(off)', 'end(off)']);
    expect(ids(model({ status: rec({ phase: TaskPhase.RESETTING, totalTime: 5 }), session: activeSession() })))
      .toEqual(['skip', 'end']);
    const finishing = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'done' } };
    expect(ids(model({ session: finishing }))).toEqual([]);
    expect(ids(model({ heartbeat: 'timeout' }))).toEqual(['start(off)']);
  });

  it('labels, keys and variants follow the mockup', () => {
    const warm = model({ status: rec({ phase: TaskPhase.WARMING_UP, totalTime: 5 }), session: activeSession() });
    expect(warm.buttons[0]).toMatchObject({ label: 'Jetzt starten', kbd: '→', variant: 'warn', icon: 'skipForward' });
    expect(warm.buttons[1]).toMatchObject({ label: 'Beenden', kbd: 'Strg+Umschalt+X', variant: 'end' });
    const reset = model({ status: rec({ phase: TaskPhase.RESETTING, totalTime: 5 }), session: activeSession() });
    expect(reset.buttons[0]).toMatchObject({ label: 'Jetzt weiter', variant: 'primary' });
    const start = model().buttons[0];
    expect(start).toMatchObject({ label: 'Aufnahme starten', kbd: 'Leertaste', size: 'xl', variant: 'primary', icon: 'play' });
  });

  it('„Jetzt speichern" waits for the first second', () => {
    const at = (elapsedS) => model({ status: rec({ phase: TaskPhase.RECORDING, totalTime: 20 }), elapsedS, session: activeSession() });
    expect(at(0.9).buttons.find((b) => b.id === 'saveNow').disabled).toBe(true);
    expect(at(1).buttons.find((b) => b.id === 'saveNow').disabled).toBe(false);
  });

  it('every command button is off while busy or while the link is lost', () => {
    const busy = model({ status: rec({ phase: TaskPhase.RECORDING, totalTime: 20 }), elapsedS: 5, busy: true, session: activeSession() });
    expect(busy.buttons.every((b) => b.disabled)).toBe(true);
    const lost = model({ heartbeat: 'timeout', status: rec({ phase: TaskPhase.RESETTING, totalTime: 5 }), session: activeSession() });
    expect(lost.view).toBe(VIEW.RESETTING);
    expect(lost.buttons.every((b) => b.disabled)).toBe(true);
    expect(model({ busy: true }).buttons[0].disabled).toBe(true);
  });

  it('F6c: „Beenden" is off during a collision, with the reason as its title', () => {
    const m = model({ collision: { active: true }, status: rec({ phase: TaskPhase.RECORDING }), session: activeSession() });
    expect(m.view).toBe(VIEW.COLLISION);
    expect(m.buttons).toHaveLength(1);
    expect(m.buttons[0]).toMatchObject({ id: 'end', disabled: true, title: 'Erst das Kollisionsfenster abschließen.' });
    expect(m.pill).toMatchObject({ title: 'Unterbrochen', sub: 'Kollision' });
  });
});

describe('Phasenleiste', () => {
  const kinds = (m) => m.segments.map((s) => `${s.kind}:${s.state}:${s.weight}`);

  it('the first cycle shows the warm-up; the save dot is fixed', () => {
    const m = model({ status: rec({ phase: TaskPhase.WARMING_UP, totalTime: 5 }), session: activeSession() });
    expect(kinds(m)).toEqual(['warmup:now:5', 'record:upcoming:20', 'save:upcoming:0', 'reset:upcoming:5']);
    expect(m.segments[1].label).toBe('Aufnehmen · Ep. 1');
    expect(m.segments[2].dot).toBe(true);
    expect(m.trackRemaining).toBe('noch 5 s');
  });

  it('after a reset the warm-up is gone; the last episode has no reset', () => {
    const m = model({
      status: rec({ phase: TaskPhase.RECORDING, totalTime: 20, currentEpisodeNumber: 2 }),
      session: activeSession({ sawReset: true }),
      elapsedS: 3,
    });
    expect(kinds(m)).toEqual(['record:now:20', 'save:upcoming:0']);
    expect(m.segments[0].label).toBe('Aufnehmen · Ep. 3');
    expect(m.trackRemaining).toBe('noch 17 s');
  });

  it('RESETTING is sized from its own total; a 0-s phase never gets a segment', () => {
    const r = model({ status: rec({ phase: TaskPhase.RESETTING, totalTime: 7, currentEpisodeNumber: 1 }), session: activeSession({ sawReset: true }) });
    expect(kinds(r)).toEqual(['record:done:20', 'save:done:0', 'reset:now:7']);
    const noWarm = model({ status: rec({ phase: TaskPhase.RECORDING, totalTime: 20, warmupTime: 0 }), session: activeSession() });
    expect(noWarm.segments.map((s) => s.kind)).toEqual(['record', 'save', 'reset']);
    const zeroReset = model({ status: rec({ phase: TaskPhase.RESETTING, totalTime: 0, resetTime: 0, currentEpisodeNumber: 1 }), session: activeSession({ sawReset: true }) });
    expect(kinds(zeroReset)).toEqual(['record:done:20', 'save:done:0']);
  });

  it('outside a session: no segments, the idle text', () => {
    expect(model().segments).toBeNull();
    expect(model().idleText).toBe('Bereit · Aufwärmen → Aufnehmen → Speichern → Zurücksetzen');
    const done = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'done' } };
    expect(model({ session: done }).idleText).toBe('Alle Episoden aufgenommen');
  });
});

describe('Episodenleiste', () => {
  it('done / current / redo badges; hidden in READY', () => {
    const session = activeSession({
      episodes: [
        { n: 1, outcome: 'saved', durationS: 20, wallMs: NOW, early: false },
        { n: 2, outcome: 'redo', durationS: 4, wallMs: NOW, early: false },
      ],
      savedCount: 1,
    });
    const m = model({ status: rec({ phase: TaskPhase.RECORDING, totalTime: 20, currentEpisodeNumber: 1 }), session });
    expect(m.dots.items).toEqual([
      { n: 1, done: true, current: false, redo: false },
      { n: 2, done: false, current: true, redo: true },
      { n: 3, done: false, current: false, redo: false },
    ]);
    expect(m.dots.label).toBe('Episoden');
    expect(model().dots.items).toEqual([]);
  });

  it('at most twelve, then „+k"', () => {
    const m = model({ status: rec({ phase: TaskPhase.RECORDING, numEpisodes: 20, totalTime: 20 }), session: activeSession() });
    expect(m.dots.items).toHaveLength(12);
    expect(m.dots.more).toBe('+8');
  });
});

describe('start blocks (Q5)', () => {
  const verdict = (kind, name, v) => ({ kind, name, topic: '', hz: 0, ageS: 0, verdict: v });

  it('the disk first', () => {
    const disk = { verdict: 'low', free: 1.2e9, startFloor: 3e9, criticalFloor: 1e9 };
    const m = model({ disk, verdicts: [verdict('camera', 'scene', 'stalled')] });
    expect(m.startBlock.kind).toBe('disk');
    expect(m.startBlock.problem).toEqual({
      kind: 'bad',
      textDe: 'Nur noch 1,2 GB frei. Zum Aufnehmen sind mindestens 3,0 GB nötig. Lösche alte Datensätze im Tab Daten.',
    });
    expect(m.buttons[0].disabled).toBe(true);
  });

  it('then a stalled source, camera → follower → leader', () => {
    const b = deriveStartBlock({ verdicts: [verdict('leader', 'leader', 'stalled'), verdict('camera', 'gripper', 'stalled')] });
    expect(b.problem.textDe).toBe(RECORD_COPY.problem.cameraStalled('gripper'));
    const f = deriveStartBlock({ verdicts: [verdict('leader', 'leader', 'stalled'), verdict('follower', 'follower', 'stalled')] });
    expect(f.problem.textDe).toBe(RECORD_COPY.problem.followerStalled);
  });

  it('the leader: switched off in Roboter Studio, not activated (link home), or silent', () => {
    const leader = [verdict('leader', 'leader', 'stalled')];
    expect(deriveStartBlock({ verdicts: leader, bridge: { available: true, followerOnly: true } }).problem.textDe)
      .toBe(RECORD_COPY.problem.leaderOff);
    expect(deriveStartBlock({ verdicts: leader, activation: { state: 'idle' } }).problem)
      .toEqual({ kind: 'bad', textDe: RECORD_COPY.problem.leaderNotActivated, linkToHome: true });
    expect(deriveStartBlock({ verdicts: leader, activation: { state: 'active' } }).problem.textDe)
      .toBe(RECORD_COPY.problem.leaderStalled);
    expect(deriveStartBlock({ verdicts: leader }).problem.textDe).toBe(RECORD_COPY.problem.leaderStalled);
  });

  it('slow sources and an unknown disk do not block', () => {
    expect(deriveStartBlock({ verdicts: [verdict('camera', 'scene', 'slow')], disk: { verdict: 'unknown' } })).toBeNull();
  });

  it('the same dataset still uploading blocks; another does not', () => {
    const session = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'uploading', expectedRepoId: 'schule-A/omx_f_Wuerfel', dismissed: true } };
    const b = deriveStartBlock({ session, form: FORM, robotType: 'omx_f' });
    expect(b).toEqual({ kind: 'uploading', problem: { kind: 'bad', textDe: RECORD_COPY.problem.startUploading } });
    expect(deriveStartBlock({ session, form: { ...FORM, taskName: 'Anders' }, robotType: 'omx_f' })).toBeNull();
  });
});

describe('the form lock (F6b)', () => {
  it('editable only when connected, reported, READY and idle', () => {
    expect(model()).toMatchObject({ editable: true, lockedReason: '' });
  });

  it('offline and connecting say why', () => {
    expect(model({ heartbeat: 'timeout' })).toMatchObject({
      editable: false,
      lockedReason: 'Nicht verbunden. Du kannst die Aufgabe bearbeiten, sobald der Roboter verbunden ist.',
    });
    expect(model({ status: { ...IDLE_STATUS, topicReceived: false } }).lockedReason)
      .toBe(RECORD_COPY.locked.offline);
  });

  it('after the session ended the reason is not „während der Aufnahme" (V2-8)', () => {
    for (const state of ['done', 'upload_failed', 'local_done', 'finalize_failed', 'uploading', 'registering', 'stopped_error']) {
      const session = { ...activeSession({ active: false, savedCount: 1 }), finish: { ...EMPTY_FINISH, state } };
      const m = model({ session });
      expect(m.view).toBe(VIEW.FINISHING);
      expect(m.editable).toBe(false);
      expect(m.lockedKind).toBe('finished');
      expect(m.lockedReason).toBe(RECORD_COPY.locked.finished);
      expect(m.lockedReason).not.toBe(RECORD_COPY.locked.running);
    }
    // still finishing on the robot: it is still the recording
    const finalizing = { ...activeSession({ savedCount: 1 }), finish: { ...EMPTY_FINISH, state: 'finalizing' } };
    const m = model({ status: rec({ phase: TaskPhase.SAVING }), session: finalizing });
    expect(m).toMatchObject({ lockedKind: 'running', lockedReason: RECORD_COPY.locked.running });
    expect(model()).toMatchObject({ lockedKind: null, lockedReason: '' });
    expect(model({ heartbeat: 'timeout' }).lockedKind).toBe('offline');
  });

  it('locked while running, STARTING and FINISHING', () => {
    expect(model({ status: rec({ phase: TaskPhase.RECORDING }), session: activeSession() }).lockedReason)
      .toBe('Während der Aufnahme gesperrt.');
    const pending = { ...EMPTY_RECORD_SESSION, pendingStart: { at: NOW, snapshot: {} } };
    expect(model({ session: pending })).toMatchObject({ editable: false, lockedReason: 'Während der Aufnahme gesperrt.' });
    const done = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'done' } };
    expect(model({ session: done }).editable).toBe(false);
    expect(model({ status: rec({ taskType: 'inference', phase: TaskPhase.INFERENCING }) }).lockedReason)
      .toBe(RECORD_COPY.locked.inference);
  });
});

describe('the pill', () => {
  it('names the phase and the seconds left', () => {
    const warm = model({ status: rec({ phase: TaskPhase.WARMING_UP, totalTime: 5 }), session: activeSession(), secondsLeft: 3 });
    expect(warm.pill).toMatchObject({ title: 'Aufwärmen', sub: 'noch 3 s · dann Episode 1', icon: 'hand', color: 'var(--rec-warm)' });
    const recm = model({ status: rec({ phase: TaskPhase.RECORDING, totalTime: 20 }), session: activeSession(), secondsLeft: 12 });
    expect(recm.pill).toMatchObject({ title: 'Aufnahme', sub: 'Episode 1 von 3 · noch 12 s', color: 'var(--rec-run)' });
    const saving = model({ status: rec({ phase: TaskPhase.SAVING }), session: activeSession() });
    expect(saving.pill).toMatchObject({ title: 'Speichern …', sub: 'Episode 1 von 3', color: 'var(--rec-save)' });
  });

  it('the finish states', () => {
    const s = (state) => ({ ...activeSession({ active: false, savedCount: 1 }), finish: { ...EMPTY_FINISH, state } });
    expect(model({ session: s('done') }).pill).toMatchObject({ title: 'Fertig', sub: '1 Episode gespeichert' });
    expect(model({ session: s('stopped_error') }).pill).toMatchObject({ title: 'Aufnahme gestoppt' });
    expect(model({ session: s('finalize_failed') }).pill).toMatchObject({ title: 'Mit Fehler beendet', icon: 'failed' });
    expect(model({ heartbeat: 'timeout' }).pill.title).toBe('Nicht verbunden');
    expect(model({ status: { ...IDLE_STATUS, topicReceived: false } }).pill.title).toBe('Verbinde …');
  });
});
