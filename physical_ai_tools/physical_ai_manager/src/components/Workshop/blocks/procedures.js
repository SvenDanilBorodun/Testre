/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import * as Blockly from 'blockly/core';

// ---------------------------------------------------------------------------
// „Funktionen": removing a parameter must not re-shuffle the ARGUMENTS
// ---------------------------------------------------------------------------
//
// Measured on Blockly 12.5.1 + @blockly/block-plus-minus 9.0.10 (2026-09-16):
// a function `tu(x, y, z)` called as `tu(11, 22, 33)`, ⊖ on the FIRST parameter
// →  the call became `tu(y=11, z=22)` and the 33 block was left loose on the
// canvas. The program still runs, the server still binds positionally, and
// every value is now attached to the WRONG parameter — a silently different
// program whose only trace is one orphaned block.
//
// TWO independent causes, both necessary:
//
//   A. Blockly's own caller (`PROCEDURE_CALL_COMMON.setProcedureParameters_`)
//      keeps arguments attached to their parameter only while the definition's
//      MUTATOR BUBBLE is open: with no bubble it wipes `quarkConnections_` /
//      `quarkIds_` and re-attaches strictly by POSITION. The gear dialog was
//      always that bubble. With ⊕/⊖ there is no bubble, ever.
//   B. The plugin's definition writes the parameter id as `paramid`, Blockly's
//      caller reads `paramId`, and the element lives in Blockly's XML
//      namespace, where `getAttribute` is case-sensitive — so the ids arrive as
//      `[null, null, …]` even when a bubble IS open.
//
// The fix re-seats each argument by the identity of the parameter it was
// written for, on the two CALL block tables. They are plain objects that
// Blockly copies into each block at construction, so patching the table before
// any block exists is enough — the same seam `registerControlsIfElseMutator`
// already uses for `controls_if`, and the reason this is not done on the
// definition side: the definition's methods live on the mutator MIXIN the
// plugin never exports.
//
// SAVED BYTES ARE UNCHANGED. The caller's `extraState` keeps Blockly's own
// `{name, params:[names]}` shape; the id map lives on an in-memory field. A
// document written before this change loads through the stock path and is
// re-saved byte-identically.

/** Identity map for the current ARG sockets — in memory, never serialized. */
const ARG_IDS = 'eduArgIds_';

const CALL_BLOCK_TYPES = ['procedures_callnoreturn', 'procedures_callreturn'];

const allUnique = (list) => new Set(list).size === list.length;
const usableIds = (ids, length) => Array.isArray(ids)
  && ids.length === length
  && ids.every((id) => typeof id === 'string' && id !== '');

/**
 * Re-seat the caller's arguments by parameter identity.
 *
 * `keyPrev[i]` identifies the parameter currently in socket ARG*i*;
 * `keyNext[i]` identifies the parameter that will be in socket ARG*i*.
 *
 * The EVENT discipline is what makes Rückgängig work, and it is the whole
 * subtlety here:
 *
 *   * the blocks whose parameter genuinely DISAPPEARED are unplugged with
 *     events ON, so each one is recorded as a single MOVE, exactly as core
 *     would have recorded it;
 *   * the survivors are re-seated with events OFF, so they contribute no
 *     events of their own — undo replays the definition's own
 *     `change:mutation` (which re-runs this function in the other direction)
 *     and then the recorded MOVE, and everything lands where it started.
 *
 * With the survivors' moves recorded as well, undo fought the mutation and
 * restored a mangled call (measured: `x=11, y=33, z=∅`).
 */
function reseatByIdentity(caller, keyPrev, keyNext, names, ids, stock) {
  const held = {};
  keyPrev.forEach((key, index) => {
    const input = caller.getInput(`ARG${index}`);
    held[key] = (input && input.connection && input.connection.targetConnection) || null;
  });

  Object.keys(held).forEach((key) => {
    const connection = held[key];
    if (!connection || keyNext.includes(key)) return;
    const block = connection.getSourceBlock();
    if (!block || block.isDisposed()) return;
    block.unplug();
    if (typeof block.bumpNeighbours === 'function') block.bumpNeighbours();
  });

  Blockly.Events.disable();
  try {
    stock.call(caller, names, ids);
    keyNext.forEach((key, index) => {
      const input = caller.getInput(`ARG${index}`);
      const connection = input && input.connection;
      if (connection && connection.targetConnection) connection.disconnect();
    });
    keyNext.forEach((key, index) => {
      const target = held[key];
      const input = caller.getInput(`ARG${index}`);
      if (!target || !input || !input.connection) return;
      const block = target.getSourceBlock();
      if (!block || block.isDisposed()) return;
      input.connection.connect(target);
    });
  } finally {
    Blockly.Events.enable();
  }
}

/**
 * Teach „Funktionsaufruf" blocks to keep every argument on its own parameter.
 *
 * MUST run AFTER `import('@blockly/block-plus-minus')` resolves: the plugin
 * replaces the DEFINITION blocks at import time, and it is the definition that
 * drives `Blockly.Procedures.mutateCallers`. Idempotent.
 */
export function registerProcedureCallArgumentFix() {
  CALL_BLOCK_TYPES.forEach((type) => {
    const table = Blockly.Blocks[type];
    if (!table || table.eduArgIdentityPatched_) return;
    const stockSetParameters = table.setProcedureParameters_;
    if (typeof stockSetParameters !== 'function') return;

    // Cause B. Blockly's own body, with the one `??` that reads the plugin's
    // lower-case spelling as well.
    table.domToMutation = function domToMutation(xmlElement) {
      const names = [];
      const ids = [];
      Array.from(xmlElement.childNodes).forEach((child) => {
        if (String(child.nodeName).toLowerCase() !== 'arg') return;
        names.push(child.getAttribute('name'));
        ids.push(child.getAttribute('paramId') ?? child.getAttribute('paramid'));
      });
      this.renameProcedure(this.getProcedureCall(), xmlElement.getAttribute('name'));
      this.setProcedureParameters_(names, ids);
    };

    // Cause A.
    table.setProcedureParameters_ = function setProcedureParameters(names, ids) {
      const previousNames = (this.arguments_ || []).slice();
      const previousIds = Array.isArray(this[ARG_IDS])
        && this[ARG_IDS].length === previousNames.length
        ? this[ARG_IDS].slice()
        : null;
      const haveIds = usableIds(ids, names.length);

      // Identity is needed whenever the SHAPE changes — a removal and the undo
      // of one alike. Ids are preferred (they survive a rename); names carry
      // the same information for a document that was just reopened, where no
      // definition change has handed this caller its ids yet. Equal length with
      // no usable ids is a rename or a reorder, which core already handles by
      // position.
      const shapeChanged = names.length !== previousNames.length;
      let keyPrev = null;
      let keyNext = null;
      if (haveIds && previousIds && allUnique(previousIds) && allUnique(ids)) {
        keyPrev = previousIds;
        keyNext = ids;
      } else if (shapeChanged && allUnique(previousNames) && allUnique(names)) {
        keyPrev = previousNames;
        keyNext = names;
      }

      if (!keyPrev) {
        const result = stockSetParameters.call(this, names, ids);
        if (haveIds) this[ARG_IDS] = ids.slice();
        return result;
      }

      reseatByIdentity(this, keyPrev, keyNext, names, ids, stockSetParameters);
      this[ARG_IDS] = haveIds ? ids.slice() : null;
      return undefined;
    };

    table.eduArgIdentityPatched_ = true;
  });
}

export default registerProcedureCallArgumentFix;
