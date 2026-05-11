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
import { COLORS, DE } from './messages_de';

const EVENT_COLOR = '#ec4899';

const COLOR_DROPDOWN = COLORS.map(([label, value]) => [label, value]);

// Hat blocks ("when X happens") have no `previousStatement` so they
// only appear at the top of the workspace. The runtime interpreter
// (overlays/workflow/interpreter.py) collects them as parallel handler
// stacks; a motion_lock in WorkflowContext keeps motion serialized so
// two handlers can't trigger arm motion simultaneously.
const HAT_SHAPE = {
  // No previousStatement — this makes the block a hat (top-only).
  nextStatement: null,
};

// Color-seen pixel threshold. Defaults to 200 px which is roughly a
// red Lego cube at 60 cm camera distance. Range is empirical.
const COLOR_PIXEL_MIN = 50;
const COLOR_PIXEL_MAX = 50_000;

// AprilTag IDs constrained by the printable kit, matching perception.js.
const MARKER_ID_MIN = 0;
const MARKER_ID_MAX = 255;

const NAME_MAX_LEN = 40;
function eventNameValidator(newValue) {
  if (typeof newValue !== 'string') return null;
  const trimmed = newValue.trim();
  if (trimmed === '') return null;
  // Forbid characters that would make audit log scraping awkward.
  if (/[\r\n\0]/.test(trimmed)) return null;
  return trimmed.slice(0, NAME_MAX_LEN);
}

export const EVENT_BLOCKS = [
  {
    type: 'edubotics_broadcast',
    message0: DE.BROADCAST,
    args0: [{ type: 'field_input', name: 'EVENT_NAME', text: 'start' }],
    previousStatement: null,
    nextStatement: null,
    colour: EVENT_COLOR,
    tooltip:
      'Sendet ein Ereignis an alle "wenn …" Hat-Blöcke mit dem '
      + 'gleichen Namen.',
    extensions: ['edubotics_validate_event_name'],
  },
  {
    type: 'edubotics_when_broadcast',
    message0: DE.WHEN_BROADCAST,
    args0: [{ type: 'field_input', name: 'EVENT_NAME', text: 'start' }],
    ...HAT_SHAPE,
    colour: EVENT_COLOR,
    tooltip:
      'Hat-Block: läuft jedes Mal, wenn ein Ereignis mit diesem Namen '
      + 'gesendet wird.',
    extensions: ['edubotics_validate_event_name'],
  },
  {
    type: 'edubotics_when_marker_seen',
    message0: DE.WHEN_MARKER_SEEN,
    args0: [
      {
        type: 'field_number',
        name: 'MARKER_ID',
        value: 5,
        min: MARKER_ID_MIN,
        max: MARKER_ID_MAX,
        precision: 1,
      },
    ],
    ...HAT_SHAPE,
    colour: EVENT_COLOR,
    extensions: ['edubotics_validate_marker_id_evt'],
  },
  {
    type: 'edubotics_when_color_seen',
    message0: DE.WHEN_COLOR_SEEN,
    args0: [
      { type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN },
      {
        type: 'field_number',
        name: 'MIN_PIXELS',
        value: 200,
        min: COLOR_PIXEL_MIN,
        max: COLOR_PIXEL_MAX,
        precision: 1,
      },
    ],
    ...HAT_SHAPE,
    colour: EVENT_COLOR,
    extensions: ['edubotics_validate_color_event'],
  },
];

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerEventBlocks() {
  registerExtensionOnce('edubotics_validate_event_name', function () {
    const f = this.getField('EVENT_NAME');
    if (f && typeof f.setValidator === 'function') {
      f.setValidator(eventNameValidator);
    }
  });
  registerExtensionOnce('edubotics_validate_marker_id_evt', function () {
    const f = this.getField('MARKER_ID');
    if (f && typeof f.setValidator === 'function') {
      f.setValidator((v) => {
        const n = Number(v);
        if (!Number.isFinite(n)) return MARKER_ID_MIN;
        if (n < MARKER_ID_MIN) return MARKER_ID_MIN;
        if (n > MARKER_ID_MAX) return MARKER_ID_MAX;
        return Math.round(n);
      });
    }
  });
  registerExtensionOnce('edubotics_validate_color_event', function () {
    const px = this.getField('MIN_PIXELS');
    if (px && typeof px.setValidator === 'function') {
      px.setValidator((v) => {
        const n = Number(v);
        if (!Number.isFinite(n)) return COLOR_PIXEL_MIN;
        if (n < COLOR_PIXEL_MIN) return COLOR_PIXEL_MIN;
        if (n > COLOR_PIXEL_MAX) return COLOR_PIXEL_MAX;
        return Math.round(n);
      });
    }
  });
  // Audit round-3 §A — guard against re-definition on hot-reload or
  // Jest re-import. Blockly.defineBlocksWithJsonArray throws "Block
  // type X is already defined" the second time a definition lands.
  // Skip entries whose type is already registered so HMR doesn't crash.
  const toDefine = EVENT_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
