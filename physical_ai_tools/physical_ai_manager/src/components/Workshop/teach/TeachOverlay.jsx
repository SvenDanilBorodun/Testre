/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen overlay (hand mode): renders the useTeachSession state machine as a
// full-screen dialog readable from a metre away, uploads kept takes, stores
// captured Positionen/Ziele in the document, and offers the home glide ONCE
// at „Fertig". Every service call and every key rule lives in the hook.
//
// Two rules here carry weight of their own:
//   * No name is ever asked while the arm is limp: a capture/take gets an
//     automatic name on the key press, and ✎ is enabled only while the arm is
//     locked (fest, abschluss, or pruefen with a confirmed re-lock).
//   * Keyboard focus never sits on a mouse-clicked button: every control's
//     pointerup hands focus back to the container, because an unprevented
//     Space re-activates a focused button (measured) — and the hook ignores
//     keys on interactive targets so a Tab-focused button still works.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import ROSLIB from 'roslib';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import * as workflowApi from '../../../services/workflowApi';
import { compactTrajectoryPoints } from '../../../utils/trajectoryCompact';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import { selectTrajectoryList } from '../../../features/workshop/studioAssetsSlice';
import { DE, formatDe } from '../blocks/messages_de';
import { useHomeGlide } from '../HomeGlidePrompt';
import {
  getDestinationStore,
  nextAutoName,
  sanitizeDestinationNameInput,
  takenDestinationNames,
} from '../sammlung/destinationStore';
import { renamePlace, renameRecording } from '../sammlung/assetCommands';
import { formatMmDe, formatSecondsDe } from '../sammlung/format';
import useTeachSession from './useTeachSession';
import { createTeachSounds } from './teachSounds';

// The cloud keeps at most 16 recording rows per workflow (SQL prune cap).
export const TEACH_TRAJECTORY_SLOTS = 16;
const SLOTS_LOW_FROM = 14;
const FOCUS_HIGHLIGHT_MS = 2000;
const NOT_SIGNED_IN_DE = 'Nicht angemeldet — Speichern nicht möglich.';
const COLLISION_BUTTON_SELECTOR =
  '[role="alertdialog"][aria-label="Kollision erkannt"] button:not([disabled])';
const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

const STATE_LINE = {
  fest: DE.TEACH_STATE_LOCKED,
  frei: DE.TEACH_STATE_FREE,
  aufnahme: DE.TEACH_STATE_REC,
  pruefen: DE.TEACH_STATE_REVIEW,
  vorschau: DE.TEACH_STATE_REVIEW,
  abschluss: DE.TEACH_STATE_DONE,
};

const HINT_LINE = {
  fest: DE.TEACH_HINT_LOCKED,
  frei: DE.TEACH_HINT_FREE,
  aufnahme: DE.TEACH_HINT_REC,
  abschluss: DE.TEACH_HINT_DONE,
};

const FOCUS_KEY = { recording: 'space', pose: 'p', ziel: 'z' };

