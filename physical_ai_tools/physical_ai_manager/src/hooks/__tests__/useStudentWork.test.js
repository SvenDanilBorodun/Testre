// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// „Deine Arbeit" — the three places its numbers could quietly be wrong.
//
// Each of these was found by reading the API contracts rather than the client,
// and each would have produced a plausible-looking number that misrepresents
// the student's work:
//
//   1. `datasets.episode_count` is NULLABLE, so `|| 0` under-reports episodes
//      with the confidence of a measurement.
//   2. `GET /workflows` returns the caller's workflows PLUS the classroom's
//      templates, so a raw `.length` credits a student with their teacher's.
//   3. A relative date built from an unparseable timestamp renders
//      „Invalid Date" straight into the UI.

import { sumEpisodes, ownWorkflows, relativeDe } from '../useStudentWork';

describe('sumEpisodes — a nullable column', () => {
  it('sums the counts that exist', () => {
    expect(sumEpisodes([{ episode_count: 18 }, { episode_count: 24 }])).toBe(42);
  });

  it('ignores rows without a count instead of reading them as zero', () => {
    // The number stays honest about the datasets it CAN speak for.
    expect(sumEpisodes([{ episode_count: 18 }, { episode_count: null }])).toBe(18);
    expect(sumEpisodes([{ episode_count: 18 }, {}])).toBe(18);
  });

  it('returns null when NOT ONE dataset has a count', () => {
    // The whole point: „—" (unknown) rather than „0" (measured as empty).
    expect(sumEpisodes([{ episode_count: null }, {}])).toBeNull();
    expect(sumEpisodes([])).toBeNull();
    expect(sumEpisodes(null)).toBeNull();
  });

  it('distinguishes a real zero from a missing value', () => {
    expect(sumEpisodes([{ episode_count: 0 }])).toBe(0);
  });

  it('rejects values that are not finite non-negative numbers', () => {
    expect(sumEpisodes([{ episode_count: -3 }, { episode_count: NaN }])).toBeNull();
    expect(sumEpisodes([{ episode_count: '12' }])).toBeNull();
  });
});

describe('ownWorkflows — a list that is broader than "mine"', () => {
  const ME = 'user-1';
  const rows = [
    { id: 'a', owner_user_id: ME, is_template: false, name: 'Turm bauen' },
    { id: 'b', owner_user_id: ME, is_template: true, name: 'Vorlage der Lehrkraft' },
    { id: 'c', owner_user_id: 'teacher-9', is_template: true, name: 'Klassenvorlage' },
    { id: 'd', owner_user_id: 'classmate-2', is_template: false, name: 'Gruppenprogramm' },
  ];

  it('keeps only the student\'s own non-template programs', () => {
    expect(ownWorkflows(rows, ME).map((w) => w.id)).toEqual(['a']);
  });

  it('excludes classroom templates the teacher published', () => {
    // The raw list length is 4; presenting that as „4 Programme" would credit
    // this student with three things they did not make.
    expect(ownWorkflows(rows, ME)).toHaveLength(1);
  });

  it('excludes a template the caller authored themselves', () => {
    // Reachable for a teacher previewing the student app: `is_template` alone
    // and `owner_user_id` alone each let one row through that the other stops.
    expect(ownWorkflows([rows[1]], ME)).toEqual([]);
  });

  it('returns [] rather than everything when the user id is unknown', () => {
    // Fail CLOSED: an un-hydrated session must not briefly show the whole
    // classroom's workflow count as the student's own.
    expect(ownWorkflows(rows, undefined)).toEqual([]);
    expect(ownWorkflows(rows, null)).toEqual([]);
  });

  it('survives a non-array payload', () => {
    expect(ownWorkflows(null, ME)).toEqual([]);
    expect(ownWorkflows({ error: 'x' }, ME)).toEqual([]);
  });
});

describe('relativeDe — coarse German dates', () => {
  const ago = (ms) => new Date(Date.now() - ms).toISOString();
  const MIN = 60_000;
  const HOUR = 60 * MIN;
  const DAY = 24 * HOUR;

  it('reads at a weekly-lesson granularity', () => {
    expect(relativeDe(ago(30 * 1000))).toBe('gerade eben');
    expect(relativeDe(ago(15 * MIN))).toBe('vor 15 Minuten');
    expect(relativeDe(ago(1 * HOUR))).toBe('vor 1 Stunde');
    expect(relativeDe(ago(5 * HOUR))).toBe('vor 5 Stunden');
    expect(relativeDe(ago(1.2 * DAY))).toBe('gestern');
    expect(relativeDe(ago(6 * DAY))).toBe('vor 6 Tagen');
    expect(relativeDe(ago(60 * DAY))).toBe('vor 2 Monaten');
  });

  it('returns empty for anything unparseable rather than „Invalid Date"', () => {
    expect(relativeDe('')).toBe('');
    expect(relativeDe(null)).toBe('');
    expect(relativeDe('not a date')).toBe('');
    expect(relativeDe(undefined)).toBe('');
  });

  it('does not produce a negative age from a clock skew', () => {
    // Server timestamps can land slightly in the future against a classroom
    // PC's clock; „vor -3 Minuten" is worse than a small rounding.
    expect(relativeDe(new Date(Date.now() + 5 * MIN).toISOString())).toBe('gerade eben');
  });
});
