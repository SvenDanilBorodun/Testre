// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// What the Aufnahme page shows, derived in one place (spec §3.4): the view,
// the episode numbers (F6d), the phase pill, the buttons with their keys, the
// Phasenleiste segments, the Episodenleiste, why Start is refused (Q5), and
// whether the task form may be edited (F6b). Pure: every input is a value.
//
// Shapes follow the props of the components that draw them:
//   pill     {title, sub, icon, color}                         → ActionBar
//   buttons  [{id, label, kbd, shortcut, icon, variant, size, disabled, title, spinning}]
//   segments [{key, kind, label, weight, state, dot}]           → PhaseTrack
//   dots     {label, items: [{n, done, current, redo}], more, color} → EpisodeDots

import TaskPhase from '../../../constants/taskPhases';
import { datasetRepoId } from '../../../utils/datasetName';
import { isFinishTracking } from '../../../features/tasks/recordSession';
import RECORD_COPY from './recordCopy';
import { diskProblem, sourcesWith, stalledSourceProblem } from './problems';

export const VIEW = Object.freeze({
  OFFLINE: 'OFFLINE',
  CONNECTING: 'CONNECTING',
  INFERENCE_BUSY: 'INFERENCE_BUSY',
  READY: 'READY',
  STARTING: 'STARTING',
  WARMUP: 'WARMUP',
  RECORDING: 'RECORDING',
  SAVING: 'SAVING',
  RESETTING: 'RESETTING',
  COLLISION: 'COLLISION',
  FINISHING: 'FINISHING',
});

/** After this long without a first record tick, STARTING says so (F2). */
export const STARTING_SLOW_MS = 8000;
export const MAX_DOTS = 12;

export const VIEW_COLOR = Object.freeze({
  [VIEW.WARMUP]: 'var(--rec-warm)',
  [VIEW.RECORDING]: 'var(--rec-run)',
  [VIEW.SAVING]: 'var(--rec-save)',
  [VIEW.FINISHING]: 'var(--rec-save)',
  [VIEW.RESETTING]: 'var(--rec-reset)',
  [VIEW.READY]: 'var(--rec-reset)',
  [VIEW.STARTING]: 'var(--rec-reset)',
  [VIEW.CONNECTING]: 'var(--rec-reset)',
  [VIEW.COLLISION]: 'var(--ink-4)',
  [VIEW.OFFLINE]: 'var(--ink-4)',
  [VIEW.INFERENCE_BUSY]: 'var(--ink-4)',
});

// The phase kind (a PhaseTrack segment / overlay key) a timed view runs.
export const VIEW_KIND = Object.freeze({
  [VIEW.WARMUP]: 'warmup',
  [VIEW.RECORDING]: 'record',
  [VIEW.SAVING]: 'save',
  [VIEW.RESETTING]: 'reset',
  [VIEW.COLLISION]: 'record',
});

const C = RECORD_COPY;
const SESSION_VIEWS = new Set([VIEW.WARMUP, VIEW.RECORDING, VIEW.SAVING, VIEW.RESETTING, VIEW.COLLISION]);
const INFERENCE_PHASES = new Set([TaskPhase.INFERENCING, TaskPhase.INFERENCE_LOADING]);

const num = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

/** The view, by the spec's rules in order. */
export function deriveView({ heartbeat, status = {}, collision = null, session = null } = {}) {
  const finish = session?.finish || { state: 'idle' };
  if (collision?.active) return VIEW.COLLISION;
  if (status.running && (status.taskType === 'inference' || INFERENCE_PHASES.has(status.phase))) {
    return VIEW.INFERENCE_BUSY;
  }
  if (status.running) {
    if (finish.state === 'finalizing' || finish.state === 'stopped_error') return VIEW.FINISHING;
    switch (status.phase) {
      case TaskPhase.WARMING_UP: return VIEW.WARMUP;
      case TaskPhase.RECORDING: return VIEW.RECORDING;
      case TaskPhase.RESETTING: return VIEW.RESETTING;
      case TaskPhase.SAVING:
      case TaskPhase.STOPPED: return VIEW.SAVING;
      default: break;
    }
  }
  if (heartbeat !== 'connected') return VIEW.OFFLINE;
  if (!status.topicReceived) return VIEW.CONNECTING;
  if (session?.pendingStart) return VIEW.STARTING;
  if (finish.state !== 'idle' && finish.state !== 'nothing' && !finish.dismissed) return VIEW.FINISHING;
  return VIEW.READY;
}

