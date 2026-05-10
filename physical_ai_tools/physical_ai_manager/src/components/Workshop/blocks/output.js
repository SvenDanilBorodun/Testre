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

const OUTPUT_COLOR = '#a855f7';

// Tone bounds. The runtime in useRosTopicSubscription generates the
// audio with an OscillatorNode; we cap frequency at 4000 Hz to avoid
// piercing a classroom's speakers.
const TONE_FREQ_MIN = 100;
const TONE_FREQ_MAX = 4000;
const TONE_SECONDS_MIN = 0.05;
const TONE_SECONDS_MAX = 5;

export const OUTPUT_BLOCKS = [
  {
    type: 'edubotics_log',
    message0: DE.LOG,
    args0: [{ type: 'input_value', name: 'MESSAGE' }],
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    tooltip: 'Schreibt eine Nachricht in das Log.',
  },
  {
    type: 'edubotics_play_sound',
    message0: DE.PLAY_SOUND,
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
    tooltip: 'Spielt einen kurzen Bestätigungston ab.',
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
    tooltip: 'Liest den Text auf Deutsch vor.',
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
    extensions: ['edubotics_validate_tone'],
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
  Blockly.defineBlocksWithJsonArray(OUTPUT_BLOCKS);
}
