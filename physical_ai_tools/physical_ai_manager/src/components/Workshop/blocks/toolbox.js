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

// Shadow blocks. Beginners can't connect a math_number to a value
// input, so every numeric value-slot gets a shadow with a sensible
// default. Students learn that the shadow is "fillable" by dragging
// over it. The shadow's NUM is rendered greyed out until replaced.
//
// Pattern reference (Blockly v12 docs):
//   https://developers.google.com/blockly/guides/configure/web/toolboxes/preset
function numberShadow(value) {
  return { shadow: { type: 'math_number', fields: { NUM: value } } };
}

function textShadow(value) {
  return { shadow: { type: 'text', fields: { TEXT: value } } };
}

// Build a category that can be hidden when the parent supplies a
// `restrictedBlocks` set (used by tutorials in phase 3). When the
// restriction list is empty, every block in the category is shown.
function filterContents(contents, restricted) {
  if (!restricted || restricted.size === 0) return contents;
  return contents.filter((entry) => {
    if (entry.kind !== 'block') return true;
    return restricted.has(entry.type);
  });
}

/**
 * Build a toolbox JSON. When `restrictedBlocks` is a non-empty Set,
 * only block entries whose `type` is in the set are kept (used by
 * tutorial scaffolding).
 */
export function buildToolbox(restrictedBlocks = null) {
  const restricted = restrictedBlocks instanceof Set
    ? restrictedBlocks
    : (restrictedBlocks ? new Set(restrictedBlocks) : null);

  const motion = filterContents([
    { kind: 'block', type: 'edubotics_home' },
    { kind: 'block', type: 'edubotics_open_gripper' },
    { kind: 'block', type: 'edubotics_close_gripper' },
    { kind: 'block', type: 'edubotics_move_to' },
    { kind: 'block', type: 'edubotics_pickup' },
    { kind: 'block', type: 'edubotics_drop_at' },
    // Grasp split (Phase 1) — explicit pick sequence (top-to-bottom order).
    { kind: 'block', type: 'edubotics_move_above' },
    { kind: 'block', type: 'edubotics_descend_to' },
    { kind: 'block', type: 'edubotics_close_on_object' },
    { kind: 'block', type: 'edubotics_lift' },
    {
      kind: 'block',
      type: 'edubotics_wait_seconds',
      inputs: { SECONDS: numberShadow(1) },
    },
    // Batch 2b — replay a recorded hand-guided motion by name.
    { kind: 'block', type: 'edubotics_replay_trajectory' },
  ], restricted);

  // Named-object AprilTag grasping (dropdown fed by the runtime catalog).
  const perception = filterContents([
    { kind: 'block', type: 'edubotics_grasp_object' },
    { kind: 'block', type: 'edubotics_while_visible' },
    { kind: 'block', type: 'edubotics_see_object' },
    { kind: 'block', type: 'edubotics_count_object' },
    { kind: 'block', type: 'edubotics_wait_until_object_seen' },
    // Grasp split (Phase 1) — natural pick sequence (find → position →
    // held? → mark done).
    { kind: 'block', type: 'edubotics_find_object' },
    { kind: 'block', type: 'edubotics_object_position' },
    { kind: 'block', type: 'edubotics_grasp_held' },
    { kind: 'block', type: 'edubotics_wait_until_held' },
    { kind: 'block', type: 'edubotics_mark_done' },
  ], restricted);

  const events = filterContents([
    { kind: 'block', type: 'edubotics_broadcast' },
    { kind: 'block', type: 'edubotics_when_broadcast' },
    { kind: 'block', type: 'edubotics_when_object_seen' },
  ], restricted);

  // Zähler — named per-run integer counters (reset / +1 / read / threshold hat).
  const counters = filterContents([
    { kind: 'block', type: 'edubotics_counter_reset' },
    { kind: 'block', type: 'edubotics_counter_add' },
    { kind: 'block', type: 'edubotics_counter_get' },
    { kind: 'block', type: 'edubotics_when_counter_gt' },
  ], restricted);

  const destinations = filterContents([
    { kind: 'block', type: 'edubotics_destination_pin' },
    { kind: 'block', type: 'edubotics_destination_ref' },
    { kind: 'block', type: 'edubotics_destination_current' },
  ], restricted);

  const logic = filterContents([
    { kind: 'block', type: 'controls_if' },
    {
      kind: 'block',
      type: 'controls_repeat_ext',
      inputs: { TIMES: numberShadow(10) },
    },
    { kind: 'block', type: 'controls_whileUntil' },
    {
      kind: 'block',
      type: 'controls_for',
      fields: { VAR: { name: 'i' } },
      inputs: {
        FROM: numberShadow(1),
        TO: numberShadow(10),
        BY: numberShadow(1),
      },
    },
    { kind: 'block', type: 'controls_forEach' },
    // Phase-2 control idioms (Scratch „forever" / NEPO „warte bis").
    { kind: 'block', type: 'edubotics_forever' },
    {
      kind: 'block',
      type: 'edubotics_wait_until',
      // Pre-fill the Boolean hole with a shadow so it isn't an empty slot.
      inputs: { BOOL: { shadow: { type: 'logic_boolean' } } },
    },
    { kind: 'block', type: 'logic_compare' },
    { kind: 'block', type: 'logic_operation' },
    { kind: 'block', type: 'logic_negate' },
    { kind: 'block', type: 'logic_boolean' },
  ], restricted);

  const lists = filterContents([
    { kind: 'block', type: 'lists_create_with' },
    {
      kind: 'block',
      type: 'lists_repeat',
      inputs: { NUM: numberShadow(5) },
    },
    { kind: 'block', type: 'lists_length' },
    { kind: 'block', type: 'lists_isEmpty' },
    { kind: 'block', type: 'lists_indexOf' },
    { kind: 'block', type: 'lists_getIndex' },
    { kind: 'block', type: 'lists_setIndex' },
    { kind: 'block', type: 'lists_getSublist' },
  ], restricted);

  const math = filterContents([
    { kind: 'block', type: 'math_number' },
    {
      kind: 'block',
      type: 'math_arithmetic',
      inputs: { A: numberShadow(1), B: numberShadow(1) },
    },
    {
      kind: 'block',
      type: 'math_random_int',
      inputs: { FROM: numberShadow(1), TO: numberShadow(100) },
    },
    {
      kind: 'block',
      type: 'math_constrain',
      inputs: {
        VALUE: numberShadow(50),
        LOW: numberShadow(1),
        HIGH: numberShadow(100),
      },
    },
    { kind: 'block', type: 'math_modulo' },
    { kind: 'block', type: 'math_round' },
  ], restricted);

  // Text category — `text` is a string literal, not a math operation;
  // surfacing it under Mathe was a taxonomy bug (audit round-3 §Z/§AA).
  // Now it lives in its own category alongside the inline shadows used
  // by `edubotics_log` / `edubotics_speak_de`.
  const textCat = filterContents([
    { kind: 'block', type: 'text', fields: { TEXT: '' } },
    // Built-in string-composition (mutator) block. Lets a student build a
    // dynamic message („Ich sehe 3 Bananen") for melde/sage; the interpreter
    // evaluates ADD0..ADDn server-side. The default text_join carries two
    // inputs (ADD0, ADD1) on init, so the text shadows attach without a
    // mutation override.
    {
      kind: 'block',
      type: 'text_join',
      inputs: { ADD0: textShadow(''), ADD1: textShadow('') },
    },
  ], restricted);

  const output = filterContents([
    {
      kind: 'block',
      type: 'edubotics_log',
      inputs: { MESSAGE: textShadow('Hallo!') },
    },
    {
      kind: 'block',
      type: 'edubotics_toast',
      inputs: { TEXT: textShadow('Fertig!') },
    },
    { kind: 'block', type: 'edubotics_play_sound' },
    {
      kind: 'block',
      type: 'edubotics_speak_de',
      inputs: { TEXT: textShadow('Fertig!') },
    },
    { kind: 'block', type: 'edubotics_play_tone' },
  ], restricted);

  // Build the toolbox JSON. Empty categories are dropped so a
  // restricted-toolbox tutorial doesn't render empty categories.
  const categories = [
    {
      kind: 'category',
      name: DE.CATEGORY_VORSCHLAEGE,
      colour: '#64748b',
      // Dynamic category populated by @blockly/suggested-blocks.
      custom: 'MOST_USED',
    },
    {
      kind: 'category',
      name: DE.CATEGORY_BEWEGUNG,
      colour: '#3b82f6',
      contents: motion,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_WAHRNEHMUNG,
      colour: '#22c55e',
      contents: perception,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_EREIGNISSE,
      colour: '#ec4899',
      contents: events,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_ZIELE,
      colour: '#f59e0b',
      contents: destinations,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_LOGIK,
      colour: '#eab308',
      contents: logic,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_LISTE,
      colour: '#0ea5e9',
      contents: lists,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_VARIABLEN,
      colour: '#a78bfa',
      custom: 'VARIABLE',
    },
    {
      kind: 'category',
      name: DE.CATEGORY_FUNKTIONEN,
      colour: '#7c3aed',
      custom: 'PROCEDURE',
    },
    {
      kind: 'category',
      name: DE.CATEGORY_MATHE,
      colour: '#0284c7',
      contents: math,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_TEXT,
      colour: '#0d9488',
      contents: textCat,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_AUSGABE,
      colour: '#a855f7',
      contents: output,
    },
    {
      kind: 'category',
      name: DE.CATEGORY_ZAEHLER,
      colour: '#0891b2',
      contents: counters,
    },
  ].filter((c) => {
    // Keep dynamic categories regardless; drop static categories with
    // no remaining contents (happens when restrictedBlocks is set).
    if (c.custom) return true;
    return Array.isArray(c.contents) && c.contents.length > 0;
  });

  return {
    kind: 'categoryToolbox',
    contents: categories,
  };
}

// Default unrestricted toolbox kept as a named export so existing
// callers (BlocklyWorkspace, tests) work without changes.
export const TOOLBOX = buildToolbox();
