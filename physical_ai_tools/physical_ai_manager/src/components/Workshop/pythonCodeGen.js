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
// preview). Maps every `edubotics_*` block to an English `robot.*` API call;
// the built-in Blockly blocks (controls_*, logic_*, math_*, lists_*, text*,
// variables_*, procedures_*) are handled for free by Blockly's own
// `pythonGenerator.forBlock` entries, which register themselves as a side
// effect of importing `blockly/python`.
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
// executed. The real program runs server-side through the Blockly-JSON
// interpreter (workflow/interpreter.py); this is only a human-readable mirror
// of the block tree, keyed on the SAME block type ids + field/input NAMEs.

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

// A pin coordinate label is either a formatted number (e.g. "0.123") or the
// "—" un-pinned sentinel; emit the number when present, else `None`.
function coord(value) {
  if (value == null) return 'None';
  const s = String(value).trim();
  if (s === '') return 'None';
  const n = Number(s);
  return Number.isFinite(n) ? String(n) : 'None';
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
// Registered ONCE onto the shared pythonGenerator singleton after the dynamic
// import resolves. Statement entries return a string ending in "\n"; value
// entries return `[code, Order]`.
function registerForBlocks(generator, Order) {
  const F = generator.forBlock;
  const val = (block, name, fallback = 'None') =>
    generator.valueToCode(block, name, Order.NONE) || fallback;
  // Render a non-default per-move „mit Tempo" field as a Python kwarg so the
  // preview matches the run (the runtime honours `geschwindigkeit`).
  const tempoOf = (block) => {
    const t = block.getFieldValue('GESCHWINDIGKEIT');
    return t && t !== 'global' ? t : null;
  };
  const kw = (t) => (t ? `, tempo='${t}'` : '');

  // ── Motion (Bewegung) ──────────────────────────────────────────────────
  F.edubotics_home = () => 'robot.home()\n';
  F.edubotics_open_gripper = () => 'robot.open_gripper()\n';
  F.edubotics_close_gripper = () => 'robot.close_gripper()\n';
  F.edubotics_move_to = (b) => `robot.move_to(${val(b, 'DESTINATION')}${kw(tempoOf(b))})\n`;
  F.edubotics_pickup = (b) => `robot.pickup(${val(b, 'TARGET')}${kw(tempoOf(b))})\n`;
  F.edubotics_drop_at = (b) => `robot.drop_at(${val(b, 'DESTINATION')}${kw(tempoOf(b))})\n`;
  F.edubotics_wait_seconds = (b) => `robot.wait(${val(b, 'SECONDS', '0')})\n`;
  // Grasp-split primitives (act on a Greifziel value, except lift()).
  F.edubotics_move_above = (b) => `robot.move_above(${val(b, 'ZIEL')}${kw(tempoOf(b))})\n`;
  F.edubotics_descend_to = (b) => `robot.descend_to(${val(b, 'ZIEL')})\n`;
  F.edubotics_close_on_object = (b) => `robot.close_on_object(${val(b, 'ZIEL')})\n`;
  F.edubotics_lift = (b) => `robot.lift(${tempoOf(b) ? `tempo='${tempoOf(b)}'` : ''})\n`;
  // Batch 2b — replay a recorded hand-guided motion by name.
  F.edubotics_replay_trajectory = (b) => `robot.replay(${pyStr(b.getFieldValue('NAME'))})\n`;

  // ── Perception (Wahrnehmung) ───────────────────────────────────────────
  F.edubotics_grasp_object = (b) =>
    `robot.grasp(${pyStr(b.getFieldValue('OBJECT_TYPE'))})\n`;
  F.edubotics_see_object = (b) => [
    `robot.sees(${pyStr(b.getFieldValue('OBJECT_TYPE'))})`,
    Order.FUNCTION_CALL,
  ];
  F.edubotics_count_object = (b) => [
    `robot.count(${pyStr(b.getFieldValue('OBJECT_TYPE'))})`,
    Order.FUNCTION_CALL,
  ];
  F.edubotics_wait_until_object_seen = (b) => [
    `robot.wait_until_seen(${pyStr(b.getFieldValue('OBJECT_TYPE'))}, `
      + `${num(b.getFieldValue('TIMEOUT'), '10')})`,
    Order.FUNCTION_CALL,
  ];
  F.edubotics_wait_until_held = (b) => [
    `robot.wait_until_held(${num(b.getFieldValue('TIMEOUT'), '10')})`,
    Order.FUNCTION_CALL,
  ];
  F.edubotics_find_object = (b) => [
    `robot.find(${pyStr(b.getFieldValue('OBJECT_TYPE'))})`,
    Order.FUNCTION_CALL,
  ];
  F.edubotics_object_position = (b) => [
    `robot.object_position(${val(b, 'ZIEL')})`,
    Order.FUNCTION_CALL,
  ];
  F.edubotics_grasp_held = () => ['robot.is_holding()', Order.FUNCTION_CALL];
  F.edubotics_mark_done = (b) => `robot.mark_done(${val(b, 'ZIEL')})\n`;
  // C-block: repeat the body while the object stays visible. (The optional
  // „höchstens N Mal" MAX_REPS cap is a runtime backstop and is not rendered
  // in this preview.)
  F.edubotics_while_visible = (b, g) => {
    const body = g.statementToCode(b, 'DO') || g.PASS;
    return `while robot.sees(${pyStr(b.getFieldValue('OBJECT_TYPE'))}):\n${body}`;
  };

  // ── Destinations (Ziele) ───────────────────────────────────────────────
  F.edubotics_destination_pin = (b) =>
    `robot.pin(${pyStr(b.getFieldValue('NAME'))}, ${coord(b.getFieldValue('X'))}, `
      + `${coord(b.getFieldValue('Y'))}, ${coord(b.getFieldValue('Z'))})\n`;
  F.edubotics_destination_current = (b) =>
    `robot.pin_current(${pyStr(b.getFieldValue('NAME'))})\n`;
  // VALUE reference to a pinned destination → the pinned NAME as a string
  // literal, so it slots into move_to(...) / drop_at(...).
  F.edubotics_destination_ref = (b) => [
    pyStr(b.getFieldValue('NAME')),
    Order.ATOMIC,
  ];

  // ── Output (Ausgabe) ───────────────────────────────────────────────────
  F.edubotics_log = (b) => `robot.log(${val(b, 'MESSAGE', '""')})\n`;
  F.edubotics_play_sound = () => 'robot.beep()\n';
  F.edubotics_speak_de = (b) => `robot.speak(${val(b, 'TEXT', '""')})\n`;
  F.edubotics_play_tone = (b) =>
    `robot.tone(${num(b.getFieldValue('FREQ'), '880')}, `
      + `${num(b.getFieldValue('SECONDS'), '0.25')})\n`;
  F.edubotics_toast = (b) =>
    `robot.toast(${val(b, 'TEXT', '""')}, level=${pyStr(b.getFieldValue('LEVEL'))}, `
      + `seconds=${num(b.getFieldValue('SECONDS'), '3')})\n`;

  // ── Counters (Zähler) ──────────────────────────────────────────────────
  F.edubotics_counter_reset = (b) =>
    `robot.counter_reset(${pyStr(b.getFieldValue('NAME'))})\n`;
  F.edubotics_counter_add = (b) =>
    `robot.counter_add(${pyStr(b.getFieldValue('NAME'))})\n`;
  F.edubotics_counter_get = (b) => [
    `robot.counter_get(${pyStr(b.getFieldValue('NAME'))})`,
    Order.FUNCTION_CALL,
  ];

  // ── Events (Ereignisse) ────────────────────────────────────────────────
  // broadcast is a plain statement; the matching when_* hats are wrapped at
  // the top level (generateHat) into `def when_…():`.
  F.edubotics_broadcast = (b) =>
    `robot.broadcast(${pyStr(b.getFieldValue('EVENT_NAME'))})\n`;

  // ── Control (Logik) ────────────────────────────────────────────────────
  F.edubotics_forever = (b, g) => {
    const body = g.statementToCode(b, 'DO') || g.PASS;
    return `while True:\n${body}`;
  };
  F.edubotics_wait_until = (b, g) => {
    const cond = g.valueToCode(b, 'BOOL', Order.NONE) || 'False';
    return `while not (${cond}):\n${g.INDENT}robot.wait(0.1)\n`;
  };
}

// Header line for a `when …` hat, derived from its fields.
function hatHeader(block) {
  switch (block.type) {
    case 'edubotics_when_broadcast':
      return `def when_${slug(block.getFieldValue('EVENT_NAME'))}():`;
    case 'edubotics_when_object_seen':
      return `def when_${slug(block.getFieldValue('OBJECT_TYPE'))}_seen():`;
    case 'edubotics_when_counter_gt':
      return `def when_${slug(block.getFieldValue('NAME'))}_over_`
        + `${num(block.getFieldValue('N'), '0')}():`;
    default:
      return 'def when_handler():';
  }
}

// Emit a hat block as a `def when_…():` whose body is its NEXT-connection
// chain, indented one level. Empty body → `pass`.
function generateHat(block, generator) {
  if (typeof block.isEnabled === 'function' && !block.isEnabled()) return '';
  const next = typeof block.getNextBlock === 'function' ? block.getNextBlock() : null;
  let body = next ? generator.blockToCode(next) : '';
  if (Array.isArray(body)) body = body[0];
  if (!body || !body.trim()) {
    body = generator.PASS; // already INDENT-prefixed "pass\n"
  } else {
    body = `${generator.prefixLines(body.replace(/\n+$/, ''), generator.INDENT)}\n`;
  }
  return `${hatHeader(block)}\n${body}`;
}

// Replicates pythonGenerator.workspaceToCode (init → top-block loop → finish →
// whitespace cleanup) but routes hat blocks through generateHat so their
// next-chain body is wrapped in a function instead of emitted flat. Hats are
// rendered FIRST (as defs), then the remaining top-level statements.
function generateWorkspace(workspace, generator) {
  generator.init(workspace);
  const top = typeof workspace.getTopBlocks === 'function'
    ? workspace.getTopBlocks(true)
    : [];
  const defs = [];
  const main = [];
  for (const block of top) {
    if (HAT_TYPES.has(block.type)) {
      const hat = generateHat(block, generator);
      if (hat) defs.push(hat);
      continue;
    }
    let code = generator.blockToCode(block);
    if (Array.isArray(code)) {
      // A naked value block left at the top level — turn it into a statement.
      code = generator.scrubNakedValue(code[0]);
    }
    if (code) main.push(code);
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
      // Keep a student's `robot` variable from shadowing our API in the
      // generated preview (the generator would otherwise rename it).
      if (typeof generator.addReservedWords === 'function') {
        generator.addReservedWords('robot');
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
