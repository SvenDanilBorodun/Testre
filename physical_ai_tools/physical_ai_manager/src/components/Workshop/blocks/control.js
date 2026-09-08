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

// Phase-2 control blocks live in the Logik category, so they take the
// Logik hue (#eab308 in toolbox.js) for visual consistency.
const CONTROL_COLOR = '#eab308';

// HARD CONTRACT with the Python interpreter (workflow/interpreter.py btype
// ladder): the type ids and input NAMEs below are read verbatim server-side.
//   edubotics_forever      → statement input `DO`
//   edubotics_wait_until   → value input `BOOL` (Boolean)
// Do not rename either id or input.
export const CONTROL_BLOCKS = [
  {
    // „wiederhole fortlaufend" — Scratch-style forever / NEPO
    // „wiederhole unendlich". A C-block whose body (DO) repeats until the
    // student presses Stopp. The interpreter deliberately does NOT cap the
    // iteration count (MAX_LOOP_ITERATIONS is not applied to this block —
    // a finite cap would silently end a loop the student meant to run
    // forever); it applies a per-iteration RATE FLOOR instead
    // (FOREVER_MIN_CYCLE_S), so an all-value body cannot flood the status
    // channel. Stopp is the only exit.
    type: 'edubotics_forever',
    message0: DE.FOREVER,
    message1: '%1',
    args1: [{ type: 'input_statement', name: 'DO' }],
    previousStatement: null,
    nextStatement: null,
    colour: CONTROL_COLOR,
    tooltip:
      'Wiederholt die enthaltenen Blöcke fortlaufend, bis du auf „Stopp" '
      + 'drückst.',
  },
  {
    // „warte bis <Bedingung>" — NEPO „warte bis" / Scratch „wait until".
    // Polls the Boolean condition until it is true (or the run stops).
    type: 'edubotics_wait_until',
    message0: DE.WAIT_UNTIL,
    args0: [{ type: 'input_value', name: 'BOOL', check: 'Boolean' }],
    previousStatement: null,
    nextStatement: null,
    colour: CONTROL_COLOR,
    tooltip:
      'Hält an, bis die Bedingung erfüllt ist, und läuft dann mit dem '
      + 'nächsten Block weiter.',
  },
];

export function registerControlBlocks() {
  // Skip re-definition on HMR / Jest re-import — Blockly.defineBlocksWithJsonArray
  // throws "Block type X is already defined" on the second landing. Audit round-3 §A.
  const toDefine = CONTROL_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}

// ---------------------------------------------------------------------------
// „wiederhole fortlaufend" dead-code warning (editor half)
// ---------------------------------------------------------------------------

// Warning id. KEYED on purpose: BlockSvg.setWarningText keeps one message per
// id, so this writer can never clobber another one. The other writer today is
// RunControls' debuggerWarnings effect, which calls setWarningText(text) /
// setWarningText(null) with NO id — and an UNKEYED null disposes the whole
// warning icon, every message on it included (verified in Blockly 12.5.1's
// BlockSvg.setWarningText: `if (!id) this.removeIcon(WarningIcon.TYPE)`). That
// direction is harmless here only because those warnings come from the IK
// pre-check and land exclusively on „bewege zu" / „aufnehmen" / „ablegen bei"
// blocks, never on a forever loop — and the listener below re-asserts on the
// next workspace edit regardless.
export const FOREVER_DEAD_CODE_WARNING_ID = 'edubotics_forever_dead_code';

/**
 * Warn, in the EDITOR, about blocks snapped underneath „wiederhole
 * fortlaufend".
 *
 * The block carries a `nextStatement` connector (Scratch's forever
 * deliberately does not), so a student can and does snap blocks below it —
 * where they are silent dead code, because the only exit from the loop is
 * Stopp. `interpreter.py::_exec_forever` logs the same fact at RUN time; this
 * is the half that says so while the program is still being written.
 *
 * Removing the connector is the real fix and it is NOT backward-safe on its
 * own: `Blockly.serialization.workspaces.load` throws MissingConnection on any
 * saved workspace that already has a block there, `BlocklyWorkspace.jsx`
 * swallows that to console.error, and the next edit autosaves the truncated
 * program — the same trap that forced the `check: 'String'` revert documented
 * in blocks/motion.js.
 *
 * Shaped exactly like `attachMotionWorkspaceValidators`: takes the workspace,
 * returns a disposer the injection effect calls on teardown.
 *
 * @param {Blockly.Workspace} workspace The injected workspace.
 * @returns {() => void} Disposer that removes the change listener.
 */
