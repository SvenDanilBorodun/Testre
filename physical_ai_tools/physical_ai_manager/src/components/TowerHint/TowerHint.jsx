// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The V1's live camera-tower hint (spec 40 WP-HINT, 52 §5.3–§5.5; decisions
// D56, D57 Q1, D88). The robot judges; the page only shows what it said and
// holds the action back while the robot would refuse it anyway:
//   * TowerHint — the animation: a render of the real V1 URDF with the tower
//     (WP-ART; robotAssets.js `towerHintPicture`), stepped by CSS. Reduced
//     motion shows the last frame — the safe pose — standing still.
//   * TowerActivationHint — the Startseite's panel while /edubotics/tower_state
//     is not `clear`: the sentence and the way out of an energised arm that
//     lies back (52 §5.4): „Arm lockern", then „Ich halte ihn" =
//     /workshop/hand_guide(true) with JogPanel's keepalive, the student lays
//     it forward, „Arm festsetzen" = hand_guide(false). No new torque path and
//     no new service: the arbiter, its 30 s idle re-torque, the driver's
//     `Goal = Present` seeding, its edge refusal and its post-energise abort
//     are all the existing ones.
//   * TowerStopDialog — „Stoppen" asks first where the limp arm would fall back
//     toward the tower.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import ReactDOM from 'react-dom';
import clsx from 'clsx';

import { Btn } from '../EbUI';
import Icon from '../icons/Icon';
import { towerHintPicture } from '../icons/robotAssets';
import { Screen, useReducedMotion } from '../sucher';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { stripSeverityPrefix } from '../../utils/homeHealth';
import {
  TOWER_CANCEL_DE, TOWER_FREE_DE, TOWER_FREE_FAILED_DE, TOWER_FREEING_DE,
  TOWER_HINT_ACTIVATE_DE, TOWER_HINT_HOLD_DE, TOWER_HINT_LIMP_DE, TOWER_HINT_STOP_DE,
  TOWER_HINT_STOP_TITLE_DE, TOWER_HOLDING_DE, TOWER_LOCK_DE, TOWER_LOCK_FAILED_DE,
  TOWER_LOCKING_DE, TOWER_STOP_ANYWAY_DE, TOWER_UNREACHABLE_DE,
} from './towerHintCopy';
import './towerHint.css';

// JogPanel's keepalive period (spec 40 §2 correction 24): the server re-torques
// a hand-guide session after 30 s without a manual call
// (_MANUAL_IDLE_RETORQUE_S), and a student laying the arm down by hand makes
// none, so the idempotent hand_guide(true) is re-sent every 15 s.
export const TOWER_KEEPALIVE_MS = 15000;

/**
 * The animated strip of `scene` ('forward' | 'fall') for `profile`, or nothing
 * for a profile without the tower. `light` picks the cream arm for a dark
 * screen. Decorative: the sentence beside it carries the meaning.
 */
export default function TowerHint({ profile, scene, light = false, className }) {
  const reduced = useReducedMotion();
  const pic = towerHintPicture(profile, scene, { light });
  if (!pic) return null;
  // Reduced motion: the last frame, still (the CSS media query does the same
  // before this hook has answered).
  const still = reduced
    ? { animation: 'none', transform: `translateX(-${(pic.safeFrame / pic.frames) * 100}%)` }
    : undefined;
  return (
    <div
      className={clsx('tower-hint', reduced && 'tower-hint--still', className)}
      data-tower-hint={scene}
      data-robot-picture={pic.file}
      aria-hidden="true"
      style={{ aspectRatio: `${pic.width} / ${pic.height}`, '--tower-hint-frames': pic.frames }}
    >
      <div className="tower-hint__clip">
        <img className="tower-hint__strip" src={pic.url} alt="" draggable="false" decoding="async" style={still} />
      </div>
    </div>
  );
}

// The freeing flow's phases: 'idle' → „Arm lockern" → 'confirm' → „Ich halte
// ihn" → 'releasing' (hand_guide(true) on its way) → 'limp' → „Arm festsetzen"
// → 'locking' (hand_guide(false) on its way) → 'idle'.
const HELD_LIMP = new Set(['releasing', 'limp', 'locking']);

function answerOf(res, fallback) {
  return stripSeverityPrefix((res && res.message) || '') || fallback;
}

