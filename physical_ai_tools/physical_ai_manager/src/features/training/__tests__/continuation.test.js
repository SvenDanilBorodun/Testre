// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The pure continuation decision (owner E32–E37, E46).

import { describe, expect, test } from 'vitest';
import { continuationState, CONTINUE_DE } from '../continuation';

const NOW = Date.parse('2026-10-20T12:00:00Z');
const FUTURE = '2026-10-27T12:00:00Z';
const PAST = '2026-10-20T11:59:59Z';

function capped(over = {}) {
  return {
    id: 1,
    status: 'succeeded',
    capped_step: 42500,
    total_steps: 50000,
    continuable_until: FUTURE,
    is_mine: true,
    ...over,
  };
}

const state = (job, jobs = [job], remaining = 5) => continuationState(job, jobs, remaining, NOW).kind;

describe('continuationState', () => {
  test('a plain succeeded job (no cap) is null', () => {
    expect(state({ id: 1, status: 'succeeded', total_steps: 50000, is_mine: true })).toBeNull();
  });

  test('capped_step == total_steps is null (nothing left to continue)', () => {
    expect(state(capped({ capped_step: 50000 }))).toBeNull();
  });

  test('a non-succeeded capped row is null', () => {
    expect(state(capped({ status: 'running' }))).toBeNull();
  });

  test('no continuable_until is null', () => {
    expect(state(capped({ continuable_until: null }))).toBeNull();
  });

  test('is_mine false or absent is not_mine (owner E46)', () => {
    expect(state(capped({ is_mine: false }))).toBe('not_mine');
    expect(state(capped({ is_mine: undefined }))).toBe('not_mine');
  });

  test('an active child is continuing', () => {
    const parent = capped();
    const child = { id: 2, continued_from: 1, status: 'running' };
    expect(continuationState(parent, [parent, child], 5, NOW).kind).toBe('continuing');
    expect(continuationState(parent, [parent, child], 5, NOW).child).toBe(child);
  });

  test('a failed child is NOT continuing', () => {
    const parent = capped();
    const child = { id: 2, continued_from: 1, status: 'failed' };
    expect(continuationState(parent, [parent, child], 5, NOW).kind).toBe('continuable');
  });

  test('a succeeded child is continued', () => {
    const parent = capped();
    const child = { id: 2, continued_from: 1, status: 'succeeded' };
    expect(continuationState(parent, [parent, child], 5, NOW).kind).toBe('continued');
  });

  test('expired past continuable_until', () => {
    expect(state(capped({ continuable_until: PAST }))).toBe('expired');
  });

  test('expiry at the exact millisecond is expired', () => {
    expect(continuationState(capped({ continuable_until: '2026-10-20T12:00:00Z' }), [], 5, NOW).kind).toBe('expired');
  });

  test('remaining 0 is no_credit', () => {
    expect(continuationState(capped(), [capped()], 0, NOW).kind).toBe('no_credit');
  });

  test('otherwise continuable', () => {
    expect(state(capped())).toBe('continuable');
  });

  test('the German copy has real umlauts and no emoji', () => {
    const all = JSON.stringify([CONTINUE_DE.button, CONTINUE_DE.stopped(1, 2), CONTINUE_DE.notMine(1, 2),
      CONTINUE_DE.expired, CONTINUE_DE.continuing, CONTINUE_DE.continued, CONTINUE_DE.noCredit,
      CONTINUE_DE.started, CONTINUE_DE.continuedFrom(42500)]);
    expect(all).toMatch(/ö|ü|ä/);
    expect(all).not.toMatch(/\p{Extended_Pictographic}/u);
    expect(CONTINUE_DE.continuedFrom(42500)).toBe('Fortsetzung ab Schritt 42.500');
  });
});
