// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Aufnahme page's one logic hook (spec §3.7). The page composes
// presentational components from what this returns; every decision — which
// view, which buttons, which problem, what a key does, what a command sends —
// is made here or in the pure models under ./model.
//
//   const c = useRecordController({ isActive });
//   c.view, c.model (deriveRecordView), c.clock ({subscribe, secondsLeft, elapsed}),
//   c.problem ({kind, textDe, linkToHome?} | null), c.question | null, c.closeQuestion(),
//   c.act(actionId), c.busy, c.form, c.setField(field, value), c.editable, c.lockedReason,
//   c.hfUsers ({list, reload(), loading}), c.signal ([{kind, name, labelDe, hz, hzText, verdict}] | null),
//   c.finish (finishSteps), c.session (sessionView), c.dismissFinish(), c.goToTraining(),
//   c.goToHome(), c.muted, c.toggleMute(), c.estimate ({totalS, parts, text}),
//   c.repoPreview (string), c.onPhaseTick(fn) → unsubscribe
// plus conveniences: c.copy, c.steppers, c.saveName, c.invalid, c.reducedMotion.
//
// It also owns the page's side effects: the window key listener, the
// Benutzer-ID auto-reload / auto-select (ported from the old InfoPanel, so
// they run whether or not „Erweitert" is open), the 700 Hz countdown ticks,
// the first-load tag seed and the capability eject.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { shallowEqual, useDispatch, useSelector, useStore } from 'react-redux';
import toast from 'react-hot-toast';

import PageType from '../../constants/pageType';
import {
  addTag,
  recordNoticeClear,
  recordSessionDismiss,
  setTaskInfo,
} from '../../features/tasks/taskSlice';
import {
  selectPhaseAnchor,
  selectRecordForm,
  selectRecordNotice,
  selectRecordSession,
  selectRecordStatus,
} from '../../features/tasks/recordSelectors';
import { moveToPage, setIsFirstLoadFalse } from '../../features/ui/uiSlice';
import {
  setDatasetRepoId,
  setSelectedDataset,
  setSelectedUser,
} from '../../features/training/trainingSlice';
import { useHfUserList } from '../../hooks/useHfUserList';
import useRobotActivation from '../../hooks/useRobotActivation';
import useRsBridgeStatus from '../../hooks/useRsBridgeStatus';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import useSignalStatus from '../../hooks/useSignalStatus';
import useSmoothPhaseClock from '../../hooks/useSmoothPhaseClock';
import { datasetRepoId, safeTaskName, safeUserId } from '../../utils/datasetName';
import {
  STEPPER_LIMITS,
  TASK_NAME_MAX,
  estimateRecording,
  forceRecordTaskInfo,
  validateRecordTaskInfo,
} from '../../utils/recordTaskInfo';
import {
  SAVING_GRACE_MS,
  diskVerdict,
  isSignalFresh,
  sourceVerdicts,
} from '../../utils/signalStatus';
import { datasetIdOf, finishSteps, sessionView } from './model/finishModel';
import { VIEW, deriveRecordView, deriveView } from './model/phaseModel';
import { firstProblem } from './model/problems';
import RECORD_COPY, { armNameDe, cameraNameDe, numberDe } from './model/recordCopy';
import { runRecordAction } from './model/recordCommands';
import { keyToAction } from './model/recordKeys';
import { createRecordSounds, isMuted, setMuted as writeMuted } from './recordSounds';

/** A failure / validation message stays this long. */
export const TRANSIENT_MS = 8000;
/** „Episode n wird wiederholt." */
export const NOTE_SHORT_MS = 3500;
/** Every other info note. */
export const NOTE_LONG_MS = 5000;

const TIMED_VIEWS = new Set([VIEW.WARMUP, VIEW.RECORDING, VIEW.RESETTING]);
const COUNTDOWN_VIEWS = new Set([VIEW.WARMUP, VIEW.RESETTING]);
const SUPPRESS_VIEWS = new Set([VIEW.SAVING, VIEW.FINISHING, VIEW.COLLISION]);
const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

