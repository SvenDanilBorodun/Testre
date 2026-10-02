// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Diese Sitzung": what the student's recording session did, episode by
// episode, and where its finish stands (spec §3.2 rules R1–R17, §3.10, §3.11).
//
// Pure. Every function takes the previous session and returns it UNCHANGED (the
// same reference) when no rule applies, so a 30 Hz /task/status feed does not
// re-render the page between events. Timestamps: `receivedAt` / `startAt` are
// `performance.now()` readings (durations); every `…WallMs`, intent `at`,
// notice `at` and upload `at` is `Date.now()` (ordering across sources).
//
// The server tells the page three things per tick: the phase, the saved count
// and the floored seconds into the phase. Everything below is inferred from
// transitions of those, plus the page's own intents — because the robot
// publishes NO tick for some real events (a redo with Zurücksetzen = 0 goes
// RECORDING → RECORDING, H15), the page's own command acknowledgements are
// evidence too.

import TaskPhase from '../../constants/taskPhases';
import { datasetRepoId } from '../../utils/datasetName';

/** The line under the finish title after a forced collision end (F1). */
export const COLLISION_END_NOTE_DE = 'Die Aufnahme wurde nach der Kollision beendet.';

/** A warning this close to the end explains the end (low disk, frame drop). */
export const END_NOTE_WINDOW_MS = 5000;
/** The server drops a run that started this soon after a wire RERECORD (Q4). */
export const RERECORD_FINISH_WINDOW_MS = 5000;
/** A run shorter than this was never a real attempt (Q3). */
export const MIN_ATTEMPT_S = 1;
/**
 * How the server's finalize-failure reason begins
 * (data_manager.DataManager._finalize_dataset). With upload ON the terminating
 * tick's warning can also be a namespace refusal or an enqueue failure; this
 * prefix tells the finalize failure apart. If the server ever words it
 * differently the page falls back to „Hochladen fehlgeschlagen" — still a
 * failure, never „bereit".
 */
export const FINALIZE_FAILED_PREFIX_DE = 'Datensatz konnte nicht abgeschlossen werden';

// Aufnahme 2.0 round 5 — byte-equal with the server's
// data_processing/record_texts_de.py (robotis_ai_setup/tests/test_record_r5_lockstep.py).
/** The robot ended the session itself: a source stopped (R5-2) or repeated frame loss (C7). */
export const SOURCE_STOP_PREFIX_DE = 'Aufnahme beendet: ';
/** A short source gap inside a take: the robot re-records it (O2). */
export const SOURCE_GAP_PREFIX_DE = 'Signalaussetzer: ';
/** The robot's answer to a command while the recorder is busy (O6). */
export const BUSY_DE = 'Die Aufnahme ist gerade beschäftigt. Bitte versuch es gleich noch einmal.';
/** Round 6 (D5/F3): how an error stop says the saved episodes are safe … */
export const ERROR_STOP_SAVED_DE = 'Die schon gespeicherten Episoden sind gesichert; du kannst sie im Tab Daten '
  + 'hochladen.';
/** … and how it says the dataset could not be finalized (the crash marker stays). */
export const ERROR_STOP_INCOMPLETE_DE = 'Der Datensatz ist unvollständig: Er konnte nicht abgeschlossen werden. '
  + 'Nimm die Episoden neu auf.';
/** Round 6 (F4): the session runs WITHOUT upload (no or refused token); the reason follows. */
export const UPLOAD_OFF_PREFIX_DE = 'Aufnahme ohne Hochladen: ';

export const OUTCOMES = Object.freeze(['saved', 'redo', 'collision', 'drop', 'ended', 'source', 'gap', 'gap_end']);
export const FINISH_STATES = Object.freeze([
  'idle', 'finalizing', 'uploading', 'registering', 'done', 'upload_failed', 'local_done',
  'finalize_failed', 'stopped_error', 'nothing',
]);