export function attachControlWorkspaceValidators(workspace) {
  if (!workspace || typeof workspace.addChangeListener !== 'function') {
    return () => {};
  }
  const listener = (event) => {
    if (!event) return;
    // MOVE covers snapping/unsnapping, CREATE a paste or an undo that
    // re-materialises the chain, DELETE the block below being dragged to the
    // trash. Anything else cannot change "is something attached below".
    if (
      event.type !== Blockly.Events.BLOCK_MOVE
      && event.type !== Blockly.Events.BLOCK_CREATE
      && event.type !== Blockly.Events.BLOCK_DELETE
    ) {
      return;
    }
    if (typeof workspace.getBlocksByType !== 'function') return;
    // `false` = do not sort by position; we touch every forever block anyway.
    const foreverBlocks = workspace.getBlocksByType('edubotics_forever', false);
    foreverBlocks.forEach((block) => {
      if (!block || typeof block.setWarningText !== 'function') return;
      const hasDeadCode = typeof block.getNextBlock === 'function'
        && !!block.getNextBlock();
      block.setWarningText(
        hasDeadCode ? DE.FOREVER_DEAD_CODE_WARNING : null,
        FOREVER_DEAD_CODE_WARNING_ID,
      );
    });
  };
  workspace.addChangeListener(listener);
  return () => {
    try {
      workspace.removeChangeListener(listener);
    } catch (_) { /* workspace disposed */ }
  };
}

// ---------------------------------------------------------------------------
// controls_if — „sonst" for a freshly dragged block
// ---------------------------------------------------------------------------
//
// @blockly/block-plus-minus 9.0.10 UNREGISTERS Blockly's own
// `controls_if_mutator` and registers its own ⊕/⊖ replacement. That
// replacement's `plus()` only ever calls `addElseIf_()`, and `hasElse_` is set
// EXCLUSIVELY by `domToMutation` / `loadExtraState` — so a block that arrives
// from a saved file can have an else, but a block dragged out of the Logik
// category can NEVER get one (measured: four ⊕ presses give IF0..IF4 and no
// else). The whole category could not express if/else for new blocks, while
// the server side (`interpreter.py::_exec_if`) has always handled ELSE.
//
// The fix re-registers `controls_if_mutator` a THIRD time, after the plugin,
// with the plugin's semantics plus an else toggle. Registration therefore
// cannot live in `registerControlBlocks()` — that runs synchronously at
// injection time, long before `initPlugins`' dynamic
// `import('@blockly/block-plus-minus')` resolves and clobbers it. It is called
// from `initPlugins` instead, immediately after that import (the plugin is
// lazy-loaded on purpose: CLAUDE.md keeps every `@blockly/*` plugin out of the
// entry bundle).
//
// SERIALIZATION IS UNCHANGED, and that is the whole backward-safety argument:
// the state keys stay `elseIfCount` (number, omitted when 0) and `hasElse`
// (literal `true`, omitted otherwise), emitted in that order, with `null` —
// i.e. NO `extraState` key at all — for a bare if/then. Those are byte-for-byte
// what BOTH Blockly core 12.5.1 and the plugin write, so a workspace saved here
// loads in either of them, and one saved by either loads here. The ⊕/⊖ fields
// and the „sonst" toggle row are `FieldImage`/`FieldLabel`, neither of which is
// serializable, so they contribute nothing to the saved bytes.

const ELSE_ADD_INPUT = 'ELSE_ADD';
const ELSE_PLUS_FIELD = 'ELSE_PLUS';
const ELSE_MINUS_FIELD = 'ELSE_MINUS';
// Marker handed to plus()/minus() through the field's `args_`, so one pair of
// callbacks can serve both the else-if rows and the else row.
const ELSE_ARG = 'ELSE';

