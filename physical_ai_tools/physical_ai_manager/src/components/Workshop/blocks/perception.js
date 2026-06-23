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
import {
  COLORS,
  OBJECT_CLASSES,
  ALLOWED_COLOR_VALUES,
  DE,
} from './messages_de';

const PERCEPTION_COLOR = '#22c55e';

const COLOR_DROPDOWN = COLORS.map(([label, value]) => [label, value]);
const OBJECT_DROPDOWN = OBJECT_CLASSES.map((c) => [c, c]);

// Timeout bounds for wait_until_* blocks. Above 120 s a student is
// almost certainly blocking a workshop session by accident.
const TIMEOUT_MIN_S = 1;
const TIMEOUT_MAX_S = 120;

// Marker IDs are AprilTag tag36h11; the family supports up to 587.
// Restrict the editor to the practical 0..255 range to match the
// printable PDF kit students get from `tools/generate_apriltags.py`.
const MARKER_ID_MIN = 0;
const MARKER_ID_MAX = 255;

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
  'edubotics_while_visible',
  'edubotics_wait_until_object_seen',
  'edubotics_when_object_seen',
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

export const PERCEPTION_BLOCKS = [
  {
    type: 'edubotics_detect_color',
    message0: DE.DETECT_COLOR,
    args0: [{ type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
    tooltip:
      'Liefert eine Liste der gefundenen Farb-Bereiche (Position, Größe).',
    extensions: ['edubotics_validate_color'],
  },
  {
    type: 'edubotics_wait_until_color',
    message0: DE.WAIT_UNTIL_COLOR,
    args0: [
      { type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN },
      { type: 'field_number', name: 'TIMEOUT', value: 10, min: TIMEOUT_MIN_S, max: TIMEOUT_MAX_S, precision: 1 },
    ],
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_color', 'edubotics_validate_timeout'],
  },
  {
    type: 'edubotics_count_color',
    message0: DE.COUNT_COLOR,
    args0: [{ type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN }],
    output: 'Number',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_color'],
  },
  {
    type: 'edubotics_detect_marker',
    message0: DE.DETECT_MARKER,
    args0: [{ type: 'field_number', name: 'MARKER_ID', value: 0, min: MARKER_ID_MIN, max: MARKER_ID_MAX, precision: 1 }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_marker_id'],
  },
  {
    type: 'edubotics_wait_until_marker',
    message0: DE.WAIT_UNTIL_MARKER,
    args0: [
      { type: 'field_number', name: 'MARKER_ID', value: 0, min: MARKER_ID_MIN, max: MARKER_ID_MAX, precision: 1 },
      { type: 'field_number', name: 'TIMEOUT', value: 10, min: TIMEOUT_MIN_S, max: TIMEOUT_MAX_S, precision: 1 },
    ],
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_marker_id', 'edubotics_validate_timeout'],
  },
  {
    type: 'edubotics_detect_object',
    message0: DE.DETECT_OBJECT,
    args0: [{ type: 'field_dropdown', name: 'CLASS', options: OBJECT_DROPDOWN }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_object_class'],
  },
  {
    type: 'edubotics_wait_until_object',
    message0: DE.WAIT_UNTIL_OBJECT,
    args0: [
      { type: 'field_dropdown', name: 'CLASS', options: OBJECT_DROPDOWN },
      { type: 'field_number', name: 'TIMEOUT', value: 10, min: TIMEOUT_MIN_S, max: TIMEOUT_MAX_S, precision: 1 },
    ],
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_object_class', 'edubotics_validate_timeout'],
  },
  {
    type: 'edubotics_count_objects_class',
    message0: DE.COUNT_OBJECT,
    args0: [{ type: 'field_dropdown', name: 'CLASS', options: OBJECT_DROPDOWN }],
    output: 'Number',
    colour: PERCEPTION_COLOR,
    extensions: ['edubotics_validate_object_class'],
  },
  // Phase-3 open-vocabulary block. Routes through the cloud burst path
  // (POST /vision/detect → OWLv2 on Modal). Frontend exposes a German
  // text input; backend translates known prompts via a synonym dict
  // before falling back to OWLv2.
  // Audit F32: distinct hue + cloud emoji so the open-vocab block is
  // visually recognisable as "uses the internet" — students should
  // know which actions touch the cloud.
  // Audit F33: edubotics_validate_open_vocab_prompt rejects empty
  // input and caps length so a runaway typer can't blow the proxy's
  // MAX_PROMPT_CHARS=200 cap with a meaningless string.
  {
    type: 'edubotics_detect_open_vocab',
    message0: '☁ ' + DE.DETECT_OPEN_VOCAB,
    args0: [{ type: 'field_input', name: 'PROMPT', text: 'rote Tasse' }],
    output: 'Array',
    colour: 230,
    extensions: ['edubotics_validate_open_vocab_prompt'],
    tooltip:
      'Beschreibt das gesuchte Objekt in deutschen Worten. Bekannte '
      + 'Begriffe werden lokal erkannt; sonst wird die Cloud-Erkennung '
      + 'genutzt.',
  },
];

const OPEN_VOCAB_PROMPT_MAX = 80;

const OBJECT_CLASS_SET = new Set(OBJECT_CLASSES);

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerPerceptionBlocks() {
  registerExtensionOnce('edubotics_validate_color', function () {
    const field = this.getField('COLOR');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator((newValue) => {
        if (!ALLOWED_COLOR_VALUES.has(newValue)) return null;
        return newValue;
      });
    }
  });
  registerExtensionOnce('edubotics_validate_object_class', function () {
    const field = this.getField('CLASS');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator((newValue) => {
        if (!OBJECT_CLASS_SET.has(newValue)) return null;
        return newValue;
      });
    }
  });
  registerExtensionOnce('edubotics_validate_timeout', function () {
    const field = this.getField('TIMEOUT');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator((newValue) => {
        const n = Number(newValue);
        if (!Number.isFinite(n)) return TIMEOUT_MIN_S;
        if (n < TIMEOUT_MIN_S) return TIMEOUT_MIN_S;
        if (n > TIMEOUT_MAX_S) return TIMEOUT_MAX_S;
        return n;
      });
    }
  });
  registerExtensionOnce('edubotics_validate_marker_id', function () {
    const field = this.getField('MARKER_ID');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator((newValue) => {
        const n = Number(newValue);
        if (!Number.isFinite(n)) return MARKER_ID_MIN;
        if (n < MARKER_ID_MIN) return MARKER_ID_MIN;
        if (n > MARKER_ID_MAX) return MARKER_ID_MAX;
        return Math.round(n);
      });
    }
  });
  // Audit F33: open-vocab prompt validator. Trim, reject empty, cap
  // length tight (80 chars) to give a clean UX before the server's
  // 200-char enforcement kicks in.
  registerExtensionOnce('edubotics_validate_open_vocab_prompt', function () {
    const field = this.getField('PROMPT');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator((newValue) => {
        if (typeof newValue !== 'string') return null;
        const trimmed = newValue.trim();
        if (!trimmed) return null;
        if (trimmed.length > OPEN_VOCAB_PROMPT_MAX) {
          return trimmed.slice(0, OPEN_VOCAB_PROMPT_MAX);
        }
        return trimmed;
      });
    }
  });
  // Skip re-definition on HMR / Jest re-import. Audit round-3 §A.
  const toDefine = PERCEPTION_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }

  // Named-object blocks: a server-fed dropdown can't be expressed in
  // defineBlocksWithJsonArray (its field_dropdown.options must be a static
  // array), so these three use a custom init() with a generator-function
  // FieldDropdown (objectTypeOptions). HMR/StrictMode-guarded like above.
  // Field name OBJECT_TYPE serializes to fields:{OBJECT_TYPE} → args['object_type'].
  defineObjectTypeBlock('edubotics_grasp_object', DE.GRASP_OBJECT_PREFIX, 'statement');
  defineObjectTypeBlock('edubotics_see_object', DE.SEE_OBJECT_PREFIX, 'Boolean');
  defineObjectTypeBlock('edubotics_count_object', DE.COUNT_OBJECT_PREFIX, 'Number');

  // P2 named-object loop + event blocks. Custom init (dynamic OBJECT_TYPE
  // dropdown can't be expressed in JSON). HMR/StrictMode-guarded.
  // „Solange <Typ> sichtbar { … }" — a C-shaped CONTROL block with a DO body.
  if (!Blockly.Blocks['edubotics_while_visible']) {
    Blockly.Blocks['edubotics_while_visible'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WHILE_VISIBLE_PREFIX)
          .appendField(new Blockly.FieldDropdown(objectTypeOptions), 'OBJECT_TYPE')
          .appendField(DE.WHILE_VISIBLE_SUFFIX);
        this.appendStatementInput('DO').setCheck(null);
        this.setPreviousStatement(true, null);
        this.setNextStatement(true, null);
        this.setColour(PERCEPTION_COLOR);
        this.setTooltip(
          'Wiederholt den Rumpf, solange noch ein Objekt dieses Typs sichtbar '
          + 'ist (greift z. B. eines nach dem anderen).');
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
