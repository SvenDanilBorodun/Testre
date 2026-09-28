/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Ziele store of a CODE document (no Blockly workspace): the same
// DestinationStore, detached, created ONCE per document by WorkshopPage and
// preloaded from the saved `blockly_json['edubotics-destinations']`.

import { describe, it, expect, vi } from 'vitest';
import {
  createDetachedDestinationStore,
  getDestinationStore,
  readDestinationEntries,
  serializeState,
} from '../destinationStore';

const A = { name: 'Ablage', kind: 'pin', source: 'camera', x: 0.2, y: 0, z: 0 };

describe('createDetachedDestinationStore', () => {
  it('starts with the normalised entries it is handed and fires no Blockly event', () => {
    const saved = { 'edubotics-destinations': serializeState([{ id: 'd_0000abcd', ...A }]) };
    const store = createDetachedDestinationStore(readDestinationEntries(saved));
    expect(store.getEntries().map((e) => [e.id, e.name])).toEqual([['d_0000abcd', 'Ablage']]);
    const res = store.add({ name: 'Kiste', kind: 'pose', source: 'capture', x: 0.1, y: 0.1, z: 0.1 });
    expect(res.ok).toBe(true);
    expect(store.getEntries().map((e) => e.name)).toEqual(['Ablage', 'Kiste']);
  });

  it('notifies subscribers on every change and keeps its own state', () => {
    const store = createDetachedDestinationStore([]);
    const other = createDetachedDestinationStore([]);
    const fn = vi.fn();
    store.subscribe(fn);
    store.add(A);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(other.getEntries()).toEqual([]);
    const { entry } = store.add({ ...A, name: 'B' });
    store.rename(entry.id, 'C');
    store.remove(entry.id);
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it('is total on garbage and is never the store getDestinationStore(null) hands out', () => {
    expect(createDetachedDestinationStore(null).getEntries()).toEqual([]);
    expect(createDetachedDestinationStore([{ name: '' }, 5]).getEntries()).toEqual([]);
    const store = createDetachedDestinationStore([A]);
    expect(getDestinationStore(null).getEntries()).toEqual([]);
    expect(getDestinationStore(null)).not.toBe(getDestinationStore(null));
    expect(store.getEntries()).toHaveLength(1);
  });
});
