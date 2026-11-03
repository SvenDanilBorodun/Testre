/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Roboter prüfen" (station spec v6.13, §7.2, §9.2 step 2). The student frees
// the arm with the EXISTING hand-guide (`/workshop/hand_guide`, not gated by
// D4), guides the tip onto any of the four dots and presses „Prüfen". While it
// waits for „Prüfen" this sends the hand-guide keepalive (JogPanel's rhythm),
// so the server's 30 s idle watchdog never re-locks the arm mid-guide. After
// the check it ends the hand-guide session — the existing re-lock holds the arm
// where it is — and offers the existing HomeGlidePrompt only once the session
// is verified (D4).
//
// The server reads FK and works whatever the torque state; this component only
// opens and closes the hand-guide and asks the controller for the check.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import toast from 'react-hot-toast';

import { Key } from '../sucher';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { useHomeGlide } from '../Workshop/HomeGlidePrompt';
import * as station from '../../features/station/controller';
import { HAND_GUIDE_KEEPALIVE_MS } from '../../features/station/stationContract';
import { STATION_DE } from '../../features/station/stationCopy';

/**
 * @param {object} p
 * @param {boolean} p.disabled          the check cannot start (no session, a program runs, …)
 * @param {(on: boolean) => void} [p.onHandGuideChange]  the arm is freed by this check
 */
export default function StationRobotCheck({ disabled = false, onHandGuideChange = null }) {
  const { handGuide } = useRosServiceCaller();
  const { offerHomeGlide, homeGlideDialog } = useHomeGlide();
  // 'idle' | 'freeing' | 'guiding' | 'checking' | 'locking'
  const [phase, setPhase] = useState('idle');
  const guidingRef = useRef(false);
  const handGuideRef = useRef(handGuide);
  handGuideRef.current = handGuide;
  const reportRef = useRef(onHandGuideChange);
  reportRef.current = onHandGuideChange;

  const setGuiding = useCallback((on) => {
    guidingRef.current = on;
    if (typeof reportRef.current === 'function') reportRef.current(on);
  }, []);

  // Never leave the arm limp: on unmount, tab close or navigation, re-lock.
  useEffect(() => {
    const relock = () => {
      if (!guidingRef.current) return;
      guidingRef.current = false;
      try {
        Promise.resolve(handGuideRef.current(false)).catch(() => {});
      } catch { /* fire and forget */ }
    };
    window.addEventListener('pagehide', relock);
    return () => {
      window.removeEventListener('pagehide', relock);
      relock();
      if (typeof reportRef.current === 'function') reportRef.current(false);
    };
  }, []);

  // The hand-guide keepalive while the student guides the tip (§7.2).
  useEffect(() => {
    if (phase !== 'guiding') return undefined;
    let inFlight = false;
    const id = setInterval(() => {
      if (inFlight || !guidingRef.current) return;
      inFlight = true;
      Promise.resolve(handGuideRef.current(true)).catch(() => {}).finally(() => { inFlight = false; });
    }, HAND_GUIDE_KEEPALIVE_MS);
    return () => clearInterval(id);
  }, [phase]);

  const relockArm = useCallback(async () => {
    setPhase('locking');
    try {
      const res = await handGuideRef.current(false);
      if (res && res.success === false) toast.error(res.message || STATION_DE.robotRelockFailed);
    } catch {
      toast.error(STATION_DE.robotRelockFailed);
    } finally {
      setGuiding(false);
      setPhase('idle');
    }
  }, [setGuiding]);

  const start = useCallback(async () => {
    setPhase('freeing');
    try {
      const res = await handGuideRef.current(true);
      if (res && res.success === false) {
        toast.error(res.message || STATION_DE.robotFreeFailed);
        setPhase('idle');
        return;
      }
      setGuiding(true);
      setPhase('guiding');
    } catch {
      toast.error(STATION_DE.robotFreeFailed);
      setPhase('idle');
    }
  }, [setGuiding]);

  const check = useCallback(async () => {
    setPhase('checking');
    const res = await station.runRobotCheck();
    // After the check the hand-guide session ends; the re-lock holds the arm in place.
    await relockArm();
    // The way home is offered only once the session is verified (D4).
    if (res && res.verified) offerHomeGlide();
  }, [relockArm, offerHomeGlide]);

  if (phase === 'idle' || phase === 'freeing') {
    return (
      <>
        {homeGlideDialog}
        <Key size="sm" icon="hand" onClick={start} disabled={disabled || phase === 'freeing'} data-testid="station-robot-check">
          {phase === 'freeing' ? STATION_DE.robotFreeing : STATION_DE.stepRobot}
        </Key>
      </>
    );
  }
  return (
    <div className="flex flex-col gap-2 rounded-md border border-[var(--line)] bg-[var(--bg-sunk)] p-2" data-testid="station-robot-guide">
      {homeGlideDialog}
      <p className="text-xs text-[var(--ink-2)]">{STATION_DE.robotIntro}</p>
      <div className="flex flex-wrap gap-2">
        <Key size="sm" variant="signal" icon="check" onClick={check} disabled={phase !== 'guiding'}>
          {STATION_DE.robotCheckGo}
        </Key>
        <Key size="sm" onClick={relockArm} disabled={phase !== 'guiding'}>
          {STATION_DE.cancel}
        </Key>
      </div>
    </div>
  );
}