function button(id, patch) {
  const base = {
    start: { label: C.btn.start, kbd: C.kbd.space, shortcut: 'Space', icon: 'play', variant: 'primary', size: 'xl' },
    skip: { kbd: C.kbd.right, shortcut: 'ArrowRight', icon: 'skipForward', size: 'md' },
    redo: { label: C.btn.redo, kbd: C.kbd.left, shortcut: 'ArrowLeft', icon: 'again', variant: 'ghost', size: 'md' },
    saveNow: { label: C.btn.saveNow, kbd: C.kbd.right, shortcut: 'ArrowRight', icon: 'check', variant: 'go', size: 'md' },
    end: { label: C.btn.end, kbd: C.kbd.end, shortcut: 'Control+Shift+X', icon: 'stop', variant: 'end', size: 'md' },
  }[id];
  return { id, title: '', disabled: false, spinning: false, ...base, ...patch };
}

function buttonsFor(view, { startBlock, busy, connected, elapsedS }) {
  const off = busy || !connected;
  const withOff = (list) => list.map((b) => (off ? { ...b, disabled: true } : b));
  switch (view) {
    case VIEW.READY:
      return withOff([button('start', { disabled: !!startBlock })]);
    case VIEW.STARTING:
      return [button('start', { disabled: true, spinning: true })];
    case VIEW.WARMUP:
      return withOff([button('skip', { label: C.btn.skipWarmup, variant: 'warn' }), button('end')]);
    case VIEW.RECORDING:
      return withOff([
        button('redo'),
        button('saveNow', { disabled: !(num(elapsedS) >= 1) }),
        button('end'),
      ]);
    case VIEW.SAVING:
      return [
        button('redo', { disabled: true }),
        button('saveNow', { disabled: true, variant: 'ghost' }),
        button('end', { disabled: true, variant: 'ghost' }),
      ];
    case VIEW.RESETTING:
      return withOff([button('skip', { label: C.btn.skipReset, variant: 'primary' }), button('end')]);
    case VIEW.COLLISION:
      return [button('end', { disabled: true, title: C.btn.endCollision })];
    case VIEW.OFFLINE:
    case VIEW.CONNECTING:
    case VIEW.INFERENCE_BUSY:
      return [button('start', { disabled: true })];
    default:
      return [];
  }
}

function pillFor(view, { form, session, episode, secondsLeft, nowWallMs }) {
  const color = VIEW_COLOR[view];
  const k = session?.savedCount ?? 0;
  switch (view) {
    case VIEW.READY:
      return { title: C.pill.ready, sub: C.pill.readySub(num(form.numEpisodes), num(form.episodeTime)), icon: 'checkCircle', color };
    case VIEW.STARTING: {
      const since = nowWallMs - num(session?.pendingStart?.at);
      const slow = Number.isFinite(since) && since >= STARTING_SLOW_MS;
      return { title: C.pill.starting, sub: slow ? C.pill.startingSlow : C.pill.startingSub, icon: 'loading', color, slow };
    }
    case VIEW.WARMUP:
      return { title: C.pill.warmup, sub: C.pill.warmupSub(secondsLeft), icon: 'hand', color };
    case VIEW.RECORDING:
      return { title: C.pill.recording, sub: C.pill.recordingSub(episode.current, episode.total, secondsLeft), icon: 'liveRecording', color };
    case VIEW.SAVING:
      return { title: C.pill.saving, sub: C.pill.savingSub(episode.current, episode.total), icon: 'save', color };
    case VIEW.RESETTING:
      return { title: C.pill.resetting, sub: C.pill.resettingSub(secondsLeft, episode.current), icon: 'again', color };
    case VIEW.COLLISION:
      return { title: C.pill.collision, sub: C.pill.collisionSub, icon: 'stop', color };
    case VIEW.FINISHING: {
      const state = session?.finish?.state;
      if (state === 'stopped_error') return { title: C.pill.stopped, sub: '', icon: 'failed', color };
      if (state === 'finalizing') return { title: C.pill.finishing, sub: '', icon: 'save', color };
      if (isFinishTracking(session?.finish)) {
        return { title: C.pill.finishing, sub: C.pill.doneSub(k), icon: 'cloudUpload', color };
      }
      return { title: C.pill.done, sub: C.pill.doneSub(k), icon: 'checkCircle', color };
    }
    case VIEW.OFFLINE:
      return { title: C.pill.offline, sub: '', icon: 'unplugged', color };
    case VIEW.CONNECTING:
      return { title: C.pill.connecting, sub: '', icon: 'loading', color };
    case VIEW.INFERENCE_BUSY:
      return { title: C.pill.inference, sub: '', icon: 'info', color };
    default:
      return null;
  }
}