export const EMPTY_FINISH = Object.freeze({
  state: 'idle',
  repoId: null,
  expectedRepoId: null,
  uploadPct: 0,
  message: '',
  registerState: null,
  endedAt: null,
  linkLost: false,
  dismissed: false,
  endNote: null,
  // round 6 (F4): the reason a session ran without upload (its local finish names it)
  uploadOff: null,
  // true while `finalizing` came only from one of the page's end intents, so a
  // failed command (`clear`) can take it back.
  fromIntent: false,
});

export const EMPTY_RECORD_SESSION = Object.freeze({
  id: 0,
  active: false,
  startedHere: false,
  adopted: false,
  startedWallMs: null,
  endedWallMs: null,
  pendingStart: null, // { at, snapshot } — set BEFORE START_RECORD is sent (F2)
  snapshot: null,
  savedCount: 0,
  adoptedSavedCount: 0,
  sawReset: false,
  run: null, // { episode, startAt, startWallMs, createdWallMs, resolved, sawSaving, durationS }
  awaitNewRun: false,
  intent: null, // { kind, at, episode }
  lastRedoSentAt: null, // wall time this client last SENT a RERECORD that was accepted
  episodes: [], // { n, outcome, durationS, wallMs, early, provisional? }
  collisionOpen: false,
  lastWarn: null,
  // round 6 (F4): the robot's „Aufnahme ohne Hochladen: …" notice for this session
  uploadOff: null,
  finish: EMPTY_FINISH,
  errorText: null,
});

const TRACKING = new Set(['uploading', 'registering']);
export const isFinishTracking = (finish) => TRACKING.has(finish?.state);

const isRecordTick = (st) => !!st && st.running === true && st.taskType === 'record';

const num = (v, fallback = 0) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
};

function episodeTimeOf(s, next) {
  return num(s.snapshot?.episodeTime, 0) || num(next?.episodeTime, 0);
}

function numEpisodesOf(s, next) {
  return num(next?.numEpisodes, 0) || num(s.snapshot?.numEpisodes, 0);
}

function snapshotFromWire(next) {
  return {
    taskName: next.taskName || '',
    // `taskStatus.userId` is only ever set when the robot names the signed-in
    // student (useRosTopicSubscription's robotNamesMe gate).
    userId: next.userId || '',
    robotType: next.robotType || '',
    pushToHub: next.pushToHub === true,
    privateMode: null, // not on the wire for an adopted session
    numEpisodes: num(next.numEpisodes),
    episodeTime: num(next.episodeTime),
    warmupTime: num(next.warmupTime),
    resetTime: num(next.resetTime),
    fps: num(next.fps),
  };
}

function expectedRepoOf(snapshot) {
  if (!snapshot || !snapshot.userId) return null;
  return datasetRepoId(snapshot.userId, snapshot.robotType, snapshot.taskName);
}

// Seconds the run had been recording at `atPerf`, never above the episode.
function runSeconds(run, atPerf, episodeTime) {
  if (!run || !Number.isFinite(atPerf) || !Number.isFinite(run.startAt)) return 0;
  const s = Math.max(0, (atPerf - run.startAt) / 1000);
  return episodeTime > 0 ? Math.min(episodeTime, s) : s;
}

function runSecondsWall(run, atWall, episodeTime) {
  if (!run || !Number.isFinite(atWall) || !Number.isFinite(run.startWallMs)) return 0;
  const s = Math.max(0, (atWall - run.startWallMs) / 1000);
  return episodeTime > 0 ? Math.min(episodeTime, s) : s;
}

const row = (n, outcome, durationS, wallMs, extra = {}) => ({
  n, outcome, durationS: Math.max(0, num(durationS)), wallMs: wallMs ?? null, early: false, ...extra,
});

// Resolve the open run with `outcome`, appending its row.
function resolveRun(s, outcome, durationS, wallMs) {
  if (!s.run || s.run.resolved) return s;
  return {
    ...s,
    run: { ...s.run, resolved: true },
    episodes: [...s.episodes, row(s.run.episode, outcome, durationS, wallMs)],
  };
}

