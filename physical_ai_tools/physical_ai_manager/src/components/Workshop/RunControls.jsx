/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useState, useEffect, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  setRunState,
  setPaused,
  clearWorkflowLog,
  toggleDebugger,
  clearVariables,
  clearWorkflowError,
  setDebuggerWarnings,
} from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
// Namespace import (not a named import) so this file builds independently of the
// cloud agent that owns `getTrajectoryByName` in workflowApi.js — a missing
// member is `undefined` and handled by the `typeof` guard below, rather than a
// build-time "not exported" error. Called by name per CONTRACT C.
import * as workflowApi from '../../services/workflowApi';
import { collectReplayNames } from './blocks/trajectories';
import { DE } from './blocks/messages_de';
import { rsControlBase, usePiMode } from '../../utils/piMode';

const BUTTON_BASE =
  'inline-flex items-center justify-center min-h-[36px] '
  + 'px-4 py-2 rounded-md text-sm font-medium '
  + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 '
  + 'disabled:opacity-50 disabled:cursor-not-allowed';

// Leader-contention guard. The follower's arm_controller subscribes to
// /leader/joint_trajectory; running a workflow publishes there too. While the
// leader arm is ON (both-arms mode), its broadcaster also floods that topic at
// ~100 Hz with the limp leader's pose, so the two writers fight and the follower
// jerks between poses ("crazy motion") — the exact failure follower-only mode
// exists to remove. We probe the GUI's localhost control bridge
// (roboter_studio_control.py, the same one LeaderToggle uses) and hard-disable
// the run button until the student has flipped to follower-only via the toggle.
// When the bridge is ABSENT (Jetson/cloud/old GUI), there is no leader to fight
// — Roboter Studio there is follower-only by construction — so we never block.
// The base is Windows-loopback (:8769) OR the Pi's same-origin /api/system proxy
// (rsControlBase, utils/piMode) — a remote browser can't reach the student PC's
// localhost. The base is derived from the LIVE `piMode` context value and the
// first probe waits for `piModeResolved`, so a boot-window probe on a Pi never
// mis-routes to the loopback base before the marker resolves.
const RS_STATUS_TIMEOUT_MS = 4000;
const RS_STATUS_POLL_MS = 8000;

// Phase-2 Tempo — the global run-bar speed multiplier injected as a top-level
// `tempo` sibling into the /workflow/start payload (BOTH sim + real). The window
// mirrors handlers/motion.py::_TEMPO_MIN/_TEMPO_MAX and the cloud validator.
// Persisted per-browser in localStorage (NOT in the DB sim_scene — RunControls
// has no setter for it; see the change report). The server clamps again
// defensively, so a tampered value can never escape [TEMPO_MIN, TEMPO_MAX].
const TEMPO_MIN = 0.5;
const TEMPO_MAX = 2.0;
const TEMPO_STORAGE_KEY = 'edubotics_workshop_tempo';
const TEMPO_PRESETS = [
  { value: TEMPO_MIN, label: DE.RUN_TEMPO_SLOW }, // langsam ×0.5
  { value: 1.0, label: DE.RUN_TEMPO_NORMAL }, //     normal  ×1
  { value: TEMPO_MAX, label: DE.RUN_TEMPO_FAST }, // schnell ×2
];

function clampTempo(value) {
  return Math.min(TEMPO_MAX, Math.max(TEMPO_MIN, value));
}

function readStoredTempo(fallback) {
  try {
    const raw = window.localStorage.getItem(TEMPO_STORAGE_KEY);
    if (raw == null) return fallback;
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) return fallback;
    return clampTempo(n);
  } catch (e) {
    return fallback;
  }
}

async function probeRsStatus(base) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), RS_STATUS_TIMEOUT_MS);
  try {
    const res = await fetch(`${base}/roboter-studio/status`, { signal: ctrl.signal });
    if (!res.ok) return { available: false, followerOnly: false };
    const body = await res.json().catch(() => ({}));
    return { available: true, followerOnly: !!body.follower_only };
  } catch (e) {
    // No bridge reachable (Jetson/cloud/old GUI) → don't block.
    return { available: false, followerOnly: false };
  } finally {
    clearTimeout(timer);
  }
}