/**
 * The flow behind TowerActivationHint. `disabled` (no bridge, a task or a
 * program running, an activation under way) refuses a new „lockern" and
 * pauses the keepalive, exactly as JogPanel's `disabled` does; „festsetzen"
 * always stays possible. `onActiveChange(true)` from the first click until the
 * arm is locked again, so the card keeps the panel while the arm is limp
 * whatever the verdict says.
 */
function useTowerFreeing({ handGuide, disabled, onActiveChange }) {
  const [phase, setPhase] = useState('idle');
  const [message, setMessage] = useState('');
  const phaseRef = useRef('idle');
  const mountedRef = useRef(true);
  const handGuideRef = useRef(handGuide);
  const disabledRef = useRef(disabled);
  const activeChangeRef = useRef(onActiveChange);
  // The keepalive in flight, if any: a „festsetzen" waits for it, so a late
  // hand_guide(true) can never free the arm again after it was locked.
  const keepaliveRef = useRef(null);
  useEffect(() => { handGuideRef.current = handGuide; }, [handGuide]);
  useEffect(() => { disabledRef.current = disabled; }, [disabled]);
  useEffect(() => { activeChangeRef.current = onActiveChange; }, [onActiveChange]);

  const go = useCallback((next) => {
    phaseRef.current = next;
    if (mountedRef.current) setPhase(next);
    if (typeof activeChangeRef.current === 'function') activeChangeRef.current(next !== 'idle');
  }, []);

  const relock = useCallback(() => {
    const fn = handGuideRef.current;
    if (typeof fn !== 'function') return;
    try {
      Promise.resolve(fn(false)).catch(() => {});
    } catch (_) { /* fire-and-forget */ }
  }, []);

  // Never leave the arm limp: on unmount, and when the tab is closed or
  // navigated away (an unmount cleanup does not run then) — JogPanel's rule.
  useEffect(() => {
    mountedRef.current = true;
    const onLeave = () => { if (HELD_LIMP.has(phaseRef.current)) relock(); };
    window.addEventListener('pagehide', onLeave);
    window.addEventListener('beforeunload', onLeave);
    return () => {
      mountedRef.current = false;
      window.removeEventListener('pagehide', onLeave);
      window.removeEventListener('beforeunload', onLeave);
      onLeave();
      if (typeof activeChangeRef.current === 'function') activeChangeRef.current(false);
    };
  }, [relock]);

  // The keepalive, with JogPanel's guards: not while a „festsetzen" is on its
  // way (phaseRef is no longer 'limp' then, set synchronously), not while
  // disabled, never a second one on top of one in flight.
  useEffect(() => {
    if (phase !== 'limp') return undefined;
    const id = setInterval(() => {
      if (phaseRef.current !== 'limp' || disabledRef.current || keepaliveRef.current) return;
      const fn = handGuideRef.current;
      if (typeof fn !== 'function') return;
      keepaliveRef.current = Promise.resolve(fn(true))
        .catch(() => {})
        .finally(() => { keepaliveRef.current = null; });
    }, TOWER_KEEPALIVE_MS);
    return () => clearInterval(id);
  }, [phase]);

  const ask = useCallback(() => {
    if (phaseRef.current !== 'idle' || disabledRef.current) return;
    setMessage('');
    go('confirm');
  }, [go]);

  const cancel = useCallback(() => {
    if (phaseRef.current === 'confirm') go('idle');
  }, [go]);

  const release = useCallback(async () => {
    if (phaseRef.current !== 'confirm' || disabledRef.current) return;
    go('releasing');
    try {
      const res = await handGuideRef.current(true);
      if (!mountedRef.current) {
        // Gone while the call was on its way: the cleanup's lock may have
        // reached the robot first, so lock once more.
        if (!(res && res.success === false)) relock();
        return;
      }
      if (res && res.success === false) {
        setMessage(answerOf(res, TOWER_FREE_FAILED_DE));
        go('idle');
        return;
      }
      go('limp');
    } catch (_) {
      if (!mountedRef.current) return;
      setMessage(TOWER_UNREACHABLE_DE);
      go('idle');
    }
  }, [go, relock]);

  const lock = useCallback(async () => {
    if (phaseRef.current !== 'limp') return;
    go('locking');
    setMessage('');
    try {
      if (keepaliveRef.current) await keepaliveRef.current;
      const res = await handGuideRef.current(false);
      if (!mountedRef.current) return;
      if (res && res.success === false) {
        // The arm stays limp (the server keeps the session): say why, keep
        // the keepalive, offer „festsetzen" again.
        setMessage(answerOf(res, TOWER_LOCK_FAILED_DE));
        go('limp');
        return;
      }
      go('idle');
    } catch (_) {
      if (!mountedRef.current) return;
      setMessage(TOWER_UNREACHABLE_DE);
      go('limp');
    }
  }, [go]);

  return { phase, message, ask, cancel, release, lock };
}

