// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme command table (spec §3.6): preconditions on the latest status,
// the SendCommand sequence, the intents around it, and what the page says —
// including F2 (a slow start stays pending), F4 (silent refusals) and Q8 (the
// honest notice when „Behalten und beenden" will be dropped).

import TaskPhase from '../../../../constants/taskPhases';
import { BUSY_DE } from '../../../../features/tasks/recordSession';
import RECORD_COPY, { KEEP_DROPPED_AFTER_REDO } from '../recordCopy';
import {
  PRECONDITIONS,
  RERECORD_FINISH_WINDOW_MS,
  isBusyAnswer,
  keepEndDropsAfterRedo,
  runRecordAction,
  transportMessageDe,
} from '../recordCommands';

const WALL = 1_700_000_000_000;

function harness({ status = {}, answers = {} } = {}) {
  const log = [];
  let t = WALL;
  const dispatch = vi.fn((action) => log.push(`intent:${action.payload.kind}`));
  const send = vi.fn(async (command) => {
    log.push(`send:${command}`);
    t += 50;
    const a = answers[command];
    if (a instanceof Error) throw a;
    if (typeof a === 'function') return a();
    return a ?? { success: true, message: '' };
  });
  const st = {
    phase: TaskPhase.READY, running: false, taskType: '', collisionActive: false, pendingStart: null,
    currentEpisodeNumber: 0, session: null, ...status,
  };
  return {
    log,
    dispatch,
    send,
    ctx: { send, dispatch, getStatus: () => st, now: () => t, snapshot: { taskName: 'Würfel' } },
    intents: () => dispatch.mock.calls.map(([a]) => a.payload),
    advance(ms) { t += ms; },
  };
}

const recording = (patch = {}) => ({
  phase: TaskPhase.RECORDING, running: true, taskType: 'record', currentEpisodeNumber: 1,
  session: { run: { episode: 2, startWallMs: WALL - 3000 }, lastRedoSentAt: null }, ...patch,
});

describe('preconditions on the latest status', () => {
  it.each([
    ['start', { phase: TaskPhase.RECORDING, running: true }],
    ['start', { pendingStart: { at: WALL } }],
    ['skip', recording()],
    ['saveNow', { ...recording(), phase: TaskPhase.SAVING }],
    ['redo', { ...recording(), phase: TaskPhase.RESETTING }],
    ['end', recording()],
    ['keepAndEnd', { ...recording(), phase: TaskPhase.SAVING }],
    ['discardAndEnd', { ...recording(), collisionActive: true }],
    ['saveNow', { ...recording(), taskType: 'inference' }],
  ])('%s on %j sends nothing', async (action, status) => {
    const h = harness({ status });
    const r = await runRecordAction(action, h.ctx);
    expect(r).toMatchObject({ ok: false, silent: true });
    expect(h.send).not.toHaveBeenCalled();
    expect(h.dispatch).not.toHaveBeenCalled();
  });

  it('covers every action', () => {
    expect(Object.keys(PRECONDITIONS).sort()).toEqual(
      ['discardAndEnd', 'end', 'keepAndEnd', 'redo', 'saveNow', 'skip', 'start'],
    );
  });
});

