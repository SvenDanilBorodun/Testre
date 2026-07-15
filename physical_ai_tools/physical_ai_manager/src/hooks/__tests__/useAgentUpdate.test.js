// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Terminal-state contract of the shared pi-agent update driver (audit fix 1):
// every path out of startUpdate/pollUpdate must leave the NON-CLOSABLE
// PiUpdateGate modal with a clickable control. Concretely: `updating` must
// return to false and `failed` must flip true on a 409 (a sibling browser's
// job is in flight — no job_id to poll), on poll-error exhaustion, and on
// 404 exhaustion, so „Ohne Update fortfahren" can appear.

import { renderHook, act } from '@testing-library/react';
import { useAgentUpdate } from '../useAgentUpdate';

function jsonRes(body, { ok = true, status = 200 } = {}) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
}

// Advance the fake clock while draining the poll loop's chained microtasks.
async function advance(ms) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function countStatusPolls() {
  return global.fetch.mock.calls.filter((c) => String(c[0]).includes('/update/status/')).length;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useAgentUpdate — terminal states', () => {
  it('drives a happy-path job to succeeded (updating false, failed false, onSettled)', async () => {
    const statusQueue = [
      { status: 'running', phase: 'images', progress: 40, message: 'Bilder werden geladen …' },
      { status: 'succeeded', message: 'Aktualisierung abgeschlossen.' },
    ];
    global.fetch = vi.fn((url, opts = {}) => {
      const u = String(url);
      if (u.includes('/update/status/')) return jsonRes(statusQueue.shift());
      if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
        return jsonRes({ ok: true, job_id: 'j1', status: 'running' }, { status: 202 });
      }
      return jsonRes({});
    });
    const onSettled = vi.fn();
    const { result } = renderHook(() => useAgentUpdate({ onSettled }));

    await act(async () => {
      await result.current.startUpdate();
    });
    // First poll ran immediately → still running.
    expect(result.current.updating).toBe(true);
    expect(result.current.updateJob.status).toBe('running');

    await advance(2000); // steady cadence → succeeded
    expect(result.current.updating).toBe(false);
    expect(result.current.failed).toBe(false);
    expect(onSettled).toHaveBeenCalledWith(expect.objectContaining({ status: 'succeeded' }));
  });

  it('409 (another browser updating): resets updating and sets failed so the skip appears', async () => {
    global.fetch = vi.fn((url, opts = {}) => {
      const u = String(url);
      if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
        return jsonRes(
          { ok: false, message: 'Eine Aktualisierung läuft bereits — bitte warten.' },
          { ok: false, status: 409 }
        );
      }
      return jsonRes({});
    });
    const { result } = renderHook(() => useAgentUpdate());

    let res;
    await act(async () => {
      res = await result.current.startUpdate();
    });
    expect(res).toEqual({ ok: true, inFlight: true });
    // The modal must not be stuck: button re-enabled + skip route unlocked.
    expect(result.current.updating).toBe(false);
    expect(result.current.failed).toBe(true);
    // The honest in-progress note is surfaced (job still marked running).
    expect(result.current.updateJob.status).toBe('running');
    expect(result.current.updateJob.message).toContain('läuft bereits');
    // No poll was started (the 409 body carries no job_id to poll).
    expect(countStatusPolls()).toBe(0);
  });

  it('poll-error exhaustion: bounded retries, then failed + onSettled', async () => {
    global.fetch = vi.fn((url, opts = {}) => {
      const u = String(url);
      if (u.includes('/update/status/')) return jsonRes({}, { ok: false, status: 502 });
      if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
        return jsonRes({ ok: true, job_id: 'j1', status: 'running' }, { status: 202 });
      }
      return jsonRes({});
    });
    const onSettled = vi.fn();
    const { result } = renderHook(() => useAgentUpdate({ onSettled }));

    await act(async () => {
      await result.current.startUpdate();
    });
    expect(result.current.updating).toBe(true);

    // 8 bounded retries at the 3 s cadence, then give up on the 9th failure.
    await advance(3000 * 12);
    expect(result.current.updating).toBe(false);
    expect(result.current.failed).toBe(true);
    expect(result.current.updateJob.status).toBe('failed');
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onSettled).toHaveBeenCalledWith(expect.objectContaining({ status: 'failed' }));

    // The loop actually stopped: 1 immediate + 8 retries, none after exhaustion.
    const pollsAtExhaustion = countStatusPolls();
    expect(pollsAtExhaustion).toBe(9);
    await advance(30000);
    expect(countStatusPolls()).toBe(pollsAtExhaustion);
  });

  it('network-throw exhaustion behaves like poll-error exhaustion', async () => {
    global.fetch = vi.fn((url, opts = {}) => {
      const u = String(url);
      if (u.includes('/update/status/')) return Promise.reject(new Error('boom'));
      if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
        return jsonRes({ ok: true, job_id: 'j1', status: 'running' }, { status: 202 });
      }
      return jsonRes({});
    });
    const { result } = renderHook(() => useAgentUpdate());
    await act(async () => {
      await result.current.startUpdate();
    });
    await advance(3000 * 12);
    expect(result.current.updating).toBe(false);
    expect(result.current.failed).toBe(true);
  });

  it('404 exhaustion (agent never came back): failed + onSettled unlock the skip route', async () => {
    global.fetch = vi.fn((url, opts = {}) => {
      const u = String(url);
      if (u.includes('/update/status/')) return jsonRes({ ok: false }, { ok: false, status: 404 });
      if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
        return jsonRes({ ok: true, job_id: 'j1', status: 'running' }, { status: 202 });
      }
      return jsonRes({});
    });
    const onSettled = vi.fn();
    const { result } = renderHook(() => useAgentUpdate({ onSettled }));
    await act(async () => {
      await result.current.startUpdate();
    });
    // While retrying, the reconnect note shows (pre-existing behaviour).
    expect(result.current.updateJob.message).toContain('Agent startet neu');

    await advance(3000 * 12);
    expect(result.current.updating).toBe(false);
    expect(result.current.failed).toBe(true);
    expect(result.current.updateJob.status).toBe('failed');
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  it('a healthy tick resets the transient error counter (blips never accumulate)', async () => {
    // Alternate error → running → error → … — with the counter reset, the loop
    // survives far past what 9 CONSECUTIVE failures would allow.
    let call = 0;
    global.fetch = vi.fn((url, opts = {}) => {
      const u = String(url);
      if (u.includes('/update/status/')) {
        call += 1;
        if (call % 2 === 1) return jsonRes({}, { ok: false, status: 502 });
        return jsonRes({ status: 'running', message: 'Aktualisierung läuft …' });
      }
      if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
        return jsonRes({ ok: true, job_id: 'j1', status: 'running' }, { status: 202 });
      }
      return jsonRes({});
    });
    const { result } = renderHook(() => useAgentUpdate());
    await act(async () => {
      await result.current.startUpdate();
    });
    // 30 alternating polls (error→3 s retry, running→2 s steady), past the cap.
    for (let i = 0; i < 30; i += 1) {
      await advance(3000);
    }
    expect(result.current.updating).toBe(true);
    expect(result.current.failed).toBe(false);
  });
});