/**
 * The Startseite's panel while the driver would refuse the activation glide
 * (52 §5.3 (a), §5.4). Mounted by ActivationCard only then, or while the arm
 * this panel freed is still limp.
 */
export function TowerActivationHint({ profile, blockedReason = null, onActiveChange }) {
  const { handGuide } = useRosServiceCaller();
  const {
    phase, message, ask, cancel, release, lock,
  } = useTowerFreeing({ handGuide, disabled: !!blockedReason, onActiveChange });
  const asking = phase === 'confirm' || phase === 'releasing';
  const limp = phase === 'limp' || phase === 'locking';
  return (
    <div className="tower-hint-panel flex flex-col gap-2 rounded-[14px] bg-[var(--amber-wash)] p-3">
      <Screen bezel="thin" className="tower-hint-panel__screen">
        <TowerHint profile={profile} scene="forward" light />
      </Screen>
      <p className="m-0 text-[13px] leading-snug text-[var(--ink)]">{TOWER_HINT_ACTIVATE_DE}</p>
      {asking && <p className="m-0 text-[13px] font-semibold leading-snug text-[var(--ink)]">{TOWER_HINT_HOLD_DE}</p>}
      {limp && <p className="m-0 text-[13px] font-semibold leading-snug text-[var(--ink)]">{TOWER_HINT_LIMP_DE}</p>}
      {message && <p className="m-0 text-[13px] leading-snug text-[color:var(--danger)]">{message}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {phase === 'idle' && (
          <Btn variant="secondary" onClick={ask} disabled={!!blockedReason} title={blockedReason || undefined}>
            {TOWER_FREE_DE}
          </Btn>
        )}
        {asking && (
          <>
            <Btn variant="primary" onClick={release} disabled={phase === 'releasing' || !!blockedReason}>
              {phase === 'releasing' ? TOWER_FREEING_DE : TOWER_HOLDING_DE}
            </Btn>
            <Btn variant="ghost" onClick={cancel} disabled={phase === 'releasing'}>{TOWER_CANCEL_DE}</Btn>
          </>
        )}
        {limp && (
          <Btn variant="primary" onClick={lock} disabled={phase === 'locking'}>
            {phase === 'locking' ? TOWER_LOCKING_DE : TOWER_LOCK_DE}
          </Btn>
        )}
      </div>
    </div>
  );
}

/**
 * „Stoppen" while the limp arm would fall back toward the tower (52 §5.3 (c)):
 * the limp-fall animation and two choices. Focus starts on „Abbrechen", the
 * choice that takes no torque off. Portalled to <body>, like the home-glide
 * warning: a `fixed` overlay inside a transformed ancestor would be clipped.
 */
export function TowerStopDialog({ open, profile, onCancel, onConfirm }) {
  if (!open) return null;
  const overlay = (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="tower-stop-title"
      aria-describedby="tower-stop-text"
    >
      <div className="mx-4 max-w-md rounded-2xl border-2 border-amber-500 bg-white p-6 shadow-2xl">
        <div className="flex flex-col items-center gap-3 text-center">
          <Icon name="warning" size="2.5rem" className="text-amber-600" />
          <h2 id="tower-stop-title" className="text-xl font-bold text-amber-700">{TOWER_HINT_STOP_TITLE_DE}</h2>
          <TowerHint profile={profile} scene="fall" />
          <p id="tower-stop-text" className="text-sm text-gray-700">{TOWER_HINT_STOP_DE}</p>
          <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
            <button
              type="button"
              onClick={onCancel}
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
              className="rounded-xl border border-gray-300 bg-white px-5 py-2.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              {TOWER_CANCEL_DE}
            </button>
            <button
              type="button"
              onClick={onConfirm}
              className="rounded-xl bg-red-600 px-5 py-2.5 text-sm font-semibold text-white shadow hover:bg-red-700"
            >
              {TOWER_STOP_ANYWAY_DE}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
  if (typeof document === 'undefined' || !document.body) return overlay;
  return ReactDOM.createPortal(overlay, document.body);
}
