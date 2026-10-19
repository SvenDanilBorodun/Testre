/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// robot_api.py is the only source for robot blocks (Principle 2 of the
// 2026-10-18 semantics round): every block with a row takes its hover text
// from the row's `doc_de_block` (German, with no Python identifiers), so a
// block can never promise what the robot does not do — the old „warte bis
// sichtbar" tooltip said the program aborts, while the robot answers falsch
// with a warning (F9a). The five blocks with a „Tempo" dropdown add one shared
// sentence about it.

import * as Blockly from 'blockly/core';
import { beforeAll, describe, expect, it } from 'vitest';
import { DE } from '../messages_de';
import { ROW_BLOCKS, registerEverything, fieldsOf, tooltipOf } from './_apiBlocks';

let ws;
beforeAll(() => {
  registerEverything();
  ws = new Blockly.Workspace();
});

const PYTHON_WORD_RE = /\b(?:True|False|None)\b|\b[a-z]+_[a-z0-9_]+\b|\[x, ?y, ?z\]/;

describe('block tooltips come from robot_api.json', () => {
  it('there are 32 row blocks', () => {
    expect(ROW_BLOCKS).toHaveLength(32);
    const missing = ROW_BLOCKS.filter((row) => typeof row.doc_de_block !== 'string').map((row) => row.name);
    expect(missing).toEqual([]);
  });

  it.each(ROW_BLOCKS.map((r) => [r.block_type, r]))('%s starts with its doc_de_block', (type, row) => {
    const block = ws.newBlock(type);
    const tip = tooltipOf(block);
    expect(tip.startsWith(row.doc_de_block)).toBe(true);
    expect(tip).not.toMatch(PYTHON_WORD_RE);
    const hasTempo = fieldsOf(block).some((f) => f.name === 'GESCHWINDIGKEIT');
    expect(tip).toBe(hasTempo ? `${row.doc_de_block} ${DE.TEMPO_TOOLTIP}` : row.doc_de_block);
  });

  it('the five Tempo blocks share one German sentence', () => {
    const tempo = ROW_BLOCKS.filter((r) => fieldsOf(ws.newBlock(r.block_type)).some((f) => f.name === 'GESCHWINDIGKEIT'));
    expect(tempo.map((r) => r.name).sort()).toEqual(['drop_at', 'lift', 'move_above', 'move_to', 'pickup']);
    expect(DE.TEMPO_TOOLTIP).toMatch(/Tempo/);
    expect(DE.TEMPO_TOOLTIP).not.toMatch(PYTHON_WORD_RE);
  });

  it('F9a: „warte bis sichtbar" says it answers falsch and never that the program aborts', () => {
    const tip = tooltipOf(ws.newBlock('edubotics_wait_until_object_seen'));
    expect(tip).toMatch(/falsch/);
    expect(tip).not.toMatch(/bricht/);
  });
});