function segmentsFor(view, { status, plan, episode, session }) {
  if (!SESSION_VIEWS.has(view) || (view === VIEW.COLLISION && !session?.active)) return null;
  const { W, E, Z } = plan;
  const total = num(status.totalTime);
  const list = [];
  if ((view === VIEW.WARMUP && total > 0) || (episode.saved === 0 && !session?.sawReset && W > 0)) {
    list.push({ key: 'warmup', kind: 'warmup', label: C.track.warmup, weight: view === VIEW.WARMUP ? total : W });
  }
  list.push({ key: 'record', kind: 'record', label: C.track.record(episode.current), weight: E });
  list.push({ key: 'save', kind: 'save', label: C.track.save, weight: 0, dot: true });
  if ((Z > 0 && !episode.isLast) || (view === VIEW.RESETTING && total > 0)) {
    list.push({ key: 'reset', kind: 'reset', label: C.track.reset, weight: view === VIEW.RESETTING ? total : Z });
  }
  const nowKind = VIEW_KIND[view];
  let nowIndex = list.findIndex((s) => s.kind === nowKind);
  if (nowIndex < 0) nowIndex = list.length; // a 0-s phase has no segment: everything before it is done
  return list.map((s, i) => ({
    ...s,
    dot: !!s.dot,
    state: i < nowIndex ? 'done' : i === nowIndex ? 'now' : 'upcoming',
  }));
}

function trackRemainingFor(view, secondsLeft) {
  if (view === VIEW.WARMUP || view === VIEW.RECORDING || view === VIEW.RESETTING) return C.track.left(secondsLeft);
  if (view === VIEW.SAVING) return C.track.saving;
  if (view === VIEW.COLLISION) return C.track.paused;
  return '';
}

function dotsFor(view, { episode, session }) {
  const color = VIEW_COLOR[view];
  const shown = SESSION_VIEWS.has(view) || view === VIEW.FINISHING;
  if (!shown || episode.total <= 0) return { label: C.dots.label, items: [], more: '', color };
  const redone = new Set((session?.episodes || []).filter((e) => e.outcome !== 'saved').map((e) => e.n));
  const items = [];
  for (let n = 1; n <= Math.min(episode.total, MAX_DOTS); n += 1) {
    const done = n <= episode.saved;
    items.push({ n, done, current: !done && !!session?.active && n === episode.current, redo: redone.has(n) });
  }
  const more = episode.total > MAX_DOTS ? C.dots.more(episode.total - MAX_DOTS) : '';
  return { label: C.dots.label, items, more, color };
}

/**
 * Why Start is refused, first match (Q5): the disk, a stalled source (camera →
 * follower → leader), or the same dataset still uploading. Validation is not a
 * block — it runs on the click.
 * @returns {null | {kind: 'disk'|'source'|'uploading', problem}}
 */