// What a run that ended WITHOUT a save and without a RESETTING was: a discard
// the page asked for, or a redo (possibly from another client / the joystick).
function unresolvedOutcome(s) {
  if (s.intent?.kind === 'discard_end' && s.intent.episode === s.run?.episode) return 'ended';
  return 'redo';
}

// The latest warning, when it is no older than END_NOTE_WINDOW_MS at `atWall`
// and begins with `prefix`.
function recentWarnStartsWith(s, prefix, atWall) {
  const w = s.lastWarn;
  if (!w || typeof w.text !== 'string' || !w.text.startsWith(prefix)) return false;
  if (!Number.isFinite(w.at) || !Number.isFinite(atWall)) return false;
  return atWall - w.at <= END_NOTE_WINDOW_MS;
}

// A save the robot did not count and did not keep: a short source gap (O2,
// „Signalaussetzer, wiederholt") when that is what the robot just said, else a
// frame drop („Bildverlust, verworfen").
function discardedSaveOutcome(s, atWall) {
  return recentWarnStartsWith(s, SOURCE_GAP_PREFIX_DE, atWall) ? 'gap' : 'drop';
}

// A collision during SAVING is recorded „Gespeichert" (owner decision Q7) —
// provisionally, because the client cannot see whether the save was already
// committed. The first record tick after the collision that shows the count
// above it (counted — in any phase, SAVING included: the last episode is
// counted while the robot is still saving) or that is not a SAVING tick
// settles it. A SAVING tick with the old count leaves it provisional: it can be
// a pre-trip tick delivered late across the two /task/status publishers.
function settleProvisional(s, next) {
  const idx = s.episodes.findIndex((e) => e.provisional);
  if (idx < 0) return s;
  const counted = num(next.currentEpisodeNumber) > s.savedCount;
  const episodes = s.episodes.slice();
  episodes[idx] = counted
    ? { ...episodes[idx], provisional: false }
    : { ...episodes[idx], provisional: false, outcome: 'collision', early: false };
  return { ...s, episodes, savedCount: counted ? s.savedCount + 1 : s.savedCount };
}

/**
 * Advance the session by one merged /task/status tick.
 * @param s     the session
 * @param prev  the pre-merge {phase, running, currentEpisodeNumber, proceedTime, receivedAt, receivedWallMs}
 * @param next  the merged taskStatus of this tick
 */
