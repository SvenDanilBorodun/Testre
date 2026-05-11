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
  setCloudVisionEnabled,
} from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { DE } from './blocks/messages_de';

const BUTTON_BASE =
  'inline-flex items-center justify-center min-h-[36px] '
  + 'px-4 py-2 rounded-md text-sm font-medium '
  + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 '
  + 'disabled:opacity-50 disabled:cursor-not-allowed';

function RunControls({ workflowId, blocklyJson, workspace = null }) {
  const dispatch = useDispatch();
  const { callService, pauseWorkflow, stepWorkflow, continueWorkflow } =
    useRosServiceCaller();
  const runState = useSelector((s) => s.workshop.runState);
  const phase = useSelector((s) => s.workshop.phase);
  const paused = useSelector((s) => s.workshop.paused);
  const log = useSelector((s) => s.workshop.log);
  const error = useSelector((s) => s.workshop.workflowError);
  const debuggerVisible = useSelector((s) => s.workshop.debuggerVisible);
  const debuggerWarnings = useSelector((s) => s.workshop.debuggerWarnings);
  const cloudVisionEnabled = useSelector((s) => s.workshop.cloudVisionEnabled);
  // Forwarded to the on-host server via StartWorkflow.srv so the
  // _cloud_vision_burst can authorise its POST to /vision/detect.
  // Empty string when the user isn't logged in (e.g. cloud-only mode).
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  const [busy, setBusy] = useState(false);

  const isRunning = runState === 'running' || phase === 'running' || paused;

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

  const handleStart = useCallback(async () => {
    if (!blocklyJson) {
      toast.error('Workflow ist leer.');
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
      const r = await callService(
        '/workflow/start',
        'physical_ai_interfaces/srv/StartWorkflow',
        {
          workflow_json: JSON.stringify(blocklyJson),
          workflow_id: workflowId || `local-${Date.now()}`,
          cloud_vision_enabled: !!cloudVisionEnabled,
          auth_token: cloudVisionEnabled && typeof accessToken === 'string'
            ? accessToken
            : '',
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
  // cloudVisionEnabled must be in the deps array — otherwise the
  // useCallback retains the value captured at the first render and
  // toggling the checkbox doesn't take effect. Audit round-3 §J / §W.
  // accessToken is in the deps so a token refresh during a session
  // is picked up at the next Start press.
  }, [blocklyJson, callService, cloudVisionEnabled, accessToken, dispatch, workflowId]);

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

  const phaseLabel = paused
    ? DE.RUN_PAUSED
    : (phase || DE.RUN_READY);

  return (
    <div className="border-t border-[var(--line)] bg-white p-3 sm:p-4">
      <div className="flex flex-wrap items-center gap-2 mb-3">
        {!isRunning ? (
          <button
            type="button"
            onClick={handleStart}
            disabled={busy}
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

        <label
          className="inline-flex items-center gap-1.5 text-xs text-[var(--ink-3)] cursor-pointer select-none"
          title="Wenn aktiv, dürfen 'finde Objekt mit Beschreibung'-Blöcke unbekannte Begriffe an die Cloud-Erkennung schicken."
        >
          <input
            type="checkbox"
            checked={!!cloudVisionEnabled}
            onChange={(e) => dispatch(setCloudVisionEnabled(e.target.checked))}
            className="w-4 h-4"
            aria-label={DE.CLOUD_VISION_TOGGLE}
          />
          {DE.CLOUD_VISION_TOGGLE}
        </label>
        <VisionQuotaChip />

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

// Audit F30: per-term cloud-vision quota chip rendered next to the
// toggle. Reads vision_quota_per_term + vision_used_per_term from
// /me; renders nothing when the API does not return them
// (e.g. migration 017 not deployed, or unbounded NULL quota).
function VisionQuotaChip() {
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  const [usage, setUsage] = useState(null);
  useEffect(() => {
    if (!accessToken) {
      setUsage(null);
      return undefined;
    }
    let cancelled = false;
    (async () => {
      try {
        const mod = await import('../../services/meApi');
        const me = await mod.getMe(accessToken);
        if (cancelled) return;
        const quota = me?.vision_quota_per_term;
        const used = me?.vision_used_per_term;
        if (typeof quota === 'number' && typeof used === 'number') {
          setUsage({ quota, used });
        } else {
          setUsage(null);
        }
      } catch (_) {
        if (!cancelled) setUsage(null);
      }
    })();
    return () => { cancelled = true; };
  }, [accessToken]);
  if (!usage) return null;
  const remaining = Math.max(0, usage.quota - usage.used);
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-blue-50 text-blue-700"
      title="Cloud-Erkennung — verbleibende Aufrufe in diesem Halbjahr"
    >
      ☁ {remaining}/{usage.quota}
    </span>
  );
}

export default RunControls;
