/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { armGeometry } from '../../utils/armProfile';

// Roboter Studio Batch 2b — „Roboter steuern (Tippbetrieb)". Incremental JOG of
// the real follower: per-joint − / + nudges (Gelenk 1–5 + Greifer-Drehung),
// cartesian X/Y/Z/roll nudges (NO pitch/yaw — the OMX-F IK is strict-vertical),
// a Schrittweite preset, and the hand-guide freischalten/festsetzen pair. The
// live 3D twin above shows the result. The WorkshopJog service is delta-based
// (`delta` per step, or absolute `target_*` for drive_to), so the natural UI is
// − / + nudges, not absolute sliders.

const DEG2RAD = Math.PI / 180;

// One dropdown, three presets. Each maps a joint-angle step (deg → rad), a
// cartesian translation step (metres) and a roll step (deg → rad). The server
// clamps to its own EEPROM/workspace limits, so these are only the per-tap size.
const STEP_PRESETS = [
  { id: 'fine', label: 'fein', jointDeg: 1, cartM: 0.002, rollDeg: 1 },
  { id: 'medium', label: 'mittel', jointDeg: 5, cartM: 0.01, rollDeg: 5 },
  { id: 'coarse', label: 'grob', jointDeg: 10, cartM: 0.025, rollDeg: 10 },
];

// WorkshopJog contract: index 0..n-1 = the arm joints, index n = the gripper
// channel. The rows are PROFILE-DRIVEN (edu6 §4.5): n from the capability
// manifest (OMX 5 → the exact pre-edu6 list incl. the 'Greifer-Drehung'
// degree row; edu6 6 → 'Gelenk 1..6' + a millimetre 'Greifer öffnen/
// schließen' row via gripper_mm_per_rad).
function buildJointRows(geo) {
  const rows = [];
  for (let i = 0; i < geo.armJoints; i += 1) {
    rows.push({ index: i, label: `Gelenk ${i + 1}`, gripperMm: null });
  }
  if (geo.gripperMmPerRad) {
    rows.push({
      index: geo.armJoints,
      label: 'Greifer öffnen/schließen',
      gripperMm: geo.gripperMmPerRad,
    });
  } else {
    rows.push({ index: geo.armJoints, label: 'Greifer-Drehung', gripperMm: null });
  }
  return rows;
}

// Cartesian axes: index 0=X, 1=Y, 2=Z, 3=roll. roll is an angle (deg step),
// X/Y/Z are translations (metre step). NO pitch/yaw.
const CART_AXES = [
  { index: 0, label: 'X', angular: false },
  { index: 1, label: 'Y', angular: false },
  { index: 2, label: 'Z', angular: false },
  { index: 3, label: 'Drehung (Roll)', angular: true },
];

const NUDGE_BTN =
  'min-w-[36px] min-h-[32px] px-2 py-1 text-sm rounded-md border '
  + 'disabled:opacity-40 disabled:cursor-not-allowed '
  + 'bg-white text-[var(--ink)] border-[var(--line)] hover:bg-[var(--bg-sunk)]';

/**
 * @param {boolean} disabled - blocked by the parent (not connected / a run is
 *   active). When true, every jog + the freischalten toggle is disabled.
 * @param {(handGuideOn: boolean) => void} onHandGuideChange - reports whether a
 *   hand-guide (torque-off) session is currently open, so the parent can disable
 *   sibling controls (RecordPanel) while the arm is limp. Mirrors
 *   RecordPanel.onRecordingChange.
 */