export function advanceRecordSession(s, prev, next) {
  if (!next) return s;
  let out = s;
  const recordTick = isRecordTick(next);
  const p = prev || {};

  // R1 — a record tick with no active session starts one.
  if (!out.active && recordTick) {
    const fromHere = !!out.pendingStart;
    const snapshot = fromHere ? { ...out.pendingStart.snapshot } : snapshotFromWire(next);
    const count = num(next.currentEpisodeNumber);
    const carry = isFinishTracking(out.finish)
      && (out.finish.expectedRepoId || out.finish.repoId)
      && (out.finish.expectedRepoId || out.finish.repoId) === expectedRepoOf(snapshot);
    out = {
      ...EMPTY_RECORD_SESSION,
      id: out.id + 1,
      active: true,
      startedHere: fromHere,
      adopted: count > 0,
      startedWallMs: next.receivedWallMs ?? null,
      snapshot,
      savedCount: count,
      adoptedSavedCount: count > 0 ? count : 0,
      // a notice dispatched just before the first record tick (F4)
      uploadOff: fromHere ? out.uploadOff : null,
      finish: carry ? out.finish : EMPTY_FINISH,
    };
  }
  if (!out.active) return out;

  const E = episodeTimeOf(out, next);
  const count = num(next.currentEpisodeNumber);
  const isRecordish = next.taskType === 'record';
  const phase = next.phase;
  const RECORDING = TaskPhase.RECORDING;

  // Q7 — settle a provisional „Gespeichert" on the first record tick that
  // counted it or is not SAVING (or the end tick). Runs before R3, so the
  // increment is consumed once.
  if (isRecordish && (phase !== TaskPhase.SAVING || count > out.savedCount)
      && out.episodes.some((e) => e.provisional)) {
    out = settleProvisional(out, next);
  }

  // R2 — the session has been through a reset.
  if (recordTick && phase === TaskPhase.RESETTING && !out.sawReset) {
    out = { ...out, sawReset: true };
  }

  // R3 — the saved count went up: one „Gespeichert" row per increment.
  if (isRecordish && count > out.savedCount) {
    const rows = [];
    let run = out.run;
    for (let n = out.savedCount + 1; n <= count; n += 1) {
      let durationS = E;
      if (run && !run.resolved) {
        durationS = run.durationS ?? runSeconds(run, next.receivedAt, E);
        run = { ...run, resolved: true };
      }
      rows.push(row(n, 'saved', durationS, next.receivedWallMs, { early: E > 0 && durationS < E - 0.5 }));
    }
    out = { ...out, savedCount: count, run, episodes: [...out.episodes, ...rows] };
  }

  const openRun = out.run && !out.run.resolved ? out.run : null;

  // R4 — RECORDING → SAVING: the run stopped recording here.
  if (recordTick && openRun && p.phase === RECORDING && phase === TaskPhase.SAVING && !openRun.sawSaving) {
    out = {
      ...out,
      run: { ...openRun, sawSaving: true, durationS: runSeconds(openRun, next.receivedAt, E) },
    };
  }

  // R5 — RECORDING → RESETTING without a save: a redo (another client, the
  // joystick) — or the page's own „Verwerfen und beenden".
  if (recordTick && out.run && !out.run.resolved && p.phase === RECORDING
      && phase === TaskPhase.RESETTING && count === num(p.currentEpisodeNumber, count)) {
    out = resolveRun(out, unresolvedOutcome(out), runSeconds(out.run, next.receivedAt, E), next.receivedWallMs);
  }

  // R6 — SAVING → RESETTING without a count: the save was discarded for a
  // frame drop, or re-recorded for a short source gap.
  if (recordTick && out.run && !out.run.resolved && p.phase === TaskPhase.SAVING
      && phase === TaskPhase.RESETTING) {
    out = resolveRun(out, discardedSaveOutcome(out, next.receivedWallMs),
      out.run.durationS ?? runSeconds(out.run, next.receivedAt, E), next.receivedWallMs);
  }

  // R7 — a new run starts: the phase entered RECORDING, the time ran backwards
  // inside RECORDING, or the page is waiting for the run after its own
  // redo/discard (which may begin at 0 s with no phase change, H15).
  const proceed = num(next.proceedTime);
  if (recordTick && phase === RECORDING && (
    !out.run
    || p.phase !== RECORDING
    || proceed < num(p.proceedTime)
    || (out.awaitNewRun && proceed <= 1)
  )) {
    if (out.run && !out.run.resolved) {
      // H15: a redo with Zurücksetzen = 0 publishes no RESETTING.
      const outcome = out.run.sawSaving ? discardedSaveOutcome(out, next.receivedWallMs) : unresolvedOutcome(out);
      const dur = out.run.sawSaving ? (out.run.durationS ?? 0) : runSeconds(out.run, next.receivedAt, E);
      out = resolveRun(out, outcome, dur, next.receivedWallMs);
    }
    out = {
      ...out,
      run: {
        episode: out.savedCount + 1,
        startAt: Number.isFinite(next.receivedAt) ? next.receivedAt - proceed * 1000 : null,
        startWallMs: Number.isFinite(next.receivedWallMs) ? next.receivedWallMs - proceed * 1000 : null,
        createdWallMs: next.receivedWallMs ?? null,
        resolved: false,
        sawSaving: false,
        durationS: null,
      },
      awaitNewRun: false,
      collisionOpen: false,
    };
  }

  // R8 — the last episode is saved: the robot is finishing on its own.
  const N = numEpisodesOf(out, next);
  if (recordTick && N > 0 && count >= N
      && (out.finish.state !== 'finalizing' || out.finish.fromIntent)) {
    out = { ...out, finish: { ...EMPTY_FINISH, state: 'finalizing' } };
  }

  // R9 — the terminating READY tick.
  if (!next.running && phase === TaskPhase.READY) {
    out = endSession(out, next, E);
  }
  return out;
}

