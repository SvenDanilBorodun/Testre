// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// A smooth phase clock from a coarse feed (spec §3.2).
//
// The recorder reports `proceed_time` as a FLOORED whole second, 30 times a
// second. The anchor remembers where the current (phase, episode) instance
// stood and when that value first arrived; `elapsedAt` runs the clock on from
// there on the browser's own clock, bounded so it can neither overtake the
// next server second nor run while the feed is frozen. Pure; both clocks are
// `performance.now()` readings.

export const STALE_HOLD_MS = 1500;

/** The anchor for `status`; `prev` itself when nothing it records changed. */
export function nextPhaseAnchor(prev, status) {
  const phase = status?.phase ?? 0;
  const episode = status?.currentEpisodeNumber ?? 0;
  const value = Number(status?.proceedTime) || 0;
  const total = Number(status?.totalTime) || 0;
  const key = `${phase}:${episode}`;
  const at = status?.receivedAt ?? null;
  if (prev && prev.key === key && value >= prev.value) {
    if (value === prev.value && total === prev.total) return prev;
    return { key, instance: prev.instance, phase, episode, value, total, at };
  }
  return { key, instance: (prev?.instance ?? 0) + 1, phase, episode, value, total, at };
}

/**
 * Seconds elapsed in the anchored phase at `nowMs`. `lastTickAt` is the arrival
 * of the most recent status of ANY kind: past it + STALE_HOLD_MS the clock
 * stops, so a frozen feed never shows a phase running out on its own.
 */
export function elapsedAt(anchor, nowMs, lastTickAt) {
  if (!anchor) return 0;
  if (anchor.at === null || anchor.at === undefined) return anchor.value;
  const t = Math.min(nowMs, (lastTickAt ?? nowMs) + STALE_HOLD_MS);
  const raw = anchor.value + Math.max(0, t - anchor.at) / 1000;
  const cap = anchor.total > 0 ? anchor.total : Infinity;
  return Math.min(raw, anchor.value + 0.999, cap);
}
