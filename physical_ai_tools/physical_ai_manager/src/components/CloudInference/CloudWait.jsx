// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The waiting card of the cloud target: what a student sees while they have to
// wait, calm and honest.
//
//   kind 'gpu'    the cloud GPU starts (the reserve and the page's own retries,
//                 features/cloudInference/controller.js). Usually seconds; in a
//                 GPU shortage it was measured at up to several minutes.
//   kind 'model'  the robot reports INFERENCE_LOADING: it connects and the model
//                 loads on the GPU until the first plan arrives.
//
// What it shows: a small picture (your robot, three dots, the cloud — an
// indeterminate movement, never a bar: nobody knows how far a GPU start has
// come), what is happening, that it is normal, the time waited so far (mm:ss),
// the page's own retry from the second attempt on, and one calm line once the
// wait is longer than usual.
//
// The clock re-renders the card once per second (one setTimeout aligned to the
// second boundary), never per frame and never the page around it. The live
// region holds the sentences only: the clock sits outside it, so a screen
// reader is not told the time every second. Under prefers-reduced-motion the
// picture stands still (a class from matchMedia, and cloudWait.css's media
// query for the same).

import React, { useEffect, useState } from 'react';
import clsx from 'clsx';

import Icon from '../icons/Icon';
import RobotIcon from '../sucher/RobotIcon';
import { cloudCopy, fill } from './cloudCopy';
import './cloudWait.css';

/** From this many seconds on, the GPU start shows its „longer than usual" line. */
export const GPU_LONG_WAIT_S = 60;
/** The model load usually takes 6–16 s; the robot gives it at most 120 s. */
export const MODEL_LONG_WAIT_S = 30;

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

/** Seconds as mm:ss („00:42", „07:41"); anything unusable is „00:00". */
export function formatElapsed(seconds) {
  const total = typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** The sentences of one waiting state, from cloudCopy. Unknown kinds wait for the GPU. */
export function waitTexts(kind) {
  if (kind === 'model') {
    return {
      title: cloudCopy.status.loading,
      sub: cloudCopy.status.loadingSub,
      what: cloudCopy.waiting.modelWhat,
      normal: cloudCopy.waiting.modelNormal,
      long: cloudCopy.waiting.modelLong,
      longAfterS: MODEL_LONG_WAIT_S,
    };
  }
  return {
    title: cloudCopy.panel.preparing,
    sub: cloudCopy.panel.preparingSub,
    what: cloudCopy.waiting.gpuWhat,
    normal: cloudCopy.waiting.gpuNormal,
    long: cloudCopy.waiting.gpuLong,
    longAfterS: GPU_LONG_WAIT_S,
  };
}

function readReducedMotion() {
  try {
    return Boolean(typeof window !== 'undefined' && window.matchMedia && window.matchMedia(REDUCED_MOTION_QUERY).matches);
  } catch {
    return false;
  }
}

/** True while the system asks for less motion; follows a change. */
export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(readReducedMotion);
  useEffect(() => {
    let mq = null;
    try {
      mq = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(REDUCED_MOTION_QUERY) : null;
    } catch {
      mq = null;
    }
    if (!mq) return undefined;
    const onChange = () => setReduced(Boolean(mq.matches));
    onChange();
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', onChange);
      return () => mq.removeEventListener('change', onChange);
    }
    if (typeof mq.addListener === 'function') {
      mq.addListener(onChange);
      return () => mq.removeListener(onChange);
    }
    return undefined;
  }, []);
  return reduced;
}

function secondsSince(origin) {
  return Math.max(0, Math.floor((Date.now() - origin) / 1000));
}

/**
 * Whole seconds since `startedAt` (ms, Date.now() scale), or since the mount
 * when there is none. One timer, re-armed on each second boundary: the value
 * changes once per second and the component re-renders exactly then.
 */
