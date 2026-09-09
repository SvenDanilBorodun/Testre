/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Audit `docs/plans/audit-G10-G12.md` §11.1-11.3.
//
// §11.1: the RUN allowlist drops THREE serializer keys, not the two the old
//        comment enumerated — the third is `workspaceComments`, a Blockly CORE
//        serializer holding the student's free-floating canvas notes.
// §11.2: `suggested-blocks` grows ~16 bytes per drag and is NEVER trimmed, so a
//        long-lived DOCUMENT eventually crosses MAX_BLOCKLY_JSON_BYTES.
// §11.3: `backpack` is one student's private clipboard and the SAVE path shipped
//        it into a row that group siblings read, `clone_workflow` copies and a
//        teacher can publish as a class template.
//
// The serializer inventory is MEASURED against real Blockly 12.5.1 plus the
// shipped plugins rather than asserted from the comment, because "which keys
// exist" is exactly the fact the old comment got wrong.

import { describe, it, expect, beforeAll } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import {
  RUN_PAYLOAD_SERIALIZER_KEYS,
  SAVE_PAYLOAD_SERIALIZER_KEYS,
  pickSerializerKeys,
  slimRunPayload,
  slimSavePayload,
} from '../blocklyPayload';

function registeredSerializers() {
  return Object.keys(
    Blockly.registry.getAllItems(Blockly.registry.Type.SERIALIZER, true) || {},
  ).sort();
}

describe('the serializer inventory this allowlist is defined against', () => {
  it('has exactly three CORE serializers before any plugin loads', () => {
    expect(registeredSerializers()).toEqual([
      'blocks', 'variables', 'workspaceComments',
    ]);
  });

  it('gains suggested-blocks on import and backpack on construction', async () => {
    await import('@blockly/suggested-blocks');
    expect(registeredSerializers()).toContain('suggested-blocks');
    // The Backpack registers from its CONSTRUCTOR, not from the module body,
    // which is why an import-only probe sees four and the running editor five.
    const mod = await import('@blockly/workspace-backpack');
    expect(typeof (mod.Backpack || mod.default)).toBe('function');
  });
});

describe('the two allowlists', () => {
  it('the RUN payload is the program only', () => {
    expect(RUN_PAYLOAD_SERIALIZER_KEYS).toEqual(['blocks', 'variables']);
  });

  it('the SAVE payload additionally keeps the student canvas notes', () => {
    expect(SAVE_PAYLOAD_SERIALIZER_KEYS)
      .toEqual(['blocks', 'variables', 'workspaceComments']);
  });

  it('they DIFFER, and the difference is workspaceComments', () => {
    // If these two ever collapse into one list, the run grows a key the
    // interpreter cannot read or the save loses a student's notes.
    const runOnly = RUN_PAYLOAD_SERIALIZER_KEYS.filter(
      (k) => !SAVE_PAYLOAD_SERIALIZER_KEYS.includes(k));
    const saveOnly = SAVE_PAYLOAD_SERIALIZER_KEYS.filter(
      (k) => !RUN_PAYLOAD_SERIALIZER_KEYS.includes(k));
    expect(runOnly).toEqual([]);
    expect(saveOnly).toEqual(['workspaceComments']);
  });
});

describe('what each path drops from a full serializer output', () => {
  const full = {
    blocks: { languageVersion: 0, blocks: [{ type: 'edubotics_home' }] },
    variables: [{ name: 'x', id: 'v1' }],
    workspaceComments: [{ id: 'c1', text: 'Notiz' }],
    'suggested-blocks': { recentlyUsedBlocks: ['a', 'b'] },
    backpack: ['<block type="edubotics_home"/>'],
  };

  it('the RUN payload drops THREE keys, not two', () => {
    const slim = slimRunPayload(full);
    expect(Object.keys(slim).sort()).toEqual(['blocks', 'variables']);
    const dropped = Object.keys(full).filter((k) => !(k in slim)).sort();
    expect(dropped).toEqual(['backpack', 'suggested-blocks', 'workspaceComments']);
  });

  it('the SAVE payload keeps the notes and drops the two plugin keys', () => {
    const slim = slimSavePayload(full);
    expect(Object.keys(slim).sort())
      .toEqual(['blocks', 'variables', 'workspaceComments']);
    expect(slim.workspaceComments).toBe(full.workspaceComments);
    expect('backpack' in slim).toBe(false);
    expect('suggested-blocks' in slim).toBe(false);
  });

  it('neither mutates the object autosave also holds', () => {
    const before = JSON.stringify(full);
    slimRunPayload(full);
    slimSavePayload(full);
    expect(JSON.stringify(full)).toBe(before);
  });
});

describe('the empty-workspace contract', () => {
  it('a missing key stays MISSING rather than becoming undefined', () => {
    // An EMPTY workspace serializes to {} with no `blocks` key at all, and the
    // spread that follows must not turn that into `blocks: undefined`.
    for (const slim of [slimRunPayload({}), slimSavePayload({})]) {
      expect(Object.keys(slim)).toEqual([]);
      expect('blocks' in slim).toBe(false);
      expect(JSON.stringify({ ...slim, tempo: 1 })).toBe('{"tempo":1}');
    }
  });

  it('tolerates a null / non-object input', () => {
    for (const bad of [null, undefined, 'x', 7, []]) {
      expect(slimRunPayload(bad)).toEqual({});
      expect(slimSavePayload(bad)).toEqual({});
    }
  });

  it('pickSerializerKeys is the shared primitive, not two copies', () => {
    expect(pickSerializerKeys({ a: 1, b: 2 }, ['a'])).toEqual({ a: 1 });
  });
});

describe('a real empty workspace really does serialize to {}', () => {
  beforeAll(() => {
    // no block registration needed — an empty workspace has nothing to save
  });

  it('measured against Blockly 12.5.1, not quoted from a comment', () => {
    const ws = new Blockly.Workspace();
    expect(Blockly.serialization.workspaces.save(ws)).toEqual({});
  });
});
