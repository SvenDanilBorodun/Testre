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
import { apiRange, apiTooltip } from './apiDocs';

const OUTPUT_COLOR = '#a855f7';

// Tone bounds — exactly what the robot takes (robot_api.py's `tone` row:
// 100–4000 Hz so a classroom's speakers are spared, 0.05–5 s). The runtime in
// useRosTopicSubscription generates the audio with an OscillatorNode.
const { min: TONE_FREQ_MIN, max: TONE_FREQ_MAX } = apiRange('edubotics_play_tone', 'freq', { min: 100, max: 4000 });
const { min: TONE_SECONDS_MIN, max: TONE_SECONDS_MAX } = apiRange(
  'edubotics_play_tone', 'seconds', { min: 0.05, max: 5 },
);
const TOAST_SECONDS = apiRange('edubotics_toast', 'seconds', { min: 1, max: 15 });

export const OUTPUT_BLOCKS = [
  {
    type: 'edubotics_log',
    message0: DE.LOG,
    args0: [{ type: 'input_value', name: 'MESSAGE' }],
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    tooltip: apiTooltip('edubotics_log'),
  },
  {
    type: 'edubotics_play_sound',
    message0: DE.PLAY_SOUND,
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    tooltip: apiTooltip('edubotics_play_sound'),
  },
  // Phase-2 speech output — frontend-only block. Backend emits a
  // [SPEAK:text] sentinel that useRosTopicSubscription intercepts and
  // routes to window.speechSynthesis (de-DE voice).
  {
    type: 'edubotics_speak_de',
    message0: DE.SPEAK_DE,
    args0: [{ type: 'input_value', name: 'TEXT' }],
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    tooltip: apiTooltip('edubotics_speak_de'),
  },
  // Phase-2 parameterized tone block. Frontend-only — backend treats
  // it as [TONE:freq:seconds].
  {
    type: 'edubotics_play_tone',
    message0: DE.PLAY_TONE,
    args0: [
      {
        type: 'field_number',
        name: 'FREQ',
        value: 880,
        min: TONE_FREQ_MIN,
        max: TONE_FREQ_MAX,
        precision: 1,
      },
      {
        type: 'field_number',
        name: 'SECONDS',
        value: 0.25,
        min: TONE_SECONDS_MIN,
        max: TONE_SECONDS_MAX,
        precision: 0.05,
      },
    ],
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    // handlers/output.py::play_tone emits [TONE:freq:seconds]; the React layer
    // turns it into an OscillatorNode beep. The row's doc names the bounds.
    tooltip: apiTooltip('edubotics_play_tone'),
    extensions: ['edubotics_validate_tone'],
  },
  // On-screen message (toast). Frontend-only — the backend emits a
  // [TOAST:level:sec:text] sentinel that useRosTopicSubscription turns into a
  // react-hot-toast popup (severity → toast type). The LEVEL dropdown VALUEs
  // (info/success/warning/error) + the TEXT input + SECONDS field NAMEs are a
  // hard contract with handlers/output.py::toast (interpreter lowercases the
  // field/input NAMEs → args level / text / seconds).
  {
    type: 'edubotics_toast',
    message0: DE.TOAST,
    args0: [
      { type: 'input_value', name: 'TEXT' },
      {
        type: 'field_dropdown',
        name: 'LEVEL',
        options: [
          [DE.TOAST_LEVEL_INFO, 'info'],
          [DE.TOAST_LEVEL_SUCCESS, 'success'],
          [DE.TOAST_LEVEL_WARNING, 'warning'],
          [DE.TOAST_LEVEL_ERROR, 'error'],
        ],
      },
      {
        type: 'field_number',
        name: 'SECONDS',
        value: 3,
        min: TOAST_SECONDS.min,
        max: TOAST_SECONDS.max,
        precision: 1,
      },
    ],
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    tooltip: apiTooltip('edubotics_toast'),
  },
];

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerOutputBlocks() {
  registerExtensionOnce('edubotics_validate_tone', function () {
    const freq = this.getField('FREQ');
    const sec = this.getField('SECONDS');
    if (freq && typeof freq.setValidator === 'function') {
      freq.setValidator((v) => {
        const n = Number(v);
        if (!Number.isFinite(n)) return TONE_FREQ_MIN;
        if (n < TONE_FREQ_MIN) return TONE_FREQ_MIN;
        if (n > TONE_FREQ_MAX) return TONE_FREQ_MAX;
        return Math.round(n);
      });
    }
    if (sec && typeof sec.setValidator === 'function') {
      sec.setValidator((v) => {
        const n = Number(v);
        if (!Number.isFinite(n)) return TONE_SECONDS_MIN;
        if (n < TONE_SECONDS_MIN) return TONE_SECONDS_MIN;
        if (n > TONE_SECONDS_MAX) return TONE_SECONDS_MAX;
        return n;
      });
    }
  });
  // Skip re-definition on HMR / Jest re-import. Audit round-3 §A.
  const toDefine = OUTPUT_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
