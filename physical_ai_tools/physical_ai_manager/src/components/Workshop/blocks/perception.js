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

const PERCEPTION_COLOR = '#22c55e';

// Timeout bounds for the wait_until_object_seen block. Above 120 s a
// student is almost certainly blocking a workshop session by accident.
const TIMEOUT_MIN_S = 1;
const TIMEOUT_MAX_S = 120;

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
  'edubotics_find_object',
  'edubotics_while_visible',
  'edubotics_wait_until_object_seen',
  'edubotics_when_object_seen',
];
// ── Grasp split (Phase 1) — static JSON perception blocks ────────────────────
// Value/statement blocks that produce or consume a Greifziel. Unlike the
// named-object blocks above they do NOT use the runtime dropdown, so they're
// plain JSON registered with the same HMR/Jest re-definition guard as the
// other files. Input/field NAMEs are a hard contract with the Python server
// (`_build_args` lowercases them → ZIEL → arg `ziel`).
const PERCEPTION_JSON_BLOCKS = [
  {
    type: 'edubotics_object_position',
    message0: DE.OBJECT_POSITION,
    args0: [{ type: 'input_value', name: 'ZIEL', check: 'Greifziel' }],
    // Output 'String' to match edubotics_destination_ref so it plugs into the
    // same „bewege zu" / „ablegen bei" sockets (which carry no check today).
    output: 'String',
    colour: PERCEPTION_COLOR,
    tooltip:
      'Liefert die Position eines erkannten Greifziels als Ziel-Wert, der '
      + 'in „bewege zu" oder „ablegen bei" gesteckt werden kann.',
  },
  {
    type: 'edubotics_grasp_held',
    message0: DE.GRASP_HELD,
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
    tooltip: 'Wahr, wenn der Greifer gerade ein Objekt hält.',
  },
  {
    type: 'edubotics_mark_done',
    message0: DE.MARK_DONE,
    args0: [{ type: 'input_value', name: 'ZIEL', check: 'Greifziel' }],
    previousStatement: null,
    nextStatement: null,
    colour: PERCEPTION_COLOR,
    tooltip:
      'Merkt sich dieses Greifziel als erledigt, damit es nicht erneut '
      + 'gegriffen wird.',
  },
];

const _objectTypePlaceholder = () => [[DE.OBJECT_TYPE_LOADING, '__none__']];
const _objectTypeEmpty = () => [[DE.OBJECT_TYPE_EMPTY, '__none__']];
let OBJECT_TYPE_OPTIONS = _objectTypePlaceholder();

// Passed to new Blockly.FieldDropdown(...). Blockly re-invokes it whenever the
// menu (re)renders, so once the cache is filled the menu shows real options.
// MUST always return a non-empty [label, value] array.
//
// Blockly calls the generator with the FIELD as `this`, which is what lets a
// block keep an object type the catalog does not (yet) know: a FieldDropdown
// REFUSES any value outside its options, so without this entry a saved
// „kugel" was dropped to the „(lädt …)" placeholder on load and then written
// back over the student's file. See makeObjectTypeField below.
function objectTypeOptions() {
  const options = OBJECT_TYPE_OPTIONS.length ? OBJECT_TYPE_OPTIONS : _objectTypeEmpty();
  const saved = this && typeof this.eduSavedObjectType_ === 'string'
    ? this.eduSavedObjectType_
    : null;
  if (!saved || saved === '__none__') return options;
  if (options.some(([, value]) => value === saved)) return options;
  // Only while the block still HOLDS it: once the student picks something else
  // the unknown type is theirs to lose, and the entry disappears with it. The
  // placeholder counts as "not yet set" — DURING the load the field still holds
  // it, and that is exactly when the entry has to be there for the incoming
  // value to pass `doClassValidation_`.
  const held = typeof this.getValue === 'function' ? this.getValue() : null;
  if (held !== null && held !== saved && held !== '__none__') return options;
  return options.concat([[`${saved} ${DE.OBJECT_TYPE_UNKNOWN}`, saved]]);
}

// One OBJECT_TYPE field, with the saved value remembered across the load.
//
// `loadState` is the only hook Blockly calls exclusively from the serializer
// (blocks/fieldLoad.js explains the seam), and it runs BEFORE the value is
// validated — which is exactly what the generator needs, because
// `doClassValidation_` asks for the options while the field still holds its
// DEFAULT and would otherwise never see the incoming value.
function makeObjectTypeField() {
  const field = new Blockly.FieldDropdown(objectTypeOptions);
  const loadState = typeof field.loadState === 'function'
    ? field.loadState.bind(field)
    : null;
  field.loadState = function loadSavedObjectType(state) {
    if (typeof state === 'string') field.eduSavedObjectType_ = state;
    // FieldDropdown validates against its CACHED option list, which was built
    // when the block was constructed — i.e. before the saved value was known.
    // Re-running the generator un-cached is what puts the „(unbekannt)" entry
    // in front of `doClassValidation_`; without it the value is still refused.
    if (typeof field.getOptions === 'function') field.getOptions(false);
    if (loadState) {
      loadState(state);
    } else {
      field.setValue(state);
    }
  };
  return field;
}

