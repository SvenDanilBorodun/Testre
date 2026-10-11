/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { SAVE_STATUS_DE, saveStatusLabel } from '../saveStatus';

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0);
const MIN = 60 * 1000;

describe('saveStatusLabel (R0-2)', () => {
  test('unsaved changes win over every save time', () => {
    expect(saveStatusLabel({ savedAt: NOW, dirty: true }, NOW)).toBe('Nicht gespeichert');
    expect(saveStatusLabel({ savedAt: null, dirty: true }, NOW)).toBe('Nicht gespeichert');
  });

  test('never saved and untouched: „Noch nicht gespeichert"', () => {
    expect(saveStatusLabel({ savedAt: null, dirty: false }, NOW)).toBe('Noch nicht gespeichert');
    expect(saveStatusLabel(undefined, NOW)).toBe('Noch nicht gespeichert');
    expect(saveStatusLabel({ savedAt: 'kaputt', dirty: false }, NOW)).toBe('Noch nicht gespeichert');
  });

  test('under a minute: „gerade eben" (a clock slightly ahead counts too)', () => {
    expect(saveStatusLabel({ savedAt: NOW, dirty: false }, NOW)).toBe('Gespeichert: gerade eben');
    expect(saveStatusLabel({ savedAt: NOW - 59 * 1000, dirty: false }, NOW)).toBe('Gespeichert: gerade eben');
    expect(saveStatusLabel({ savedAt: NOW + 5000, dirty: false }, NOW)).toBe('Gespeichert: gerade eben');
  });

  test('minutes, floored, at least one', () => {
    expect(saveStatusLabel({ savedAt: NOW - MIN, dirty: false }, NOW)).toBe('Gespeichert: vor 1 Min.');
    expect(saveStatusLabel({ savedAt: NOW - 59.9 * MIN, dirty: false }, NOW)).toBe('Gespeichert: vor 59 Min.');
  });

  test('hours below a day', () => {
    expect(saveStatusLabel({ savedAt: NOW - 60 * MIN, dirty: false }, NOW)).toBe('Gespeichert: vor 1 Std.');
    expect(saveStatusLabel({ savedAt: NOW - 23.9 * 60 * MIN, dirty: false }, NOW)).toBe('Gespeichert: vor 23 Std.');
  });

  test('a day or more: the date as TT.MM.JJJJ', () => {
    const savedAt = new Date(2026, 8, 3, 10, 0, 0).getTime();
    expect(saveStatusLabel({ savedAt, dirty: false }, savedAt + 48 * 60 * MIN)).toBe('Gespeichert: am 03.09.2026');
  });

  test('the strings', () => {
    expect(SAVE_STATUS_DE).toEqual({
      SAVED: 'Gespeichert: %1',
      JUST_NOW: 'gerade eben',
      MINUTES: 'vor %1 Min.',
      HOURS: 'vor %1 Std.',
      DATE: 'am %1',
      DIRTY: 'Nicht gespeichert',
      NEVER: 'Noch nicht gespeichert',
    });
  });
});