function mmss(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function messageOf(err) {
  if (!err) return '';
  if (typeof err === 'string') return err;
  return String(err.detail || err.message || '');
}

/** ✎ is allowed only while the arm is locked — never while it is limp or moving. */
export function teachRenameEnabled(state, relock) {
  return state === 'fest' || state === 'abschluss' || (state === 'pruefen' && relock !== 'failed');
}

/** The slot line under the review: null, or the German warning. */
export function teachSlotsLine(used) {
  if (used >= TEACH_TRAJECTORY_SLOTS) return DE.TEACH_SLOTS_FULL;
  if (used >= SLOTS_LOW_FROM) return formatDe(DE.TEACH_SLOTS_LOW, TEACH_TRAJECTORY_SLOTS - used);
  return null;
}

/** The German meta line of one „In dieser Runde" row. */
export function teachListMeta(item) {
  if (item.kind === 'recording') {
    const status = item.status === 'saved' ? DE.TEACH_LIST_SAVED
      : item.status === 'failed' ? DE.TEACH_LIST_FAILED
        : DE.TEACH_LIST_SAVING;
    return formatDe(DE.TEACH_LIST_RECORDING_META, formatSecondsDe(Number(item.durationS) || 0), status);
  }
  if (item.kind === 'pose') return formatDe(DE.TEACH_LIST_POSE_META, formatMmDe(item.z));
  return formatDe(DE.TEACH_LIST_PLACE_META, formatMmDe(item.x), formatMmDe(item.y), DE.CARD_SOURCE_TOUCH);
}

function TeachOverlay({
  mode = 'hand', focus = null, onClose, workspace, accessToken, workflowId, robotType,
  heartbeatOk, rsBridge, saveWorkflowNow, refetchTrajectories,
}) {
  const containerRef = useRef(null);
  const trajectoryList = useSelector(selectTrajectoryList);
  const collisionActive = useSelector((s) => !!(s.tasks && s.tasks.collision && s.tasks.collision.active));
  const { offerHomeGlide, homeGlideActive } = useHomeGlide();
  const { handGuide, recordControl, capturePose, replayMotion } = useRosServiceCaller();
  const services = useMemo(
    () => ({ handGuide, recordControl, capturePose, replayMotion }),
    [handGuide, recordControl, capturePose, replayMotion],
  );
  const sounds = useMemo(() => createTeachSounds(), []);
  useEffect(() => () => { try { sounds.dispose(); } catch (_) { /* ignore */ } }, [sounds]);

  // Latest values for callbacks that outlive a render (uploads, the hook).
  const latest = useRef({});
  latest.current = {
    accessToken, workflowId, robotType, saveWorkflowNow, refetchTrajectories, workspace, onClose,
  };

  // „In dieser Runde". The ref is written synchronously, so two keeps inside
  // one tick still get „Bewegung 1" and „Bewegung 2".
  const [items, setItems] = useState([]);
  const itemsRef = useRef([]);
  const keySeq = useRef(0);
  const updateItems = useCallback((fn) => {
    itemsRef.current = fn(itemsRef.current);
    setItems(itemsRef.current);
  }, []);
  const patchItem = useCallback((key, patch) => {
    updateItems((list) => list.map((it) => (it.key === key ? { ...it, ...patch } : it)));
  }, [updateItems]);

  const cloudItems = useMemo(
    () => ((trajectoryList && Array.isArray(trajectoryList.items)) ? trajectoryList.items : []),
    [trajectoryList],
  );
  const cloudItemsRef = useRef(cloudItems);
  cloudItemsRef.current = cloudItems;

  const recordingNames = useCallback(() => [
    ...cloudItemsRef.current.map((row) => row && row.name),
    ...itemsRef.current.filter((it) => it.kind === 'recording').map((it) => it.name),
  ], []);

  // Names issued for captures still on their way to the server.
  const issuedPlaceNames = useRef(new Set());
  const captureNamer = useCallback((kind) => {
    let taken = [];
    try { taken = takenDestinationNames(latest.current.workspace); } catch (_) { taken = []; }
    const all = [...taken, ...issuedPlaceNames.current];
    const template = kind === 'pose' ? DE.TEACH_AUTO_NAME_POSE : DE.TEACH_AUTO_NAME_ZIEL;
    const name = nextAutoName(template, all);
    issuedPlaceNames.current.add(name);
    return name;
  }, []);

  const uploadRecording = useCallback(async (key, name, take) => {
    patchItem(key, { status: 'saving', error: '' });
    const cur = latest.current;
    const token = cur.accessToken;
    if (!token) {
      patchItem(key, { status: 'failed', error: NOT_SIGNED_IN_DE });
      return;
    }
    let wfId = cur.workflowId;
    if (!wfId) {
      // D5: the first kept take of an unsaved workflow creates it through the
      // page's one save path. A keep arriving during that create gets the
      // coalesced follow-up (an update, created:false), so one toast only.
      let r;
      try {
        r = await cur.saveWorkflowNow({ toastOnSuccess: false });
      } catch (err) {
        r = { ok: false, error: err };
      }
      if (!r || !r.ok || !r.workflowId) {
        patchItem(key, { status: 'failed', error: messageOf(r && r.error) });
        return;
      }
      if (r.created) toast.success(DE.TEACH_AUTOSAVED_WORKFLOW);
      wfId = r.workflowId;
    }
    const payload = {
      name,
      fps: take.fps,
      points: compactTrajectoryPoints(take.points),
      duration_s: take.durationS,
    };
    const profile = String(latest.current.robotType || '').trim();
    if (profile) payload.robot_profile = profile;
    try {
      await workflowApi.createTrajectory(token, wfId, payload);
    } catch (err) {
      patchItem(key, { status: 'failed', error: messageOf(err) });
      return;
    }
    patchItem(key, { status: 'saved', error: '' });
    const refetch = latest.current.refetchTrajectories;
    if (typeof refetch === 'function') refetch();
  }, [patchItem]);

  const onKeep = useCallback((take) => {
    if (!take) return;
    const name = nextAutoName(DE.TEACH_AUTO_NAME_RECORDING, recordingNames());
    keySeq.current += 1;
    const key = `rec-${keySeq.current}`;
    updateItems((list) => [
      ...list, { key, kind: 'recording', name, status: 'saving', error: '', durationS: take.durationS, take },
    ]);
    uploadRecording(key, name, take);
  }, [recordingNames, updateItems, uploadRecording]);

  // Only successful captures arrive here; the hook toasts a refusal itself.
  // The list is the feedback, so a stored capture shows no toast.
  const onCapture = useCallback(({ kind, name, response }) => {
    if (!response || !response.success) return;
    issuedPlaceNames.current.delete(name);
    const input = {
      name,
      kind: kind === 'pose' ? 'pose' : 'pin',
      source: 'capture',
      x: response.world_x,
      y: response.world_y,
      z: response.world_z,
    };
    const profile = String(latest.current.robotType || '').trim();
    if (profile) input.robot_type = profile;
    let result;
    try {
      result = getDestinationStore(latest.current.workspace).add(input);
    } catch (err) {
      result = { ok: false, error: messageOf(err) || DE.ERR_COORDINATES };
    }
    if (!result || !result.ok) {
      toast.error((result && result.error) || DE.ERR_COORDINATES);
      return;
    }
    const { entry } = result;
    keySeq.current += 1;
    updateItems((list) => [...list, {
      key: `${kind}-${keySeq.current}`,
      kind: kind === 'pose' ? 'pose' : 'ziel',
      name: entry.name,
      entryId: entry.id,
      x: entry.x,
      y: entry.y,
      z: entry.z,
    }]);
  }, [updateItems]);

  const onError = useCallback((message) => {
    if (message) toast.error(message);
  }, []);

  // D7: the home glide is offered exactly once, at „Fertig", and only over an
  // arm this session released AND whose last re-lock was confirmed — never
  // offline, never over an arm whose re-lock failed.
  const finishedRef = useRef(false);
  const onFinished = useCallback(({ releasedOnce, relockOk, offline }) => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    if (releasedOnce && relockOk && !offline) offerHomeGlide();
    const close = latest.current.onClose;
    if (typeof close === 'function') close();
  }, [offerHomeGlide]);

  // /joint_states for the real-arm preview; the hook opens it only in `vorschau`.
  const subscribeFollowerJoints = useCallback((cb) => {
    const ros = rosConnectionManager && rosConnectionManager.ros;
    if (!ros) return () => {};
    const topic = new ROSLIB.Topic({
      ros, name: '/joint_states', messageType: 'sensor_msgs/msg/JointState', throttle_rate: 30,
    });
    topic.subscribe((msg) => {
      if (msg && Array.isArray(msg.position)) cb(msg.position);
    });
    return () => {
      try { topic.unsubscribe(); } catch (_) { /* socket already gone */ }
    };
  }, []);

  const session = useTeachSession({
    enabled: true,
    mode,
    heartbeatOk: heartbeatOk !== false,
    collisionActive,
    leaderLive: !!(rsBridge && rsBridge.leaderOn),
    roundItemCount: items.length,
    services,
    sounds,
    onCapture,
    onKeep,
    onError,
    onFinished,
    subscribeFollowerJoints,
  });
  const {
    state, countdownLeft, elapsedS, busy, take, relock, previewNoMotionHint, actions, onKeyDown,
    setCaptureNamer,
  } = session;

  useEffect(() => { setCaptureNamer(captureNamer); }, [setCaptureNamer, captureNamer]);

  // The hook's keys, in the capture phase, while the overlay is open.
  useEffect(() => {
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [onKeyDown]);

  // Focus: the container on open, back to where it was on close.
  useEffect(() => {
    const previous = document.activeElement;
    if (containerRef.current) containerRef.current.focus();
    return () => {
      if (previous && typeof previous.focus === 'function' && document.contains(previous)) {
        try { previous.focus(); } catch (_) { /* element gone */ }
      }
    };
  }, []);

  // While the CollisionModal is up the overlay is inert and the modal owns the
  // keyboard; when it clears, focus comes back to the container.
  const collisionSeenRef = useRef(false);
  useEffect(() => {
    if (collisionActive) {
      collisionSeenRef.current = true;
      const focusModal = () => {
        const btn = document.querySelector(COLLISION_BUTTON_SELECTOR);
        if (!btn) return false;
        btn.focus();
        return true;
      };
      if (focusModal()) return undefined;
      // The modal may commit a moment after this effect.
      const t = setTimeout(focusModal, 0);
      return () => clearTimeout(t);
    }
    if (collisionSeenRef.current) {
      collisionSeenRef.current = false;
      if (containerRef.current) containerRef.current.focus();
    }
    return undefined;
  }, [collisionActive]);

  const refocus = useCallback(() => {
    setTimeout(() => {
      if (containerRef.current) containerRef.current.focus();
    }, 0);
  }, []);

  const onContainerKeyDown = (e) => {
    if (e.key !== 'Tab' || collisionActive) return;
    const root = containerRef.current;
    if (!root) return;
    const nodes = Array.from(root.querySelectorAll(FOCUSABLE_SELECTOR));
    if (nodes.length === 0) {
      e.preventDefault();
      root.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    const outside = active === root || !root.contains(active);
    if (e.shiftKey && (active === first || outside)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || outside)) {
      e.preventDefault();
      first.focus();
    }
  };

  // A flyout's „✋ … vormachen" pre-highlights its action for 2 s.
  const [highlightKey, setHighlightKey] = useState(FOCUS_KEY[focus] || null);
  useEffect(() => {
    if (!highlightKey) return undefined;
    const t = setTimeout(() => setHighlightKey(null), FOCUS_HIGHLIGHT_MS);
    return () => clearTimeout(t);
  }, [highlightKey]);

  const leaderOn = !!(rsBridge && rsBridge.leaderOn);
  const [leaderTurnedOn, setLeaderTurnedOn] = useState(false);
  useEffect(() => { if (leaderOn) setLeaderTurnedOn(true); }, [leaderOn]);

  const [renaming, setRenaming] = useState(null); // { key, draft }
  const renameOk = teachRenameEnabled(state, relock);
  const commitRename = async () => {
    const cur = renaming;
    const item = cur && itemsRef.current.find((it) => it.key === cur.key);
    if (!item || !renameOk) {
      setRenaming(null);
      return;
    }
    const ctx = latest.current;
    if (item.kind === 'recording') {
      const result = await renameRecording({
        workspace: ctx.workspace,
        api: workflowApi,
        accessToken: ctx.accessToken,
        workflowId: ctx.workflowId,
        fromName: item.name,
        toName: cur.draft,
        items: cloudItemsRef.current,
        saveWorkflowNow: ctx.saveWorkflowNow,
        confirmReplace: async (to) => window.confirm(formatDe(DE.CONFIRM_REPLACE_RECORDING, to)),
      });
      if (result.cancelled) return;
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      patchItem(item.key, { name: String(cur.draft).trim() });
      if (typeof ctx.refetchTrajectories === 'function') ctx.refetchTrajectories();
    } else {
      const result = renamePlace({ workspace: ctx.workspace, entryId: item.entryId, toName: cur.draft });
      if (!result.ok) {
        toast.error(result.error);
        return;
      }
      patchItem(item.key, { name: result.entry.name });
    }
    setRenaming(null);
    refocus();
  };

  const online = heartbeatOk !== false && !leaderTurnedOn;
  const inReview = !!take && (state === 'pruefen' || state === 'vorschau');
  const pendingUploads = items.filter((it) => it.kind === 'recording' && it.status === 'saving').length;
  const slotsLine = teachSlotsLine(cloudItems.length + pendingUploads);
  const reviewName = inReview
    ? nextAutoName(DE.TEACH_AUTO_NAME_RECORDING, [
      ...cloudItems.map((row) => row && row.name),
      ...items.filter((it) => it.kind === 'recording').map((it) => it.name),
    ])
    : '';
  const stateLine = state === 'countdown'
    ? formatDe(DE.TEACH_COUNTDOWN, countdownLeft)
    : (STATE_LINE[state] || '');
  const cell = {
    space: online && ['fest', 'countdown', 'frei', 'aufnahme'].includes(state),
    f: online && (['fest', 'countdown', 'frei', 'aufnahme'].includes(state)
      || (state === 'pruefen' && relock === 'failed')),
    p: online && ['fest', 'frei', 'aufnahme'].includes(state),
    z: online && ['fest', 'frei'].includes(state),
  };
  const fLocks = state === 'frei' || state === 'aufnahme' || state === 'pruefen';
  const showRelockBanner = relock === 'failed' && (state === 'frei' || state === 'pruefen');
  const previewDisabled = state !== 'pruefen' || busy || homeGlideActive || !online || relock === 'failed';

  const handlePreviewOnRobot = () => {
    if (!window.confirm(DE.TEACH_REVIEW_ON_ROBOT_CONFIRM)) return;
    actions.previewOnRobot();
  };

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal={collisionActive ? undefined : 'true'}
      aria-labelledby="teach-title"
      tabIndex={-1}
      inert={collisionActive || undefined}
      onKeyDown={onContainerKeyDown}
      data-testid="teach-overlay"
      className="fixed inset-0 z-[55] flex bg-slate-900/70 p-2 outline-none sm:p-6"
    >
      <div className="mx-auto flex h-full w-full max-w-6xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
        <header className="flex flex-wrap items-center gap-3 border-b border-[var(--line)] px-4 py-3">
          <h2 id="teach-title" className="text-xl font-semibold text-[var(--ink)]">
            {`✋ ${DE.TEACH_TITLE}`}
          </h2>
          <span className="rounded-full bg-[var(--bg-sunk)] px-2.5 py-0.5 text-sm text-[var(--ink-3)]">
            {DE.TEACH_MODE_HAND}
          </span>
          <button
            type="button"
            onClick={() => actions.finish()}
            onPointerUp={refocus}
            className="ml-auto rounded-lg border border-[var(--line)] px-4 py-2 text-base hover:bg-[var(--bg-sunk)]"
          >
            {`${DE.TEACH_DONE} (Esc)`}
          </button>
        </header>
        <div className="flex min-h-0 flex-1 flex-col overflow-auto md:flex-row">
          <section className="flex min-w-0 flex-1 flex-col gap-4 p-4 sm:p-6">
            {heartbeatOk === false && (
              <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-base text-red-800">{DE.TEACH_OFFLINE}</p>
            )}
            {leaderTurnedOn && (
              <p role="alert" className="rounded-lg bg-amber-50 px-3 py-2 text-base text-amber-900">
                {DE.TEACH_LEADER_TURNED_ON}
              </p>
            )}
            {showRelockBanner && (
              <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg bg-red-50 px-3 py-2 text-base text-red-800">
                <span className="flex-1">{DE.TEACH_RELOCK_FAILED}</span>
                <button
                  type="button"
                  onClick={() => actions.lock()}
                  onPointerUp={refocus}
                  className="rounded-lg bg-red-600 px-4 py-2 font-semibold text-white hover:bg-red-700"
                >
                  {DE.TEACH_KEY_LOCK}
                </button>
              </div>
            )}
            <div>
              <p aria-live="assertive" className="text-3xl font-semibold text-[var(--ink)]">
                {stateLine}
                {state === 'aufnahme' && <span className="ml-3 tabular-nums">{mmss(elapsedS)}</span>}
              </p>
              {HINT_LINE[state] && <p className="mt-1 text-lg text-[var(--ink-3)]">{HINT_LINE[state]}</p>}
            </div>
            {state !== 'abschluss' && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <ActionButton
                  icon={state === 'aufnahme' ? '■' : '⏺'}
                  label={state === 'aufnahme' ? DE.TEACH_KEY_STOP : DE.TEACH_KEY_REC}
                  keyHint="Leertaste"
                  disabled={!cell.space}
                  highlighted={highlightKey === 'space'}
                  onClick={actions.space}
                  onPointerUp={refocus}
                />
                <ActionButton
                  icon="📍"
                  label={DE.TEACH_KEY_POSE}
                  keyHint="P"
                  disabled={!cell.p}
                  highlighted={highlightKey === 'p'}
                  onClick={actions.capturePose}
                  onPointerUp={refocus}
                />
                <ActionButton
                  icon="🎯"
                  label={DE.TEACH_KEY_ZIEL}
                  keyHint="Z"
                  disabled={!cell.z}
                  title={state === 'aufnahme' ? DE.TEACH_ZIEL_BLOCKED_REC : undefined}
                  highlighted={highlightKey === 'z'}
                  onClick={actions.captureZiel}
                  onPointerUp={refocus}
                />
                <ActionButton
                  icon={fLocks ? '🔒' : '✋'}
                  label={fLocks ? DE.TEACH_KEY_LOCK : DE.TEACH_KEY_FREE}
                  keyHint="F"
                  disabled={!cell.f}
                  onClick={actions.toggleFree}
                  onPointerUp={refocus}
                />
              </div>
            )}
            {inReview && (
              <div className="flex flex-col gap-3 rounded-xl border border-[var(--line)] p-4" data-testid="teach-review">
                <p className="text-lg text-[var(--ink)]">
                  {formatDe(DE.TEACH_REVIEW_META, reviewName, formatSecondsDe(Number(take.durationS) || 0),
                    Array.isArray(take.points) ? take.points.length : 0)}
                </p>
                {state === 'vorschau' && (
                  <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg bg-amber-50 px-3 py-2 text-base text-amber-900">
                    <div className="flex-1">
                      <p>{DE.TEACH_ROBOT_PREVIEW_RUNNING}</p>
                      {previewNoMotionHint && <p>{DE.TEACH_ROBOT_PREVIEW_NO_MOTION}</p>}
                    </div>
                    <button
                      type="button"
                      onClick={() => actions.stopPreview()}
                      onPointerUp={refocus}
                      className="min-h-[64px] rounded-lg bg-red-600 px-6 text-lg font-semibold text-white hover:bg-red-700"
                    >
                      {DE.TEACH_ROBOT_PREVIEW_STOP}
                    </button>
                  </div>
                )}
                <div className="flex flex-wrap gap-2">
                  <SmallKeyButton label={`✓ ${DE.TEACH_REVIEW_KEEP}`} keyHint="Enter" disabled={state !== 'pruefen' || !online} onClick={actions.keep} onPointerUp={refocus} />
                  <SmallKeyButton label={`↺ ${DE.TEACH_REVIEW_AGAIN}`} keyHint="R" disabled={state !== 'pruefen' || !online} onClick={actions.again} onPointerUp={refocus} />
                  <SmallKeyButton label={DE.TEACH_REVIEW_DISCARD} keyHint="Entf" disabled={state !== 'pruefen' || !online} onClick={actions.discard} onPointerUp={refocus} />
                  <SmallKeyButton label={DE.TEACH_REVIEW_ON_ROBOT} disabled={previewDisabled} onClick={handlePreviewOnRobot} onPointerUp={refocus} />
                </div>
                {slotsLine && <p className="text-sm text-amber-800">{slotsLine}</p>}
              </div>
            )}
            {state === 'abschluss' && (
              <div className="flex flex-wrap gap-3">
                <SmallKeyButton label={DE.TEACH_CONTINUE} onClick={actions.continueTeaching} onPointerUp={refocus} />
                <SmallKeyButton label={DE.TEACH_CLOSE} keyHint="Esc" primary onClick={actions.finish} onPointerUp={refocus} />
              </div>
            )}
          </section>
          <aside className="flex w-full shrink-0 flex-col gap-2 border-t border-[var(--line)] p-4 md:w-80 md:border-l md:border-t-0">
            <h3 className="text-base font-semibold text-[var(--ink)]">{DE.TEACH_LIST_TITLE}</h3>
            {items.length === 0 && !inReview && (
              <p className="text-sm text-[var(--ink-3)]">{DE.TEACH_LIST_EMPTY}</p>
            )}
            <ul className="flex flex-col gap-1.5">
              {items.map((item) => (
                <ListRow
                  key={item.key}
                  item={item}
                  renameOk={renameOk}
                  editing={renameOk && renaming && renaming.key === item.key ? renaming : null}
                  onStartRename={() => setRenaming({ key: item.key, draft: item.name })}
                  onDraft={(draft) => setRenaming((cur) => (cur ? { ...cur, draft } : cur))}
                  onCommit={commitRename}
                  onCancel={() => { setRenaming(null); refocus(); }}
                  onRetry={() => uploadRecording(item.key, item.name, item.take)}
                  refocus={refocus}
                />
              ))}
              {inReview && (
                <li className="rounded-lg bg-[var(--bg-sunk)] px-2 py-1.5" data-testid="teach-list-reviewing">
                  <p className="truncate text-sm font-medium text-[var(--ink)]">{reviewName}</p>
                  <p className="text-xs text-[var(--ink-3)]">{DE.TEACH_LIST_REVIEWING}</p>
                </li>
              )}
            </ul>
          </aside>
        </div>
      </div>
    </div>
  );
}