// Byte-identical to @blockly/block-plus-minus's own field images (src/
// field_plus.js / src/field_minus.js), copied rather than deep-imported: the
// package publishes only a UMD `dist/index.js` with no `exports` map and no
// `"type": "module"`, so `@blockly/block-plus-minus/src/field_plus` is an
// untranspiled ESM file inside a CJS package — resolvable today by luck, not by
// contract. Copying keeps the two ⊕ buttons visually identical without betting
// the build on that.
const PLUS_IMAGE
  = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC'
  + '9zdmciIHZlcnNpb249IjEuMSIgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0Ij48cGF0aCBkPSJNMT'
  + 'ggMTBoLTR2LTRjMC0xLjEwNC0uODk2LTItMi0ycy0yIC44OTYtMiAybC4wNzEgNGgtNC4wNz'
  + 'FjLTEuMTA0IDAtMiAuODk2LTIgMnMuODk2IDIgMiAybDQuMDcxLS4wNzEtLjA3MSA0LjA3MW'
  + 'MwIDEuMTA0Ljg5NiAyIDIgMnMyLS44OTYgMi0ydi00LjA3MWw0IC4wNzFjMS4xMDQgMCAyLS'
  + '44OTYgMi0ycy0uODk2LTItMi0yeiIgZmlsbD0id2hpdGUiIC8+PC9zdmc+Cg==';
const MINUS_IMAGE
  = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAw'
  + 'MC9zdmciIHZlcnNpb249IjEuMSIgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0Ij48cGF0aCBkPS'
  + 'JNMTggMTFoLTEyYy0xLjEwNCAwLTIgLjg5Ni0yIDJzLjg5NiAyIDIgMmgxMmMxLjEwNCAw'
  + 'IDItLjg5NiAyLTJzLS44OTYtMi0yLTJ6IiBmaWxsPSJ3aGl0ZSIgLz48L3N2Zz4K';

/** The plugin's serialization_helper.getExtraBlockState, re-implemented. */
function extraBlockState(block) {
  if (block.saveExtraState) {
    const state = block.saveExtraState();
    return state ? JSON.stringify(state) : '';
  }
  if (block.mutationToDom) {
    const state = block.mutationToDom();
    return state ? Blockly.Xml.domToText(state) : '';
  }
  return '';
}

/**
 * Run `apply` inside one event group and fire the BlockChange('mutation')
 * event Blockly's undo stack needs — the plugin's `onClick_` pattern. Without
 * the event the shape changes and Rückgängig cannot restore it.
 */
function withMutationEvent(block, apply) {
  if (block.isInFlyout) return;
  Blockly.Events.setGroup(true);
  try {
    const before = extraBlockState(block);
    apply();
    const after = extraBlockState(block);
    if (before !== after) {
      Blockly.Events.fire(
        new Blockly.Events.BlockChange(block, 'mutation', null, before, after),
      );
    }
  } finally {
    Blockly.Events.setGroup(false);
  }
}

function makeToggleField(image, arg, handler) {
  const field = new Blockly.FieldImage(image, 15, 15, undefined, (clicked) => {
    const block = clicked.getSourceBlock();
    if (!block) return;
    withMutationEvent(block, () => handler.call(block, arg));
  });
  return field;
}

/** Localized „sonst". Falls back to the German literal if no locale is set. */
function elseLabel() {
  return Blockly.Msg['CONTROLS_IF_MSG_ELSE'] || DE.IF_ELSE_ADD;
}