// CONTRACT C — a fetched trajectory must reduce to { fps, points } for the run
// payload. The cloud row is expected to expose `points` (array) + `fps`
// directly (CONTRACT B), but tolerate a stringified `points_json` fallback so a
// small cloud-shape difference doesn't wedge a replay run. `robot_profile` (the
// migration-035 arm-family tag, widened by 039) is carried through so the caller
// can refuse a cross-profile replay BEFORE it reaches the runtime. Two different
// failures, and only the first is about width: an OMX recording on an Edu:1
// (5 arm joints -> the SAME 7-wide points) passes every width check there is and
// simply drives the wrong arm, while an OMX 7-wide recording on an
// edu6 rig otherwise dies later with the misleading „Aufnahme ist beschädigt.").
function normalizeTrajectory(t) {
  if (!t || typeof t !== 'object') return null;
  let points = Array.isArray(t.points) ? t.points : null;
  let fps = Number(t.fps) || 0;
  if (!points && typeof t.points_json === 'string') {
    try {
      const parsed = JSON.parse(t.points_json);
      if (parsed && Array.isArray(parsed.points)) {
        points = parsed.points;
        if (!fps) fps = Number(parsed.fps) || 0;
      }
    } catch (_) { /* leave points null → caller errors */ }
  }
  if (!points || points.length === 0) return null;
  const robotProfile = typeof t.robot_profile === 'string' ? t.robot_profile : null;
  return { fps, points, robotProfile };
}

// Canonical arm-family id for a recording tag / rig identity. An absent/NULL tag
// is a LEGACY (pre-035) recording — always OMX by construction — so it maps to
// 'omx_f', and an unknown rig identity likewise falls back to 'omx_f' (the
// pre-edu6 default). A recording is replayable here only when its family matches
// the current rig's.
//
// Compare IDS, never widths. Since the Edu:1 (migration 039) two genuinely
// different arms share a Contract-B point width of 7, so a length-based shortcut
// here would silently re-open cross-arm replay for exactly that pair.
function trajectoryMatchesRig(trajProfile, rigRobotType) {
  const traj = (typeof trajProfile === 'string' && trajProfile.trim()) || 'omx_f';
  const rig = (typeof rigRobotType === 'string' && rigRobotType.trim()) || 'omx_f';
  return traj === rig;
}

