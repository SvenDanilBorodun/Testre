// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Send-time values, validation and the time estimate of the Aufnahme page
// (spec §3.8, F6e, H7).

import {
  STEPPER_LIMITS,
  TASK_NAME_MAX,
  VALIDATION_DE,
  estimateRecording,
  forceRecordTaskInfo,
  formatMinSec,
  validateRecordTaskInfo,
} from '../recordTaskInfo';

const VALID = Object.freeze({
  taskName: 'Würfel in die Schale',
  taskInstruction: ['Greife den roten Würfel.'],
  userId: 'schule-A',
  fps: 30,
  warmupTime: 5,
  episodeTime: 20,
  resetTime: 5,
  numEpisodes: 5,
  pushToHub: true,
  privateMode: true,
  tags: ['omx_f'],
});

const check = (patch) => validateRecordTaskInfo({ ...VALID, ...patch }, { robotType: 'omx_f' });

describe('forceRecordTaskInfo (H7: the values the page no longer offers)', () => {
  it('forces optimized save on, rosbag2 and inference-recording off', () => {
    const out = forceRecordTaskInfo({
      ...VALID, useOptimizedSave: false, recordRosBag2: true, recordInferenceMode: true,
    });
    expect(out.useOptimizedSave).toBe(true);
    expect(out.recordRosBag2).toBe(false);
    expect(out.recordInferenceMode).toBe(false);
    expect(out.taskName).toBe(VALID.taskName);
  });

  it('keeps only the first non-empty instruction, trimmed', () => {
    expect(forceRecordTaskInfo({ taskInstruction: ['  ', '  Greife  ', 'zweite'] }).taskInstruction)
      .toEqual(['Greife']);
    expect(forceRecordTaskInfo({ taskInstruction: ['', '   '] }).taskInstruction).toEqual([]);
    expect(forceRecordTaskInfo({}).taskInstruction).toEqual([]);
  });

  it('does not mutate its input', () => {
    const input = { ...VALID, taskInstruction: [' a ', 'b'] };
    forceRecordTaskInfo(input);
    expect(input.taskInstruction).toEqual([' a ', 'b']);
  });
});

