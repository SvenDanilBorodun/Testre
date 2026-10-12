/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The toolbar's save status: what the CLOUD holds of the open document (R0-2).
// `saveStatus` is the page's `{ savedAt, dirty }`: `savedAt` the time of the
// last successful save (or the opened row's `updated_at`), `dirty` whether the
// document or its simulator scene differs from what that save sent. The local
// crash-recovery autosave is never shown here. `loading` (the page is still
// fetching the program the student picked) wins over both: until that row has
// opened, nothing about the previous program or a save time is true.

export const SAVE_STATUS_DE = Object.freeze({
  SAVED: 'Gespeichert: %1',
  JUST_NOW: 'gerade eben',
  MINUTES: 'vor %1 Min.',
  HOURS: 'vor %1 Std.',
  DATE: 'am %1',
  DIRTY: 'Nicht gespeichert',
  NEVER: 'Noch nicht gespeichert',
  LOADING: 'Wird geladen …',
});

const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

function fill(template, value) {
  return template.split('%1').join(String(value));
}

/** The German status line for `saveStatus` at the time `now` (ms). */
export function saveStatusLabel(saveStatus, now) {
  const status = saveStatus || {};
  if (status.loading) return SAVE_STATUS_DE.LOADING;
  if (status.dirty) return SAVE_STATUS_DE.DIRTY;
  const savedAt = Number(status.savedAt);
  if (!status.savedAt || !Number.isFinite(savedAt)) return SAVE_STATUS_DE.NEVER;
  const age = now - savedAt;
  let when;
  if (age < MINUTE_MS) {
    when = SAVE_STATUS_DE.JUST_NOW;
  } else if (age < HOUR_MS) {
    when = fill(SAVE_STATUS_DE.MINUTES, Math.max(1, Math.floor(age / MINUTE_MS)));
  } else if (age < DAY_MS) {
    when = fill(SAVE_STATUS_DE.HOURS, Math.floor(age / HOUR_MS));
  } else {
    when = fill(SAVE_STATUS_DE.DATE, new Date(savedAt).toLocaleDateString('de-DE', {
      day: '2-digit', month: '2-digit', year: 'numeric',
    }));
  }
  return fill(SAVE_STATUS_DE.SAVED, when);
}
