/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// What the 256 KB autosave cap is allowed to refuse.
//
// The cap mirrors the SERVER's, and the server is only ever sent the DOCUMENT
// (`slimSavePayload`: blocks + variables + workspaceComments). Since the editor
// plugins actually load (2026-09-11), `workspaces.save()` ALSO returns
// `backpack` and `suggested-blocks` — and `suggested-blocks` gains a little on
// every block a student drags and is never trimmed. Measuring that against the
// server's cap would let plugin state kill a crash-recovery snapshot while the
// cloud save of the same workflow succeeds, behind a German „zu groß" toast
// that names the wrong thing.
//
// So: full output normally; the document alone rather than nothing; and the
// toast only for a document that is genuinely too big.

import { renderHook, act } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const idb = vi.hoisted(() => ({
  get: vi.fn(async () => undefined),
  set: vi.fn(async () => undefined),
  del: vi.fn(async () => undefined),
}));
const toastMock = vi.hoisted(() => Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }));
const blocklyState = vi.hoisted(() => ({ current: {} }));

vi.mock('idb-keyval', () => ({ get: idb.get, set: idb.set, del: idb.del }));
vi.mock('react-hot-toast', () => ({ default: toastMock }));
vi.mock('blockly/core', () => ({
  serialization: { workspaces: { save: () => blocklyState.current } },
}));

const { useAutosave } = await import('../useAutosave');
const { DE } = await import('../blocks/messages_de');

function fakeWorkspace() {
  return { addChangeListener: vi.fn(), removeChangeListener: vi.fn() };
}

/** A block list whose JSON is `kb` kilobytes, give or take. */
function blocksOfSize(kb) {
  return [{ type: 'text_print', fields: { TEXT: 'x'.repeat(kb * 1024) } }];
}

async function saveOnce() {
  const { result } = renderHook(() => useAutosave({ workspace: fakeWorkspace(), scopeKey: 'student-1' }));
  await act(async () => { await result.current.save(); });
}

function storedState() {
  expect(idb.set).toHaveBeenCalledTimes(1);
  return idb.set.mock.calls[0][1].state;
}

describe('autosave size cap', () => {
  beforeEach(() => {
    idb.get.mockClear();
    idb.set.mockClear();
    toastMock.error.mockClear();
  });

  it('stores the FULL serializer output while it fits', async () => {
    blocklyState.current = {
      blocks: { blocks: blocksOfSize(1) },
      variables: [],
      'suggested-blocks': { recentlyUsedBlocks: ['edubotics_home'] },
      backpack: ['<block type="edubotics_home"/>'],
    };
    await saveOnce();
    expect(Object.keys(storedState()).sort())
      .toEqual(['backpack', 'blocks', 'suggested-blocks', 'variables']);
    expect(toastMock.error).not.toHaveBeenCalled();
  });

  it('drops the PLUGIN keys rather than the snapshot when plugin state blows the cap', async () => {
    blocklyState.current = {
      blocks: { blocks: blocksOfSize(4) },
      variables: [],
      // The unbounded one: never trimmed, grows per drag.
      'suggested-blocks': { recentlyUsedBlocks: [ 'x'.repeat(300 * 1024) ] },
      backpack: ['<block type="edubotics_home"/>'],
    };
    await saveOnce();
    const state = storedState();
    expect(Object.keys(state).sort()).toEqual(['blocks', 'variables']);
    // The student's actual work is still there.
    expect(state.blocks.blocks[0].fields.TEXT.length).toBe(4 * 1024);
    expect(toastMock.error).not.toHaveBeenCalled();
  });

  it('still refuses — and says so in German — when the DOCUMENT itself is too big', async () => {
    blocklyState.current = {
      blocks: { blocks: blocksOfSize(300) },
      variables: [],
      'suggested-blocks': { recentlyUsedBlocks: [] },
    };
    await saveOnce();
    expect(idb.set).not.toHaveBeenCalled();
    expect(toastMock.error).toHaveBeenCalledWith(DE.AUTOSAVE_TOO_BIG, { id: 'autosave-too-big' });
  });
});