export function useElapsedSeconds(startedAt) {
  const [mountedAt] = useState(() => Date.now());
  const origin = typeof startedAt === 'number' && Number.isFinite(startedAt) ? startedAt : mountedAt;
  const [seconds, setSeconds] = useState(() => secondsSince(origin));
  useEffect(() => {
    let timer = null;
    const tick = () => {
      const ms = Math.max(0, Date.now() - origin);
      setSeconds(Math.floor(ms / 1000));
      timer = setTimeout(tick, 1000 - (ms % 1000));
    };
    tick();
    return () => {
      if (timer !== null) clearTimeout(timer);
    };
  }, [origin]);
  return seconds;
}

function Picture({ robotProfile, reduced }) {
  // The robot's sprite, driven by this card's own reduced-motion state
  // (handed in, so the card keeps one matchMedia listener).
  return (
    <div className="ciw-picture" aria-hidden="true" data-testid="cloud-wait-picture">
      <span className="ciw-node ciw-node-robot">
        <RobotIcon profile={robotProfile} size={40} animate reducedMotion={reduced} />
      </span>
      <span className="ciw-dots">
        <Icon name="dot" size={8} className="ciw-dot" />
        <Icon name="dot" size={8} className="ciw-dot" />
        <Icon name="dot" size={8} className="ciw-dot" />
      </span>
      <span className="ciw-node ciw-node-cloud">
        <span className="ciw-halo" />
        <Icon name="cloud" size={26} />
      </span>
    </div>
  );
}

/**
 * @param {object} p
 * @param {'gpu'|'model'} p.kind
 * @param {'light'|'dark'} [p.tone]   'light' on the white panel, 'dark' over the camera stage
 * @param {?number} [p.startedAt]     ms (Date.now()); the mount when absent
 * @param {number}  [p.attempt]       the page's current attempt (1 = the first)
 * @param {number}  [p.attempts]      attempts in total; without it no retry line
 * @param {?string} [p.robotProfile]  the robot's profile id, for the sprite (Sucher)
 */
export default function CloudWait({
  kind = 'gpu', tone = 'light', startedAt = null, attempt = 1, attempts = null, robotProfile = null,
}) {
  const texts = waitTexts(kind);
  const seconds = useElapsedSeconds(startedAt);
  const reduced = usePrefersReducedMotion();
  const showRetry = kind !== 'model'
    && Number.isInteger(attempt) && attempt >= 2
    && Number.isInteger(attempts) && attempts >= attempt;
  const showLong = seconds >= texts.longAfterS;

  return (
    <div
      className={clsx('ciw', tone === 'dark' && 'ciw-dark', reduced && 'ciw-reduced')}
      data-testid="cloud-wait"
      data-kind={kind === 'model' ? 'model' : 'gpu'}
      data-reduced-motion={reduced ? 'true' : 'false'}
    >
      <Picture robotProfile={robotProfile} reduced={reduced} />
      <div className="ciw-text" role="status" aria-live="polite" aria-atomic="false">
        <div className="ciw-title">{texts.title}</div>
        <div className="ciw-sub">{texts.sub}</div>
        {showRetry && (
          <div className="ciw-retry" data-testid="cloud-wait-retry">
            <Icon name="refresh" size={13} className="ciw-retry-icon" />
            <span>{fill(cloudCopy.waiting.retry, { n: attempt, total: attempts })}</span>
          </div>
        )}
        <div className="ciw-line">{texts.what}</div>
        <div className="ciw-line">{texts.normal}</div>
        {showLong && <div className="ciw-line ciw-long" data-testid="cloud-wait-long">{texts.long}</div>}
      </div>
      <div className="ciw-elapsed">
        <Icon name="hourglass" size={13} />
        <span>{cloudCopy.waiting.elapsed}</span>
        <span className="ciw-clock" data-testid="cloud-wait-elapsed">{formatElapsed(seconds)}</span>
      </div>
    </div>
  );
}