describe('start', () => {
  it('the intent (with the snapshot) goes out BEFORE the command', async () => {
    const h = harness();
    expect(await runRecordAction('start', h.ctx)).toEqual({ ok: true });
    expect(h.log).toEqual(['intent:start', 'send:start_record']);
    expect(h.intents()[0]).toMatchObject({ kind: 'start', at: WALL, snapshot: { taskName: 'Würfel' } });
  });

  it('a refusal drops the pending start and shows the server text', async () => {
    const h = harness({ answers: { start_record: { success: false, message: 'Nur noch 1,2 GB frei.' } } });
    expect(await runRecordAction('start', h.ctx)).toEqual({ ok: false, messageDe: 'Nur noch 1,2 GB frei.' });
    expect(h.log).toEqual(['intent:start', 'send:start_record', 'intent:start_failed']);
  });

  it('F2: a timeout keeps the start pending, silently', async () => {
    const h = harness({ answers: { start_record: new Error('Service call timeout for /task/command') } });
    expect(await runRecordAction('start', h.ctx)).toMatchObject({ ok: false, silent: true, pending: true });
    expect(h.log).toEqual(['intent:start', 'send:start_record']);
  });

  it('any other transport failure: start_failed + „Keine Verbindung zum Roboter."', async () => {
    const h = harness({ answers: { start_record: new Error('ROS connection is not available') } });
    expect(await runRecordAction('start', h.ctx)).toEqual({ ok: false, messageDe: 'Keine Verbindung zum Roboter.' });
    expect(h.log).toEqual(['intent:start', 'send:start_record', 'intent:start_failed']);
  });
});

describe('skip / saveNow (F4)', () => {
  it('send NEXT; a refusal stays silent', async () => {
    const warm = harness({ status: { ...recording(), phase: TaskPhase.WARMING_UP } });
    expect(await runRecordAction('skip', warm.ctx)).toEqual({ ok: true });
    expect(warm.log).toEqual(['send:next']);
    const tooEarly = harness({ status: recording(), answers: { next: { success: false, message: 'Die Episode läuft erst seit weniger als einer Sekunde.' } } });
    expect(await runRecordAction('saveNow', tooEarly.ctx)).toEqual({ ok: false, silent: true });
    const nothing = harness({ status: { ...recording(), phase: TaskPhase.RESETTING }, answers: { next: { success: false, message: '' } } });
    expect(await runRecordAction('skip', nothing.ctx)).toEqual({ ok: false, silent: true });
  });

  it('a transport failure is not silent', async () => {
    const h = harness({ status: recording(), answers: { next: new Error('Service call timeout for /task/command') } });
    expect(await runRecordAction('saveNow', h.ctx)).toEqual({ ok: false, messageDe: RECORD_COPY.problem.timeout });
  });
});

describe('redo', () => {
  it('RERECORD, then the acknowledgement with the send time, then a note', async () => {
    const h = harness({ status: recording() });
    expect(await runRecordAction('redo', h.ctx)).toEqual({ ok: true, note: 'Episode 2 wird wiederholt.' });
    expect(h.log).toEqual(['send:rerecord', 'intent:redo_ack']);
    expect(h.intents()[0]).toMatchObject({ kind: 'redo_ack', sentAt: WALL, at: WALL + 50 });
  });

  it('a refusal (already saved) shows the server text', async () => {
    const h = harness({ status: recording(), answers: { rerecord: { success: false, message: 'Die Episode ist schon gespeichert und kann nicht mehr verworfen werden.' } } });
    expect(await runRecordAction('redo', h.ctx))
      .toEqual({ ok: false, messageDe: 'Die Episode ist schon gespeichert und kann nicht mehr verworfen werden.' });
    expect(h.dispatch).not.toHaveBeenCalled();
  });
});

describe('end / keepAndEnd', () => {
  it('„Beenden" in warm-up / reset: the intent first, then FINISH', async () => {
    const h = harness({ status: { ...recording(), phase: TaskPhase.RESETTING } });
    expect(await runRecordAction('end', h.ctx)).toEqual({ ok: true });
    expect(h.log).toEqual(['intent:end', 'send:finish']);
  });

  it('a failed FINISH clears the intent and shows why', async () => {
    const h = harness({ status: { ...recording(), phase: TaskPhase.WARMING_UP }, answers: { finish: { success: false, message: 'Gerade läuft keine Aufnahme.' } } });
    expect(await runRecordAction('end', h.ctx)).toEqual({ ok: false, messageDe: 'Gerade läuft keine Aufnahme.' });
    expect(h.log).toEqual(['intent:end', 'send:finish', 'intent:clear']);
  });

  it('„Behalten und beenden": keep_end then FINISH', async () => {
    const h = harness({ status: recording() });
    expect(await runRecordAction('keepAndEnd', h.ctx)).toEqual({ ok: true });
    expect(h.log).toEqual(['intent:keep_end', 'send:finish']);
  });

  it('Q8: the notice at the window boundary (±0.1 s)', async () => {
    const at = async (sinceRedoMs) => {
      const session = { run: { episode: 2, startWallMs: WALL - sinceRedoMs + 1000 }, lastRedoSentAt: WALL - sinceRedoMs };
      const h = harness({ status: recording({ session }) });
      return runRecordAction('keepAndEnd', h.ctx);
    };
    expect(await at(RERECORD_FINISH_WINDOW_MS - 100)).toEqual({ ok: true, note: KEEP_DROPPED_AFTER_REDO });
    expect(await at(RERECORD_FINISH_WINDOW_MS + 100)).toEqual({ ok: true });
  });
});

