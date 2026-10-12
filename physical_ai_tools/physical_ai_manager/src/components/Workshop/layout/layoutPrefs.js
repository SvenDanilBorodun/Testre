/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The pane sizes of Roboter Studio, remembered on THIS PC: window furniture of
// a shared classroom machine, so every key is MACHINE-scoped
// (utils/sessionScope.js) and survives a sign-out.
//
// One literal-const key and one reader/writer per key, on purpose: the
// storage-coverage scan of utils/__tests__/sessionScope.test.js discovers a
// key only at a `getItem(CONST)` / `setItem(CONST)` call site, so a generic
// `read(key)` helper would make every key invisible to it.
//
// A read never throws (private mode, blocked storage, garbage) and answers the
// default; a write is silent on failure — the in-memory value still holds.

const DOCK_WIDTH_KEY = 'edubotics_workshop_dock_width';
const DOCK_SPLIT_KEY = 'edubotics_workshop_dock_split';
const SIM_WIDTH_KEY = 'edubotics_workshop_sim_width';
const SIM_SPLIT_KEY = 'edubotics_workshop_sim_split';
const SIM_HINT_DISMISSED_KEY = 'edubotics_workshop_sim_hint_dismissed';
const CODE_SIDEBAR_WIDTH_KEY = 'edubotics_code_sidebar_width';
const METHODS_WIDTH_KEY = 'edubotics_code_methods_width';
const METHODS_OPEN_KEY = 'edubotics_code_methods_open';

// The static bounds of each size. A divider narrows them further from the
// space really available (layout/ResizeHandle.jsx `bounds`).
export const DOCK_WIDTH = Object.freeze({ default: 380, min: 280, max: 760 });
export const DOCK_SPLIT = Object.freeze({ default: 0.5, min: 0.18, max: 0.82 });
export const SIM_WIDTH = Object.freeze({ default: 0.55, min: 0.30, max: 0.75 });
export const SIM_SPLIT = Object.freeze({ default: 0.40, min: 0.25, max: 0.80 });
export const CODE_SIDEBAR_WIDTH = Object.freeze({ default: 176, min: 140, max: 360 });
export const METHODS_WIDTH = Object.freeze({ default: 300, min: 220, max: 520 });

function clampToSpec(n, spec) {
  return Math.min(spec.max, Math.max(spec.min, n));
}

// A stored number, clamped into the spec; the default for null, empty or
// non-numeric text.
function parseNumber(raw, spec) {
  if (raw === null || raw === undefined || String(raw).trim() === '') return spec.default;
  const n = Number(raw);
  if (!Number.isFinite(n)) return spec.default;
  return clampToSpec(n, spec);
}

// What is written: a px size as an integer, a fraction with four decimals.
function pxText(n, spec) {
  return String(Math.round(clampToSpec(Number.isFinite(n) ? n : spec.default, spec)));
}
function fractionText(n, spec) {
  return String(+clampToSpec(Number.isFinite(n) ? n : spec.default, spec).toFixed(4));
}

export function readDockWidth() {
  try {
    return parseNumber(window.localStorage.getItem(DOCK_WIDTH_KEY), DOCK_WIDTH);
  } catch (_) {
    return DOCK_WIDTH.default;
  }
}

export function writeDockWidth(px) {
  try {
    window.localStorage.setItem(DOCK_WIDTH_KEY, pxText(px, DOCK_WIDTH));
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

export function readDockSplit() {
  try {
    return parseNumber(window.localStorage.getItem(DOCK_SPLIT_KEY), DOCK_SPLIT);
  } catch (_) {
    return DOCK_SPLIT.default;
  }
}

export function writeDockSplit(fraction) {
  try {
    window.localStorage.setItem(DOCK_SPLIT_KEY, fractionText(fraction, DOCK_SPLIT));
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

export function readSimWidth() {
  try {
    return parseNumber(window.localStorage.getItem(SIM_WIDTH_KEY), SIM_WIDTH);
  } catch (_) {
    return SIM_WIDTH.default;
  }
}

export function writeSimWidth(fraction) {
  try {
    window.localStorage.setItem(SIM_WIDTH_KEY, fractionText(fraction, SIM_WIDTH));
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

export function readSimSplit() {
  try {
    return parseNumber(window.localStorage.getItem(SIM_SPLIT_KEY), SIM_SPLIT);
  } catch (_) {
    return SIM_SPLIT.default;
  }
}

export function writeSimSplit(fraction) {
  try {
    window.localStorage.setItem(SIM_SPLIT_KEY, fractionText(fraction, SIM_SPLIT));
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

/** True once the simulator's „Test im Simulator." notice was closed on this PC. */
export function readSimHintDismissed() {
  try {
    return window.localStorage.getItem(SIM_HINT_DISMISSED_KEY) === '1';
  } catch (_) {
    return false;
  }
}

export function writeSimHintDismissed() {
  try {
    window.localStorage.setItem(SIM_HINT_DISMISSED_KEY, '1');
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

export function readCodeSidebarWidth() {
  try {
    return parseNumber(window.localStorage.getItem(CODE_SIDEBAR_WIDTH_KEY), CODE_SIDEBAR_WIDTH);
  } catch (_) {
    return CODE_SIDEBAR_WIDTH.default;
  }
}

export function writeCodeSidebarWidth(px) {
  try {
    window.localStorage.setItem(CODE_SIDEBAR_WIDTH_KEY, pxText(px, CODE_SIDEBAR_WIDTH));
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

export function readMethodsWidth() {
  try {
    return parseNumber(window.localStorage.getItem(METHODS_WIDTH_KEY), METHODS_WIDTH);
  } catch (_) {
    return METHODS_WIDTH.default;
  }
}

export function writeMethodsWidth(px) {
  try {
    window.localStorage.setItem(METHODS_WIDTH_KEY, pxText(px, METHODS_WIDTH));
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}

/** Whether the Methoden column is open: absent = open, `'0'` = closed. */
export function readMethodsOpen() {
  try {
    return window.localStorage.getItem(METHODS_OPEN_KEY) !== '0';
  } catch (_) {
    return true;
  }
}

export function writeMethodsOpen(open) {
  try {
    window.localStorage.setItem(METHODS_OPEN_KEY, open ? '1' : '0');
  } catch (_) { /* quota / private mode — keep the in-memory value */ }
}