// The mixin. `elseIfCount_` / `hasElse_` keep the plugin's names and meaning so
// the save/load shape is identical; `addElseIf_` / `removeElseIf_` /
// `updateShape_` are the plugin's own logic, with `removeElseIf_`'s shift loop
// taught about the toggle row (see there).
const controlsIfElseMutator = {
  elseIfCount_: 0,
  hasElse_: false,

  mutationToDom: function mutationToDom() {
    if (!this.elseIfCount_ && !this.hasElse_) {
      return null;
    }
    const container = Blockly.utils.xml.createElement('mutation');
    container.setAttribute('elseif', this.elseIfCount_);
    if (this.hasElse_) {
      // Has to be stored as an int for backwards compat.
      container.setAttribute('else', 1);
    }
    return container;
  },

  domToMutation: function domToMutation(xmlElement) {
    const targetCount = parseInt(xmlElement.getAttribute('elseif'), 10) || 0;
    this.hasElse_ = !!parseInt(xmlElement.getAttribute('else'), 10);
    this.applyElseState_();
    this.updateShape_(targetCount);
  },

  saveExtraState: function saveExtraState() {
    if (!this.elseIfCount_ && !this.hasElse_) {
      return null;
    }
    const state = Object.create(null);
    if (this.elseIfCount_) {
      state['elseIfCount'] = this.elseIfCount_;
    }
    if (this.hasElse_) {
      state['hasElse'] = true;
    }
    return state;
  },

  loadExtraState: function loadExtraState(state) {
    const targetCount = state['elseIfCount'] || 0;
    this.hasElse_ = !!state['hasElse'];
    this.applyElseState_();
    this.updateShape_(targetCount);
  },

  /**
   * Bring the ELSE / „sonst"-toggle rows in line with `hasElse_`.
   *
   * The plugin only ever ADDS an ELSE input here; it never removes one,
   * because nothing in it could clear `hasElse_`. Now that ⊖ can, the removal
   * branch is required — otherwise undoing „sonst hinzufügen" would leave a
   * stale ELSE input on a block whose state says it has none, and the next
   * save would drop `hasElse` while the row was still on screen.
   */
  applyElseState_: function applyElseState_() {
    if (this.hasElse_) {
      if (!this.getInput('ELSE')) {
        this.appendStatementInput('ELSE')
          .appendField(elseLabel())
          .appendField(
            makeToggleField(MINUS_IMAGE, ELSE_ARG, this.minus),
            ELSE_MINUS_FIELD,
          );
      }
      this.removeInput(ELSE_ADD_INPUT, /* opt_quiet */ true);
    } else {
      // Disconnect any body FIRST so the student's blocks survive on the
      // canvas: removeInput disposes what is plugged into the input, and this
      // branch is reached by Rückgängig as well as by ⊖. Blockly core's own
      // updateShape_ disposes here; we deliberately do not.
      const existing = this.getInput('ELSE');
      if (existing && existing.connection && existing.connection.isConnected()) {
        existing.connection.disconnect();
        this.bumpNeighbours();
      }
      this.removeInput('ELSE', /* opt_quiet */ true);
      if (!this.getInput(ELSE_ADD_INPUT)) {
        this.appendDummyInput(ELSE_ADD_INPUT)
          .appendField(elseLabel())
          .appendField(
            makeToggleField(PLUS_IMAGE, ELSE_ARG, this.plus),
            ELSE_PLUS_FIELD,
          );
      }
    }
  },

  updateShape_: function updateShape_(targetCount) {
    while (this.elseIfCount_ < targetCount) {
      this.addElseIf_();
    }
    while (this.elseIfCount_ > targetCount) {
      this.removeElseIf_();
    }
  },

  plus: function plus(args) {
    if (args === ELSE_ARG) {
      if (this.hasElse_) return;
      this.hasElse_ = true;
      this.applyElseState_();
      return;
    }
    this.addElseIf_();
  },

  minus: function minus(args) {
    if (args === ELSE_ARG) {
      if (!this.hasElse_) return;
      this.hasElse_ = false;
      // applyElseState_ disconnects the body before removing the input, so a
      // mis-click costs the student nothing but a re-drag.
      this.applyElseState_();
      return;
    }
    if (this.elseIfCount_ === 0) {
      return;
    }
    this.removeElseIf_(args);
  },

  addElseIf_: function addElseIf_() {
    // Because else-if inputs are 1-indexed we increment first, decrement last.
    this.elseIfCount_++;
    this.appendValueInput('IF' + this.elseIfCount_)
      .setCheck('Boolean')
      .appendField(Blockly.Msg['CONTROLS_IF_MSG_ELSEIF'])
      .appendField(
        makeToggleField(MINUS_IMAGE, this.elseIfCount_, this.minus),
        'MINUS' + this.elseIfCount_,
      );
    this.appendStatementInput('DO' + this.elseIfCount_).appendField(
      Blockly.Msg['CONTROLS_IF_MSG_THEN'],
    );

    // Whichever of the two trailing rows exists stays LAST. Exactly one of
    // them does — that invariant is what keeps removeElseIf_'s index
    // arithmetic (elseIfIndex = index * 2 over IF/DO pairs) correct.
    if (this.getInput('ELSE')) {
      this.moveInputBefore('ELSE', /* put at end */ null);
    } else if (this.getInput(ELSE_ADD_INPUT)) {
      this.moveInputBefore(ELSE_ADD_INPUT, /* put at end */ null);
    }
  },

  removeElseIf_: function removeElseIf_(index = undefined) {
    // The strategy for removing a part at an index is to:
    //  - Kick any blocks connected to the relevant inputs.
    //  - Move all connect blocks from the other inputs up.
    //  - Remove the last input.
    // This makes sure all of our indices are correct.
    if (index !== undefined && index !== this.elseIfCount_) {
      // Each else-if is two inputs on the block:
      // the else-if input and the do input.
      const elseIfIndex = index * 2;
      const inputs = this.inputList;
      let connection = inputs[elseIfIndex].connection; // If connection.
      if (connection.isConnected()) {
        connection.disconnect();
      }
      connection = inputs[elseIfIndex + 1].connection; // Do connection.
      if (connection.isConnected()) {
        connection.disconnect();
      }
      this.bumpNeighbours();
      for (let i = elseIfIndex + 2, input; (input = this.inputList[i]); i++) {
        // The plugin breaks on 'ELSE' alone. ELSE_ADD is the row that stands
        // in for it while the block has no else, it is a DUMMY input with a
        // null connection, and it is equally always last — so it must break
        // here too or `input.connection.targetConnection` throws and the
        // student's block is left half-rebuilt.
        if (input.name === 'ELSE' || input.name === ELSE_ADD_INPUT) {
          break; // Should be last, so break.
        }
        const targetConnection = input.connection.targetConnection;
        if (targetConnection) {
          this.inputList[i - 2].connection.connect(targetConnection);
        }
      }
    }

    this.removeInput('IF' + this.elseIfCount_);
    this.removeInput('DO' + this.elseIfCount_);
    // Because else-if inputs are 1-indexed we increment first, decrement last.
    this.elseIfCount_--;
  },
};

