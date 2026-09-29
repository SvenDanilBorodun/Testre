// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The dataset repo name the recorder will use, computed in the browser.
//
// This is a byte-for-byte twin of the server's
// `data_manager.safe_dataset_task_name` / `safe_dataset_user_id` (owner
// decision Q2). Both sides are pinned by the SAME fixture,
// `__tests__/datasetName.cases.json`, generated from the server reference: the
// page shows the student the exact name the robot will write under and
// predicts the repo id the upload reports on /huggingface/status, so a single
// diverging character would make the finish card wait for an upload that
// never names its repo.
//
// Order is load-bearing (the same as the server): NFC → the German pairs →
// NFKD with every combining mark dropped (é → e, ç → c) → the unsafe-character
// replacement → collapse runs of `-` and of `.` → strip `-` at both ends.
// The `u` flag on UNSAFE is mandatory: without it an astral character (an
// emoji) is TWO UTF-16 code units and becomes two dashes, where Python
// replaces one code point with one dash. `\p{M}` is the same predicate as
// Python's `unicodedata.category(ch).startswith('M')`.

export const DE_TRANSLITERATION = Object.freeze([
  ['ä', 'ae'], ['ö', 'oe'], ['ü', 'ue'], ['Ä', 'Ae'], ['Ö', 'Oe'], ['Ü', 'Ue'], ['ß', 'ss'], ['ẞ', 'SS'],
]);

const UNSAFE = /[^a-zA-Z0-9._-]/gu;
const MARKS = /\p{M}/gu;

/** The task half of the repo name (Q2). May return '' (nothing usable). */
export function safeTaskName(name) {
  let s = String(name ?? '').normalize('NFC');
  for (const [a, b] of DE_TRANSLITERATION) s = s.split(a).join(b);
  s = s.normalize('NFKD').replace(MARKS, '');
  s = s.replace(UNSAFE, '-').replace(/-{2,}/g, '-').replace(/\.{2,}/g, '.');
  return s.replace(/^-+|-+$/g, '');
}

/**
 * The namespace half — HEAD's server rule, unchanged: no transliteration and
 * no collapse, because the server's namespace guard compares this value with
 * the whoami account names verbatim.
 */
export function safeUserId(uid) {
  const s = String(uid ?? '').replace(UNSAFE, '-').replace(/^-+|-+$/g, '');
  return !s || /^\.+$/.test(s) ? 'unknown-user' : s;
}

/** `<user>/<robotType>_<task>` exactly as the server builds `_save_repo_name`. */
export const datasetRepoId = (userId, robotType, taskName) =>
  `${safeUserId(userId)}/${robotType}_${safeTaskName(taskName)}`;

// Hugging Face's repo-name rule (huggingface_hub `validate_repo_id`): 1-96
// characters of [A-Za-z0-9_.-], a word character at both ends, no `--` / `..`,
// no `.git` suffix.
export const HF_REPO_NAME_MAX = 96;
const WORD_CHAR = /^[A-Za-z0-9_]$/;

/**
 * Why Hugging Face would refuse `name` as a repo name, or null when it is
 * acceptable: 'empty' | 'too_long' | 'double' | 'edge' | 'git'.
 */
export function hfRepoNameProblem(name) {
  const s = String(name ?? '');
  if (!s) return 'empty';
  if (s.length > HF_REPO_NAME_MAX) return 'too_long';
  if (s.includes('--') || s.includes('..')) return 'double';
  if (!WORD_CHAR.test(s[0]) || !WORD_CHAR.test(s[s.length - 1])) return 'edge';
  if (s.endsWith('.git')) return 'git';
  return null;
}
