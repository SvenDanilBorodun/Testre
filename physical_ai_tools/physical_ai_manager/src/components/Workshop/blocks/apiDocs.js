/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// What a robot block SAYS comes from the one table, robot_api.py (Principle 2
// of the 2026-10-18 semantics round), through its generated robot_api.json:
// the hover text is the row's `doc_de_block` (German, with no Python
// identifiers — the twin of `doc_de_java`) and a number field allows exactly
// the row's lo..hi. A hand-written tooltip once said „warte bis sichtbar"
// aborts the program while the robot answers falsch with a warning (F9a), and
// the TIMEOUT fields allowed 1–120 while the robot takes 0–300 (F15).
// blocks/__tests__/tooltipsFromApi.test.js and fieldRangesFromApi.test.js
// hold every row block to it.
//
// robot_api.json is already part of the entry bundle (codeProject.js and
// codeAssetUsage.js read it), so the blocks reading it costs nothing.

import api from '../code/robot_api.json';
import { DE } from './messages_de';

const ROWS = new Map(
  (api.methods || [])
    .filter((m) => typeof m.block_type === 'string')
    .map((m) => [m.block_type, m]),
);

/** The robot_api row of a block type, or null. */
export function apiRow(blockType) {
  return ROWS.get(blockType) || null;
}

/**
 * A row block's tooltip: its `doc_de_block`, and for the five blocks with a
 * „Tempo" dropdown the one shared sentence about it.
 */
export function apiTooltip(blockType, { tempo = false } = {}) {
  const row = ROWS.get(blockType);
  const doc = row && typeof row.doc_de_block === 'string' ? row.doc_de_block : '';
  return tempo ? `${doc} ${DE.TEMPO_TOOLTIP}` : doc;
}

/**
 * `{min, max}` of a parameter (by its `arg_key`), as robot_api.py bounds it.
 * Throws when the row or the bound is missing: a block definition that names
 * a parameter the table does not have must fail at registration, not drift.
 */
export function apiRange(blockType, argKey) {
  const row = ROWS.get(blockType);
  const param = row && (row.params || []).find((p) => p.arg_key === argKey);
  if (!param || typeof param.lo !== 'number' || typeof param.hi !== 'number') {
    throw new Error(`robot_api.json has no range for ${blockType}.${argKey}`);
  }
  return { min: param.lo, max: param.hi };
}
