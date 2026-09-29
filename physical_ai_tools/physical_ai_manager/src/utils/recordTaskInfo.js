// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// What the Aufnahme page sends, what it refuses to send, and how long the
// student's plan takes (spec §3.8).
//
// H7: taskSlice hydrates only `userId`, and the /task/status ADOPT path writes
// `useOptimizedSave`, `recordRosBag2` and multi-task instructions back into the
// form from whatever the robot last ran. The page no longer offers any of those
// switches, so every RECORD-page command forces them at SEND time instead of
// trusting the store.

import { hfRepoNameProblem, safeTaskName } from './datasetName';

/** Stepper limits (owner decision F6e): [min, max], whole numbers. */
export const STEPPER_LIMITS = Object.freeze({
  warmupTime: Object.freeze([0, 60]),
  episodeTime: Object.freeze([3, 120]),
  resetTime: Object.freeze([0, 60]),
  numEpisodes: Object.freeze([1, 100]),
});

export const TASK_NAME_MAX = 60;
export const FPS_LIMITS = Object.freeze([1, 60]);

/** The validation sentences, one per rule (spec §3.8, German UI). */
export const VALIDATION_DE = Object.freeze({
  nameEmpty: 'Bitte gib einen Aufgabennamen ein.',
  nameTooLong: `Der Aufgabenname darf höchstens ${TASK_NAME_MAX} Zeichen lang sein.`,
  nameNoChars: 'Der Aufgabenname braucht mindestens einen Buchstaben oder eine Ziffer (a–z, 0–9).',
  nameHf: 'Aus diesem Aufgabennamen wird kein gültiger Hugging-Face-Name (zu lang oder mit einem '
    + 'Punkt am Anfang oder Ende). Ändere den Namen oder schalte unter „Erweitert“ das Hochladen aus.',
  instructionEmpty: 'Bitte gib eine Aufgabenanweisung ein.',
  userId: 'Bitte wähle unter „Erweitert“ eine Benutzer-ID.',
  fps: '„Bilder pro Sekunde“ muss zwischen 1 und 60 liegen.',
  times: 'Bitte prüfe die Zeiten unter „Ablauf“.',
});

/** The first instruction that is not blank, trimmed — or '' when none. */
export function firstInstruction(taskInstruction) {
  const list = Array.isArray(taskInstruction) ? taskInstruction : [taskInstruction];
  for (const item of list) {
    const text = String(item ?? '').trim();
    if (text) return text;
  }
  return '';
}

/** The task_info every RECORD-page command carries (H7). Never mutates. */
export function forceRecordTaskInfo(taskInfo) {
  const first = firstInstruction(taskInfo?.taskInstruction);
  return {
    ...taskInfo,
    taskInstruction: first ? [first] : [],
    useOptimizedSave: true,
    recordRosBag2: false,
    recordInferenceMode: false,
  };
}

// A whole number or NaN. '' / null / undefined are NOT zero here: an emptied
// field is a mistake to report, not a 0-s phase.
function toWholeNumber(value) {
  if (typeof value === 'number') return Number.isInteger(value) ? value : NaN;
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value);
    return Number.isInteger(n) ? n : NaN;
  }
  return NaN;
}

function inRange(value, [lo, hi]) {
  const n = toWholeNumber(value);
  return Number.isInteger(n) && n >= lo && n <= hi;
}

/**
 * The first reason the form cannot start a recording, or null.
 * @returns {null | {field: string, messageDe: string}}
 */
export function validateRecordTaskInfo(taskInfo, { robotType } = {}) {
  const info = taskInfo || {};
  const name = String(info.taskName ?? '');
  if (!name.trim()) return { field: 'taskName', messageDe: VALIDATION_DE.nameEmpty };
  if (name.length > TASK_NAME_MAX) return { field: 'taskName', messageDe: VALIDATION_DE.nameTooLong };
  const safe = safeTaskName(name);
  if (!safe) return { field: 'taskName', messageDe: VALIDATION_DE.nameNoChars };
  if (info.pushToHub && hfRepoNameProblem(`${robotType || ''}_${safe}`)) {
    return { field: 'taskName', messageDe: VALIDATION_DE.nameHf };
  }
  if (!firstInstruction(info.taskInstruction)) {
    return { field: 'taskInstruction', messageDe: VALIDATION_DE.instructionEmpty };
  }
  if (!info.userId) return { field: 'userId', messageDe: VALIDATION_DE.userId };
  if (!inRange(info.fps, FPS_LIMITS)) return { field: 'fps', messageDe: VALIDATION_DE.fps };
  for (const [field, limits] of Object.entries(STEPPER_LIMITS)) {
    if (!inRange(info[field], limits)) return { field, messageDe: VALIDATION_DE.times };
  }
  return null;
}

const safeSeconds = (v) => {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : 0;
};

export const ESTIMATE_BAR_MAX_EPISODES = 30;

/**
 * The plan's length: warm-up once, N episodes, a reset between episodes and
 * none after the last. `parts` feeds the estimate bar (`flex: seconds`),
 * zero-length parts dropped, at most 30 episodes drawn.
 * @returns {{totalS: number, parts: Array<[number, string]>}}
 */
export function estimateRecording({ warmupTime, episodeTime, resetTime, numEpisodes } = {}) {
  const w = safeSeconds(warmupTime);
  const e = safeSeconds(episodeTime);
  const z = safeSeconds(resetTime);
  const n = Math.max(0, Math.floor(safeSeconds(numEpisodes)));
  const totalS = w + n * e + Math.max(0, n - 1) * z;
  const parts = [[w, 'var(--rec-warm)']];
  for (let i = 0; i < n && i < ESTIMATE_BAR_MAX_EPISODES; i += 1) {
    parts.push([e, 'var(--rec-run)']);
    if (i < n - 1) parts.push([z, 'var(--rec-reset)']);
  }
  return { totalS, parts: parts.filter(([seconds]) => seconds > 0) };
}

/** `m:ss` from seconds (rounded), e.g. 125 → '2:05'. */
export function formatMinSec(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