export function deriveStartBlock({ disk = null, verdicts = null, bridge = null, activation = null, session = null, form = {}, robotType = '' } = {}) {
  const diskP = diskProblem(disk, { running: false });
  if (diskP) return { kind: 'disk', problem: diskP };
  const stalled = sourcesWith(verdicts, 'stalled')[0];
  if (stalled) return { kind: 'source', problem: stalledSourceProblem(stalled, { bridge, activation }) };
  const finish = session?.finish;
  if (isFinishTracking(finish)) {
    const uploadingRepo = finish.expectedRepoId || finish.repoId;
    if (uploadingRepo && uploadingRepo === datasetRepoId(form.userId, robotType, form.taskName)) {
      return { kind: 'uploading', problem: { kind: 'bad', textDe: C.problem.startUploading } };
    }
  }
  return null;
}

/**
 * Everything the page draws from the current state.
 * @param input.heartbeat   'connected' | …
 * @param input.status      selectRecordStatus output
 * @param input.form        selectRecordForm output
 * @param input.collision   state.tasks.collision
 * @param input.session     the record session
 * @param input.nowWallMs   Date.now()
 * @param input.elapsedS    the clock's elapsed seconds in the phase
 * @param input.secondsLeft the clock's whole seconds left (derived from elapsedS when absent)
 * @param input.busy        a command is in flight
 * @param input.disk        {verdict, free, startFloor, criticalFloor} | null
 * @param input.verdicts    sourceVerdicts output | null
 * @param input.bridge      useRsBridgeStatus output | null
 * @param input.activation  useRobotActivation status | null
 */
export function deriveRecordView({
  heartbeat = 'disconnected',
  status = {},
  form = {},
  collision = null,
  session = null,
  nowWallMs = Date.now(),
  elapsedS = 0,
  secondsLeft,
  busy = false,
  disk = null,
  verdicts = null,
  bridge = null,
  activation = null,
} = {}) {
  const view = deriveView({ heartbeat, status, collision, session });
  const running = !!status.running;
  const finishingAfter = view === VIEW.FINISHING && !running;
  const snapshot = session?.snapshot || {};
  const total = running
    ? (num(status.numEpisodes) || num(form.numEpisodes))
    : finishingAfter ? (num(snapshot.numEpisodes) || num(form.numEpisodes)) : num(form.numEpisodes);
  const saved = finishingAfter ? num(session?.savedCount) : num(status.currentEpisodeNumber);
  const current = Math.max(1, Math.min(saved + 1, Math.max(total, 1)));
  const isLast = saved + 1 >= total;
  const episode = { current, total, saved, isLast };

  const plan = running
    ? { W: num(status.warmupTime), E: num(status.episodeTime), Z: num(status.resetTime) }
    : { W: num(form.warmupTime), E: num(form.episodeTime), Z: num(form.resetTime) };
  const left = Number.isFinite(secondsLeft)
    ? secondsLeft
    : Math.max(0, Math.ceil(num(status.totalTime) - num(elapsedS)));

  const connected = heartbeat === 'connected';
  const startBlock = deriveStartBlock({
    disk, verdicts, bridge, activation, session, form, robotType: status.robotType,
  });
  const segments = segmentsFor(view, { status, plan, episode, session });
  const idleText = view === VIEW.FINISHING ? C.track.allDone : C.track.idle;

  const editable = connected && !!status.topicReceived && status.phase === TaskPhase.READY && !running
    && view !== VIEW.STARTING && view !== VIEW.FINISHING;
  let lockedReason = '';
  if (!editable) {
    if (!connected || !status.topicReceived) lockedReason = C.locked.offline;
    else if (view === VIEW.INFERENCE_BUSY) lockedReason = C.locked.inference;
    else lockedReason = C.locked.running;
  }

  return {
    view,
    episode,
    isLastEpisode: isLast,
    phaseColor: VIEW_COLOR[view],
    phaseKind: VIEW_KIND[view] || null,
    pill: pillFor(view, { form, session, episode, secondsLeft: left, nowWallMs }),
    buttons: buttonsFor(view, { startBlock, busy, connected, elapsedS }),
    segments,
    idleText,
    trackRemaining: trackRemainingFor(view, left),
    dots: dotsFor(view, { episode, session }),
    startBlock,
    editable,
    lockedReason,
    secondsLeft: left,
  };
}