function endSession(s, next, E) {
  let out = s;
  const endedWallMs = next.receivedWallMs ?? null;
  if (out.run && !out.run.resolved) {
    const run = out.run;
    // „Behalten und beenden": the run lasted until the PRESS. The server judges
    // the same run at receipt (press + RTT), so a run the client measures
    // ≥ 1 s is always kept by the server, and only one under 1 s can be dropped.
    const dur = out.intent?.kind === 'keep_end' && Number.isFinite(out.intent.at)
      ? runSecondsWall(run, out.intent.at, E)
      : run.durationS ?? runSeconds(run, next.receivedAt, E);
    let outcome;
    if (out.intent?.kind === 'discard_end') {
      // The run the page discarded → „Beim Beenden verworfen"; a run that only
      // started after the discard was sent is the one FINISH dropped (Q4) → no row.
      outcome = Number.isFinite(run.createdWallMs) && run.createdWallMs >= out.intent.at ? null : 'ended';
    } else if (dur < MIN_ATTEMPT_S) {
      outcome = null; // never a real attempt (Q3)
    } else if (out.intent?.kind === 'keep_end' && keepEndDropsRun(out, run)) {
      outcome = 'ended'; // Q8: too soon after „Wiederholen"
    } else if (!out.intent && recentWarnStartsWith(out, SOURCE_STOP_PREFIX_DE, endedWallMs)) {
      outcome = 'source'; // the robot ended the session: a source stopped (R5-2) or C7
    } else if (recentWarnStartsWith(out, SOURCE_GAP_PREFIX_DE, endedWallMs)) {
      outcome = 'gap_end'; // a source gap in the take FINISH ended: discarded, not re-recorded
    } else {
      outcome = 'drop'; // FINISH met a frame drop, or an F1 end
    }
    out = outcome
      ? resolveRun(out, outcome, dur, endedWallMs)
      : { ...out, run: { ...run, resolved: true } };
  }
  const snapshot = out.snapshot || {};
  // F4: a session the robot ran without upload ends local; its notice on the
  // terminating tick (the upload it skipped) is not a blocked upload.
  const uploadOff = typeof out.uploadOff === 'string' && out.uploadOff ? out.uploadOff : null;
  const rawWarn = next.recordWarn || '';
  const warn = uploadOff && rawWarn.startsWith(UPLOAD_OFF_PREFIX_DE) ? '' : rawWarn;
  const pushToHub = !!snapshot.pushToHub && !uploadOff;
  let finish;
  if (out.savedCount === 0) {
    finish = { ...EMPTY_FINISH, state: 'nothing', endedAt: endedWallMs };
  } else if (warn) {
    // The terminating tick carries a warning iff the upload was blocked. With
    // upload OFF the only cause is a failed finalize (V1-2); with it ON, the
    // finalize sentence says which.
    const finalizeFailed = !pushToHub || warn.startsWith(FINALIZE_FAILED_PREFIX_DE);
    finish = {
      ...EMPTY_FINISH, state: finalizeFailed ? 'finalize_failed' : 'upload_failed', message: warn, endedAt: endedWallMs,
    };
  } else if (!pushToHub) {
    finish = { ...EMPTY_FINISH, state: 'local_done', endedAt: endedWallMs };
  } else {
    finish = {
      ...EMPTY_FINISH,
      state: 'uploading',
      expectedRepoId: out.startedHere ? expectedRepoOf(snapshot) : null,
      endedAt: endedWallMs,
    };
  }
  if (uploadOff) finish.uploadOff = uploadOff;
  let endNote = null;
  if (out.collisionOpen) {
    endNote = COLLISION_END_NOTE_DE;
  } else if (out.lastWarn && out.lastWarn.text !== warn && out.lastWarn.text !== uploadOff
      && Number.isFinite(endedWallMs)
      && endedWallMs - out.lastWarn.at <= END_NOTE_WINDOW_MS) {
    endNote = out.lastWarn.text;
  }
  finish.endNote = endNote;
  // V2-6: a session ended by a tick that is not its own terminating record
  // tick (the idle identity tick after a link loss across the end) never saw
  // the end — what the upload did since is unknown, not „nicht begonnen".
  finish.linkLost = next.taskType !== 'record';
  return {
    ...out,
    active: false,
    endedWallMs,
    run: null,
    awaitNewRun: false,
    intent: null,
    pendingStart: null,
    collisionOpen: false,
    finish,
  };
}

