/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
// Namespace import (not named) so this file builds independently of the cloud
// agent that owns `createTrajectory` in workflowApi.js — a missing member is
// `undefined` and handled by the `typeof` guard below. Called by name per the
// CONTRACT (workflowApi.createTrajectory).
import * as workflowApi from '../../services/workflowApi';

// Roboter Studio Batch 2b — „Bewegung aufnehmen". The student hand-guides the
// follower (frees it in the JogPanel) while this panel records the joint stream
// via /workshop/record. On stop it shows a short review (Punkte + Dauer) with a
// live preview (/workshop/replay) and a save (name → cloud trajectory on the
// current workflow, consumed later by the „spiele Bewegung ab" replay block).

// Mirror the backend destination/trajectory name validator: letters (incl.
// ä ö ü ß), digits, space, underscore, hyphen — 1..40 chars.
const NAME_MAX_LEN = 40;
const NAME_RE = /^[A-Za-zÄÖÜäöüß0-9 _-]{1,40}$/;

// Mirror the backend RECORD_MAX_S cap (physical_ai_server.py::RECORD_MAX_S).
// The backend stops SAMPLING at this cap, but the local elapsed timer keeps
// counting, so without a client-side stop the UI would keep showing a live
// recording after the backend already auto-stopped. Kept in sync by hand.
const RECORD_MAX_S = 120;
function sanitizeName(raw) {
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim().slice(0, NAME_MAX_LEN);
  if (trimmed === '') return '';
  if (!NAME_RE.test(trimmed)) return '';
  return trimmed;
}

function fmtClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

// Parse a CONTRACT-B trajectory JSON string → { fps, points } or null.
function parsePoints(pointsJson) {
  try {
    const obj = JSON.parse(pointsJson);
    if (obj && Array.isArray(obj.points)) {
      return { fps: Number(obj.fps) || 0, points: obj.points };
    }
  } catch (_) { /* fall through */ }
  return null;
}

/**
 * @param {string|null} accessToken - cloud JWT (from the auth session).
 * @param {string|null} workflowId  - the current saved workflow's id; a
 *   trajectory can only be saved onto a saved workflow.
 * @param {boolean} disabled - blocked by the parent (not connected / a run is
 *   active).
 * @param {(recording: boolean) => void} onRecordingChange - reports whether a
 *   recording is currently in flight, so the parent can disable sibling controls
 *   (JogPanel + „fahre dorthin") while the arm is being hand-guided.
 */