function ActionButton({ icon, label, keyHint, disabled, highlighted, title, onClick, onPointerUp }) {
  return (
    <button
      type="button"
      onClick={onClick}
      onPointerUp={onPointerUp}
      disabled={disabled}
      title={title}
      className={
        'flex min-h-[64px] items-center gap-3 rounded-xl border px-4 text-left text-lg font-semibold '
        + 'disabled:cursor-not-allowed disabled:opacity-40 hover:bg-[var(--bg-sunk)] '
        + (highlighted ? 'border-[var(--accent)] ring-4 ring-[var(--accent)]/40' : 'border-[var(--line)]')
      }
    >
      <span aria-hidden="true">{icon}</span>
      <span className="flex-1">{label}</span>
      <kbd className="rounded border border-[var(--line)] bg-[var(--bg-sunk)] px-2 py-0.5 text-sm font-normal">{keyHint}</kbd>
    </button>
  );
}

function SmallKeyButton({ label, keyHint, disabled, primary, onClick, onPointerUp }) {
  return (
    <button
      type="button"
      onClick={onClick}
      onPointerUp={onPointerUp}
      disabled={disabled}
      className={
        'flex items-center gap-2 rounded-lg border px-4 py-2 text-base disabled:cursor-not-allowed disabled:opacity-40 '
        + (primary
          ? 'border-[var(--accent)] bg-[var(--accent)] text-white hover:opacity-90'
          : 'border-[var(--line)] hover:bg-[var(--bg-sunk)]')
      }
    >
      <span>{label}</span>
      {keyHint && <kbd className="rounded border border-current/30 px-1.5 text-xs">{keyHint}</kbd>}
    </button>
  );
}