// Q4/Q8: the server drops a run that STARTED after this client's RERECORD when
// the FINISH comes within the window of that RERECORD.
function keepEndDropsRun(s, run) {
  const sent = s.lastRedoSentAt;
  if (!Number.isFinite(sent) || !Number.isFinite(run.startWallMs)) return false;
  return run.startWallMs >= sent && s.intent.at - sent <= RERECORD_FINISH_WINDOW_MS;
}

/**
 * R12 — the collision e-stop tripped (`payload.active` false → true). A run
 * that was recording is „Kollision, verworfen"; one that was already SAVING is
 * „Gespeichert" (Q7, provisional until the count confirms it). While the robot
 * is finishing a collision decides nothing (F7b): the finish completes.
 */
export function noteCollision(s, taskStatus, payload) {
  if (!payload?.active || payload.wasActive || !s.active) return s;
  let out = { ...s, collisionOpen: true };
  const run = out.run;
  if (!run || run.resolved || out.finish.state === 'finalizing') return out;
  const E = episodeTimeOf(out, taskStatus);
  const wallMs = payload.receivedWallMs ?? null;
  if (taskStatus?.phase === TaskPhase.SAVING) {
    const durationS = run.durationS ?? runSeconds(run, payload.receivedAt, E);
    out = {
      ...out,
      run: { ...run, resolved: true },
      episodes: [...out.episodes, row(run.episode, 'saved', durationS, wallMs, {
        provisional: true, early: E > 0 && durationS < E - 0.5,
      })],
    };
  } else {
    out = resolveRun(out, 'collision', runSeconds(run, payload.receivedAt, E), wallMs);
  }
  return { ...out, awaitNewRun: true };
}

/** R16 — the link dropped while the upload was being tracked. */
export function noteLink(s, heartbeat) {
  if (heartbeat === 'connected' || !isFinishTracking(s.finish) || s.finish.linkLost) return s;
  return { ...s, finish: { ...s.finish, linkLost: true } };
}

/** R10 / R11 — a notice derived from a /task/status error. */
export function noteRecordNotice(s, notice) {
  if (!notice) return s;
  if (notice.kind === 'error') {
    if (s.active) {
      return {
        ...s,
        active: false,
        endedWallMs: notice.at ?? null,
        run: s.run && !s.run.resolved ? { ...s.run, resolved: true } : s.run,
        awaitNewRun: false,
        intent: null,
        pendingStart: null,
        finish: { ...EMPTY_FINISH, state: 'stopped_error', endedAt: notice.at ?? null },
        errorText: notice.text || '',
      };
    }
    if (s.pendingStart) return { ...s, pendingStart: null };
    return s;
  }
  if (notice.kind === 'warn' && (s.active || s.pendingStart)) {
    const text = notice.text || '';
    let out = s;
    if (text.startsWith(UPLOAD_OFF_PREFIX_DE) && s.uploadOff !== text) out = { ...out, uploadOff: text };
    if (s.active) out = { ...out, lastWarn: { text, at: notice.at ?? null } };
    return out;
  }
  return s;
}

