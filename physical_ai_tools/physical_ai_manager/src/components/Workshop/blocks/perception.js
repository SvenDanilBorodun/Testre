/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import * as Blockly from 'blockly/core';
import { DE } from './messages_de';

const PERCEPTION_COLOR = '#22c55e';

// Timeout bounds for the wait_until_object_seen block. Above 120 s a
// student is almost certainly blocking a workshop session by accident.
const TIMEOUT_MIN_S = 1;
const TIMEOUT_MAX_S = 120;

// ── Named-object dynamic dropdown (Roboter Studio AprilTag grasping) ─────────
// The object-type list comes from the RUNTIME catalog (GetObjectCatalog over
// rosbridge), not a static array — the first server-fed dropdown in the editor.
// A module-level cache is filled by WorkshopPage via setObjectCatalogOptions
// after the service answers; the FieldDropdown generator reads it lazily, so a
// block placed BEFORE the catalog arrived updates once it does (via
// refreshObjectTypeDropdowns). The dropdown VALUE is the catalog type key; the
// LABEL is the German display name.
const OBJECT_TYPE_BLOCK_TYPES = [
  'edubotics_grasp_object',
  'edubotics_see_object',
  'edubotics_count_object',
  'edubotics_find_object',
  'edubotics_while_visible',
  'edubotics_wait_until_object_seen',
  'edubotics_when_object_seen',
];
// ── Grasp split (Phase 1) — static JSON perception blocks ────────────────────
// Value/statement blocks that produce or consume a Greifziel. Unlike the
// named-object blocks above they do NOT use the runtime dropdown, so they're
// plain JSON registered with the same HMR/Jest re-definition guard as the
// other files. Input/field NAMEs are a hard contract with the Python server
// (`_build_args` lowercases them → ZIEL → arg `ziel`).
const PERCEPTION_JSON_BLOCKS = [
  {
    type: 'edubotics_object_position',
    message0: DE.OBJECT_POSITION,
    args0: [{ type: 'input_value', name: 'ZIEL', check: 'Greifziel' }],
    // Output 'String' to match edubotics_destination_ref so it plugs into the
    // same „bewege zu" / „ablegen bei" sockets (which carry no check today).
    output: 'String',
    colour: PERCEPTION_COLOR,
    tooltip:
      'Liefert die Position eines erkannten Greifziels als Ziel-Wert, der '
      + 'in „bewege zu" oder „ablegen bei" gesteckt werden kann.',
  },
  {
    type: 'edubotics_grasp_held',
    message0: DE.GRASP_HELD,
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
    tooltip: 'Wahr, wenn der Greifer gerade ein Objekt hält.',
  },
  {
    type: 'edubotics_mark_done',
    message0: DE.MARK_DONE,
    args0: [{ type: 'input_value', name: 'ZIEL', check: 'Greifziel' }],
    previousStatement: null,
    nextStatement: null,
    colour: PERCEPTION_COLOR,
    tooltip:
      'Merkt sich dieses Greifziel als erledigt, damit es nicht erneut '
      + 'gegriffen wird.',
  },
];

const _objectTypePlaceholder = () => [[DE.OBJECT_TYPE_LOADING, '__none__']];
const _objectTypeEmpty = () => [[DE.OBJECT_TYPE_EMPTY, '__none__']];
let OBJECT_TYPE_OPTIONS = _objectTypePlaceholder();

// Passed to new Blockly.FieldDropdown(...). Blockly re-invokes it whenever the
// menu (re)renders, so once the cache is filled the menu shows real options.
// MUST always return a non-empty [label, value] array.
function objectTypeOptions() {
  return OBJECT_TYPE_OPTIONS.length ? OBJECT_TYPE_OPTIONS : _objectTypeEmpty();
}

let _workspaceAccessor = null;

// WorkshopPage wires this so a late catalog refresh can reach the live blocks.
export function setWorkspaceAccessor(fn) {
  _workspaceAccessor = typeof fn === 'function' ? fn : null;
}

// Re-resolve + re-render every live OBJECT_TYPE dropdown after the catalog
// arrives (the generator's cached option list + a now-invalid stored value
// would otherwise persist on a block dragged out before the fetch resolved).
function refreshObjectTypeDropdowns() {
  const ws = _workspaceAccessor ? _workspaceAccessor() : null;
  if (!ws || typeof ws.getAllBlocks !== 'function') return;
  const valid = objectTypeOptions().map((o) => o[1]);
  ws.getAllBlocks(false).forEach((b) => {
    if (!OBJECT_TYPE_BLOCK_TYPES.includes(b.type)) return;
    const field = b.getField('OBJECT_TYPE');
    if (!field) return;
    if (typeof field.getOptions === 'function') field.getOptions(false); // re-run generator, un-cached
    if (valid.length && !valid.includes(field.getValue())) {
      field.setValue(valid[0]); // snap a stale/placeholder value to a real type
    }
    if (typeof field.forceRerender === 'function') field.forceRerender();
  });
}

// Called by WorkshopPage with [[label_de, type_name], ...] from GetObjectCatalog.
export function setObjectCatalogOptions(pairs) {
  OBJECT_TYPE_OPTIONS = (Array.isArray(pairs) && pairs.length)
    ? pairs.map(([label, value]) => [String(label), String(value)])
    : _objectTypeEmpty();
  refreshObjectTypeDropdowns();
}