function ListRow({ item, renameOk, editing, onStartRename, onDraft, onCommit, onCancel, onRetry, refocus }) {
  const isRecording = item.kind === 'recording';
  // A recording is renamed in the cloud, so only once it is saved there.
  const canRename = renameOk && (!isRecording || item.status === 'saved');
  if (editing) {
    return (
      <li className="rounded-lg border border-[var(--accent)] px-2 py-1.5">
        <form className="flex flex-wrap items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); onCommit(); }}>
          <input
            type="text"
            aria-label={DE.DRAWER_NAME}
            value={editing.draft}
            maxLength={isRecording ? 40 : 24}
            onChange={(e) => onDraft(isRecording ? e.target.value : sanitizeDestinationNameInput(e.target.value))}
            // The ✎ click hands focus here on purpose (never back to the container).
            // eslint-disable-next-line jsx-a11y/no-autofocus
            autoFocus
            className="min-w-0 flex-1 rounded border border-[var(--line)] px-2 py-1 text-sm"
          />
          <button type="submit" onPointerUp={refocus} className="rounded border border-[var(--line)] px-2 py-1 text-sm">
            {DE.DRAWER_SAVE_NAME}
          </button>
          <button type="button" onClick={onCancel} className="rounded border border-[var(--line)] px-2 py-1 text-sm">
            {DE.DRAWER_CANCEL}
          </button>
        </form>
      </li>
    );
  }
  return (
    <li className="rounded-lg px-2 py-1.5 hover:bg-[var(--bg-sunk)]" data-testid={`teach-item-${item.kind}`}>
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 truncate text-sm font-medium text-[var(--ink)]" title={item.name}>{item.name}</p>
        <button
          type="button"
          onClick={onStartRename}
          disabled={!canRename}
          title={renameOk ? DE.DRAWER_RENAME : DE.TEACH_RENAME_LOCKED}
          aria-label={`${DE.DRAWER_RENAME}: ${item.name}`}
          className="rounded px-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-40 hover:bg-white"
        >
          ✎
        </button>
      </div>
      <p className="text-xs text-[var(--ink-3)]">{teachListMeta(item)}</p>
      {isRecording && item.status === 'failed' && (
        <div className="mt-1 flex flex-wrap items-center gap-2">
          {item.error && <span className="text-xs text-red-700">{item.error}</span>}
          <button
            type="button"
            onClick={onRetry}
            onPointerUp={refocus}
            className="rounded border border-[var(--line)] px-2 py-0.5 text-xs hover:bg-white"
          >
            {DE.TEACH_LIST_RETRY}
          </button>
        </div>
      )}
    </li>
  );
}

export default TeachOverlay;