describe('keepEndDropsAfterRedo', () => {
  it('needs the run to start after the RERECORD and FINISH within the window', () => {
    expect(keepEndDropsAfterRedo({ redoSentAt: 0, runStartedAt: 1000, finishAt: 4900 })).toBe(true);
    expect(keepEndDropsAfterRedo({ redoSentAt: 0, runStartedAt: 1000, finishAt: 5000 })).toBe(true);
    expect(keepEndDropsAfterRedo({ redoSentAt: 0, runStartedAt: 1000, finishAt: 5100 })).toBe(false);
    // the run began before the RERECORD: not the run the robot drops
    expect(keepEndDropsAfterRedo({ redoSentAt: 1000, runStartedAt: 900, finishAt: 2000 })).toBe(false);
    // no RERECORD this session
    expect(keepEndDropsAfterRedo({ redoSentAt: null, runStartedAt: 900, finishAt: 2000 })).toBe(false);
  });
});

describe('discardAndEnd', () => {
  it('RERECORD, acknowledgement, FINISH', async () => {
    const h = harness({ status: recording() });
    expect(await runRecordAction('discardAndEnd', h.ctx)).toEqual({ ok: true });
    expect(h.log).toEqual(['intent:discard_end', 'send:rerecord', 'intent:discard_ack', 'send:finish']);
    expect(h.intents()[1]).toMatchObject({ kind: 'discard_ack', sentAt: WALL });
  });

  it('a refused RERECORD (already committed) still ends, with a note', async () => {
    const h = harness({ status: recording(), answers: { rerecord: { success: false, message: 'schon gespeichert' } } });
    expect(await runRecordAction('discardAndEnd', h.ctx))
      .toEqual({ ok: true, note: 'Die Episode war schon fertig und wurde gespeichert.' });
    expect(h.log).toEqual(['intent:discard_end', 'send:rerecord', 'send:finish']);
  });

  it('a thrown RERECORD sends no FINISH', async () => {
    const h = harness({ status: recording(), answers: { rerecord: new Error('ROS connection failed') } });
    expect(await runRecordAction('discardAndEnd', h.ctx)).toEqual({ ok: false, messageDe: 'Keine Verbindung zum Roboter.' });
    expect(h.log).toEqual(['intent:discard_end', 'send:rerecord', 'intent:clear']);
  });

  it('a failed FINISH after the discard clears the intent', async () => {
    const h = harness({ status: recording(), answers: { finish: new Error('Service call timeout for /task/command') } });
    expect(await runRecordAction('discardAndEnd', h.ctx)).toEqual({ ok: false, messageDe: RECORD_COPY.problem.timeout });
    expect(h.log).toEqual(['intent:discard_end', 'send:rerecord', 'intent:discard_ack', 'send:finish', 'intent:clear']);
  });
});

