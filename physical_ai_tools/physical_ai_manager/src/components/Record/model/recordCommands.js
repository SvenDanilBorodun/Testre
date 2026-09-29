// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// What each Aufnahme action sends, and what the page says about it (spec §3.6).
//
// Every action re-checks its precondition on the LATEST status at the moment
// it runs, and sends nothing when the phase has moved on meanwhile (a click
// that raced the robot's own transition). The robot serialises commands with
// its record ticks, so the sequences below are race-safe on its side; this
// file decides the page's intents around them (features/tasks/recordSession):
// the intents that must exist BEFORE the command (`start`, the three ends) are
// dispatched before sending, the acknowledgements after.
//
// Result: `{ok, messageDe?, note?, silent?}` — `messageDe` is a failure the
// banner shows (8 s), `note` a short info line, `silent` a refusal the page
// does not comment on (F4: the phase display is the truth).

import TaskPhase from '../../../constants/taskPhases';
import { recordIntent } from '../../../features/tasks/taskSlice';
import RECORD_COPY, { KEEP_DROPPED_AFTER_REDO } from './recordCopy';

/** The server drops a run that started after a wire RERECORD when FINISH comes this soon (Q4). */
export const RERECORD_FINISH_WINDOW_MS = 5000;

export const RECORD_ACTIONS = Object.freeze(['start', 'skip', 'saveNow', 'redo', 'end', 'keepAndEnd', 'discardAndEnd']);

/** The German sentence for a thrown transport error. */
export function transportMessageDe(error) {
  const text = String(error?.message || error || '');
  return /timeout/i.test(text) ? RECORD_COPY.problem.timeout : RECORD_COPY.problem.noConnection;
}

const isTimeout = (error) => /timeout/i.test(String(error?.message || error || ''));

/**
 * Q4 / Q8: will the robot drop the running episode on „Behalten und beenden"?
 * It does when the run STARTED after this client's accepted RERECORD and the
 * FINISH arrives within the window of that RERECORD. All three times on one
 * clock (wall ms).
 */
export function keepEndDropsAfterRedo({ redoSentAt, runStartedAt, finishAt }) {
  if (![redoSentAt, runStartedAt, finishAt].every(Number.isFinite)) return false;
  return runStartedAt >= redoSentAt && finishAt - redoSentAt <= RERECORD_FINISH_WINDOW_MS;
}

const inRecordSession = (st) => !!st && st.running === true && st.taskType !== 'inference' && !st.collisionActive;

/** The precondition of each action on the latest status. */
export const PRECONDITIONS = Object.freeze({
  start: (st) => !!st && st.phase === TaskPhase.READY && !st.running && !st.pendingStart && !st.collisionActive,
  skip: (st) => inRecordSession(st) && (st.phase === TaskPhase.WARMING_UP || st.phase === TaskPhase.RESETTING),
  saveNow: (st) => inRecordSession(st) && st.phase === TaskPhase.RECORDING,
  redo: (st) => inRecordSession(st) && st.phase === TaskPhase.RECORDING,
  end: (st) => inRecordSession(st) && (st.phase === TaskPhase.WARMING_UP || st.phase === TaskPhase.RESETTING),
  keepAndEnd: (st) => inRecordSession(st) && st.phase === TaskPhase.RECORDING,
  discardAndEnd: (st) => inRecordSession(st) && st.phase === TaskPhase.RECORDING,
});

const refusedText = (result) => (result && result.message) || RECORD_COPY.problem.noConnection;

// send → {result} | {error}
async function trySend(send, command) {
  try {
    return { result: await send(command) };
  } catch (error) {
    return { error };
  }
}

/**
 * Run one action.
 * @param action  one of RECORD_ACTIONS
 * @param ctx.send       (command) => Promise<{success, message}>  (sendRecordCommand)
 * @param ctx.getStatus  () => {phase, running, taskType, collisionActive, pendingStart, session}
 * @param ctx.dispatch   the store's dispatch
 * @param ctx.now        () => wall ms (Date.now)
 * @param ctx.snapshot   the forced form snapshot (start only)
 */
export async function runRecordAction(action, { send, getStatus, dispatch, now = () => Date.now(), snapshot = null } = {}) {
  const st = getStatus();
  const check = PRECONDITIONS[action];
  if (!check || !check(st)) return { ok: false, silent: true, skipped: true };
  const session = st.session || null;
  const episode = session?.run?.episode ?? ((Number(st.currentEpisodeNumber) || 0) + 1);

  switch (action) {
    case 'start': {
      dispatch(recordIntent({ kind: 'start', at: now(), snapshot }));
      const { result, error } = await trySend(send, 'start_record');
      if (error) {
        // F2: a slow answer is not a failure — Start stays pending until the
        // first record tick or an error tick says what happened.
        if (isTimeout(error)) return { ok: false, silent: true, pending: true };
        dispatch(recordIntent({ kind: 'start_failed', at: now() }));
        return { ok: false, messageDe: transportMessageDe(error) };
      }
      if (result && result.success === false) {
        dispatch(recordIntent({ kind: 'start_failed', at: now() }));
        return { ok: false, messageDe: refusedText(result) };
      }
      return { ok: true };
    }

    case 'skip':
    case 'saveNow': {
      const { result, error } = await trySend(send, 'next');
      if (error) return { ok: false, messageDe: transportMessageDe(error) };
      if (result && result.success === false) return { ok: false, silent: true }; // F4
      return { ok: true };
    }

    case 'redo': {
      const sentAt = now();
      const { result, error } = await trySend(send, 'rerecord');
      if (error) return { ok: false, messageDe: transportMessageDe(error) };
      if (result && result.success === false) return { ok: false, messageDe: refusedText(result) };
      dispatch(recordIntent({ kind: 'redo_ack', at: now(), sentAt }));
      return { ok: true, note: RECORD_COPY.note.redo(episode) };
    }

    case 'end':
    case 'keepAndEnd': {
      const finishAt = now();
      const drops = action === 'keepAndEnd' && keepEndDropsAfterRedo({
        redoSentAt: session?.lastRedoSentAt,
        runStartedAt: session?.run?.startWallMs,
        finishAt,
      });
      dispatch(recordIntent({ kind: action === 'end' ? 'end' : 'keep_end', at: finishAt }));
      const { result, error } = await trySend(send, 'finish');
      if (error || (result && result.success === false)) {
        dispatch(recordIntent({ kind: 'clear', at: now() }));
        return { ok: false, messageDe: error ? transportMessageDe(error) : refusedText(result) };
      }
      return drops ? { ok: true, note: KEEP_DROPPED_AFTER_REDO } : { ok: true };
    }

    case 'discardAndEnd': {
      dispatch(recordIntent({ kind: 'discard_end', at: now() }));
      const sentAt = now();
      const redo = await trySend(send, 'rerecord');
      if (redo.error) {
        dispatch(recordIntent({ kind: 'clear', at: now() }));
        return { ok: false, messageDe: transportMessageDe(redo.error) };
      }
      let note;
      if (redo.result && redo.result.success === false) {
        // Already committed by the robot: it keeps the episode; end anyway.
        note = RECORD_COPY.note.alreadySaved;
      } else {
        dispatch(recordIntent({ kind: 'discard_ack', at: now(), sentAt }));
      }
      const { result, error } = await trySend(send, 'finish');
      if (error || (result && result.success === false)) {
        dispatch(recordIntent({ kind: 'clear', at: now() }));
        return { ok: false, messageDe: error ? transportMessageDe(error) : refusedText(result) };
      }
      return note ? { ok: true, note } : { ok: true };
    }

    default:
      return { ok: false, silent: true };
  }
}