describe('validateRecordTaskInfo', () => {
  it('accepts a complete form, and 0-s warm-up / reset', () => {
    expect(check({})).toBeNull();
    expect(check({ warmupTime: 0, resetTime: 0 })).toBeNull();
    expect(check({ taskName: 'x'.repeat(TASK_NAME_MAX) })).toBeNull();
  });

  it.each([
    [{ taskName: '' }, 'taskName', VALIDATION_DE.nameEmpty],
    [{ taskName: '   ' }, 'taskName', VALIDATION_DE.nameEmpty],
    [{ taskName: 'x'.repeat(61) }, 'taskName', VALIDATION_DE.nameTooLong],
    [{ taskName: '日本語' }, 'taskName', VALIDATION_DE.nameNoChars],
    [{ taskName: 'a.' }, 'taskName', VALIDATION_DE.nameHf],
    [{ taskInstruction: [] }, 'taskInstruction', VALIDATION_DE.instructionEmpty],
    [{ taskInstruction: ['  '] }, 'taskInstruction', VALIDATION_DE.instructionEmpty],
    [{ userId: undefined }, 'userId', VALIDATION_DE.userId],
    [{ userId: '' }, 'userId', VALIDATION_DE.userId],
    [{ fps: 0 }, 'fps', VALIDATION_DE.fps],
    [{ fps: 61 }, 'fps', VALIDATION_DE.fps],
    [{ fps: 29.5 }, 'fps', VALIDATION_DE.fps],
    [{ warmupTime: 61 }, 'warmupTime', VALIDATION_DE.times],
    [{ warmupTime: -1 }, 'warmupTime', VALIDATION_DE.times],
    [{ episodeTime: 2 }, 'episodeTime', VALIDATION_DE.times],
    [{ episodeTime: 121 }, 'episodeTime', VALIDATION_DE.times],
    [{ episodeTime: 2.5 }, 'episodeTime', VALIDATION_DE.times],
    [{ resetTime: 61 }, 'resetTime', VALIDATION_DE.times],
    [{ numEpisodes: 0 }, 'numEpisodes', VALIDATION_DE.times],
    [{ numEpisodes: 101 }, 'numEpisodes', VALIDATION_DE.times],
    [{ numEpisodes: '' }, 'numEpisodes', VALIDATION_DE.times],
  ])('%j → %s', (patch, field, messageDe) => {
    expect(check(patch)).toEqual({ field, messageDe });
  });

  it('checks the HF name only when the dataset is uploaded', () => {
    expect(check({ taskName: 'a.', pushToHub: false })).toBeNull();
    // the robot type prefix counts towards the 96 characters
    const long = 'ä'.repeat(46); // → 92 characters after transliteration, 98 with the prefix
    expect(check({ taskName: long })).toEqual({ field: 'taskName', messageDe: VALIDATION_DE.nameHf });
    expect(check({ taskName: long, pushToHub: false })).toBeNull();
  });

  it('reports the rules in the spec order', () => {
    expect(check({ taskName: '', taskInstruction: [], userId: '' }).field).toBe('taskName');
    expect(check({ taskInstruction: [], userId: '' }).field).toBe('taskInstruction');
    expect(check({ userId: '', fps: 0 }).field).toBe('userId');
    expect(check({ fps: 0, episodeTime: 1 }).field).toBe('fps');
  });

  it('pins the German sentences', () => {
    expect(VALIDATION_DE).toEqual({
      nameEmpty: 'Bitte gib einen Aufgabennamen ein.',
      nameTooLong: 'Der Aufgabenname darf höchstens 60 Zeichen lang sein.',
      nameNoChars: 'Der Aufgabenname braucht mindestens einen Buchstaben oder eine Ziffer (a–z, 0–9).',
      nameHf: 'Aus diesem Aufgabennamen wird kein gültiger Hugging-Face-Name (zu lang oder mit einem '
        + 'Punkt am Anfang oder Ende). Ändere den Namen oder schalte unter „Erweitert“ das Hochladen aus.',
      instructionEmpty: 'Bitte gib eine Aufgabenanweisung ein.',
      userId: 'Bitte wähle unter „Erweitert“ eine Benutzer-ID.',
      fps: '„Bilder pro Sekunde“ muss zwischen 1 und 60 liegen.',
      times: 'Bitte prüfe die Zeiten unter „Ablauf“.',
    });
    expect(STEPPER_LIMITS).toEqual({
      warmupTime: [0, 60], episodeTime: [3, 120], resetTime: [0, 60], numEpisodes: [1, 100],
    });
    expect(TASK_NAME_MAX).toBe(60);
  });
});

describe('estimateRecording', () => {
  it('W=5, E=20, Z=5, N=5 → 2:05', () => {
    const e = estimateRecording({ warmupTime: 5, episodeTime: 20, resetTime: 5, numEpisodes: 5 });
    expect(e.totalS).toBe(125);
    expect(formatMinSec(e.totalS)).toBe('2:05');
    expect(e.parts).toEqual([
      [5, 'var(--rec-warm)'],
      [20, 'var(--rec-run)'], [5, 'var(--rec-reset)'],
      [20, 'var(--rec-run)'], [5, 'var(--rec-reset)'],
      [20, 'var(--rec-run)'], [5, 'var(--rec-reset)'],
      [20, 'var(--rec-run)'], [5, 'var(--rec-reset)'],
      [20, 'var(--rec-run)'],
    ]);
  });

  it('drops zero parts and caps the bar at 30 episodes', () => {
    const e = estimateRecording({ warmupTime: 0, episodeTime: 10, resetTime: 0, numEpisodes: 100 });
    expect(e.totalS).toBe(1000);
    expect(e.parts).toHaveLength(30);
    expect(e.parts.every(([s]) => s > 0)).toBe(true);
  });

  it('one episode has no reset', () => {
    const e = estimateRecording({ warmupTime: 3, episodeTime: 10, resetTime: 7, numEpisodes: 1 });
    expect(e.totalS).toBe(13);
    expect(e.parts).toEqual([[3, 'var(--rec-warm)'], [10, 'var(--rec-run)']]);
  });

  it('treats garbage as 0 instead of NaN', () => {
    const e = estimateRecording({ warmupTime: 'x', episodeTime: 10, resetTime: undefined, numEpisodes: 2 });
    expect(e.totalS).toBe(20);
  });
});

describe('formatMinSec', () => {
  it.each([[0, '0:00'], [5, '0:05'], [65, '1:05'], [125, '2:05'], [3600, '60:00'], [59.6, '1:00']])(
    '%s → %s', (s, out) => expect(formatMinSec(s)).toBe(out),
  );
});
