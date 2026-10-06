// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The player's frame math (spec §F2), pure. Measured in Chrome (P6): seeking a
// <video> to `i/fps` lands one frame early in 52 of 300 frames, to
// `(i + 0.5)/fps` in none — so every seek aims at the middle of the frame, and
// the shown frame is read back from the presented frame's `mediaTime`.

import COPY from '../datenCopy';
import { fill, fmtTime } from './format';

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

/** The last valid frame index of an episode of `length` frames. */
export function lastFrame(length) {
  return Math.max(0, Math.floor(Number(length) || 0) - 1);
}

/** Clamp a frame index into the episode. */
export function clampFrame(i, length) {
  const v = Math.round(Number(i));
  return clamp(Number.isFinite(v) ? v : 0, 0, lastFrame(length));
}

/** The `currentTime` that shows frame `i` exactly: the middle of the frame. */
export function seekTime(i, fps) {
  return (Number(i) + 0.5) / Number(fps);
}

/** The frame a `requestVideoFrameCallback` metadata `mediaTime` presents. */
export function idxFromMediaTime(mediaTime, fps, length) {
  return clamp(Math.round(Number(mediaTime) * Number(fps)), 0, lastFrame(length));
}

/** The frame a plain `currentTime` shows (the fallback without rVFC). */
export function idxFromCurrentTime(currentTime, fps, length) {
  return clamp(Math.floor(Number(currentTime) * Number(fps) + 1e-3), 0, lastFrame(length));
}

/** One frame (or `d` frames) further, by INDEX — never `currentTime ± 1/fps`. */
export function stepTarget(idx, d, length) {
  return clampFrame(Number(idx) + Number(d), length);
}

/** Umschalt+←/→: `seconds` further. */
export function shiftTarget(idx, seconds, fps, length) {
  return clampFrame(Number(idx) + Math.round(Number(seconds) * Number(fps)), length);
}

/** A scrubber position `frac` ∈ [0, 1] → the frame under it. */
export function scrubTarget(frac, length) {
  return clampFrame(clamp(Number(frac) || 0, 0, 1) * lastFrame(length), length);
}

/** The scrubber fill / cursor fraction of frame `idx`. */
export function frameFraction(idx, length) {
  const last = lastFrame(length);
  return last > 0 ? clamp(Number(idx) / last, 0, 1) : 0;
}

/** „0:03,4 / 0:20,0": the frame's time and the episode's duration. */
export function timeLabel(idx, fps, length) {
  return [fmtTime(Number(idx) / Number(fps)), fmtTime(Number(length) / Number(fps))];
}

/** „Bild 4 / 600" (1-based). */
export function frameLabel(idx, length) {
  return fill(COPY.player.frame, { i: Number(idx) + 1, n: Math.max(0, Number(length) || 0) });
}

/** The scrubber's `aria-valuetext`: „0:03,4 von 0:20,0". */
export function ariaValueText(idx, fps, length) {
  const [cur, total] = timeLabel(idx, fps, length);
  return fill(COPY.player.timelineValue, { cur, total });
}

/** The frame a hint's seconds point at. */
export function frameAt(seconds, fps, length) {
  return clampFrame(Math.floor(Number(seconds) * Number(fps) + 1e-6), length);
}
