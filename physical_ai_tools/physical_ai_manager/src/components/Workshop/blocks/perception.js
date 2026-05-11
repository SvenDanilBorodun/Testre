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
  {
    type: 'edubotics_detect_open_vocab',
    message0: DE.DETECT_OPEN_VOCAB,
    args0: [{ type: 'field_input', name: 'PROMPT', text: 'rote Tasse' }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
    tooltip:
      'Beschreibt das gesuchte Objekt in deutschen Worten. Bekannte '
      + 'Begriffe werden lokal erkannt; sonst wird die Cloud-Erkennung '
      + 'genutzt.',
  },
];

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
  // Skip re-definition on HMR / Jest re-import. Audit round-3 §A.
  const toDefine = PERCEPTION_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
