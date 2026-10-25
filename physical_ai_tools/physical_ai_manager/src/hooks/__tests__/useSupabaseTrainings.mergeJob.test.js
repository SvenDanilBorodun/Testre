// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// mergeJob keeps an API row's is_mine when a Realtime / direct-poll row without
// the key arrives; a row first seen through Realtime has is_mine undefined
// (fail-safe: the „Fortsetzen" key stays hidden until the next API list).

import { describe, expect, test } from 'vitest';
import { mergeJob, LIVE_COLUMNS } from '../useSupabaseTrainings';

describe('mergeJob and is_mine', () => {
  test('a LIVE_COLUMNS row merged into an API row with is_mine:true keeps it', () => {
    const fromApi = { id: 1, is_mine: true, status: 'succeeded', requested_at: '2026-10-20T10:00:00Z' };
    const fromRealtime = { id: 1, status: 'succeeded', current_step: 42500, requested_at: '2026-10-20T10:00:00Z' };
    const merged = mergeJob([fromApi], fromRealtime);
    expect(merged.find((j) => j.id === 1).is_mine).toBe(true);
    expect(merged.find((j) => j.id === 1).current_step).toBe(42500);
  });

  test('a row first seen through Realtime has no is_mine', () => {
    const merged = mergeJob([], { id: 2, status: 'running', requested_at: '2026-10-20T10:00:00Z' });
    expect(merged.find((j) => j.id === 2).is_mine).toBeUndefined();
  });

  test('LIVE_COLUMNS selects the three continuation columns (granted by 050)', () => {
    for (const col of ['capped_step', 'continuable_until', 'continued_from']) {
      expect(LIVE_COLUMNS.split(',')).toContain(col);
    }
  });
});