function RecordPanel({
  accessToken = null,
  workflowId = null,
  disabled = false,
  onRecordingChange = null,
}) {
  const { recordControl, replayMotion, handGuide } = useRosServiceCaller();
  // 'idle' | 'recording' | 'review' | 'saving'
  const [state, setState] = useState('idle');
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  // The recorded trajectory awaiting save: { raw, fps, points, sampleCount, duration }
  const [recorded, setRecorded] = useState(null);
  const timerRef = useRef(null);

  const stopTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // FIX 2: report recording state up so the parent disables JogPanel + the
  // „fahre dorthin" drive-to while a hand-guide recording is running.
  useEffect(() => {
    if (typeof onRecordingChange === 'function') {
      onRecordingChange(state === 'recording');
    }
  }, [state, onRecordingChange]);

  // FIX 4: unmount / tab-close must never leave the arm limp mid-recording NOR
  // the Handbetrieb session open behind the student's back. Latest-value refs
  // (mirror JogPanel's unmount idiom) so the cleanup closure sees the live state
  // + service fns.
  const stateRef = useRef(state);
  const recordControlRef = useRef(recordControl);
  const handGuideRef = useRef(handGuide);
  const onRecordingChangeRef = useRef(onRecordingChange);
  useEffect(() => { stateRef.current = state; }, [state]);
  useEffect(() => { recordControlRef.current = recordControl; }, [recordControl]);
  useEffect(() => { handGuideRef.current = handGuide; }, [handGuide]);
  useEffect(() => { onRecordingChangeRef.current = onRecordingChange; }, [onRecordingChange]);

  useEffect(() => {
    // Mid-recording: record('cancel') re-torques the follower + discards the take
    // + clears the backend on_manual mutex. In review/saving the arm is already
    // re-torqued but the session is still OPEN — hand_guide(false) closes it so the
    // student isn't stranded behind „Handbetrieb ist aktiv" after navigating away.
    const teardownSession = () => {
      const st = stateRef.current;
      if (st === 'recording' && typeof recordControlRef.current === 'function') {
        try {
          Promise.resolve(recordControlRef.current('cancel')).catch(() => {});
        } catch (_) { /* fire-and-forget */ }
      } else if ((st === 'review' || st === 'saving')
          && typeof handGuideRef.current === 'function') {
        try {
          Promise.resolve(handGuideRef.current(false)).catch(() => {});
        } catch (_) { /* fire-and-forget */ }
      }
    };
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', teardownSession);
    }
    return () => {
      if (typeof window !== 'undefined') {
        window.removeEventListener('pagehide', teardownSession);
      }
      stopTimer();
      teardownSession();
      // Re-enable the sibling controls the parent disabled for us.
      if (typeof onRecordingChangeRef.current === 'function') {
        onRecordingChangeRef.current(false);
      }
    };
  }, [stopTimer]);

  // FIX 3: close the Handbetrieb session (re-fix the arm + clear the backend
  // on_manual mutex). `record stop` re-torques the arm but keeps the session
  // OPEN, so this hand_guide(false) is the SOLE close — without it the student is
  // stranded behind „Handbetrieb ist aktiv" and can't run the recorded motion.
  // A „Vorschau abspielen" (replay) still works after this: the backend replay
  // transiently re-opens the session for its own drive. Best-effort + idempotent.
  const closeManualSession = useCallback(async () => {
    if (typeof handGuide !== 'function') return;
    try {
      await handGuide(false);
    } catch (e) {
      console.warn('hand_guide(false) after recording failed', e);
    }
  }, [handGuide]);

  const handleStart = useCallback(async () => {
    setBusy(true);
    try {
      const res = await recordControl('start');
      if (!res || !res.success) {
        toast.error((res && res.message) || 'Aufnahme konnte nicht gestartet werden.');
        return;
      }
      setElapsed(0);
      setState('recording');
      stopTimer();
      const startedAt = Date.now();
      timerRef.current = setInterval(() => {
        setElapsed((Date.now() - startedAt) / 1000);
      }, 250);
    } catch (e) {
      toast.error(`Aufnahme fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [recordControl, stopTimer]);

  const handleStop = useCallback(async () => {
    setBusy(true);
    try {
      const res = await recordControl('stop');
      stopTimer();
      // FIX 3: `record stop` keeps the manual session open — close it on EVERY
      // stop path so the student is never stranded behind „Handbetrieb ist aktiv".
      // (Preview still works; the backend replay re-opens the session transiently.)
      closeManualSession();
      if (!res || !res.success) {
        toast.error((res && res.message) || 'Aufnahme konnte nicht beendet werden.');
        setState('idle');
        return;
      }
      const sampleCount = Number(res.sample_count) || 0;
      const parsed = parsePoints(res.points_json);
      if (sampleCount <= 0 || !parsed || parsed.points.length === 0) {
        toast('Keine Bewegung aufgenommen — bitte den Arm während der Aufnahme bewegen.', { icon: '💡' });
        setState('idle');
        setRecorded(null);
        return;
      }
      setRecorded({
        raw: res.points_json,
        fps: parsed.fps,
        points: parsed.points,
        sampleCount,
        duration: Number(res.duration_s) || 0,
      });
      setState('review');
    } catch (e) {
      toast.error(`Beenden fehlgeschlagen: ${e.message || e}`);
      closeManualSession();  // stop threw — still release the arm/session
      setState('idle');
    } finally {
      setBusy(false);
    }
  }, [recordControl, stopTimer, closeManualSession]);

  // FIX 5: mirror the backend RECORD_MAX_S cap. When the elapsed timer reaches
  // the cap, the backend has already auto-stopped SAMPLING — so auto-invoke the
  // same Stopp path here (once) to retrieve the capped trajectory for review and
  // keep the UI from showing a phantom live recording. Ref-guarded so the 250 ms
  // timer can't fire Stopp repeatedly; reset whenever we leave the recording state.
  const autoStoppedRef = useRef(false);
  useEffect(() => {
    if (state !== 'recording') {
      autoStoppedRef.current = false;
      return;
    }
    if (elapsed >= RECORD_MAX_S && !autoStoppedRef.current) {
      autoStoppedRef.current = true;
      toast('Maximale Aufnahmedauer erreicht — Aufnahme wird beendet.', { icon: '⏱️' });
      handleStop();
    }
  }, [elapsed, state, handleStop]);

  const handleCancel = useCallback(async () => {
    setBusy(true);
    try {
      await recordControl('cancel');
    } catch (e) {
      // Best-effort — the local recording is discarded regardless.
      console.warn('recordControl(cancel) failed', e);
    } finally {
      stopTimer();
      setState('idle');
      setRecorded(null);
      setBusy(false);
    }
  }, [recordControl, stopTimer]);

  const handleDiscard = useCallback(() => {
    closeManualSession();  // FIX 3: release the arm/session on discard
    setRecorded(null);
    setState('idle');
  }, [closeManualSession]);

  const handlePreview = useCallback(async () => {
    if (!recorded) return;
    setBusy(true);
    try {
      const res = await replayMotion({ name: '', points_json: recorded.raw, speed: 1.0 });
      if (!res || !res.success) {
        toast.error((res && res.message) || 'Vorschau nicht möglich.');
      }
    } catch (e) {
      toast.error(`Vorschau fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [recorded, replayMotion]);

  const handleSave = useCallback(async () => {
    if (!recorded) return;
    if (!workflowId) {
      toast.error('Bitte zuerst den Workflow speichern, dann die Bewegung speichern.');
      return;
    }
    if (!accessToken) {
      toast.error('Nicht angemeldet — Speichern nicht möglich.');
      return;
    }
    if (typeof window === 'undefined') return;
    const raw = window.prompt('Name für die aufgenommene Bewegung:', '');
    if (raw === null) return; // student cancelled the prompt
    const name = sanitizeName(raw);
    if (!name) {
      toast.error(
        'Bitte einen gültigen Namen verwenden '
        + '(Buchstaben, Zahlen, Leerzeichen, _ und -).',
      );
      return;
    }
    if (typeof workflowApi.createTrajectory !== 'function') {
      toast.error('Speichern von Bewegungen ist zurzeit nicht verfügbar.');
      return;
    }
    setState('saving');
    setBusy(true);
    try {
      await workflowApi.createTrajectory(accessToken, workflowId, {
        name,
        fps: recorded.fps,
        points: recorded.points,
        // FIX 6: persist the reviewed duration (the route already accepts it).
        duration_s: recorded.duration,
      });
      toast.success(
        `Bewegung „${name}" gespeichert. Verwende sie mit „spiele Bewegung ${name} ab".`,
      );
      closeManualSession();  // FIX 3: release the arm/session after a save
      setRecorded(null);
      setState('idle');
    } catch (e) {
      toast.error(`Speichern fehlgeschlagen: ${e.message || e}`);
      setState('review');
    } finally {
      setBusy(false);
    }
  }, [recorded, workflowId, accessToken, closeManualSession]);

  return (
    <div className="rounded-lg border border-[var(--line)] bg-white p-3">
      <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
        <h3 className="text-sm font-semibold text-[var(--ink)]">Bewegung aufnehmen</h3>
        {state === 'recording' && (
          <span className="inline-flex items-center gap-1.5 text-xs font-mono text-red-600" aria-live="polite">
            <span className="w-2 h-2 rounded-full bg-red-500 motion-safe:animate-pulse" aria-hidden="true" />
            {fmtClock(elapsed)}
          </span>
        )}
      </div>

      {state === 'idle' && (
        <>
          <button
            type="button"
            onClick={handleStart}
            disabled={disabled || busy}
            title="Eine handgeführte Bewegung des Arms aufnehmen"
            className={
              'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
              + 'disabled:cursor-not-allowed bg-[var(--accent)] text-white '
              + 'border-[var(--accent)] hover:opacity-90'
            }
          >
            Bewegung aufnehmen
          </button>
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5">
            Schalte den Arm frei, bewege ihn von Hand und nimm die Bewegung auf.
            {!workflowId && ' Zum Speichern muss der Workflow zuerst gespeichert werden.'}
          </p>
        </>
      )}

      {state === 'recording' && (
        <div className="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            onClick={handleStop}
            disabled={busy}
            className={
              'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
              + 'disabled:cursor-not-allowed bg-red-500 text-white '
              + 'border-red-500 hover:bg-red-600'
            }
          >
            Stopp
          </button>
          <button
            type="button"
            onClick={handleCancel}
            disabled={busy}
            className="text-xs px-2.5 py-1 rounded-md border border-[var(--line)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] disabled:opacity-50"
          >
            Verwerfen
          </button>
          <span className="text-[11px] text-[var(--ink-3)]">Bewegung wird aufgenommen …</span>
        </div>
      )}

      {(state === 'review' || state === 'saving') && recorded && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-[var(--ink-3)]">
            Aufgenommen: {recorded.sampleCount} Punkte · {recorded.duration.toFixed(1)} s
            {recorded.fps ? ` · ${recorded.fps} Hz` : ''}
          </p>
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={handlePreview}
              disabled={disabled || busy}
              title="Die aufgenommene Bewegung auf dem Roboter abspielen"
              className="text-xs px-2.5 py-1 rounded-md border border-[var(--line)] text-[var(--ink)] hover:bg-[var(--bg-sunk)] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Vorschau abspielen
            </button>
            <button
              type="button"
              onClick={handleSave}
              // FIX FE-3: also honour the incoming `disabled` prop. Saving runs
              // closeManualSession() → handGuide(false), which re-torques the arm;
              // the parent sets `disabled` when JogPanel opened a hand-guide
              // session (among other guards), so without this a Save would stiffen
              // the arm out from under JogPanel while it still shows „freigeschaltet".
              disabled={busy || !workflowId || disabled}
              title={workflowId
                ? 'Bewegung benennen und speichern'
                : 'Bitte zuerst den Workflow speichern'}
              className={
                'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
                + 'disabled:cursor-not-allowed bg-[var(--accent)] text-white '
                + 'border-[var(--accent)] hover:opacity-90'
              }
            >
              {state === 'saving' ? 'Wird gespeichert …' : 'Speichern'}
            </button>
            <button
              type="button"
              onClick={handleDiscard}
              disabled={busy}
              className="text-xs px-2.5 py-1 rounded-md border border-[var(--line)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] disabled:opacity-50"
            >
              Verwerfen
            </button>
          </div>
          {!workflowId && (
            <p className="text-[11px] text-amber-700">
              Zum Speichern muss der Workflow zuerst gespeichert werden.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default RecordPanel;