let _workspaceAccessor = null;

// WorkshopPage wires this so a late catalog refresh can reach the live blocks.
export function setWorkspaceAccessor(fn) {
  _workspaceAccessor = typeof fn === 'function' ? fn : null;
}

// Re-resolve + re-render every live OBJECT_TYPE dropdown after the catalog
// arrives (the generator's cached option list would otherwise persist on a
// block dragged out before the fetch resolved).
//
// It promotes the PLACEHOLDER and nothing else. Snapping every value the
// catalog does not contain to `valid[0]` is what turned a saved „kugel" into
// „wuerfel" the moment the catalog landed — silently, unundoably (Blockly
// refuses to set the placeholder back), and on the ORDINARY lesson-start path,
// since the catalog fetch waits for `calibrated || simMode` while
// EDUBOTICS_FORCE_RECALIBRATION ships ON. A block the student never touched
// keeps what the student saved; only „(lädt …)" — which is ours, not theirs —
// is replaced.
function refreshObjectTypeDropdowns() {
  const ws = _workspaceAccessor ? _workspaceAccessor() : null;
  if (!ws || typeof ws.getAllBlocks !== 'function') return;
  const valid = objectTypeOptions().map((o) => o[1]);
  ws.getAllBlocks(false).forEach((b) => {
    if (!OBJECT_TYPE_BLOCK_TYPES.includes(b.type)) return;
    const field = b.getField('OBJECT_TYPE');
    if (!field) return;
    if (typeof field.getOptions === 'function') field.getOptions(false); // re-run generator, un-cached
    if (valid.length && field.getValue() === '__none__' && valid[0] !== '__none__') {
      field.setValue(valid[0]); // the placeholder is ours to replace
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

export function registerPerceptionBlocks() {
  // Named-object blocks: a server-fed dropdown can't be expressed in
  // defineBlocksWithJsonArray (its field_dropdown.options must be a static
  // array), so these three use a custom init() with a generator-function
  // FieldDropdown (objectTypeOptions). HMR/StrictMode-guarded.
  // Field name OBJECT_TYPE serializes to fields:{OBJECT_TYPE} → args['object_type'].
  // Tooltips describe what handlers/perception_blocks.py actually does: all
  // four skip instances already CLAIMED this run (grasp_object claims a tag on
  // success, mark_done claims it by hand), which is the single most surprising
  // thing about them and is why „Anzahl" can shrink as a program runs.
  defineObjectTypeBlock(
    'edubotics_grasp_object', DE.GRASP_OBJECT_PREFIX, 'statement',
    'Sucht ein Objekt dieses Typs, fährt darüber, greift es von oben und '
    + 'prüft, ob es wirklich hält. Klappt es nicht, wird es noch einmal '
    + 'versucht. Jedes Objekt wird nur einmal gegriffen.');
  defineObjectTypeBlock(
    'edubotics_see_object', DE.SEE_OBJECT_PREFIX, 'Boolean',
    'Wahr, wenn gerade mindestens ein Objekt dieses Typs zu sehen ist. '
    + 'Bereits gegriffene Objekte zählen nicht mit.');
  defineObjectTypeBlock(
    'edubotics_count_object', DE.COUNT_OBJECT_PREFIX, 'Number',
    'Zählt, wie viele Objekte dieses Typs gerade zu sehen sind. Bereits '
    + 'gegriffene Objekte zählen nicht mit.');
  // Grasp split (Phase 1): „finde <Typ>" outputs a Greifziel value (the helper
  // calls setOutput(true, kind) for any non-'statement' kind).
  defineObjectTypeBlock(
    'edubotics_find_object', DE.FIND_OBJECT_PREFIX, 'Greifziel',
    'Sucht ein Objekt dieses Typs und gibt es als Greifziel zurück — oder '
    + '„nichts", wenn keines zu sehen ist. Merke dir das Ergebnis in einer '
    + 'Variablen, statt „finde" mehrmals hintereinander zu verwenden.');

  // Static JSON perception blocks (no runtime dropdown). HMR/Jest-guarded.
  const toDefine = PERCEPTION_JSON_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }

  // P2 named-object loop + event blocks. Custom init (dynamic OBJECT_TYPE
  // dropdown can't be expressed in JSON). HMR/StrictMode-guarded.
  // „Solange <Typ> sichtbar { … }" — a C-shaped CONTROL block with a DO body.
  if (!Blockly.Blocks['edubotics_while_visible']) {
    Blockly.Blocks['edubotics_while_visible'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WHILE_VISIBLE_PREFIX)
          .appendField(makeObjectTypeField(), 'OBJECT_TYPE')
          .appendField(DE.WHILE_VISIBLE_SUFFIX)
          // #6 optional repetition cap: 0 = unbegrenzt (the server's wall-clock
          // and no-progress guards still bound it). The field key MAX_REPS is
          // read by interpreter._exec_while_visible.
          .appendField(DE.WHILE_VISIBLE_MAX_PREFIX)
          .appendField(new Blockly.FieldNumber(0, 0, 999, 1), 'MAX_REPS')
          .appendField(DE.WHILE_VISIBLE_MAX_SUFFIX);
        this.appendStatementInput('DO').setCheck(null);
        this.setPreviousStatement(true, null);
        this.setNextStatement(true, null);
        this.setColour(PERCEPTION_COLOR);
        this.setTooltip(
          'Wiederholt den Rumpf, solange noch ein Objekt dieses Typs sichtbar '
          + 'ist (greift z. B. eines nach dem anderen). „höchstens N Mal" '
          + 'begrenzt die Anzahl der Durchläufe; 0 bedeutet unbegrenzt.');
      },
    };
  }
  // „warte bis <Typ> sichtbar (max N s)" — Boolean value block + TIMEOUT field.
  if (!Blockly.Blocks['edubotics_wait_until_object_seen']) {
    Blockly.Blocks['edubotics_wait_until_object_seen'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WAIT_UNTIL_OBJECT_SEEN_PREFIX)
          .appendField(makeObjectTypeField(), 'OBJECT_TYPE')
          .appendField(DE.WAIT_UNTIL_OBJECT_SEEN_MID)
          .appendField(
            new Blockly.FieldNumber(10, TIMEOUT_MIN_S, TIMEOUT_MAX_S, 1), 'TIMEOUT')
          .appendField(DE.WAIT_UNTIL_OBJECT_SEEN_SUFFIX);
        this.setOutput(true, 'Boolean');
        this.setColour(PERCEPTION_COLOR);
        // perception_blocks.wait_until_object_seen polls and RAISES a German
        // timeout — it does not quietly return false, so say so.
        this.setTooltip(
          'Wartet, bis ein Objekt dieses Typs zu sehen ist. Taucht in der '
          + 'eingestellten Zeit keines auf, bricht das Programm mit einer '
          + 'Meldung ab.');
      },
    };
  }
  // „warte bis Greifer hält (max N s)" — Boolean value block + TIMEOUT field,
  // NO object dropdown (it watches the gripper, not the scene). Custom init for
  // symmetry with wait_until_object_seen; field name TIMEOUT → args['timeout']
  // (a hard contract with perception_blocks.wait_until_held).
  if (!Blockly.Blocks['edubotics_wait_until_held']) {
    Blockly.Blocks['edubotics_wait_until_held'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WAIT_UNTIL_HELD_PREFIX)
          .appendField(
            new Blockly.FieldNumber(10, TIMEOUT_MIN_S, TIMEOUT_MAX_S, 1), 'TIMEOUT')
          .appendField(DE.WAIT_UNTIL_HELD_SUFFIX);
        this.setOutput(true, 'Boolean');
        this.setColour(PERCEPTION_COLOR);
        this.setTooltip(
          'Wartet, bis der Greifer ein Objekt hält (oder die Zeit abläuft). ' +
          'Verwende ihn direkt nach „Greifer schließen" — bei geöffnetem ' +
          'Greifer kann fälschlich „hält" gemeldet werden.');
      },
    };
  }
  // „wenn <Typ> erkannt" — a HAT block (top-only: nextStatement, no previous).
  if (!Blockly.Blocks['edubotics_when_object_seen']) {
    Blockly.Blocks['edubotics_when_object_seen'] = {
      init() {
        this.appendDummyInput()
          .appendField(DE.WHEN_OBJECT_SEEN_PREFIX)
          .appendField(makeObjectTypeField(), 'OBJECT_TYPE')
          .appendField(DE.WHEN_OBJECT_SEEN_SUFFIX);
        this.setNextStatement(true, null);   // hat: top-only
        this.setColour(PERCEPTION_COLOR);
        // Edge-triggered on the SET of unclaimed-visible tag ids changing, so
        // it fires once per OBJECT, not once per run and not repeatedly while
        // the same object stays in view. That is the whole point of the block
        // and is impossible to guess from the label.
        this.setTooltip(
          'Startet die Blöcke darunter, sobald ein neues Objekt dieses Typs '
          + 'auftaucht. Für jedes Objekt genau einmal — nicht immer wieder, '
          + 'solange es liegen bleibt.');
      },
    };
  }
}

// Define one named-object block. `kind` is 'statement' (chains vertically, no
// output) or an output type string ('Boolean' | 'Number'). `tooltip` is the
// German hover text — REQUIRED: this helper used to set none at all, so all
// four named-object blocks (the most-used blocks in the editor) shipped with an
// empty tooltip while every hand-written block around them had one.
function defineObjectTypeBlock(type, prefix, kind, tooltip) {
  if (Blockly.Blocks[type]) return; // HMR / Jest re-import guard
  Blockly.Blocks[type] = {
    init() {
      this.appendDummyInput()
        .appendField(prefix)
        .appendField(makeObjectTypeField(), 'OBJECT_TYPE');
      if (kind === 'statement') {
        this.setPreviousStatement(true, null);
        this.setNextStatement(true, null);
      } else {
        this.setOutput(true, kind);
      }
      this.setColour(PERCEPTION_COLOR);
      this.setTooltip(tooltip);
    },
  };
}