export const RECORD_STEPPERS = Object.freeze([
  { field: 'warmupTime', label: RECORD_COPY.stepper.warmupTime, unit: RECORD_COPY.stepper.seconds, color: 'var(--rec-warm)' },
  { field: 'episodeTime', label: RECORD_COPY.stepper.episodeTime, unit: RECORD_COPY.stepper.seconds, color: 'var(--rec-run)' },
  { field: 'resetTime', label: RECORD_COPY.stepper.resetTime, unit: RECORD_COPY.stepper.seconds, color: 'var(--rec-reset)' },
  { field: 'numEpisodes', label: RECORD_COPY.stepper.numEpisodes, unit: '', color: 'var(--ink-3)' },
].map((s) => Object.freeze({ ...s, min: STEPPER_LIMITS[s.field][0], max: STEPPER_LIMITS[s.field][1] })));

function readReducedMotion() {
  try {
    return !!(window.matchMedia && window.matchMedia(REDUCED_MOTION_QUERY).matches);
  } catch {
    return false;
  }
}

// The instruction the textarea shows: the first entry with content (untrimmed,
// so typing keeps its spaces), else the first entry.
function instructionText(list) {
  if (!Array.isArray(list)) return String(list ?? '');
  const withText = list.find((x) => String(x ?? '').trim());
  return String(withText ?? list[0] ?? '');
}

function snapshotOf(taskInfo, robotType) {
  const forced = forceRecordTaskInfo(taskInfo || {});
  return {
    taskName: String(forced.taskName || ''),
    userId: String(forced.userId || ''),
    robotType: robotType || '',
    pushToHub: !!forced.pushToHub,
    privateMode: forced.privateMode !== false,
    numEpisodes: Number(forced.numEpisodes) || 0,
    episodeTime: Number(forced.episodeTime) || 0,
    warmupTime: Number(forced.warmupTime) || 0,
    resetTime: Number(forced.resetTime) || 0,
    fps: Number(forced.fps) || 0,
  };
}