function JogPanel({ disabled = false, onHandGuideChange = null }) {
  const { jogArm, handGuide } = useRosServiceCaller();
  const [stepId, setStepId] = useState('medium');
  const [busy, setBusy] = useState(false);
  // Hand-guide (torque-off) state. While the arm is freigeschaltet, jog nudges
  // are meaningless (the arm is limp) and are disabled; „Arm festsetzen"
  // re-enables them.
  const [handGuideOn, setHandGuideOn] = useState(false);

  const preset = STEP_PRESETS.find((p) => p.id === stepId) || STEP_PRESETS[1];

  // Best-effort: re-fix the arm if the panel unmounts OR the tab is closed /
  // navigated away while it is still freigeschaltet, so a student never leaves
  // the follower limp. CRITICAL: a React unmount cleanup does NOT run on
  // tab-close / navigation, so the unmount-only cleanup missed the cardinal
  // limp-arm case — a `pagehide` (+ `beforeunload`) listener covers it, exactly
  // mirroring RecordPanel's teardown. Latest-value refs so the closures see the
  // live state + service fns.
  const handGuideOnRef = useRef(false);
  const handGuideFnRef = useRef(handGuide);
  const onHandGuideChangeRef = useRef(onHandGuideChange);
  useEffect(() => { handGuideOnRef.current = handGuideOn; }, [handGuideOn]);
  useEffect(() => { handGuideFnRef.current = handGuide; }, [handGuide]);
  useEffect(() => { onHandGuideChangeRef.current = onHandGuideChange; }, [onHandGuideChange]);
  useEffect(() => {
    const refixArm = () => {
      if (handGuideOnRef.current && typeof handGuideFnRef.current === 'function') {
        try {
          Promise.resolve(handGuideFnRef.current(false)).catch(() => {});
        } catch (_) { /* fire-and-forget */ }
      }
    };
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', refixArm);
      window.addEventListener('beforeunload', refixArm);
    }
    return () => {
      if (typeof window !== 'undefined') {
        window.removeEventListener('pagehide', refixArm);
        window.removeEventListener('beforeunload', refixArm);
      }
      refixArm();
      // Clear the parent's mirror so a stale „freigeschaltet" can't survive the
      // unmount (mirrors RecordPanel's onRecordingChange(false) on teardown).
      if (typeof onHandGuideChangeRef.current === 'function') {
        onHandGuideChangeRef.current(false);
      }
    };
  }, []);

  // FIX (hand-guide desync): report the freischalten state up so the parent can
  // disable RecordPanel while a JogPanel hand-guide session is open — a driven /
  // recorded move would fight the limp arm. Mirrors RecordPanel.onRecordingChange.
  useEffect(() => {
    if (typeof onHandGuideChange === 'function') {
      onHandGuideChange(handGuideOn);
    }
  }, [handGuideOn, onHandGuideChange]);

  // FIX FE-4: keepalive for the backend idle watchdog. The watchdog re-torques +
  // ends a hand-guide session after 30 s with NO manual SERVICE call — but a
  // student physically hand-positioning the limp arm makes no call, so an
  // actively-used session would get stiffened after 30 s. While freigeschaltet,
  // re-send the idempotent handGuide(true) every 15 s (the backend re-stamps its
  // activity timer on that call). A real unmount / tab-close stops the interval,
  // so the 30 s backstop still re-torques an ABANDONED limp arm. Guarded so it
  // never overlaps a real service call: `busy` covers a festsetzen toggle (set
  // synchronously in toggleHandGuide below, so there is no render-lag window where
  // a stray keepalive could re-free the arm after festsetzen re-torqued it),
  // `disabled` covers a disconnect / running program, keepaliveInFlightRef stops a
  // second keepalive stacking. Jogs cannot run here (jogDisabled includes
  // handGuideOn), so the only concurrent call is festsetzen.
  const busyRef = useRef(busy);
  const disabledRef = useRef(disabled);
  const keepaliveInFlightRef = useRef(false);
  useEffect(() => { busyRef.current = busy; }, [busy]);
  useEffect(() => { disabledRef.current = disabled; }, [disabled]);
  useEffect(() => {
    if (!handGuideOn) return undefined;
    const id = setInterval(() => {
      if (busyRef.current || disabledRef.current || keepaliveInFlightRef.current) return;
      if (!handGuideOnRef.current || typeof handGuideFnRef.current !== 'function') return;
      keepaliveInFlightRef.current = true;
      Promise.resolve(handGuideFnRef.current(true))
        .catch(() => {})
        .finally(() => { keepaliveInFlightRef.current = false; });
    }, 15000);
    return () => clearInterval(id);
  }, [handGuideOn]);

  const jogDisabled = disabled || busy || handGuideOn;

  const caps = useSelector((st) => (st.tasks && st.tasks.taskStatus ? st.tasks.taskStatus.capabilities : null));
  const geo = useMemo(() => armGeometry(caps), [caps]);
  const jointRows = useMemo(() => buildJointRows(geo), [geo]);

  const doJoint = useCallback(async (index, sign) => {
    // The gripper row on an mm-mapped profile steps in JAW MILLIMETRES
    // (preset.cartM metres of jaw travel → rad via mm-per-rad); every other
    // row keeps the degree step.
    const mmRow = index === geo.armJoints && geo.gripperMmPerRad;
    const delta = mmRow
      ? sign * ((preset.cartM * 1000) / geo.gripperMmPerRad)
      : sign * preset.jointDeg * DEG2RAD;
    setBusy(true);
    try {
      const res = await jogArm({ mode: 'joint', index, delta });
      if (res && res.success === false) {
        toast.error(res.message || 'Bewegung nicht möglich.');
      }
    } catch (e) {
      toast.error(`Bewegung fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [jogArm, preset, geo]);

  const doCartesian = useCallback(async (axis, sign) => {
    const delta = axis.angular
      ? sign * preset.rollDeg * DEG2RAD
      : sign * preset.cartM;
    setBusy(true);
    try {
      const res = await jogArm({ mode: 'cartesian', index: axis.index, delta });
      if (res && res.success === false) {
        toast.error(res.message || 'Bewegung nicht möglich.');
      }
    } catch (e) {
      toast.error(`Bewegung fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [jogArm, preset]);

  const toggleHandGuide = useCallback(async (enabled) => {
    // FIX FE-4: flip busyRef synchronously (not just via the busy-sync effect,
    // which lags a render) so the keepalive interval can NEVER fire handGuide(true)
    // after a festsetzen has already sent handGuide(false) — which would strand
    // the arm limp while the UI shows „festgesetzt".
    busyRef.current = true;
    setBusy(true);
    try {
      const res = await handGuide(enabled);
      if (res && res.success === false) {
        toast.error(res.message || 'Umschalten nicht möglich.');
        return;
      }
      setHandGuideOn(enabled);
      toast.success(
        enabled
          ? 'Arm freigeschaltet — du kannst ihn jetzt mit der Hand bewegen.'
          : 'Arm festgesetzt.',
      );
    } catch (e) {
      toast.error(`Umschalten fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [handGuide]);

  return (
    <div className="rounded-lg border border-[var(--line)] bg-white p-3">
      <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
        <h3 className="text-sm font-semibold text-[var(--ink)]">
          Roboter steuern (Tippbetrieb)
        </h3>
        <label className="flex items-center gap-1.5 text-xs text-[var(--ink-3)]">
          Schrittweite
          <select
            value={stepId}
            onChange={(e) => setStepId(e.target.value)}
            disabled={disabled || busy}
            className="text-xs rounded-md border border-[var(--line)] bg-white px-1.5 py-1 disabled:opacity-50"
          >
            {STEP_PRESETS.map((p) => (
              <option key={p.id} value={p.id}>{p.label}</option>
            ))}
          </select>
        </label>
      </div>

      {disabled && (
        <p className="text-[11px] text-[var(--ink-3)] mb-2">
          Der Tippbetrieb ist nur mit verbundenem Roboter und außerhalb eines
          laufenden Programms möglich.
        </p>
      )}

      {/* Hand-guide (torque off/on) */}
      <div className="flex items-center gap-2 flex-wrap mb-3">
        {!handGuideOn ? (
          <button
            type="button"
            onClick={() => toggleHandGuide(true)}
            disabled={disabled || busy}
            title="Motoren lösen, damit du den Arm mit der Hand bewegen kannst"
            className={
              'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
              + 'disabled:cursor-not-allowed bg-[var(--accent)] text-white '
              + 'border-[var(--accent)] hover:opacity-90'
            }
          >
            Arm freischalten
          </button>
        ) : (
          <button
            type="button"
            onClick={() => toggleHandGuide(false)}
            disabled={busy}
            title="Motoren wieder festsetzen, damit der Arm seine Position hält"
            className={
              'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
              + 'disabled:cursor-not-allowed bg-amber-500 text-white '
              + 'border-amber-500 hover:bg-amber-600'
            }
          >
            Arm festsetzen
          </button>
        )}
        <span className="text-[11px] text-[var(--ink-3)]">
          {handGuideOn
            ? 'Arm ist freigeschaltet — jetzt „Position merken" verwenden.'
            : 'Freischalten, von Hand bewegen, dann „Position merken".'}
        </span>
      </div>

      {/* Per-joint nudges */}
      <div className="grid grid-cols-1 gap-1.5 mb-3">
        {jointRows.map((j) => (
          <div key={j.index} className="flex items-center gap-2">
            <span className="text-xs text-[var(--ink-3)] w-32 shrink-0">{j.label}</span>
            <button
              type="button"
              className={NUDGE_BTN}
              disabled={jogDisabled}
              onClick={() => doJoint(j.index, -1)}
              aria-label={`${j.label} verringern`}
              title={j.gripperMm
                ? `${j.label} −${(preset.cartM * 1000).toFixed(0)} mm`
                : `${j.label} −${preset.jointDeg}°`}
            >
              −
            </button>
            <button
              type="button"
              className={NUDGE_BTN}
              disabled={jogDisabled}
              onClick={() => doJoint(j.index, +1)}
              aria-label={`${j.label} erhöhen`}
              title={j.gripperMm
                ? `${j.label} +${(preset.cartM * 1000).toFixed(0)} mm`
                : `${j.label} +${preset.jointDeg}°`}
            >
              +
            </button>
          </div>
        ))}
      </div>

      {/* Cartesian nudges */}
      <div className="grid grid-cols-1 gap-1.5">
        <span className="text-[11px] text-[var(--ink-3)]">Richtung (Greiferpunkt)</span>
        {CART_AXES.map((a) => (
          <div key={a.index} className="flex items-center gap-2">
            <span className="text-xs text-[var(--ink-3)] w-32 shrink-0">{a.label}</span>
            <button
              type="button"
              className={NUDGE_BTN}
              disabled={jogDisabled}
              onClick={() => doCartesian(a, -1)}
              aria-label={`${a.label} verringern`}
              title={a.angular ? `${a.label} −${preset.rollDeg}°` : `${a.label} −${(preset.cartM * 1000).toFixed(0)} mm`}
            >
              −
            </button>
            <button
              type="button"
              className={NUDGE_BTN}
              disabled={jogDisabled}
              onClick={() => doCartesian(a, +1)}
              aria-label={`${a.label} erhöhen`}
              title={a.angular ? `${a.label} +${preset.rollDeg}°` : `${a.label} +${(preset.cartM * 1000).toFixed(0)} mm`}
            >
              +
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

export default JogPanel;
