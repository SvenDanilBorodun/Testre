/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The arm check's part of the calibration panel (spec 40 S4, S6 5., S8): the
// arm moves on its own, so it starts only from ONE deliberate press, after the
// preparation lines and a 5 s countdown that „Stopp" ends without a call. For
// everyone who can see it (D13: a student too). While it runs: „Pose i von n"
// and „Stopp" (the caller's /workflow/stop: nothing is written, the arm stays
// where it is). The arm check starts only at the Grundstellung (X-TARGETSTART),
// so the preparation offers „Arm in die Grundstellung fahren" beside it (step
// `home`, no countdown: it is the robot's own glide home, refused while a target
// is seen), and „Stopp" while that glide — or any program — runs.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Icon from '../../icons/Icon';
import { CALIBRATION_DE, fillDe } from './calibrationCopy';
import { VIEWS } from './calibrationModel';

export const ARM_CHECK_COUNTDOWN_S = 5;

const PRIMARY = 'inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border '
  + 'bg-[var(--accent)] text-white border-[var(--accent)] hover:opacity-90 '
  + 'disabled:opacity-50 disabled:cursor-not-allowed';
const STOP = 'inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border '
  + 'bg-red-600 text-white border-red-600 hover:bg-red-700 disabled:opacity-50';
const SECONDARY = 'inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border '
  + 'border-[var(--line)] bg-white text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] '
  + 'disabled:opacity-50 disabled:cursor-not-allowed';

/**
 * @param {object} props
 * @param {object} props.view       calibrationView(state)
 * @param {?string} props.blockedReason  why a start is not possible now (German), or null
 * @param {function} props.onStart  starts the arm check (after the countdown)
 * @param {function} props.onStop   stops the running arm check
 * @param {function} [props.onCountdownChange]  told true/false while the countdown runs
 * @param {function} [props.onHome]  sends step `home` (the glide to the Grundstellung)
 * @param {boolean} [props.programRunning]  a program runs (the glide home among them)
 * @param {function} [props.onStopProgram]  stops it (/workflow/stop)
 */
function ArmCheckView({
  view, blockedReason = null, onStart, onStop, onCountdownChange = null, onHome = null, programRunning = false,
  onStopProgram = null,
}) {
  const [secondsLeft, setSecondsLeft] = useState(null);
  const timerRef = useRef(null);
  const onStartRef = useRef(onStart);
  onStartRef.current = onStart;

  const clear = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    setSecondsLeft(null);
  }, []);

  // A countdown never outlives the panel, nor a state that no longer offers it.
  useEffect(() => clear, [clear]);
  const offered = view.canStartArmCheck && !blockedReason;
  useEffect(() => {
    if (!offered && timerRef.current) clear();
  }, [offered, clear]);
  useEffect(() => {
    if (onCountdownChange) onCountdownChange(secondsLeft !== null);
  }, [secondsLeft, onCountdownChange]);

  const begin = () => {
    if (timerRef.current || !offered) return;
    let left = ARM_CHECK_COUNTDOWN_S;
    setSecondsLeft(left);
    timerRef.current = setInterval(() => {
      left -= 1;
      if (left > 0) {
        setSecondsLeft(left);
        return;
      }
      clearInterval(timerRef.current);
      timerRef.current = null;
      setSecondsLeft(null);
      onStartRef.current();
    }, 1000);
  };

  if (view.view === VIEWS.ARM_CHECK_RUNNING) {
    const p = view.progress;
    return (
      <div className="flex flex-col gap-3">
        {p && (
          <div className="flex flex-col gap-1">
            <div
              role="progressbar"
              aria-label={CALIBRATION_DE.ARM_CHECK_PROGRESS}
              aria-valuemin={0}
              aria-valuemax={p.poses}
              aria-valuenow={p.pose}
              className="h-2 w-full overflow-hidden rounded-full bg-[var(--bg-sunk)]"
            >
              <div
                className="h-full bg-[var(--accent)] transition-[width]"
                style={{ width: `${Math.round((100 * p.pose) / p.poses)}%` }}
              />
            </div>
            <span className="text-xs text-[var(--ink-3)]">
              {fillDe(CALIBRATION_DE.CAPTURED, { k: p.captured })}
            </span>
          </div>
        )}
        <div>
          <button type="button" className={STOP} onClick={onStop}>
            <Icon name="stop" />
            {CALIBRATION_DE.STOP}
          </button>
        </div>
      </div>
    );
  }

  if (!view.canStartArmCheck) return null;

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-medium text-[var(--ink)]">{CALIBRATION_DE.ARM_CHECK_PREP_TITLE}</p>
        <ul className="mt-1 list-disc pl-5 text-sm text-[var(--ink-3)]">
          {CALIBRATION_DE.ARM_CHECK_PREP.map((line) => <li key={line}>{line}</li>)}
        </ul>
      </div>
      {secondsLeft !== null ? (
        <div className="flex flex-wrap items-center gap-3" role="status">
          <span className="text-sm font-semibold text-[var(--ink)]">
            {fillDe(CALIBRATION_DE.COUNTDOWN, { s: secondsLeft })}
          </span>
          <button type="button" className={STOP} onClick={clear}>
            <Icon name="stop" />
            {CALIBRATION_DE.STOP}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className={PRIMARY}
            onClick={begin}
            disabled={!!blockedReason}
            title={blockedReason || undefined}
          >
            <Icon name="play" />
            {CALIBRATION_DE.ARM_CHECK_START}
          </button>
          {onHome && (
            <button
              type="button"
              className={SECONDARY}
              onClick={onHome}
              disabled={!!blockedReason}
              title={blockedReason || undefined}
            >
              <Icon name="home" />
              {CALIBRATION_DE.ARM_CHECK_HOME}
            </button>
          )}
          {programRunning && onStopProgram && (
            <button type="button" className={STOP} onClick={onStopProgram}>
              <Icon name="stop" />
              {CALIBRATION_DE.STOP}
            </button>
          )}
          {blockedReason && <span className="text-xs text-[var(--ink-3)]">{blockedReason}</span>}
        </div>
      )}
    </div>
  );
}

export default ArmCheckView;