describe('never STOP or SKIP_TASK', () => {
  it('no action sends them', async () => {
    const sent = new Set();
    for (const [action, status] of [
      ['start', {}], ['skip', { ...recording(), phase: TaskPhase.WARMING_UP }], ['saveNow', recording()],
      ['redo', recording()], ['end', { ...recording(), phase: TaskPhase.RESETTING }], ['keepAndEnd', recording()],
      ['discardAndEnd', recording()],
    ]) {
      const h = harness({ status });
      // eslint-disable-next-line no-await-in-loop
      await runRecordAction(action, h.ctx);
      h.send.mock.calls.forEach(([c]) => sent.add(c));
    }
    expect([...sent].sort()).toEqual(['finish', 'next', 'rerecord', 'start_record']);
  });
});

describe('transportMessageDe', () => {
  it('timeout vs. no connection', () => {
    expect(transportMessageDe(new Error('Service call timeout for /task/command')))
      .toBe('Der Roboter hat nicht rechtzeitig geantwortet. Bitte versuch es noch einmal.');
    expect(transportMessageDe(new Error('boom'))).toBe('Keine Verbindung zum Roboter.');
  });
});

// O6 (round 5): the robot answers a command it cannot take right now (its
// recorder is busy, e.g. during the ~1 s official discard) with BUSY_DE and
// changes nothing. That answer is never silent, and in „Verwerfen und beenden"
// a busy RERECORD is not „already saved": FINISH would keep the take.
describe('the busy answer (O6)', () => {
  const BUSY = { success: false, message: BUSY_DE };

  it('isBusyAnswer recognises exactly the robot\'s busy refusal', () => {
    expect(isBusyAnswer(BUSY)).toBe(true);
    expect(isBusyAnswer({ success: false, message: ` ${BUSY_DE} ` })).toBe(true);
    expect(isBusyAnswer({ success: true, message: BUSY_DE })).toBe(false);
    expect(isBusyAnswer({ success: false, message: 'schon gespeichert' })).toBe(false);
    expect(isBusyAnswer(null)).toBe(false);
  });

  it('„Verwerfen und beenden": a busy RERECORD sends NO finish, clears the intent and says so', async () => {
    const h = harness({ status: recording(), answers: { rerecord: BUSY } });
    expect(await runRecordAction('discardAndEnd', h.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
    expect(h.log).toEqual(['intent:discard_end', 'send:rerecord', 'intent:clear']);
    expect(h.send.mock.calls.map(([c]) => c)).not.toContain('finish');
  });

  it('„Verwerfen und beenden": a busy FINISH after the discard clears the intent and says so', async () => {
    const h = harness({ status: recording(), answers: { finish: BUSY } });
    expect(await runRecordAction('discardAndEnd', h.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
    expect(h.log).toEqual(['intent:discard_end', 'send:rerecord', 'intent:discard_ack', 'send:finish', 'intent:clear']);
  });

  it('„Jetzt speichern" and „Jetzt starten/weiter" show a busy answer (F4 silence is for refusals only)', async () => {
    const save = harness({ status: recording(), answers: { next: BUSY } });
    expect(await runRecordAction('saveNow', save.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
    const skip = harness({ status: { ...recording(), phase: TaskPhase.RESETTING }, answers: { next: BUSY } });
    expect(await runRecordAction('skip', skip.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
  });

  it('„Wiederholen", „Beenden" and „Behalten und beenden" show it and change nothing', async () => {
    const redo = harness({ status: recording(), answers: { rerecord: BUSY } });
    expect(await runRecordAction('redo', redo.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
    expect(redo.dispatch).not.toHaveBeenCalled();
    const end = harness({ status: { ...recording(), phase: TaskPhase.RESETTING }, answers: { finish: BUSY } });
    expect(await runRecordAction('end', end.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
    expect(end.log).toEqual(['intent:end', 'send:finish', 'intent:clear']);
    const keep = harness({ status: recording(), answers: { finish: BUSY } });
    expect(await runRecordAction('keepAndEnd', keep.ctx)).toEqual({ ok: false, messageDe: BUSY_DE });
    expect(keep.log).toEqual(['intent:keep_end', 'send:finish', 'intent:clear']);
  });
});
