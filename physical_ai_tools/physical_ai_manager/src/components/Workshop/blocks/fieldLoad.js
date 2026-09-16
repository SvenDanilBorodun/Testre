/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// ---------------------------------------------------------------------------
// The DESERIALIZATION seam: a field validator guards EDITS, never LOADS.
// ---------------------------------------------------------------------------
//
// A Blockly field validator runs on every `setValue`, and
// `Blockly.serialization` sets each saved value through exactly that path — so
// every validator in this folder was also judging bytes a student had already
// saved, and rewriting them. Measured on a plain load of a realistic document
// (2026-09-16), 10 of 15 blocks came back changed: a pin named „Ablage.1"
// loaded as „Ablage1", „!!!" collapsed to the block DEFAULT „A" (so two pins
// could silently become one), a 38-character name was cut to 24, and
// `Bewegung [2]` became `Bewegung 1` — a name that can address a DIFFERENT real
// recording. The rewrite then reached Redux and the autosave on the same tick,
// because Blockly delivers load events asynchronously and nothing downstream
// can tell a load event from a student's edit.
//
// `Field.loadState` is the discriminator the validators lack: it exists on
// Field, FieldTextInput, FieldDropdown and FieldNumber, and Blockly calls it
// ONLY from the serializer (verified against Blockly 12.5.1 — a prototype spy
// recorded exactly the loaded values and nothing else). The alternative
// discriminator — `Blockly.Events.isEnabled() === false` during a load — is
// true inside ANY `Events.disable()` block, ours included, so it would also
// suppress validation in places that are not deserialization at all.
//
// INVARIANT: deserialization never discards or rewrites a value a student
// saved. What the server refuses, the server refuses out loud, in German, at
// run time — `blocks/motion.js` documents the same choice for the Greifziel
// sockets: „the server refusal — which is loud, German, and cannot eat a saved
// file — stands alone."  `blocks/savedValueWarnings.js` is the editor half: it
// marks such a value on the block instead of silently repairing it.

/**
 * Wire a validator that guards EDITS only.
 *
 * Wraps the field's own `loadState` so anything the serializer writes is kept
 * verbatim, and routes every other `setValue` through `validator` unchanged.
 * The wrapper lives on the field INSTANCE (Blockly creates one field per
 * block), so no prototype and no other block type is affected.
 *
 * @param {Blockly.Field} field The field to guard, usually from `this.getField`.
 * @param {function(*): *} validator The interactive-edit validator.
 */
export function setEditValidator(field, validator) {
  if (!field || typeof field.setValidator !== 'function') return;
  if (typeof validator !== 'function') return;
  const loadState = typeof field.loadState === 'function'
    ? field.loadState.bind(field)
    : null;
  field.loadState = function loadSavedValue(state) {
    field.eduLoadingSavedValue_ = true;
    try {
      if (loadState) {
        loadState(state);
      } else {
        field.setValue(state);
      }
    } finally {
      field.eduLoadingSavedValue_ = false;
    }
  };
  field.setValidator(function validateEdit(value) {
    // Deserialization: hands off. Everything else is a student edit.
    if (field.eduLoadingSavedValue_) return value;
    return validator.call(this, value);
  });
}