export function registerPerceptionBlocks() {
  // Named-object blocks: a server-fed dropdown can't be expressed in
  // defineBlocksWithJsonArray (its field_dropdown.options must be a static
  // array), so these three use a custom init() with a generator-function
  // FieldDropdown (objectTypeOptions). HMR/StrictMode-guarded.
  // Field name OBJECT_TYPE serializes to fields:{OBJECT_TYPE} → args['object_type'].
  defineObjectTypeBlock('edubotics_grasp_object', DE.GRASP_OBJECT_PREFIX, 'statement');
  defineObjectTypeBlock('edubotics_see_object', DE.SEE_OBJECT_PREFIX, 'Boolean');
  defineObjectTypeBlock('edubotics_count_object', DE.COUNT_OBJECT_PREFIX, 'Number');
  // Grasp split (Phase 1): „finde <Typ>" outputs a Greifziel value (the helper
  // calls setOutput(true, kind) for any non-'statement' kind).
  defineObjectTypeBlock('edubotics_find_object', DE.FIND_OBJECT_PREFIX, 'Greifziel');

  // Static JSON perception blocks (no runtime dropdown). HMR/Jest-guarded.
  const toDefine = PERCEPTION_JSON_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }

  // P2 named-object loop + event blocks. Custom init (dynamic OBJECT_TYPE
  // dropdown can't be expressed in JSON). HMR/StrictMode-guarded.
  // „Solange <Typ> sichtbar { … }" — a C-shaped CONTROL block with a DO body.
  if (!Blockly.Blocks['edubotics_while_visible']) {
    Blockly.Blocks['edubotics_while_visible'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WHILE_VISIBLE_PREFIX)
          .appendField(new Blockly.FieldDropdown(objectTypeOptions), 'OBJECT_TYPE')
          .appendField(DE.WHILE_VISIBLE_SUFFIX)
          // #6 optional repetition cap: 0 = unbegrenzt (the server's wall-clock
          // and no-progress guards still bound it). The field key MAX_REPS is
          // read by interpreter._exec_while_visible.
          .appendField(DE.WHILE_VISIBLE_MAX_PREFIX)
          .appendField(new Blockly.FieldNumber(0, 0, 999, 1), 'MAX_REPS')
          .appendField(DE.WHILE_VISIBLE_MAX_SUFFIX);
        this.appendStatementInput('DO').setCheck(null);
        this.setPreviousStatement(true, null);
        this.setNextStatement(true, null);
        this.setColour(PERCEPTION_COLOR);
        this.setTooltip(
          'Wiederholt den Rumpf, solange noch ein Objekt dieses Typs sichtbar '
          + 'ist (greift z. B. eines nach dem anderen). „höchstens N Mal" '
          + 'begrenzt die Anzahl der Durchläufe; 0 bedeutet unbegrenzt.');
      },
    };
  }
  // „warte bis <Typ> sichtbar (max N s)" — Boolean value block + TIMEOUT field.
  if (!Blockly.Blocks['edubotics_wait_until_object_seen']) {
    Blockly.Blocks['edubotics_wait_until_object_seen'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WAIT_UNTIL_OBJECT_SEEN_PREFIX)
          .appendField(new Blockly.FieldDropdown(objectTypeOptions), 'OBJECT_TYPE')
          .appendField(DE.WAIT_UNTIL_OBJECT_SEEN_MID)
          .appendField(
            new Blockly.FieldNumber(10, TIMEOUT_MIN_S, TIMEOUT_MAX_S, 1), 'TIMEOUT')
          .appendField(DE.WAIT_UNTIL_OBJECT_SEEN_SUFFIX);
        this.setOutput(true, 'Boolean');
        this.setColour(PERCEPTION_COLOR);
      },
    };
  }
  // „wenn <Typ> erkannt" — a HAT block (top-only: nextStatement, no previous).
  if (!Blockly.Blocks['edubotics_when_object_seen']) {
    Blockly.Blocks['edubotics_when_object_seen'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WHEN_OBJECT_SEEN_PREFIX)
          .appendField(new Blockly.FieldDropdown(objectTypeOptions), 'OBJECT_TYPE')
          .appendField(DE.WHEN_OBJECT_SEEN_SUFFIX);
        this.setNextStatement(true, null);   // hat: top-only
        this.setColour(PERCEPTION_COLOR);
      },
    };
  }
}

// Define one named-object block. `kind` is 'statement' (chains vertically, no
// output) or an output type string ('Boolean' | 'Number').
function defineObjectTypeBlock(type, prefix, kind) {
  if (Blockly.Blocks[type]) return; // HMR / Jest re-import guard
  Blockly.Blocks[type] = {
    init() {
      this.appendDummyInput()
        .appendField(prefix)
        .appendField(new Blockly.FieldDropdown(objectTypeOptions), 'OBJECT_TYPE');
      if (kind === 'statement') {
        this.setPreviousStatement(true, null);
        this.setNextStatement(true, null);
      } else {
        this.setOutput(true, kind);
      }
      this.setColour(PERCEPTION_COLOR);
    },
  };
}