export default function useRecordController({ isActive = true } = {}) {
  const dispatch = useDispatch();
  const store = useStore();
  const { sendRecordCommand } = useRosServiceCaller();

  const heartbeat = useSelector((s) => s.tasks.heartbeatStatus);
  const status = useSelector(selectRecordStatus, shallowEqual);
  const formRaw = useSelector(selectRecordForm, shallowEqual);
  const collision = useSelector((s) => s.tasks.collision);
  const session = useSelector(selectRecordSession);
  const notice = useSelector(selectRecordNotice);
  const anchor = useSelector(selectPhaseAnchor);
  const isFirstLoad = useSelector((s) => s.ui?.isFirstLoad?.record);

  const connected = heartbeat === 'connected';

  // --- the wall / perf clock the time rules read (1 Hz) -------------------
  const [now, setNow] = useState(() => ({ wall: Date.now(), perf: performance.now() }));
  useEffect(() => {
    const id = setInterval(() => setNow({ wall: Date.now(), perf: performance.now() }), 1000);
    return () => clearInterval(id);
  }, []);

  // --- rig facts ------------------------------------------------------------
  const signal = useSignalStatus({ enabled: connected });
  const bridge = useRsBridgeStatus({ enabled: status.capabilities?.has_leader !== false });
  const activation = useRobotActivation({ enabled: connected }).status;

  const [reducedMotion, setReducedMotion] = useState(readReducedMotion);
  useEffect(() => {
    let mq;
    try { mq = window.matchMedia ? window.matchMedia(REDUCED_MOTION_QUERY) : null; } catch { mq = null; }
    if (!mq || typeof mq.addEventListener !== 'function') return undefined;
    const onChange = () => setReducedMotion(!!mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  // --- local page state -------------------------------------------------------
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [transient, setTransient] = useState(null); // {kind, textDe, until, field?}
  const [question, setQuestion] = useState(null); // {kind: 'end', episode}
  const [muted, setMuted] = useState(isMuted);
  const [hfLoading, setHfLoading] = useState(false);
  const soundsRef = useRef(null);
  if (soundsRef.current === null) soundsRef.current = createRecordSounds();
  useEffect(() => () => soundsRef.current && soundsRef.current.dispose(), []);

  const showTransient = useCallback((kind, textDe, ms, extra = {}) => {
    setTransient({ kind, textDe, until: Date.now() + ms, ...extra });
  }, []);

  // --- the view, the verdicts, the model -------------------------------------
  const view0 = deriveView({ heartbeat, status, collision, session });
  const savingEndedAtRef = useRef(null);
  const prevViewRef = useRef(view0);
  if (prevViewRef.current !== view0) {
    if (prevViewRef.current === VIEW.SAVING) savingEndedAtRef.current = performance.now();
    prevViewRef.current = view0;
  }
  const perfNow = Math.max(now.perf, signal.receivedAt ?? 0);
  const inSavingGrace = savingEndedAtRef.current !== null
    && perfNow - savingEndedAtRef.current < SAVING_GRACE_MS;
  const running = !!status.running;
  const expectedHz = Number(running ? status.fps : formRaw.fps) || 0;
  const fresh = isSignalFresh(signal.payload, signal.receivedAt, perfNow);
  const verdicts = useMemo(() => sourceVerdicts(signal.payload, {
    expectedHz,
    receivedAt: signal.receivedAt,
    nowMs: perfNow,
    suppress: SUPPRESS_VIEWS.has(view0) || inSavingGrace,
  }), [signal.payload, signal.receivedAt, expectedHz, perfNow, view0, inSavingGrace]);
  const disk = useMemo(() => {
    if (!fresh || !signal.payload.disk) return null;
    const d = signal.payload.disk;
    return {
      verdict: diskVerdict(signal.payload),
      free: d.free_bytes,
      startFloor: d.start_floor_bytes,
      criticalFloor: d.critical_floor_bytes,
    };
  }, [fresh, signal.payload]);

  const frozen = !TIMED_VIEWS.has(view0);
  const rawClock = useSmoothPhaseClock(anchor, { frozen, reducedMotion });
  // For one render after a phase change the clock's whole seconds still belong
  // to the previous instance; until it catches up, the robot's own count
  // (floored seconds) stands in.
  const clockCurrent = rawClock.instance === (anchor ? anchor.instance : null);
  const proceed = Number(status.proceedTime) || 0;
  const clock = useMemo(() => (clockCurrent ? rawClock : {
    ...rawClock,
    elapsed: proceed,
    secondsLeft: Math.max(0, Math.ceil((Number(status.totalTime) || 0) - proceed)),
  }), [clockCurrent, rawClock, proceed, status.totalTime]);

  const model = useMemo(() => deriveRecordView({
    heartbeat,
    status,
    form: formRaw,
    collision,
    session,
    nowWallMs: now.wall,
    // Never ahead of the robot's own count („Jetzt speichern" from 1 s on).
    elapsedS: Math.min(clock.elapsed, proceed),
    secondsLeft: clock.secondsLeft,
    busy,
    disk,
    verdicts,
    bridge,
    activation,
  }), [heartbeat, status, formRaw, collision, session, now.wall, clock.elapsed, clock.secondsLeft, proceed, busy,
    disk, verdicts, bridge, activation]);
  const { view } = model;
  const currentEpisode = model.episode.current;

  const problem = useMemo(() => firstProblem({
    view,
    running,
    heartbeat,
    notice,
    transient,
    session,
    disk,
    verdicts,
    bridge,
    activation,
    fps: expectedHz,
    nowWallMs: now.wall,
  }), [view, running, heartbeat, notice, transient, session, disk, verdicts, bridge, activation, expectedHz, now.wall]);

  const invalid = transient && transient.field && Date.now() < transient.until
    ? { field: transient.field, messageDe: transient.textDe }
    : null;

  // --- the question („Beenden" while an episode runs) -------------------------
  const questionEpisode = question ? question.episode : null;
  useEffect(() => {
    if (!question) return;
    if (view !== VIEW.RECORDING || currentEpisode !== questionEpisode) setQuestion(null);
  }, [question, questionEpisode, view, currentEpisode]);

  const closeQuestion = useCallback(() => setQuestion(null), []);

  // --- commands ------------------------------------------------------------------
  const getStatus = useCallback(() => {
    const st = store.getState().tasks;
    return {
      phase: st.taskStatus.phase,
      running: st.taskStatus.running,
      taskType: st.taskStatus.taskType,
      currentEpisodeNumber: st.taskStatus.currentEpisodeNumber,
      collisionActive: !!st.collision?.active,
      pendingStart: st.recordSession?.pendingStart || null,
      session: st.recordSession || null,
    };
  }, [store]);

  const runAction = useCallback(async (action, extra = {}) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      const result = await runRecordAction(action, {
        send: sendRecordCommand,
        getStatus,
        dispatch,
        now: () => Date.now(),
        ...extra,
      });
      if (result?.messageDe) {
        showTransient('bad', result.messageDe, TRANSIENT_MS);
      } else if (result?.note) {
        showTransient('info', result.note, action === 'redo' ? NOTE_SHORT_MS : NOTE_LONG_MS);
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [dispatch, getStatus, sendRecordCommand, showTransient]);

  const act = useCallback((actionId) => {
    if (actionId === 'closeQuestion' || actionId === 'back') {
      setQuestion(null);
      return;
    }
    if (busyRef.current) return;
    switch (actionId) {
      case 'start': {
        if (model.view !== VIEW.READY || model.startBlock) return;
        const taskInfo = store.getState().tasks.taskInfo;
        const robotType = store.getState().tasks.taskStatus.robotType;
        const invalidNow = validateRecordTaskInfo(forceRecordTaskInfo(taskInfo), { robotType });
        if (invalidNow) {
          showTransient('warn', invalidNow.messageDe, TRANSIENT_MS, { field: invalidNow.field });
          return;
        }
        setTransient(null);
        soundsRef.current.prime();
        runAction('start', { snapshot: snapshotOf(taskInfo, robotType) });
        return;
      }
      case 'end':
        if (model.view === VIEW.RECORDING) {
          setQuestion({ kind: 'end', episode: currentEpisode });
          return;
        }
        runAction('end');
        return;
      case 'keepAndEnd':
      case 'discardAndEnd':
        setQuestion(null);
        runAction(actionId);
        return;
      case 'skip':
      case 'saveNow':
      case 'redo':
        runAction(actionId);
        return;
      default:
    }
  }, [model.view, model.startBlock, currentEpisode, runAction, showTransient, store]);

  // --- the window key listener ---------------------------------------------------
  const saveNowEnabled = !!model.buttons.find((b) => b.id === 'saveNow' && !b.disabled);
  const keyCtxRef = useRef(null);
  keyCtxRef.current = {
    view,
    question,
    busy,
    collisionActive: !!collision?.active,
    connected,
    startBlocked: !!model.startBlock,
    saveNowEnabled,
  };
  const actRef = useRef(act);
  actRef.current = act;
  useEffect(() => {
    if (!isActive) return undefined;
    const onKeyDown = (event) => {
      const action = keyToAction(event, keyCtxRef.current);
      if (!action) return;
      event.preventDefault();
      actRef.current(action);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isActive]);

  // --- phase ticks: listeners + the 700 Hz countdown ----------------------------
  const tickListenersRef = useRef(new Set());
  const lastTickRef = useRef(null);
  const instance = rawClock.instance;
  const anchorInstance = anchor ? anchor.instance : null;
  const anchorTotal = anchor ? anchor.total : 0;
  useEffect(() => {
    const prev = lastTickRef.current;
    const cur = { view, secondsLeft: rawClock.secondsLeft, instance };
    lastTickRef.current = cur;
    // Only seconds that belong to the running phase (not the previous one's,
    // which the clock still shows for one render after a change).
    if (!prev || !TIMED_VIEWS.has(view) || instance !== anchorInstance) return;
    if (prev.secondsLeft === cur.secondsLeft && prev.instance === cur.instance && prev.view === cur.view) return;
    const event = { ...cur, last3: cur.secondsLeft >= 1 && cur.secondsLeft <= 3 };
    tickListenersRef.current.forEach((fn) => {
      try { fn(event); } catch { /* a listener must not break the page */ }
    });
    if (COUNTDOWN_VIEWS.has(view) && anchorTotal >= 1 && event.last3) soundsRef.current.tick();
  }, [view, rawClock.secondsLeft, instance, anchorInstance, anchorTotal]);

  const onPhaseTick = useCallback((fn) => {
    tickListenersRef.current.add(fn);
    return () => { tickListenersRef.current.delete(fn); };
  }, []);

  // --- „Beendet. Es wurde keine Episode gespeichert …" --------------------------
  const finishState = session?.finish?.state;
  const prevFinishRef = useRef({ id: session?.id, state: finishState });
  useEffect(() => {
    const prev = prevFinishRef.current;
    prevFinishRef.current = { id: session?.id, state: finishState };
    if (finishState === 'nothing' && (prev.state !== 'nothing' || prev.id !== session?.id)) {
      showTransient('info', RECORD_COPY.note.nothingSaved, NOTE_LONG_MS);
    }
  }, [finishState, session?.id, showTransient]);

  // --- the form ------------------------------------------------------------------
  const form = useMemo(() => ({
    ...formRaw,
    taskInstruction: instructionText(formRaw.taskInstruction),
  }), [formRaw]);

  const { editable } = model;
  const setField = useCallback((field, value) => {
    if (!editable) return;
    if (field === 'taskInstruction') {
      dispatch(setTaskInfo({ taskInstruction: [String(value ?? '')] }));
      return;
    }
    if (field === 'taskName') {
      dispatch(setTaskInfo({ taskName: String(value ?? '').slice(0, TASK_NAME_MAX) }));
      return;
    }
    dispatch(setTaskInfo({ [field]: value }));
  }, [dispatch, editable]);

  // --- Benutzer-ID (ported from InfoPanel) ----------------------------------------
  const { hfUserList, reload } = useHfUserList();
  const reloadHfUsers = useCallback(async () => {
    setHfLoading(true);
    try {
      return await reload();
    } finally {
      setHfLoading(false);
    }
  }, [reload]);
  useEffect(() => {
    if (connected && hfUserList.length === 0) reload();
  }, [connected, hfUserList.length, reload]);
  useEffect(() => {
    if (editable && formRaw.userId === undefined && hfUserList.length > 0) {
      dispatch(setTaskInfo({ userId: hfUserList[0] }));
    }
  }, [editable, formRaw.userId, hfUserList, dispatch]);
  const hfUsers = useMemo(
    () => ({ list: hfUserList, reload: reloadHfUsers, loading: hfLoading }),
    [hfUserList, reloadHfUsers, hfLoading],
  );

  // --- moved from the old RecordPage: capability eject + first-load tag seed ------
  const recordable = status.capabilities?.recordable;
  useEffect(() => {
    if (recordable === false) {
      toast.error('Aufnahme ist auf diesem Roboter nicht verfügbar.');
      dispatch(moveToPage(PageType.HOME));
    }
  }, [recordable, dispatch]);

  const tagCount = Array.isArray(formRaw.tags) ? formRaw.tags.length : 0;
  useEffect(() => {
    // Wait for the robot identity before seeding the default tags AND before
    // consuming first-load (the identity arrives a beat after the page mounts).
    if (!status.robotType) return;
    if (isFirstLoad && tagCount === 0) {
      dispatch(addTag(status.robotType));
      dispatch(addTag('edubotics'));
    }
    dispatch(setIsFirstLoadFalse('record'));
  }, [tagCount, status.robotType, dispatch, isFirstLoad]);

  // --- derived page data ------------------------------------------------------------
  const signalRows = useMemo(() => (verdicts
    ? verdicts.map((v) => ({
      kind: v.kind,
      name: v.name,
      labelDe: v.kind === 'camera' ? cameraNameDe(v.name) : armNameDe(v.kind),
      hz: v.hz,
      // the tile badge's text: „29,8 Hz", or „—" before a rate is measurable
      hzText: v.hz === null || v.hz === undefined ? '—' : `${numberDe(v.hz)} Hz`,
      verdict: v.verdict,
    }))
    : null), [verdicts]);

  const finish = useMemo(
    () => finishSteps(session, { nowWallMs: now.wall, heartbeat }),
    [session, now.wall, heartbeat],
  );
  const sessionCard = useMemo(
    () => sessionView(session, { numEpisodes: formRaw.numEpisodes, nowWallMs: now.wall }),
    [session, formRaw.numEpisodes, now.wall],
  );

  const estimate = useMemo(() => {
    const e = estimateRecording(formRaw);
    return { ...e, text: RECORD_COPY.estimate(e.totalS, Number(formRaw.numEpisodes) || 0) };
  }, [formRaw]);

  const repoPreview = useMemo(() => {
    const safe = safeTaskName(formRaw.taskName);
    return safe
      ? datasetRepoId(formRaw.userId, status.robotType, formRaw.taskName)
      : `${safeUserId(formRaw.userId)}/${status.robotType}_…`;
  }, [formRaw.taskName, formRaw.userId, status.robotType]);
  const saveName = useMemo(() => {
    const local = !formRaw.pushToHub;
    const isPublic = !local && formRaw.privateMode === false;
    return {
      text: local ? RECORD_COPY.save.local : isPublic ? RECORD_COPY.save.public : RECORD_COPY.save.private,
      repoId: repoPreview,
      public: isPublic,
      local,
    };
  }, [formRaw.pushToHub, formRaw.privateMode, repoPreview]);

  // --- navigation / finish actions ---------------------------------------------------
  const dismissFinish = useCallback(() => {
    dispatch(recordSessionDismiss());
    dispatch(recordNoticeClear());
  }, [dispatch]);

  const goToTraining = useCallback(() => {
    const repoId = datasetIdOf(store.getState().tasks.recordSession);
    const [user, ...rest] = String(repoId || '').split('/');
    if (user && rest.length) {
      dispatch(setSelectedUser(user));
      dispatch(setSelectedDataset(rest.join('/')));
      dispatch(setDatasetRepoId(repoId));
    }
    dispatch(moveToPage(PageType.TRAINING));
  }, [dispatch, store]);

  const goToHome = useCallback(() => dispatch(moveToPage(PageType.HOME)), [dispatch]);

  const toggleMute = useCallback(() => {
    setMuted((m) => {
      const next = !m;
      writeMuted(next);
      return next;
    });
  }, []);

  const questionModel = question ? {
    kind: question.kind,
    episode: question.episode,
    title: RECORD_COPY.question.endTitle(question.episode),
    sub: RECORD_COPY.question.endSub,
    answers: [
      { id: 'keepAndEnd', label: RECORD_COPY.question.keep, variant: 'ghost', icon: 'check' },
      { id: 'discardAndEnd', label: RECORD_COPY.question.discard, variant: 'end' },
      { id: 'back', label: RECORD_COPY.question.back, variant: 'primary', kbd: RECORD_COPY.kbd.esc },
    ],
  } : null;

  return {
    view,
    model,
    clock,
    problem,
    question: questionModel,
    closeQuestion,
    act,
    busy,
    form,
    setField,
    editable: model.editable,
    lockedReason: model.lockedReason,
    hfUsers,
    signal: signalRows,
    finish,
    session: sessionCard,
    dismissFinish,
    goToTraining,
    goToHome,
    muted,
    toggleMute,
    estimate,
    repoPreview,
    onPhaseTick,
    // conveniences
    copy: RECORD_COPY,
    steppers: RECORD_STEPPERS,
    saveName,
    invalid,
    reducedMotion,
  };
}
