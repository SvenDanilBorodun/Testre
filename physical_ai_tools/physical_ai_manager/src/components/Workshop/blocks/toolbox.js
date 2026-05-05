/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { DE } from './messages_de';

export const TOOLBOX = {
  kind: 'categoryToolbox',
  contents: [
    {
      kind: 'category',
      name: DE.CATEGORY_BEWEGUNG,
      colour: '#3b82f6',
      contents: [
        { kind: 'block', type: 'edubotics_home' },
        { kind: 'block', type: 'edubotics_open_gripper' },
        { kind: 'block', type: 'edubotics_close_gripper' },
        { kind: 'block', type: 'edubotics_move_to' },
        { kind: 'block', type: 'edubotics_pickup' },
        { kind: 'block', type: 'edubotics_drop_at' },
        { kind: 'block', type: 'edubotics_wait_seconds' },
      ],
    },
    {
      kind: 'category',
      name: DE.CATEGORY_WAHRNEHMUNG,
      colour: '#22c55e',
      contents: [
        // Color first (simplest), then markers, then objects — students
        // see the progression from "find the red cube" to "find every banana".
        { kind: 'block', type: 'edubotics_detect_color' },
        { kind: 'block', type: 'edubotics_wait_until_color' },
        { kind: 'block', type: 'edubotics_count_color' },
        { kind: 'block', type: 'edubotics_detect_marker' },
        { kind: 'block', type: 'edubotics_wait_until_marker' },
        { kind: 'block', type: 'edubotics_detect_object' },
        { kind: 'block', type: 'edubotics_wait_until_object' },
        { kind: 'block', type: 'edubotics_count_objects_class' },
      ],
    },
    {
      kind: 'category',
      name: DE.CATEGORY_ZIELE,
      colour: '#f59e0b',
      contents: [
        { kind: 'block', type: 'edubotics_destination_pin' },
        { kind: 'block', type: 'edubotics_destination_current' },
      ],
    },
    {
      kind: 'category',
      name: DE.CATEGORY_LOGIK,
      colour: '#eab308',
      contents: [
        { kind: 'block', type: 'controls_if' },
        { kind: 'block', type: 'controls_repeat_ext' },
        { kind: 'block', type: 'controls_whileUntil' },
        { kind: 'block', type: 'controls_for' },
        { kind: 'block', type: 'controls_forEach' },
        { kind: 'block', type: 'logic_compare' },
        { kind: 'block', type: 'logic_operation' },
        { kind: 'block', type: 'logic_negate' },
        { kind: 'block', type: 'logic_boolean' },
        { kind: 'block', type: 'math_number' },
        { kind: 'block', type: 'math_arithmetic' },
        { kind: 'block', type: 'text' },
      ],
    },
    {
      kind: 'category',
      name: DE.CATEGORY_VARIABLEN,
      colour: '#a78bfa',
      custom: 'VARIABLE',
    },
    {
      kind: 'category',
      name: DE.CATEGORY_AUSGABE,
      colour: '#a855f7',
      contents: [
        { kind: 'block', type: 'edubotics_log' },
        { kind: 'block', type: 'edubotics_play_sound' },
      ],
    },
  ],
};