/** R13 — the page's own intents and command acknowledgements. */
export function applyRecordIntent(s, intent) {
  const kind = intent?.kind;
  const at = intent?.at ?? null;
  switch (kind) {
    case 'start':
      if (s.active) return s;
      return {
        ...EMPTY_RECORD_SESSION,
        id: s.id,
        finish: isFinishTracking(s.finish) ? s.finish : EMPTY_FINISH,
        pendingStart: { at, snapshot: intent.snapshot ? { ...intent.snapshot } : null },
      };
    case 'start_failed':
      return s.pendingStart ? { ...s, pendingStart: null } : s;
    case 'redo_ack':
    case 'discard_ack': {
      const sentAt = intent.sentAt ?? at;
      let out = { ...s, lastRedoSentAt: sentAt };
      const run = out.run;
      // Only the run that was recording when the RERECORD was SENT; a run the
      // robot started since (reset 0, H15) is already the new one.
      const isOld = run && (!Number.isFinite(run.createdWallMs) || !Number.isFinite(sentAt)
        || run.createdWallMs <= sentAt);
      if (isOld) {
        if (!run.resolved) {
          const outcome = kind === 'redo_ack' ? 'redo' : 'ended';
          out = resolveRun(out, outcome, runSecondsWall(run, at, episodeTimeOf(out, null)), at);
        }
        out = { ...out, awaitNewRun: true };
      }
      return out;
    }
    case 'discard_end':
    case 'end':
    case 'keep_end':
      if (!s.active) return s;
      return {
        ...s,
        intent: { kind, at, episode: s.run && !s.run.resolved ? s.run.episode : null },
        finish: { ...EMPTY_FINISH, state: 'finalizing', fromIntent: true },
      };
    case 'clear': {
      let out = s.intent ? { ...s, intent: null } : s;
      if (out.active && out.finish.state === 'finalizing' && out.finish.fromIntent) {
        out = { ...out, finish: { ...EMPTY_FINISH } };
      }
      return out;
    }
    default:
      return s;
  }
}

/** R14 — a /huggingface/status upload message. */
export function applyUploadStatus(s, { repoId, status, percentage, message, at } = {}) {
  const f = s.finish;
  if (!isFinishTracking(f)) return s;
  if (Number.isFinite(f.endedAt) && Number.isFinite(at) && at < f.endedAt) return s;
  const matches = f.expectedRepoId ? repoId === f.expectedRepoId : (!f.repoId || repoId === f.repoId);
  if (!repoId || !matches) return s;
  // A status of this upload that arrives is the link seeing it again: the
  // state is known once more (R16 / V2-6 set linkLost while it was not).
  if (status === 'Uploading') {
    if (f.state !== 'uploading') return s;
    const pct = Math.max(0, Math.min(100, Math.round(num(percentage))));
    if (pct === f.uploadPct && f.repoId === repoId && !f.linkLost) return s;
    return { ...s, finish: { ...f, uploadPct: pct, repoId, linkLost: false } };
  }
  if (status === 'Success') {
    if (f.state !== 'uploading') return s;
    return { ...s, finish: { ...f, state: 'registering', repoId, uploadPct: 100, linkLost: false } };
  }
  if (status === 'Failed') {
    return { ...s, finish: { ...f, state: 'upload_failed', repoId, message: message || '', linkLost: false } };
  }
  return s;
}

/** R15 — the cloud dataset registration of the uploaded repo. */
export function applyRegisterStatus(s, { repoId, state } = {}) {
  const f = s.finish;
  if (f.state !== 'registering' || !repoId || repoId !== f.repoId) return s;
  if (state === 'done') return { ...s, finish: { ...f, state: 'done', registerState: 'done' } };
  if (state === 'failed' || state === 'skipped') {
    return { ...s, finish: { ...f, state: 'done', registerState: state } };
  }
  return s; // 'pending' keeps registering
}

/** R17 — „Neue Aufnahme": clear the card; an upload still in flight stays tracked. */
export function dismissRecordSession(s, { running } = {}) {
  if (running || s.active) return s;
  const finish = isFinishTracking(s.finish) ? { ...s.finish, dismissed: true } : EMPTY_FINISH;
  return { ...EMPTY_RECORD_SESSION, id: s.id, pendingStart: s.pendingStart, finish };
}
