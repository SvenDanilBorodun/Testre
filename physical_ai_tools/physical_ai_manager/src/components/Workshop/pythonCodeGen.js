/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Roboter Studio — Blockly → real-Python code generator (read-only "Code"
// preview). Renders every `edubotics_*` block that has a robot_api row as the
// text API's own `robot.*` call, from that row (see registerForBlocks), so the
// preview is a program the robot would run. The built-in Blockly blocks
// (controls_*, logic_*, math_*, lists_*, text*, variables_*, procedures_*) are
// handled for free by Blockly's own `pythonGenerator.forBlock` entries, which
// register themselves as a side effect of importing `blockly/python`.
//
// LAZY CHUNK: `blockly/python` (the Python generator + the full standard-block
// generator set, ~hundreds of KB) is pulled in via a DYNAMIC `import()` so it
// never lands in the entry/WorkshopPage bundle the white-screen CI greps
// watch. It only loads when the student first opens the „Code"-panel and a
// generation runs. (Blockly v12: pythonGenerator.forBlock dispatch, statement
// generators return a string, value generators return `[code, Order]`.
// Docs: https://docs.blockly.com/guides/create-custom-blocks/code-generation/block-code/)
//
// NOTE: this module is a PURE DISPLAY path — the produced Python is never
// executed by the editor (test_preview_matches_api.py checks every robot call
// in it against the server's table, test_blockly_conformance.py runs the
// built-in blocks' preview with CPython). The real program runs server-side
// through the Blockly-JSON interpreter (workflow/interpreter.py); this is a
// human-readable mirror of the block tree, keyed on the SAME block type ids +
// field/input NAMEs.

import robotApi from './code/robot_api.json';

// ── Python-literal + identifier helpers ──────────────────────────────────────

// Single-quoted Python string literal with proper escaping. Single quotes
// match Blockly's OWN built-in generators (the `text` block, which carries the
// student's free-text, quotes with single quotes too), so the whole preview
// reads uniformly. Umlauts stay literal (valid Python 3 source).
function pyStr(value) {
  const s = String(value == null ? '' : value);
  return `'${s
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')}'`;
}

// Numeric field → bare number token. Falls back to `fallback` when the field
// is missing or non-finite.
function num(value, fallback = '0') {
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : fallback;
}

// Slugify any label/name to a safe Python identifier fragment for the
// `def when_…():` hat wrappers (lower-case, umlauts transliterated, every other
// non-word run → `_`, never empty, never leading-digit).
function slug(value) {
  let t = String(value == null ? '' : value).toLowerCase().trim();
  t = t
    .replace(/ä/g, 'ae')
    .replace(/ö/g, 'oe')
    .replace(/ü/g, 'ue')
    .replace(/ß/g, 'ss');
  t = t.replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');
  if (!t) t = 'ereignis';
  if (/^[0-9]/.test(t)) t = `_${t}`;
  return t;
}

// The only fields (and the pin's one dummy input) of a row block that carry
// no robot_api parameter: the per-move „Tempo" dropdown (rendered as a German
// comment), the pin's „fahre dorthin" row and button, and the reference
// block's „Ziel"/„Position" label. pythonCodeGen.api.cases.test.js holds it.
export const BLOCK_ONLY_FIELDS = new Set(['GESCHWINDIGKEIT', 'DRIVE_TO_ROW', 'DRIVE_BTN', 'KIND']);

// Hat (top-only "wenn …") blocks. Their body is the NEXT-connection chain, not
// a statement INPUT, so they're wrapped in a `def when_…():` at the top level
// (see generateHat) — NOT via a forBlock entry. They can only ever be
// top-level (no previousStatement), so blockToCode is never asked to nest them.
export const HAT_TYPES = new Set([
  'edubotics_when_broadcast',
  'edubotics_when_object_seen',
  'edubotics_when_counter_gt',
]);

// ── Per-block Python generators (the block → robot.* map) ────────────────────
// ONE row-driven generator for every block that has a robot_api row (F8 of the
// 2026-10-18 semantics round; robot_api.py is the only source for robot
// blocks): the call is `robot.<row.name>(…)`, and each argument is the input or
// field named `arg_key.upper()`, emitted by the PARAMETER'S KIND — a number
// unquoted, a name or a text quoted, an empty input `None`. A hand-written
// map drifted from the API (`tempo=` keywords the API does not take,
// `robot.broadcast`, a pin's coordinates as text). The fields that carry no
// parameter are BLOCK_ONLY_FIELDS; pythonCodeGen.api.cases.test.js holds that
// set and test_preview_matches_api.py judges every rendered call with the
// server's own validator.
//
// Registered ONCE onto the shared pythonGenerator singleton after the dynamic
// import resolves. Statement entries return a string ending in "\n"; value
// entries return `[code, Order]`.

const ROWS = (robotApi.methods || []).filter((m) => typeof m.block_type === 'string');

// The comment a pin gets when one of its coordinates is not set yet. It goes
// at the END of the line: a comment inside the argument list would comment out
// the rest of the call.
const UNPINNED_COMMENT = 'nicht gepinnt';

// A field's value as the literal of a parameter of `kind`.
function fieldLiteral(kind, value) {
  if (kind === 'float' || kind === 'int') {
    if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'None';
    const s = String(value == null ? '' : value).trim();
    if (s === '') return 'None';
    const n = Number(s);
    return Number.isFinite(n) ? String(n) : 'None';
  }
  return pyStr(value);
}

// One line of German comment, never a line break a name could smuggle in.
function commentText(value) {
  return String(value == null ? '' : value).replace(/[\r\n]+/g, ' ');
}

function rowGenerator(row, generator, Order) {
  return (block) => {
    const args = [];
    const notes = [];
    let positional = true;
    let unpinned = false;
    for (const p of row.params) {
      const key = String(p.arg_key || p.name).toUpperCase();
      const input = block.getInput(key);
      let code;
      if (input && input.connection) {
        code = generator.valueToCode(block, key, Order.NONE) || 'None';
      } else if (block.getField(key)) {
        code = fieldLiteral(p.kind, block.getFieldValue(key));
        if (code === 'None' && (p.kind === 'float' || p.kind === 'int')) unpinned = true;
      } else if (p.required) {
        code = 'None';
      } else {
        // An optional parameter the block has no field for keeps the API's
        // default; every later argument then goes by its name.
        positional = false;
        continue;
      }
      args.push(positional ? code : `${p.name}=${code}`);
    }
    if (unpinned && row.block_type === 'edubotics_destination_pin') notes.push(UNPINNED_COMMENT);
    const tempo = block.getField('GESCHWINDIGKEIT');
    if (tempo && block.getFieldValue('GESCHWINDIGKEIT') !== 'global') {
      const label = typeof tempo.getText === 'function' ? tempo.getText() : block.getFieldValue('GESCHWINDIGKEIT');
      notes.push(`mit Tempo „${commentText(label)}“`);
    }
    const call = `robot.${row.name}(${args.join(', ')})`;
    if (row.table === 'value') return [call, Order.FUNCTION_CALL];
    return `${call}${notes.length ? `  # ${notes.join(', ')}` : ''}\n`;
  };
}

// The hats of the workspace being generated (generateWorkspace sets it before
// any block is generated): the function names per broadcast message, so a
// „sende Nachricht" starts the hats that wait for it; `threaded` records that
// a send did, so the module imports `threading`.
let currentHats = { byEvent: new Map(), threaded: false };

// The loop variable of a counted „Solange … sichtbar" (a reserved word, so a
// student's variable never takes it).
const WHILE_VISIBLE_PASS_VAR = '_durchgang';

function registerForBlocks(generator, Order) {
  const F = generator.forBlock;
  for (const row of ROWS) F[row.block_type] = rowGenerator(row, generator, Order);

  // ── Events (Ereignisse) ────────────────────────────────────────────────
  // A broadcast has no text-API row: a text program uses functions. On the
  // robot every hat runs in its own thread and a send goes on at once (E3 of
  // the Roboter Studio review fixes), so the preview STARTS each hat that
  // waits for the message (defined above it as `def when_…():`) in a thread.
  F.edubotics_broadcast = (b) => {
    const name = String(b.getFieldValue('EVENT_NAME') == null ? '' : b.getFieldValue('EVENT_NAME')).trim();
    const fns = currentHats.byEvent.get(name) || [];
    if (!fns.length) return `pass  # Nachricht „${commentText(name)}“ — kein „wenn“-Block wartet darauf\n`;
    currentHats.threaded = true;
    return fns
      .map((fn) => `threading.Thread(target=${fn}).start()  # Nachricht „${commentText(name)}“ — der „wenn“-Block läuft nebenher\n`)
      .join('');
  };

  // ── Control (Logik) ────────────────────────────────────────────────────
  // C-block: repeat the body while the object stays visible (E2 of the
  // Roboter Studio review fixes). „höchstens N Mal" (MAX_REPS > 0) renders a
  // counted loop that ends early when nothing is seen; 0 keeps the `while`.
  // One German comment above it states what a text loop cannot show: the
  // observation pose before every look, the skip of an object the body could
  // not grasp, the end without progress. (Rendering the pose as robot.home()
  // or the skip as a try/except was rejected: the pose is an operator
  // setting, and a text program cannot tell a skip from a real refusal.)
  F.edubotics_while_visible = (b, g) => {
    const body = g.statementToCode(b, 'DO') || g.PASS;
    const type = pyStr(b.getFieldValue('OBJECT_TYPE'));
    const comment = `# „Solange ${commentText(fieldText(b, 'OBJECT_TYPE'))} sichtbar“: vor jedem Blick fährt der Arm in `
      + 'die Beobachtungsstellung; ein Objekt, das nicht gegriffen werden kann, wird übersprungen; ohne Fortschritt '
      + 'endet die Schleife von selbst.\n';
    const raw = Number(b.getFieldValue('MAX_REPS'));
    const reps = Number.isFinite(raw) ? Math.max(0, Math.trunc(raw)) : 0;
    if (reps === 0) return `${comment}while robot.sees(${type}):\n${body}`;
    // The body is the loop's own (statementToCode indents it one level),
    // beside the `if`, never under its `break`.
    return `${comment}for ${WHILE_VISIBLE_PASS_VAR} in range(${reps}):\n`
      + `${g.INDENT}if not robot.sees(${type}):\n${g.INDENT}${g.INDENT}break\n${body}`;
  };
  F.edubotics_forever = (b, g) => {
    const body = g.statementToCode(b, 'DO') || g.PASS;
    return `while True:\n${body}`;
  };
  F.edubotics_wait_until = (b, g) => {
    const cond = g.valueToCode(b, 'BOOL', Order.NONE) || 'False';
    return `while not (${cond}):\n${g.INDENT}robot.wait(0.1)\n`;
  };
}

// The function name a hat gets (today's naming): `when_<message>`,
// `when_<type>_seen`, `when_<counter>_over_<n>`.
function hatBaseName(block) {
  switch (block.type) {
    case 'edubotics_when_broadcast':
      return `when_${slug(block.getFieldValue('EVENT_NAME'))}`;
    case 'edubotics_when_object_seen':
      return `when_${slug(block.getFieldValue('OBJECT_TYPE'))}_seen`;
    case 'edubotics_when_counter_gt':
      return `when_${slug(block.getFieldValue('NAME'))}_over_${num(block.getFieldValue('N'), '0')}`;
    default:
      return 'when_handler';
  }
}

function fieldText(block, name) {
  const field = block.getField(name);
  if (field && typeof field.getText === 'function') return field.getText();
  return block.getFieldValue(name);
}

// The German line above a hat's `def`: it is not called by the program, it
// runs by itself.
function hatComment(block) {
  switch (block.type) {
    case 'edubotics_when_broadcast':
      return `# Läuft von selbst, sobald die Nachricht „${commentText(String(block.getFieldValue('EVENT_NAME') || '').trim())}“ gesendet wird.`;
    case 'edubotics_when_object_seen':
      return `# Läuft von selbst, sobald ein neues Objekt „${commentText(fieldText(block, 'OBJECT_TYPE'))}“ zu sehen ist.`;
    case 'edubotics_when_counter_gt':
      return `# Läuft von selbst, sobald der Zähler „${commentText(block.getFieldValue('NAME'))}“ größer als ${num(block.getFieldValue('N'), '0')} ist.`;
    default:
      return '# Läuft von selbst.';
  }
}

// Emit a hat block as a `def when_…():` whose body is its NEXT-connection
// chain, indented one level. Empty body → `pass`.
function generateHat(block, fnName, generator) {
  const next = typeof block.getNextBlock === 'function' ? block.getNextBlock() : null;
  let body = next ? generator.blockToCode(next) : '';
  if (Array.isArray(body)) body = body[0];
  if (!body || !body.trim()) {
    body = generator.PASS; // already INDENT-prefixed "pass\n"
  } else {
    body = `${generator.prefixLines(body.replace(/\n+$/, ''), generator.INDENT)}\n`;
  }
  return `${hatComment(block)}\ndef ${fnName}():\n${body}`;
}

// Replicates pythonGenerator.workspaceToCode (init → top-block loop → finish →
// whitespace cleanup) but routes hat blocks through generateHat so their
// next-chain body is wrapped in a function instead of emitted flat. Hats are
// rendered FIRST (as defs), then the remaining top-level statements. The
// preview starts with `import robot`, like a text program.
//
// ORDER: `getTopBlocks(false)` — CREATION order, deliberately NOT the spatial
// `true`. Execution order is the SERVER's contract and it follows the
// serializer: `Blockly.serialization.workspaces.save()` writes `blocks.blocks`
// in creation order, RunControls spreads that array into /workflow/start
// verbatim, and workflow/interpreter.py walks it front-to-back. The serializer
// is therefore authoritative; this preview only mirrors it.
//
// Measured (Blockly 12.5.1, headless): two top-level stacks created FIRST then
// SECOND, then SECOND dragged above FIRST (y = -200 vs y = 0) —
//   getTopBlocks(true)  -> [SECOND, FIRST]   (spatial)
//   getTopBlocks(false) -> [FIRST, SECOND]   (creation)
//   saved blocks.blocks -> [FIRST, SECOND]   (what actually runs)
// With `true` the „Code"-Vorschau reordered itself on a pure drag while the run
// did not, so the panel showed a program the robot never executed. Changing the
// EXECUTION order to match the layout instead would silently repoint every
// already-saved workflow, so the preview is the side that moves.
function generateWorkspace(workspace, generator) {
  generator.init(workspace);
  // An import definition: `finish` puts every import first, this one before
  // any Blockly adds (`from numbers import Number`, `import math`).
  generator.definitions_.import_robot = 'import robot';
  const top = typeof workspace.getTopBlocks === 'function'
    ? workspace.getTopBlocks(false)
    : [];
  // Name every enabled hat first, so a broadcast anywhere can call it.
  const hats = [];
  const used = new Set();
  const byEvent = new Map();
  for (const block of top) {
    if (!HAT_TYPES.has(block.type)) continue;
    if (typeof block.isEnabled === 'function' && !block.isEnabled()) continue;
    const base = hatBaseName(block);
    let fn = base;
    for (let k = 2; used.has(fn); k += 1) fn = `${base}_${k}`;
    used.add(fn);
    hats.push([block, fn]);
    if (block.type === 'edubotics_when_broadcast') {
      const name = String(block.getFieldValue('EVENT_NAME') == null ? '' : block.getFieldValue('EVENT_NAME')).trim();
      if (!byEvent.has(name)) byEvent.set(name, []);
      byEvent.get(name).push(fn);
    }
  }
  currentHats = { byEvent, threaded: false };
  const defs = [];
  const main = [];
  try {
    for (const [block, fn] of hats) defs.push(generateHat(block, fn, generator));
    for (const block of top) {
      if (HAT_TYPES.has(block.type)) continue;
      let code = generator.blockToCode(block);
      if (Array.isArray(code)) {
        // A naked value block left at the top level — turn it into a statement.
        code = generator.scrubNakedValue(code[0]);
      }
      if (code) main.push(code);
    }
    // A send started a hat in a thread: `import threading` right after
    // `import robot` (which stays the first import), before any import a
    // built-in block added while it was generated.
    if (currentHats.threaded) {
      const before = generator.definitions_;
      const after = Object.create(null);
      after.import_robot = before.import_robot;
      after.import_threading = 'import threading';
      for (const key of Object.keys(before)) if (!(key in after)) after[key] = before[key];
      generator.definitions_ = after;
    }
  } finally {
    currentHats = { byEvent: new Map(), threaded: false };
  }
  let code = [...defs, ...main].join('\n');
  code = generator.finish(code);
  // Mirror workspaceToCode's trailing-whitespace cleanup.
  code = code
    .replace(/^\s+\n/, '')
    .replace(/\n\s+$/, '\n')
    .replace(/[ \t]+\n/g, '\n');
  return code;
}

// Memoized dynamic load of the Python generator + one-time forBlock
// registration. The dynamic import is what makes `blockly/python` a separate
// lazy chunk.
let _ready = null;
function load() {
  if (!_ready) {
    _ready = import('blockly/python').then((mod) => {
      const generator = mod.pythonGenerator;
      const Order = mod.Order;
      // Keep a student's variable from shadowing our API in the generated
      // preview (the generator renames a variable that takes a reserved
      // word): `robot`, the `threading` a send starts its hats with, and the
      // pass counter of a counted „Solange … sichtbar".
      if (typeof generator.addReservedWords === 'function') {
        generator.addReservedWords(`robot,threading,${WHILE_VISIBLE_PASS_VAR}`);
      }
      registerForBlocks(generator, Order);
      return { generator, Order };
    });
  }
  return _ready;
}

/**
 * Generate a read-only Python preview of every top-level block stack in
 * `workspace`. Async: resolves the lazy `blockly/python` chunk on first call,
 * then runs synchronously. Returns the Python source as a string.
 */
export async function generatePython(workspace) {
  if (!workspace) return '';
  const { generator } = await load();
  return generateWorkspace(workspace, generator);
}

export default generatePython;
