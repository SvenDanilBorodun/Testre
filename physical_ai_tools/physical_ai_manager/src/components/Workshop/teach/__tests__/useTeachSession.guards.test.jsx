/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review round 1 (A2): three guards of the kind contract sit BEHIND
// handlerFor's gate (teachGates.js::teachKeyOffered), so a mutation that
// removed one left every suite green. Each is tested here at the unit that
// owns it, reached past the gate:
//   * capture()'s own `captureOffered` check  → through the engine's test seam;
//   * previewOnRobot's `kind === 'recording'`  → through the public action, with
//     the window's kind changed under a take in review;
//   * no P/Z in a take's row (hand and leader) → the tables' own key sets.

import { act, renderHook } from '@testing-library/react';
import useTeachSession, { createTeachEngine } from '../useTeachSession';

const flush = async () => {
  for (let i = 0; i < 25; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
};

function engineFor(over = {}) {
  const services = {
    capturePose: vi.fn(() => Promise.resolve({ success: true, message: 'ok' })),
    handGuide: vi.fn(() => Promise.resolve({ success: true })),
    recordControl: vi.fn(() => Promise.resolve({ success: true })),
    replayMotion: vi.fn(() => Promise.resolve({ success: true })),
  };
  const props = {
    enabled: true, mode: 'hand', kind: 'recording', heartbeatOk: true, services,
    onCapture: vi.fn(), onError: vi.fn(), ...over,
  };
  const engine = createTeachEngine(() => props, () => {});
  engine.setCaptureNamer((k) => `${k}-1`);
  return { engine, services, props };
}

describe('capture() refuses a kind its window does not offer, even past the key gate', () => {
  test.each([
    ['recording', 'pose'], ['recording', 'ziel'], ['pose', 'ziel'], ['ziel', 'pose'],
  ])('hand mode: a %s window never captures a %s', async (kind, captured) => {
    const { engine, services, props } = engineFor({ kind });
    engine.__testing.capture(captured);
    await flush();
    expect(services.capturePose).not.toHaveBeenCalled();
    expect(props.onCapture).not.toHaveBeenCalled();
  });

  test.each([['recording', 'pose'], ['pose', 'ziel'], ['ziel', 'pose']])(
    'leader mode (bereit): a %s window never captures a %s',
    async (kind, captured) => {
      const { engine, services } = engineFor({ kind, mode: 'leader' });
      engine.__testing.capture(captured);
      await flush();
      expect(services.capturePose).not.toHaveBeenCalled();
    },
  );

  test.each([['pose', 'pose'], ['ziel', 'ziel']])('positive control: a %s window captures a %s', async (kind, captured) => {
    const { engine, services, props } = engineFor({ kind });
    engine.__testing.capture(captured);
    await flush();
    expect(services.capturePose).toHaveBeenCalledTimes(1);
    expect(props.onCapture).toHaveBeenCalledWith(expect.objectContaining({ kind: captured }));
  });
});

describe('a take\'s row handles no P and no Z (D4), in hand AND leader mode', () => {
  test('the tables\' own key sets', () => {
    const { engine } = engineFor();
    const { hand, leader } = engine.__testing.tableKeys();
    expect(hand.aufnahme.sort()).toEqual(['escape', 'f', 'space']);
    expect(leader.aufnahme.sort()).toEqual(['escape', 'space']);
    for (const table of [hand, leader]) {
      for (const st of ['aufnahme', 'pruefen']) {
        expect(table[st]).not.toContain('p');
        expect(table[st]).not.toContain('z');
      }
    }
    // Where a capture belongs, it is there (the positive control).
    expect(hand.fest).toEqual(expect.arrayContaining(['p', 'z']));
    expect(hand.frei).toEqual(expect.arrayContaining(['p', 'z']));
    expect(leader.bereit).toEqual(expect.arrayContaining(['p', 'z']));
  });
});

describe('„Auf dem Roboter ansehen" replays only in a Bewegung window', () => {
  function points(n) {
    return JSON.stringify({ fps: 25, points: Array.from({ length: n }, (_, i) => [0.1 * i, 0, 0, 0, 0, 0.8, 0.04 * i]) });
  }

  async function toReview(kind) {
    vi.useFakeTimers();
    const services = {
      handGuide: vi.fn(() => Promise.resolve({ success: true, message: 'ok' })),
      recordControl: vi.fn((action) => Promise.resolve(action === 'stop'
        ? { success: true, sample_count: 5, duration_s: 0.2, points_json: points(5) }
        : { success: true })),
      capturePose: vi.fn(),
      replayMotion: vi.fn(() => Promise.resolve({ success: true, estimated_duration_s: 1 })),
    };
    let props = {
      enabled: true, mode: 'hand', kind: 'recording', heartbeatOk: true, services,
      onTake: vi.fn(), onError: vi.fn(), onCapture: vi.fn(), onKeep: vi.fn(),
    };
    const view = renderHook((p) => useTeachSession(p), { initialProps: props });
    // Space in fest: the 3-2-1 countdown, then the take starts.
    await act(async () => { view.result.current.actions.space(); await flush(); });
    await act(async () => { vi.advanceTimersByTime(3000); await flush(); });
    expect(view.result.current.state).toBe('aufnahme');
    await act(async () => { view.result.current.actions.space(); await flush(); });
    await act(async () => { vi.advanceTimersByTime(1000); await flush(); });
    expect(view.result.current.state).toBe('pruefen');
    props = { ...props, kind };
    view.rerender(props);
    return { view, services };
  }

  afterEach(() => { vi.useRealTimers(); });

  test.each([['pose'], ['ziel']])('a take in review under a %s window is not replayed', async (kind) => {
    const { view, services } = await toReview(kind);
    await act(async () => { view.result.current.actions.previewOnRobot(); await flush(); });
    expect(services.replayMotion).not.toHaveBeenCalled();
    view.unmount();
  });

  test('positive control: in a Bewegung window it is', async () => {
    const { view, services } = await toReview('recording');
    await act(async () => { view.result.current.actions.previewOnRobot(); await flush(); });
    expect(services.replayMotion).toHaveBeenCalledTimes(1);
    view.unmount();
  });
});