function RunControls({
  workflowId,
  blocklyJson,
  workspace = null,
  simMode = false,
  simScene = null,
  // Redesign: the Debug panel now lives in the RightDock. When these props are
  // supplied (WorkshopPage), the „Debug" button toggles the dock tab and reflects
  // its open state. They fall back to the Redux `debuggerVisible` flag when
  // absent so the component still works standalone (and existing tests pass).
  debugOpen = null,
  onToggleDebug = null,
}) {
  const dispatch = useDispatch();
  const {
    callService,
    pauseWorkflow,
    stepWorkflow,
    continueWorkflow,
    setWorkflowBreakpoints,
  } = useRosServiceCaller();
  const runState = useSelector((s) => s.workshop.runState);
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  // The rig's data-robot-type identity (taskStatus.robotType: 'omx_f' /
  // 'edu6_studio' / 'edu1_studio'), used to refuse replaying a recording made on
  // a different arm family. NOT a width proxy: 'edu1_studio' and 'omx_f' are
  // both 7-wide, so for that pair this tag is the ONLY separator there is.
  const robotType = useSelector((s) => (s.tasks && s.tasks.taskStatus
    ? s.tasks.taskStatus.robotType : ''));
  const phase = useSelector((s) => s.workshop.phase);
  const currentBlockId = useSelector((s) => s.workshop.currentBlockId);
  const paused = useSelector((s) => s.workshop.paused);
  const log = useSelector((s) => s.workshop.log);
  const error = useSelector((s) => s.workshop.workflowError);
  const debuggerVisible = useSelector((s) => s.workshop.debuggerVisible);
  const debuggerWarnings = useSelector((s) => s.workshop.debuggerWarnings);
  const breakpoints = useSelector((s) => s.workshop.breakpoints);
  const [busy, setBusy] = useState(false);
  // Redesign (compact density): the Protokoll log used to be an always-on 192px
  // block. It is now a collapsed-by-default drawer that auto-opens on a run start
  // or an error, so the editor keeps that vertical space when nothing is running.
  const [logOpen, setLogOpen] = useState(false);
  // Effective Debug open/toggle: prefer the dock-backed props (WorkshopPage),
  // else the standalone Redux flag + toggleDebugger dispatch.
  const debugIsOpen = debugOpen != null ? debugOpen : debuggerVisible;
  const handleDebugClick = useCallback(() => {
    if (typeof onToggleDebug === 'function') onToggleDebug();
    else dispatch(toggleDebugger());
  }, [onToggleDebug, dispatch]);

  // Phase-2 Tempo: per-browser preference. Seeded from a forward-compat
  // simScene.tempo (a future WorkshopPage could persist it in the DB sim_scene)
  // when present, else the stored localStorage value, else normal (1.0).
  // The seed is INTENTIONALLY one-shot (no reconciling effect on later
  // simScene.tempo changes): localStorage has priority and is the sole owner —
  // there is no DB/sim_scene writer for tempo (see the leader-contention block
  // comment), so tracking simScene.tempo would only fight that priority and is
  // inert in practice. Add a reconciling effect only once a real DB writer ships.
  const [tempo, setTempo] = useState(() => {
    const fromScene = simScene && Number.isFinite(simScene.tempo)
      ? clampTempo(simScene.tempo)
      : null;
    return readStoredTempo(fromScene != null ? fromScene : 1.0);
  });

  const handleTempo = useCallback((value) => {
    const clamped = clampTempo(value);
    setTempo(clamped);
    try {
      window.localStorage.setItem(TEMPO_STORAGE_KEY, String(clamped));
    } catch (e) {
      // Storage quota / disabled — keep the in-memory choice; it still ships in
      // the run payload below, just doesn't survive a reload.
    }
  }, []);

  // Leader-contention gate (see the leader-contention comment above). true ONLY when
  // the bridge is reachable AND the leader is on; false while probing, on any
  // probe error, and on Jetson/cloud (no bridge) — i.e. it fails open.
  const [rsLeaderOn, setRsLeaderOn] = useState(false);
  // Pi mode from context (never the synchronous module cache): `piMode` selects
  // the control base and `piModeResolved` gates the FIRST probe so it can't fire
  // with the loopback default during the boot window on a Pi.
  const { piMode, piModeResolved } = usePiMode();

  const isRunning = runState === 'running' || phase === 'running' || paused;

  // Poll the GUI control bridge so the gate tracks the leader toggle live (the
  // student flips it from the same header). Only block on a POSITIVE answer
  // (bridge present AND leader on); any unreachable/error result fails open so a
  // transient probe hiccup never wedges the run button. Held until Pi mode has
  // resolved (piModeResolved) so rsControlBase(piMode) never routes to the
  // loopback base during the Pi boot window; a missing provider resolves
  // immediately (default context), so Windows behaviour is unchanged.
  useEffect(() => {
    if (!piModeResolved) return undefined;
    let cancelled = false;
    let intervalId = null;
    const tick = async () => {
      const { available, followerOnly } = await probeRsStatus(rsControlBase(piMode));
      if (cancelled) return;
      setRsLeaderOn(available && !followerOnly);
    };
    tick();
    intervalId = setInterval(tick, RS_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, [piMode, piModeResolved]);

  // Track the block ids we last warned on so a rerun without warnings
  // clears the previous bubbles. Audit round-3 §K — the prior version
  // skipped the effect when `debuggerWarnings` became empty, leaving
  // stale yellow markers on the workspace.
  const previouslyWarnedIdsRef = useRef([]);

  // Re-attach (or clear) IK pre-check warnings on blocks whenever the
  // warnings list changes. Each warning is {block_id, message}. The
  // workspace ref comes in as a prop from WorkshopPage so we don't
  // depend on a global Blockly singleton (audit §12 found that path
  // silently dropped every warning).
  useEffect(() => {
    if (!workspace || typeof workspace.getBlockById !== 'function') return;
    const list = Array.isArray(debuggerWarnings) ? debuggerWarnings : [];
    const nextIds = list
      .filter((w) => w && w.block_id)
      .map((w) => w.block_id);
    // Clear any previously warned block that isn't in the new list.
    const nextSet = new Set(nextIds);
    previouslyWarnedIdsRef.current.forEach((bid) => {
      if (!nextSet.has(bid)) {
        const block = workspace.getBlockById(bid);
        if (block && typeof block.setWarningText === 'function') {
          block.setWarningText(null);
        }
      }
    });
    list.forEach((warn) => {
      if (!warn || !warn.block_id) return;
      const block = workspace.getBlockById(warn.block_id);
      if (block && typeof block.setWarningText === 'function') {
        block.setWarningText(warn.message || null);
      }
    });
    previouslyWarnedIdsRef.current = nextIds;
  }, [debuggerWarnings, workspace]);

  // Live run-highlighting. The interpreter emits WorkflowStatus.current_block_id
  // per executed block (→ workshopSlice.currentBlockId, flicker-guarded). Paint
  // the running block with Blockly's single-block highlight, which auto-clears
  // the previous one. `highlightBlock(null)` clears everything when the run is
  // not running (idle/stopped/finished/error — handleStop dispatches
  // setRunState('stopped') → runState 'idle', re-running this effect) or the id
  // is empty. NOTE: glowStack/glowBlock do not exist in blockly@12.5.1; the
  // installed API is WorkspaceSvg.highlightBlock(id|null).
  useEffect(() => {
    if (!workspace || typeof workspace.highlightBlock !== 'function') return;
    if (runState === 'running' && currentBlockId) {
      workspace.highlightBlock(currentBlockId);
    } else {
      workspace.highlightBlock(null);
    }
  }, [currentBlockId, runState, workspace]);

  // Auto-open the Protokoll drawer when a run starts or an error appears, so the
  // student never misses live output just because the log was collapsed.
  const prevRunStateRef = useRef(runState);
  useEffect(() => {
    if (runState === 'running' && prevRunStateRef.current !== 'running') {
      setLogOpen(true);
    }
    prevRunStateRef.current = runState;
  }, [runState]);
  useEffect(() => {
    if (error) setLogOpen(true);
  }, [error]);

  const handleStart = useCallback(async () => {
    if (!blocklyJson) {
      toast.error('Workflow ist leer.');
      return;
    }
    // Belt-and-suspenders: refuse to start while the leader arm is on. The
    // button is already disabled in this state, but a stale render or a
    // keyboard activation must not start a contended run. In simMode the run
    // drives a VIRTUAL arm (no leader contention) — the gate is bypassed.
    if (rsLeaderOn && !simMode) {
      toast.error(
        'Bitte zuerst „Leader abschalten" (oben), bevor du das Programm ausführst.');
      return;
    }
    setBusy(true);
    try {
      dispatch(clearWorkflowLog());
      dispatch(clearVariables());
      // The previous run's red „Fehler" alert. It belongs with the three clears
      // around it and was the only one missing: the server sends no status after
      // a terminal `error` phase, so nothing else clears it until the NEW run's
      // first tick — and if this start aborts below (empty program, missing
      // trajectory, a refused service call) that tick never comes and the dead
      // run's alert stays over the live editor. Cleared HERE rather than on
      // setRunState('running') at the bottom, because every one of those abort
      // paths returns before it.
      dispatch(clearWorkflowError());
      // Clear stale unreachable warnings from a previous run before
      // dispatching the new ones; the effect above handles the actual
      // block-level setWarningText(null) calls.
      dispatch(setDebuggerWarnings([]));
      // Audit BP-r1: push breakpoints to the runtime BEFORE /workflow/start
      // returns. Otherwise BreakpointList's debounce + post-start sync
      // races with the runtime's first tick and breakpoints on the very
      // first blocks get skipped. The .srv has no breakpoints field
      // (Agent D owns contracts) so this is an explicit pre-call.
      if (Array.isArray(breakpoints) && breakpoints.length > 0) {
        try {
          await setWorkflowBreakpoints(breakpoints);
        } catch (e) {
          // Non-fatal — log and continue; the late BreakpointList sync
          // path will retry once `runState === 'running'`. The student
          // sees a missing-breakpoint UX but not a failed start.
          console.warn('Pre-start setWorkflowBreakpoints failed:', e);
        }
      }
      // CONTRACT C: collect every trajectory name referenced by a „spiele
      // Bewegung ab" replay block, fetch each recorded trajectory from the cloud
      // and build a top-level `trajectories` sibling { name: { fps, points } }.
      // The interpreter ignores the sibling; the server's WorkflowContext reads
      // it to resolve each replay block. Fail LOUD (abort the start) if a
      // referenced trajectory can't be fetched — running a replay program
      // without its data would silently no-op the motion.
      const replayNames = collectReplayNames(blocklyJson);
      const trajectories = {};
      if (replayNames.length > 0) {
        if (!workflowId) {
          toast.error(
            'Bitte zuerst den Workflow speichern — aufgenommene Bewegungen '
            + 'gehören zu einem gespeicherten Workflow.');
          return;
        }
        if (typeof workflowApi.getTrajectoryByName !== 'function') {
          toast.error('Aufgenommene Bewegungen können zurzeit nicht geladen werden.');
          return;
        }
        try {
          const fetched = await Promise.all(
            replayNames.map((name) =>
              workflowApi.getTrajectoryByName(accessToken, workflowId, name)
                .then((t) => [name, t])),
          );
          for (const [name, t] of fetched) {
            const norm = normalizeTrajectory(t);
            if (!norm) {
              throw new Error(`Bewegung „${name}" wurde nicht gefunden.`);
            }
            // Cross-profile replay refusal: a recording's arm family must match
            // the current rig. On a width-differing pair the alternative is
            // the misleading „Aufnahme ist beschädigt." later; on the SAME-width
            // pair (omx_f / edu1_studio) there is no later failure at all — the
            // wrong arm just moves. A clean German refusal here (outside the
            // load-failure catch so it isn't prefixed) aborts the run.
            if (!trajectoryMatchesRig(norm.robotProfile, robotType)) {
              toast.error(
                'Diese Bewegung wurde mit einem anderen Robotertyp aufgenommen '
                + 'und kann hier nicht abgespielt werden.');
              return;
            }
            // Inject only the run-payload sibling (Contract C: { fps, points }) —
            // the arm-family tag was consumed by the refusal check above.
            trajectories[name] = { fps: norm.fps, points: norm.points };
          }
        } catch (e) {
          toast.error(`Bewegung konnte nicht geladen werden: ${e.message || e}`);
          return;
        }
      }
      // Phase-3: in simMode inject the `sim` sibling into the workflow_json
      // string (Contract B) — `Interpreter.from_json` reads only `data['blocks']`
      // and silently ignores siblings, so the server runs the SAME program on a
      // virtual arm against the placed objects. The persisted blockly_json /
      // autosave blob never carries `sim` (it lives only in this run payload).
      // Phase-4: ALSO inject a TOP-LEVEL `zones` sibling into BOTH the sim and the
      // real-run payload — the server's WorkflowContext reads it for the no-go
      // path-guard (the interpreter still ignores the sibling). Zones persist via
      // workflows.sim_scene.zones; this is the run payload only.
      const simObjects = (simScene && Array.isArray(simScene.objects))
        ? simScene.objects
        : [];
      const zones = (simScene && Array.isArray(simScene.zones))
        ? simScene.zones
        : [];
      // Phase-2: inject the global run-bar `tempo` as a top-level sibling into
      // BOTH branches. `Interpreter.from_json` reads only `data['blocks']`, so
      // the server's WorkflowManager._parse_tempo picks it up while the
      // interpreter ignores it — exactly like `zones`/`sim`.
      const workflowJsonStr = simMode
        ? JSON.stringify({
            ...(blocklyJson || {}),
            sim: { enabled: true, objects: simObjects },
            zones,
            tempo,
            trajectories,
          })
        : JSON.stringify({ ...(blocklyJson || {}), zones, tempo, trajectories });
      const r = await callService(
        '/workflow/start',
        'physical_ai_interfaces/srv/StartWorkflow',
        {
          workflow_json: workflowJsonStr,
          workflow_id: workflowId || `local-${Date.now()}`,
        }
      );
      if (!r.success) {
        toast.error(r.message || 'Workflow konnte nicht gestartet werden.');
        return;
      }
      // The runtime returns unreachable_block_ids[] + unreachable_messages[]
      // (parallel arrays in the StartWorkflow.srv response) when the IK
      // pre-check finds destinations the arm can't reach. Pair them up
      // and surface as setWarningText on the affected blocks (the
      // useEffect above).
      const ids = Array.isArray(r.unreachable_block_ids) ? r.unreachable_block_ids : [];
      const msgs = Array.isArray(r.unreachable_messages) ? r.unreachable_messages : [];
      const warnings = ids.map((bid, i) => ({
        block_id: bid,
        message: msgs[i] || 'Diese Position ist außerhalb des Arbeitsbereichs.',
      }));
      dispatch(setDebuggerWarnings(warnings));
      dispatch(setRunState('running'));
      dispatch(setPaused(false));
      if (warnings.length > 0) {
        // German plural — `Block` (singular) vs `Blöcke` (plural).
        const noun = warnings.length === 1 ? 'Block' : 'Blöcke';
        toast(`${warnings.length} ${noun} markiert: außerhalb des Arbeitsbereichs.`, { icon: '⚠️' });
      } else {
        toast.success(r.message);
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [
    blocklyJson,
    rsLeaderOn,
    simMode,
    simScene,
    tempo,
    callService,
    dispatch,
    workflowId,
    accessToken,
    robotType,
    breakpoints,
    setWorkflowBreakpoints,
  ]);

  const handleStop = useCallback(async () => {
    setBusy(true);
    try {
      const r = await callService(
        '/workflow/stop',
        'physical_ai_interfaces/srv/StopWorkflow',
        {}
      );
      dispatch(setRunState('stopped'));
      dispatch(setPaused(false));
      if (!r.success) {
        toast.error(r.message || 'Stopp fehlgeschlagen.');
      } else {
        toast.success(r.message);
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [callService, dispatch]);

  const handlePause = useCallback(async () => {
    setBusy(true);
    try {
      const r = await pauseWorkflow();
      if (r && r.success) {
        dispatch(setPaused(true));
      } else {
        toast.error((r && r.message) || 'Pause fehlgeschlagen.');
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [pauseWorkflow, dispatch]);

  const handleStep = useCallback(async () => {
    setBusy(true);
    try {
      const r = await stepWorkflow();
      if (!r || !r.success) {
        toast.error((r && r.message) || 'Schritt fehlgeschlagen.');
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [stepWorkflow]);

  const handleContinue = useCallback(async () => {
    setBusy(true);
    try {
      const r = await continueWorkflow();
      if (r && r.success) {
        dispatch(setPaused(false));
      } else {
        toast.error((r && r.message) || 'Weiterführen fehlgeschlagen.');
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [continueWorkflow, dispatch]);

  // State-driven German label (#L2): the raw server `phase` is English
  // ('running'/'done') and now lingers between blocks (truthy-guarded), so map
  // to German by state instead of echoing the raw string into the UI (Rule §1).
  const phaseLabel = paused
    ? DE.RUN_PAUSED
    : phase === 'error'
    ? DE.RUN_ERROR
    : isRunning
    ? DE.RUN_RUNNING
    : DE.RUN_READY;

  return (
    <div className="border-t border-[var(--line)] bg-white p-3 sm:p-4">
      <div className="flex flex-wrap items-center gap-2 mb-3">
        {!isRunning ? (
          <button
            type="button"
            onClick={handleStart}
            disabled={busy || (rsLeaderOn && !simMode)}
            title={rsLeaderOn && !simMode
              ? 'Bitte zuerst „Leader abschalten" (oben), bevor du das Programm ausführst.'
              : undefined}
            className={
              BUTTON_BASE
              + ' bg-[var(--accent)] text-white hover:opacity-90 '
              + 'focus-visible:ring-blue-500'
            }
            aria-label={DE.RUN_START}
          >
            ▶ {DE.RUN_START}
          </button>
        ) : !paused ? (
          <button
            type="button"
            onClick={handlePause}
            disabled={busy}
            className={
              BUTTON_BASE
              + ' bg-amber-500 text-white hover:bg-amber-600 '
              + 'focus-visible:ring-amber-500'
            }
            aria-label={DE.RUN_PAUSE}
          >
            ⏸ {DE.RUN_PAUSE}
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={handleStep}
              disabled={busy}
              className={
                BUTTON_BASE
                + ' bg-blue-500 text-white hover:bg-blue-600 '
                + 'focus-visible:ring-blue-500'
              }
              aria-label={DE.RUN_STEP}
            >
              ↪ {DE.RUN_STEP}
            </button>
            <button
              type="button"
              onClick={handleContinue}
              disabled={busy}
              className={
                BUTTON_BASE
                + ' bg-[var(--accent)] text-white hover:opacity-90 '
                + 'focus-visible:ring-blue-500'
              }
              aria-label={DE.RUN_CONTINUE}
            >
              ▶ {DE.RUN_CONTINUE}
            </button>
          </>
        )}
        <button
          type="button"
          onClick={handleStop}
          disabled={busy || !isRunning}
          className={
            BUTTON_BASE
            + ' bg-red-500 text-white hover:bg-red-600 '
            + 'focus-visible:ring-red-500'
          }
          aria-label={DE.RUN_STOP}
        >
          ■ {DE.RUN_STOP}
        </button>
        <span
          className={
            'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium '
            + (paused
              ? 'bg-amber-100 text-amber-700'
              : isRunning
              ? 'bg-green-100 text-green-700'
              : phase === 'error'
              ? 'bg-red-100 text-red-700'
              : 'bg-gray-100 text-gray-600')
          }
          aria-live="polite"
        >
          <span
            className={
              'w-1.5 h-1.5 rounded-full '
              + (isRunning && !paused ? 'bg-green-500 motion-safe:animate-pulse' : 'bg-gray-400')
            }
            aria-hidden="true"
          />
          {phaseLabel}
        </span>

        {/* Phase-2 global Tempo control. Applies to the WHOLE program at the
            next Start (the server reads ctx.tempo once at workflow start). */}
        <div
          className="inline-flex items-center gap-1"
          role="group"
          aria-label={DE.RUN_TEMPO_LABEL}
        >
          <span className="text-xs text-[var(--ink-3)]">{DE.RUN_TEMPO_LABEL}:</span>
          {TEMPO_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              onClick={() => handleTempo(p.value)}
              disabled={busy}
              aria-pressed={tempo === p.value}
              title={`Tempo ${p.label} (×${p.value})`}
              className={
                'min-h-[28px] px-2 py-1 text-xs rounded-md border '
                + 'disabled:opacity-50 disabled:cursor-not-allowed '
                + (tempo === p.value
                  ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                  : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
              }
            >
              {p.label}
            </button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => setLogOpen((v) => !v)}
            aria-pressed={logOpen}
            title={logOpen ? 'Protokoll ausblenden' : 'Protokoll anzeigen'}
            className={
              BUTTON_BASE
              + ' border border-[var(--line)] bg-white text-[var(--ink)] '
              + 'hover:bg-[var(--bg-sunk)] focus-visible:ring-blue-500'
            }
          >
            {logOpen ? '▾' : '▸'} {DE.DOCK_LOG_LABEL}
            {!logOpen && log.length > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 rounded-full bg-[var(--bg-sunk)] text-[10px] text-[var(--ink-3)]">
                {log.length}
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={handleDebugClick}
            aria-pressed={debugIsOpen}
            className={
              BUTTON_BASE
              + ' border border-[var(--line)] bg-white text-[var(--ink)] '
              + 'hover:bg-[var(--bg-sunk)] focus-visible:ring-blue-500'
            }
          >
            🔍 Debug
          </button>
        </div>
      </div>

      {rsLeaderOn && !simMode && !isRunning && (
        <div
          role="alert"
          className="bg-amber-50 border border-amber-200 text-amber-800 text-sm rounded-md p-2 mb-2"
        >
          Der Leader-Arm ist noch eingeschaltet. Bitte zuerst oben
          {' '}„Leader abschalten" drücken, bevor du das Programm ausführst —
          {' '}sonst kämpfen Teleoperation und Programm um den Roboter.
        </div>
      )}

      {simMode && !isRunning && (
        <div
          role="status"
          className="bg-blue-50 border border-blue-200 text-blue-800 text-sm rounded-md p-2 mb-2"
        >
          Simulator-Modus: Das Programm läuft auf einem virtuellen Roboter.
          Geprüft werden Logik, Reihenfolge und Erreichbarkeit — nicht die echte
          Physik.
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="bg-red-50 border border-red-200 text-red-800 text-sm rounded-md p-2 mb-2"
        >
          {error}
        </div>
      )}

      {logOpen && (
        <div
          className="bg-[var(--bg-sunk)] rounded-md p-3 max-h-40 overflow-y-auto font-mono text-xs"
          aria-label="Workflow-Log"
        >
          {log.length === 0 ? (
            <p className="text-[var(--ink-4)]">Keine Meldungen.</p>
          ) : (
            log.map((entry, idx) => (
              <div key={idx} className="text-[var(--ink-3)]">
                <span className="text-[var(--ink-4)] mr-2">
                  {new Date(entry.ts).toLocaleTimeString('de-DE')}
                </span>
                {entry.text}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export default RunControls;
