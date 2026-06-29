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
  setDebuggerWarnings,
} from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { DE } from './blocks/messages_de';

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
const RS_CONTROL_BASE = 'http://localhost:8769';
const RS_STATUS_TIMEOUT_MS = 4000;
const RS_STATUS_POLL_MS = 8000;

async function probeRsStatus() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), RS_STATUS_TIMEOUT_MS);
  try {
    const res = await fetch(`${RS_CONTROL_BASE}/roboter-studio/status`, { signal: ctrl.signal });
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

function RunControls({
  workflowId,
  blocklyJson,
  workspace = null,
  simMode = false,
  simScene = null,
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
  const phase = useSelector((s) => s.workshop.phase);
  const currentBlockId = useSelector((s) => s.workshop.currentBlockId);
  const paused = useSelector((s) => s.workshop.paused);
  const log = useSelector((s) => s.workshop.log);
  const error = useSelector((s) => s.workshop.workflowError);
  const debuggerVisible = useSelector((s) => s.workshop.debuggerVisible);
  const debuggerWarnings = useSelector((s) => s.workshop.debuggerWarnings);
  const breakpoints = useSelector((s) => s.workshop.breakpoints);
  const [busy, setBusy] = useState(false);

  // Leader-contention gate (see RS_CONTROL_BASE comment above). true ONLY when
  // the bridge is reachable AND the leader is on; false while probing, on any
  // probe error, and on Jetson/cloud (no bridge) — i.e. it fails open.
  const [rsLeaderOn, setRsLeaderOn] = useState(false);

  const isRunning = runState === 'running' || phase === 'running' || paused;

  // Poll the GUI control bridge so the gate tracks the leader toggle live (the
  // student flips it from the same header). Only block on a POSITIVE answer
  // (bridge present AND leader on); any unreachable/error result fails open so a
  // transient probe hiccup never wedges the run button.
  useEffect(() => {
    let cancelled = false;
    let intervalId = null;
    const tick = async () => {
      const { available, followerOnly } = await probeRsStatus();
      if (cancelled) return;
      setRsLeaderOn(available && !followerOnly);
    };
    tick();
    intervalId = setInterval(tick, RS_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, []);

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
      const workflowJsonStr = simMode
        ? JSON.stringify({
            ...(blocklyJson || {}),
            sim: { enabled: true, objects: simObjects },
            zones,
          })
        : JSON.stringify({ ...(blocklyJson || {}), zones });
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
    callService,
    dispatch,
    workflowId,
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

  const handleToggleDebugger = useCallback(() => {
    dispatch(toggleDebugger());
  }, [dispatch]);

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

        <button
          type="button"
          onClick={handleToggleDebugger}
          aria-pressed={debuggerVisible}
          className={
            BUTTON_BASE
            + ' ml-auto border border-[var(--line)] bg-white text-[var(--ink)] '
            + 'hover:bg-[var(--bg-sunk)] focus-visible:ring-blue-500'
          }
        >
          🔍 Debug
        </button>
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

      <div
        className="bg-[var(--bg-sunk)] rounded-md p-3 max-h-48 overflow-y-auto font-mono text-xs"
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
    </div>
  );
}

export default RunControls;
