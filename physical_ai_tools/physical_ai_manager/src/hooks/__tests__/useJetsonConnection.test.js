// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import { planJetsonDiscoveryAction } from '../useJetsonConnection';

// RECONNECT_GRACE_MS is 30_000 in the module under test.
const GRACE_MS = 30_000;
const ME = 'user-uuid';
const ROOM = 'classroom-1';

describe('planJetsonDiscoveryAction (M4 refresh-vs-close)', () => {
  test('server still names us owner → resume (no claim call)', () => {
    expect(
      planJetsonDiscoveryAction({ owner: ME, userId: ME, marker: null, classroomId: ROOM, now: 1000 })
    ).toBe('resume');
  });

  test('a different user owns the lock → busy', () => {
    expect(
      planJetsonDiscoveryAction({ owner: 'someone-else', userId: ME, marker: null, classroomId: ROOM, now: 1000 })
    ).toBe('busy');
  });

  test('free lock, no marker → available (normal idle Jetson)', () => {
    expect(
      planJetsonDiscoveryAction({ owner: null, userId: ME, marker: null, classroomId: ROOM, now: 1000 })
    ).toBe('available');
  });

  test('free lock, fresh marker for this classroom → reclaim (refresh path)', () => {
    const now = 1_000_000;
    const marker = { classroomId: ROOM, at: now - 2_000 };  // 2 s ago
    expect(
      planJetsonDiscoveryAction({ owner: null, userId: ME, marker, classroomId: ROOM, now })
    ).toBe('reclaim');
  });

  test('free lock, marker for a DIFFERENT classroom → available (no cross-room reclaim)', () => {
    const now = 1_000_000;
    const marker = { classroomId: 'other-room', at: now - 2_000 };
    expect(
      planJetsonDiscoveryAction({ owner: null, userId: ME, marker, classroomId: ROOM, now })
    ).toBe('available');
  });

  test('free lock, stale marker (older than the grace window) → available', () => {
    const now = 1_000_000;
    const marker = { classroomId: ROOM, at: now - (GRACE_MS + 1) };
    expect(
      planJetsonDiscoveryAction({ owner: null, userId: ME, marker, classroomId: ROOM, now })
    ).toBe('available');
  });

  test('free lock, marker timestamp in the future (clock skew) → available, not reclaim', () => {
    const now = 1_000_000;
    const marker = { classroomId: ROOM, at: now + 5_000 };
    expect(
      planJetsonDiscoveryAction({ owner: null, userId: ME, marker, classroomId: ROOM, now })
    ).toBe('available');
  });

  test('owner present takes precedence over a stale self-marker', () => {
    // If the server says someone else owns it, we must show busy even if a
    // refresh marker is present.
    const now = 1_000_000;
    const marker = { classroomId: ROOM, at: now - 1_000 };
    expect(
      planJetsonDiscoveryAction({ owner: 'someone-else', userId: ME, marker, classroomId: ROOM, now })
    ).toBe('busy');
  });
});
