/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { describe, it, expect, vi } from 'vitest';
import {
  createSammlungProvider,
  DEFAULT_SAMMLUNG_SNAPSHOT,
  EMPTY_SAMMLUNG_PROVIDER,
} from '../provider';

describe('Sammlung provider', () => {
  it('starts from the defaults and merges capabilities/trajectories one level', () => {
    const p = createSammlungProvider({ capabilities: { hardware: true } });
    expect(p.getSnapshot().capabilities).toEqual({
      ...DEFAULT_SAMMLUNG_SNAPSHOT.capabilities, hardware: true,
    });
    p.setSnapshot({ capabilities: { simMode: true }, trajectories: { status: 'loading' } });
    const snap = p.getSnapshot();
    expect(snap.capabilities.hardware).toBe(true);
    expect(snap.capabilities.simMode).toBe(true);
    expect(snap.trajectories).toEqual({ status: 'loading', items: [] });
    // Other keys are replaced, not merged.
    p.setSnapshot({ lastPreviewResult: { a: 1 } });
    p.setSnapshot({ lastPreviewResult: { b: 2 } });
    expect(p.getSnapshot().lastPreviewResult).toEqual({ b: 2 });
    p.setSnapshot({ restrictedBlocks: ['edubotics_home'] });
    expect(p.getSnapshot().restrictedBlocks).toEqual(['edubotics_home']);
    expect(DEFAULT_SAMMLUNG_SNAPSHOT.capabilities.hardware).toBe(false);
  });

  it('notifies subscribers synchronously and stops after unsubscribe', () => {
    const p = createSammlungProvider();
    const fn = vi.fn();
    const off = p.subscribe(fn);
    p.setSnapshot({ robotType: 'omx_f' });
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0][0].robotType).toBe('omx_f');
    off();
    p.setSnapshot({ robotType: 'edu6_studio' });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('dispatches to the current action handler; none is a no-op', () => {
    const p = createSammlungProvider();
    expect(() => p.dispatchAction({ type: 'manage' })).not.toThrow();
    const first = vi.fn();
    const second = vi.fn();
    p.setActionHandler(first);
    p.dispatchAction({ type: 'teach' });
    p.setActionHandler(second);
    p.dispatchAction({ type: 'pinCamera' });
    expect(first).toHaveBeenCalledWith({ type: 'teach' });
    expect(second).toHaveBeenCalledWith({ type: 'pinCamera' });
    expect(first).toHaveBeenCalledTimes(1);
  });

  it('the empty provider offers nothing and never throws', () => {
    expect(Object.isFrozen(EMPTY_SAMMLUNG_PROVIDER)).toBe(true);
    expect(EMPTY_SAMMLUNG_PROVIDER.getSnapshot().capabilities.hardware).toBe(false);
    expect(EMPTY_SAMMLUNG_PROVIDER.getSnapshot().trajectories.status).toBe('none');
    expect(() => EMPTY_SAMMLUNG_PROVIDER.dispatchAction({ type: 'teach' })).not.toThrow();
    expect(typeof EMPTY_SAMMLUNG_PROVIDER.subscribe(() => {})).toBe('function');
  });
});