/** Adds the ⊕ button and the „sonst" toggle row to a fresh if block. */
function controlsIfElseHelper() {
  this.getInput('IF0').insertFieldAt(
    0,
    makeToggleField(PLUS_IMAGE, undefined, this.plus),
    'PLUS',
  );
  // `hasElse_` is false at construction; this materialises the toggle row.
  // A block that then loads `hasElse: true` swaps it for the real ELSE input.
  this.applyElseState_();
}

/**
 * Re-register `controls_if_mutator` with else support.
 *
 * MUST be called AFTER `import('@blockly/block-plus-minus')` resolves — the
 * plugin unregisters and re-registers the same name at import time, so an
 * earlier registration is simply overwritten.
 *
 * Idempotent: re-registering the same name is what both Blockly core and the
 * plugin already do, and mixins are applied per block at CONSTRUCTION time, so
 * blocks that already exist keep whichever mutator they were built with. That
 * is also why blocks restored by `Blockly.serialization.workspaces.load` on the
 * injection tick keep Blockly core's mutator (the load is synchronous, the
 * plugin import is not) — they already had a working else via the gear dialog,
 * and both mutators write the same bytes.
 */
export function registerControlsIfElseMutator() {
  if (Blockly.Extensions.isRegistered('controls_if_mutator')) {
    Blockly.Extensions.unregister('controls_if_mutator');
  }
  Blockly.Extensions.registerMutator(
    'controls_if_mutator',
    controlsIfElseMutator,
    controlsIfElseHelper,
  );
}
