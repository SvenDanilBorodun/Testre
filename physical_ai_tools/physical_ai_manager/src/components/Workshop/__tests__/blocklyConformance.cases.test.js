/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The preview is the meaning of a block (Principle 1 of the 2026-10-18
// semantics round). For the BUILT-IN blocks (controls_*, logic_*, math_*,
// lists_*, text_*, variables_*), the Python the „Code"-Vorschau shows is
// Blockly's own generator output, and the server's interpreter has to agree
// with it — or refuse in German where that Python raises.
//
// This test builds a corpus of built-in-block programs, loads each into a
// HEADLESS Blockly workspace, renders it through the editor's own preview
// path (pythonCodeGen.generatePython) and compares the result with
// fixtures/blockly-conformance-cases.json. The fixture is what
// physical_ai_server/test/test_blockly_conformance.py EXECUTES twice: the
// preview with CPython, the workspace JSON with workflow/interpreter.py; every
// disagreement must be a listed, reasoned deviation (its INTENTIONAL dict). A
// change to Blockly, to the corpus or to the preview generator therefore fails
// here until the fixture is regenerated, and the regenerated programs are
// judged there:
//
//   EDUBOTICS_REGEN_BLOCKLY_CASES=1 npx vitest run src/components/Workshop/__tests__/blocklyConformance.cases.test.js

import fs from 'fs';
import path from 'path';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { beforeAll, describe, expect, it } from 'vitest';
import { generatePython } from '../pythonCodeGen';

const FIXTURE = path.join(__dirname, 'fixtures', 'blockly-conformance-cases.json');
const REGEN = !['', '0', undefined].includes(process.env.EDUBOTICS_REGEN_BLOCKLY_CASES);

// ── a tiny builder for serialized Blockly JSON ──────────────────────────────

function makeBuilder() {
  let n = 0;
  const id = () => {
    n += 1;
    return `x${n}`;
  };
  const B = (type, fields, inputs, extraState) => {
    const b = { type, id: id() };
    if (fields) b.fields = fields;
    if (inputs) {
      const ins = {};
      for (const [k, v] of Object.entries(inputs)) if (v) ins[k] = { block: v };
      b.inputs = ins;
    }
    if (extraState !== undefined) b.extraState = extraState;
    return b;
  };
  return { B, reset: () => { n = 0; } };
}

const VARS = ['a', 'b', 'i', 'trace', 'r'];
const V = (name) => ({ id: `v_${name}` });

function buildCorpus() {
  const { B } = makeBuilder();
  const chain = (...blocks) => {
    const bs = blocks.filter(Boolean);
    for (let k = 0; k + 1 < bs.length; k += 1) bs[k].next = { block: bs[k + 1] };
    return bs[0];
  };
  const num = (x) => B('math_number', { NUM: x });
  const txt = (s) => B('text', { TEXT: s });
  const get = (name) => B('variables_get', { VAR: V(name) });
  const set = (name, v) => B('variables_set', { VAR: V(name) }, { VALUE: v });
  const lst = (...items) => B(
    'lists_create_with', null,
    Object.fromEntries(items.map((it, k) => [`ADD${k}`, it])),
    { itemCount: items.length },
  );
  const append = (name, v) => B('lists_setIndex', { MODE: 'INSERT', WHERE: 'LAST' }, { LIST: get(name), TO: v });
  const arith = (op, a, b) => B('math_arithmetic', { OP: op }, { A: a, B: b });
  const bool = (v) => B('logic_boolean', { BOOL: v ? 'TRUE' : 'FALSE' });
  const join = (...items) => B(
    'text_join', null,
    Object.fromEntries(items.map((it, k) => [`ADD${k}`, it])),
    { itemCount: items.length },
  );
  const forLoop = (from, to, by, body) => chain(
    set('trace', lst()),
    B('controls_for', { VAR: V('i') }, { FROM: from, TO: to, BY: by, DO: body || append('trace', get('i')) }),
  );
  const L3 = () => lst(num(10), num(20), num(30));
  const getIdx = (at, where = 'FROM_START', list = L3()) => B(
    'lists_getIndex', { MODE: 'GET', WHERE: where }, { VALUE: list, AT: at },
  );

  const cases = [];
  const add = (name, ...roots) => {
    cases.push({
      name,
      ws: {
        blocks: { languageVersion: 0, blocks: roots },
        variables: VARS.map((v) => ({ name: v, id: `v_${v}` })),
      },
    });
  };

  // F1 — „zähle" loops: literal and variable bounds, floats, negative steps.
  const forCases = [
    [1, 3, 1], [3, 1, 1], [1, 3, -1], [3, 1, -1], [1, 10, 3], [10, 1, 3],
    [1.5, 3, 0.5], [3, 1.5, 0.5], [1, 1, 1], [0, 2, 0.7], [2, 0, -0.7], [5, 1, -2],
  ];
  for (const [f, t, s] of forCases) {
    add(`for_lit_${f}_${t}_${s}`, forLoop(num(f), num(t), num(s)));
    add(`for_var_${f}_${t}_${s}`, chain(set('a', num(f)), forLoop(get('a'), num(t), num(s))));
  }
  // F7 — a „zähle" bound that is text. A text block cannot plug into a
  // number socket (Blockly's connection checks refuse it), so every text in a
  // math socket below arrives the way a student gets it there: in a variable.
  add('for_text_from', chain(set('a', txt('1')), forLoop(get('a'), num(3), num(1))));
  add('for_text_by', chain(set('a', txt('1')), forLoop(num(1), num(3), get('a'))));

  // „wiederhole n mal" with a non-integer count (the generator truncates).
  for (const n of [2.7, 2.2, 0, -1, 3]) {
    add(`repeat_${n}`, chain(
      set('trace', lst()),
      B('controls_repeat_ext', null, { TIMES: num(n), DO: append('trace', num(1)) }),
    ));
  }

  // F6 — „und"/„oder" short-circuit: B is not evaluated when A decides.
  const divByZero = () => B('logic_compare', { OP: 'EQ' }, { A: arith('DIVIDE', num(1), num(0)), B: num(1) });
  add('and_short', set('r', B('logic_operation', { OP: 'AND' }, { A: bool(false), B: divByZero() })));
  add('or_short', set('r', B('logic_operation', { OP: 'OR' }, { A: bool(true), B: divByZero() })));
  add('and_value', chain(set('a', num(3)), set('b', txt('x')),
    set('r', B('logic_operation', { OP: 'AND' }, { A: get('a'), B: get('b') }))));
  add('or_value', chain(set('a', num(0)), set('b', txt('x')),
    set('r', B('logic_operation', { OP: 'OR' }, { A: get('a'), B: get('b') }))));
  add('if_short_list', chain(set('trace', lst()), B('controls_if', null, {
    IF0: B('logic_operation', { OP: 'AND' }, {
      A: B('logic_compare', { OP: 'GT' }, { A: B('lists_length', null, { VALUE: get('trace') }), B: num(0) }),
      B: B('logic_compare', { OP: 'EQ' }, { A: getIdx(num(1), 'FROM_START', get('trace')), B: num(5) }),
    }),
    DO0: set('r', num(1)),
  })));

  // F12 — rounding (Python rounds half to even, and so does the interpreter).
  for (const x of [2.5, 3.5, -2.5, 0.5, 2.4]) {
    add(`round_${x}`, set('r', B('math_round', { OP: 'ROUND' }, { NUM: num(x) })));
  }

  // F7 — a math socket that holds text, a list or a digit text.
  add('arith_text', chain(set('a', txt('abc')), set('r', arith('ADD', get('a'), num(1)))));
  add('arith_list', chain(set('a', lst(num(1))), set('r', arith('ADD', get('a'), num(1)))));
  add('arith_textnum', chain(set('a', txt('2')), set('r', arith('ADD', get('a'), num(1)))));
  add('mod_list', chain(set('a', lst(num(1))), set('r', B('math_modulo', null, { DIVIDEND: get('a'), DIVISOR: num(2) }))));
  add('mod_textnum', chain(set('a', txt('7')), set('r', B('math_modulo', null, { DIVIDEND: get('a'), DIVISOR: num(2) }))));
  add('mod_neg', set('r', B('math_modulo', null, { DIVIDEND: num(-7), DIVISOR: num(3) })));
  add('randint_text', chain(set('a', txt('q')), set('r', B('math_random_int', null, { FROM: get('a'), TO: num(3) }))));
  add('randint_textnum', chain(set('a', txt('2')), set('r', B('math_random_int', null, { FROM: get('a'), TO: num(3) }))));
  add('constrain_text', chain(set('a', txt('q')),
    set('r', B('math_constrain', null, { VALUE: get('a'), LOW: num(1), HIGH: num(3) }))));
  add('constrain_textnum', chain(set('a', txt('2')),
    set('r', B('math_constrain', null, { VALUE: get('a'), LOW: num(1), HIGH: num(3) }))));
  add('round_textnum', chain(set('a', txt('2')), set('r', B('math_round', { OP: 'ROUND' }, { NUM: get('a') }))));
  add('div_int', set('r', arith('DIVIDE', num(7), num(2))));
  add('pow', set('r', arith('POWER', num(2), num(10))));
  add('math_change_var_text', chain(set('a', txt('q')), B('math_change', { VAR: V('a') }, { DELTA: num(2) })));
  add('math_change_text', chain(set('a', num(1)), set('b', txt('2')), B('math_change', { VAR: V('a') }, { DELTA: get('b') })));

  // N13 — lists.
  for (const at of [1, 1.5, 2.7, 3, 0, 4]) add(`getidx_${at}`, set('r', getIdx(num(at))));
  add('getidx_noat', set('r', getIdx(null)));
  add('getidx_fromend_1', set('r', getIdx(num(1), 'FROM_END')));
  add('getidx_text_at', chain(set('a', txt('2')), set('r', getIdx(get('a')))));
  add('setidx_text_at', chain(set('a', L3()), set('b', txt('2')),
    B('lists_setIndex', { MODE: 'SET', WHERE: 'FROM_START' }, { LIST: get('a'), AT: get('b'), TO: num(5) })));
  add('setidx_noat', chain(set('a', L3()),
    B('lists_setIndex', { MODE: 'SET', WHERE: 'FROM_START' }, { LIST: get('a'), TO: num(5) })));
  add('len_num', chain(set('a', num(5)), set('r', B('lists_length', null, { VALUE: get('a') }))));
  add('len_text', set('r', B('lists_length', null, { VALUE: txt('abc') })));
  add('len_no_input', set('r', B('lists_length', null, null)));
  add('isempty_num', chain(set('a', num(0)), set('r', B('lists_isEmpty', null, { VALUE: get('a') }))));
  add('isempty_no_input', set('r', B('lists_isEmpty', null, null)));
  add('indexof_text', chain(set('a', txt('abc')),
    set('r', B('lists_indexOf', { END: 'FIRST' }, { VALUE: get('a'), FIND: txt('b') }))));
  add('indexof_text_last', chain(set('a', txt('abcb')),
    set('r', B('lists_indexOf', { END: 'LAST' }, { VALUE: get('a'), FIND: txt('b') }))));
  add('indexof_text_missing', chain(set('a', txt('abc')),
    set('r', B('lists_indexOf', { END: 'FIRST' }, { VALUE: get('a'), FIND: txt('z') }))));
  add('indexof_list_last', set('r', B('lists_indexOf', { END: 'LAST' }, { VALUE: lst(num(1), num(2), num(1)), FIND: num(1) })));
  add('sublist', set('r', B('lists_getSublist', { WHERE1: 'FROM_START', WHERE2: 'FROM_START' }, { LIST: L3(), AT1: num(2), AT2: num(3) })));
  add('sublist_text_at', chain(set('a', txt('2')),
    set('r', B('lists_getSublist', { WHERE1: 'FROM_START', WHERE2: 'FROM_START' }, { LIST: L3(), AT1: get('a'), AT2: num(3) }))));
  add('repeat_text', chain(set('a', txt('2')), set('r', B('lists_repeat', null, { ITEM: txt('x'), NUM: get('a') }))));
  add('repeat_list_3', set('r', B('lists_repeat', null, { ITEM: txt('x'), NUM: num(3) })));

  // Comparison and text.
  add('cmp_eq_numtext', chain(set('a', txt('1')), set('r', B('logic_compare', { OP: 'EQ' }, { A: num(1), B: get('a') }))));
  add('cmp_eq_intfloat', set('r', B('logic_compare', { OP: 'EQ' }, { A: num(1), B: num(1.0) })));
  add('text_join', set('r', join(txt('x='), num(3))));
  add('text_join_float', set('r', join(txt('x='), arith('DIVIDE', num(1), num(3)))));
  add('text_join_list', chain(set('a', lst(num(1), num(2))), set('r', join(get('a')))));

  // One case per INTENTIONAL deviation the cases above do not reach yet.
  // #3 — a complex power result is refused.
  add('pow_complex', set('r', arith('POWER', num(-8), num(0.5))));
  // #4 — math_constrain with LOW > HIGH swaps the bounds (the preview answers HIGH).
  add('constrain_low_gt_high', set('r', B('math_constrain', null, { VALUE: num(5), LOW: num(10), HIGH: num(1) })));
  // #5 — math_random_int with FROM > TO swaps (the preview raises).
  add('random_from_gt_to', set('r', B('math_random_int', null, { FROM: num(5), TO: num(1) })));
  // #6 — „ist leer" of an empty value (a variable that holds nothing) is wahr.
  add('isempty_empty_socket', set('r', B('lists_isEmpty', null, { VALUE: get('b') })));
  // #7 — text renders like everywhere else a student sees a value.
  add('join_float_and_list', set('r', join(txt('A'), arith('DIVIDE', num(4), num(2)), num(3), lst(num(1), txt('b')))));
  // #8 — the loop, list and text caps.
  add('cap_for_loop', chain(set('r', num(0)),
    B('controls_for', { VAR: V('i') }, { FROM: num(1), TO: num(10001), BY: num(1), DO: set('r', get('i')) })));
  add('cap_lists_repeat', set('r', B('lists_repeat', null, { ITEM: txt('x'), NUM: num(1001) })));
  add('cap_lists_create_with', set('r', lst(...Array.from({ length: 21 }, (_, k) => num(k)))));
  add('cap_text_join', set('r', join(txt('x'.repeat(6000)), txt('y'.repeat(6000)))));

  // The review-fix round (2026-10-25). Every socket left out of `inputs` below
  // is EMPTY in the saved JSON too (Blockly keeps an empty socket empty).
  const cmp = (op, a, b) => B('logic_compare', { OP: op }, { A: a, B: b });
  const pdef = (name, params, stack, ret) => B(
    ret ? 'procedures_defreturn' : 'procedures_defnoreturn',
    { NAME: name },
    { STACK: stack, RETURN: ret },
    { params: params.map((p) => ({ name: p, id: `v_${p}` })) },
  );
  const pcall = (name, args, params, ret = false) => B(
    ret ? 'procedures_callreturn' : 'procedures_callnoreturn', null,
    Object.fromEntries(args.map((a, k) => [`ARG${k}`, a])),
    { name, params },
  );
  // procedures_ifreturn has no JSON extraState: Blockly saves its XML mutation.
  const ifret = (cond, value) => B('procedures_ifreturn', null, { CONDITION: cond, VALUE: value },
    '<mutation value="1"></mutation>');
  // M1 — a „Funktion" call has its own frame: a parameter shadows the global
  // and a write to it stays in the call; a nested call reads the GLOBAL, not
  // its caller's parameter (the preview declares every other variable global).
  add('proc_param_shadows_global',
    chain(set('a', num(7)), pcall('setze', [num(1)], ['a']), set('r', get('a'))),
    pdef('setze', ['a'], set('b', get('a'))));
  add('proc_nested_reads_global',
    chain(set('a', num(5)), pcall('aussen', [num(1)], ['a'])),
    pdef('aussen', ['a'], pcall('innen', [], [])),
    pdef('innen', [], set('b', get('a'))));
  add('proc_recursion_sum_20',
    set('r', pcall('summe', [num(20)], ['a'], true)),
    pdef('summe', ['a'], ifret(cmp('LTE', get('a'), num(0)), num(0)),
      arith('ADD', get('a'), pcall('summe', [arith('MINUS', get('a'), num(1))], ['a'], true))));
  add('proc_param_write_is_local',
    chain(set('a', num(1)), pcall('f', [num(2)], ['a'])),
    pdef('f', ['a'], chain(set('a', num(3)), set('b', get('a')))));
  // B6 — deeper than the robot's Python allows (INTENTIONAL #10).
  add('proc_recursion_400',
    set('r', pcall('tief', [num(400)], ['a'], true)),
    pdef('tief', ['a'], ifret(cmp('LTE', get('a'), num(0)), num(0)),
      arith('ADD', num(1), pcall('tief', [arith('MINUS', get('a'), num(1))], ['a'], true))));
  // B1 — an empty socket of „vergleiche", „und"/„oder", „nicht", „Rest von"
  // (INTENTIONAL #9) and „beschränke"'s empty HIGH (the preview's infinity).
  add('cmp_eq_empty', set('r', cmp('EQ', null, num(0))));
  add('cmp_lt_empty', set('r', cmp('LT', num(1), null)));
  add('and_empty_b', set('r', B('logic_operation', { OP: 'AND' }, { A: bool(true) })));
  add('or_empty_a', set('r', B('logic_operation', { OP: 'OR' }, { B: bool(true) })));
  add('neg_empty', set('r', B('logic_negate', null, null)));
  add('mod_empty_divisor', set('r', B('math_modulo', null, { DIVIDEND: num(7) })));
  add('mod_empty_dividend', set('r', B('math_modulo', null, { DIVISOR: num(3) })));
  add('constrain_empty_high', set('r', B('math_constrain', null, { VALUE: num(50), LOW: num(1) })));
  // B2 — a variable that holds nothing is not 0 in a number socket; „ändere
  // um" counts a Wahrheitswert like the preview's isinstance(x, Number).
  add('arith_unset_var', set('r', arith('ADD', get('a'), num(1))));
  add('for_unset_to', forLoop(num(1), get('b'), num(1)));
  add('repeat_unset_times', chain(
    set('trace', lst()),
    B('controls_repeat_ext', null, { TIMES: get('b'), DO: append('trace', num(1)) }),
  ));
  add('change_bool', chain(set('a', bool(true)), B('math_change', { VAR: V('a') }, { DELTA: num(1) })));
  // B3 — element 0 in „füge ein … von hinten" and „nimm Teilliste" (INTENTIONAL #2).
  add('setidx_insert_fromend_0', chain(set('a', L3()),
    B('lists_setIndex', { MODE: 'INSERT', WHERE: 'FROM_END' }, { LIST: get('a'), AT: num(0), TO: num(99) }),
    set('b', get('a'))));
  add('sublist_fromstart_0', set('r', B('lists_getSublist', { WHERE1: 'FROM_START', WHERE2: 'FROM_END' },
    { LIST: L3(), AT1: num(0), AT2: num(1) })));
  add('sublist_fromend_0', set('r', B('lists_getSublist', { WHERE1: 'FROM_END', WHERE2: 'FROM_END' },
    { LIST: L3(), AT1: num(0), AT2: num(1) })));
  // B4 — a text is read like a list of characters by „nimm Teilliste" and
  // „hole Element"; „entferne" and a number keep their refusal.
  add('sublist_text', chain(set('a', txt('abcd')),
    set('r', B('lists_getSublist', { WHERE1: 'FROM_START', WHERE2: 'FROM_START' }, { LIST: get('a'), AT1: num(2), AT2: num(3) }))));
  add('getidx_text', chain(set('a', txt('abc')), set('r', getIdx(num(2), 'FROM_START', get('a')))));
  add('sublist_number', chain(set('a', num(5)),
    set('r', B('lists_getSublist', { WHERE1: 'FROM_START', WHERE2: 'FROM_START' }, { LIST: get('a'), AT1: num(1), AT2: num(2) }))));
  add('getidx_text_remove', chain(set('a', txt('abc')),
    set('r', B('lists_getIndex', { MODE: 'GET_REMOVE', WHERE: 'FROM_START' }, { VALUE: get('a'), AT: num(2) }))));
  // B5 — a fractional count / fractional bounds.
  add('repeat_list_2.7', set('r', B('lists_repeat', null, { ITEM: txt('x'), NUM: num(2.7) })));
  add('randint_1.5', set('r', B('math_random_int', null, { FROM: num(1.5), TO: num(3.5) })));

  // A seeded fuzz over arithmetic, rounding, modulo, „zähle" and „wiederhole".
  // Combinations that land on a listed deviation (a complex power) are left
  // out: those are covered once, by name, above.
  let seed = 7;
  const rnd = () => {
    seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const pick = (xs) => xs[Math.floor(rnd() * xs.length)];
  const vals = [0, 1, 2, 3, -1, -2.5, 2.5, 0.5, 7, 1.5];
  for (let k = 0; k < 120; k += 1) {
    const kind = pick(['for', 'arith', 'round', 'mod', 'repeat']);
    if (kind === 'for') {
      add(`fz${k}_for`, forLoop(num(pick(vals)), num(pick(vals)), num(pick([1, -1, 2, -2, 0.5, -0.5]))));
    } else if (kind === 'arith') {
      const op = pick(['ADD', 'MINUS', 'MULTIPLY', 'DIVIDE', 'POWER']);
      const a = pick(vals);
      const b = pick(vals);
      if (op === 'POWER' && a < 0 && !Number.isInteger(b)) continue;
      add(`fz${k}_arith`, set('r', arith(op, num(a), num(b))));
    } else if (kind === 'round') {
      add(`fz${k}_round`, set('r', B('math_round', { OP: pick(['ROUND', 'ROUNDUP', 'ROUNDDOWN']) }, { NUM: num(pick(vals)) })));
    } else if (kind === 'mod') {
      add(`fz${k}_mod`, set('r', B('math_modulo', null, { DIVIDEND: num(pick(vals)), DIVISOR: num(pick(vals)) })));
    } else {
      add(`fz${k}_rep`, chain(
        set('trace', lst()),
        B('controls_repeat_ext', null, { TIMES: num(pick(vals)), DO: append('trace', num(1)) }),
      ));
    }
  }
  return cases;
}

async function compute() {
  const out = [];
  for (const c of buildCorpus()) {
    const ws = new Blockly.Workspace();
    try {
      Blockly.serialization.workspaces.load(c.ws, ws);
      const saved = Blockly.serialization.workspaces.save(ws);
      // eslint-disable-next-line no-await-in-loop
      out.push({ name: c.name, ws: saved, py: await generatePython(ws) });
    } finally {
      ws.dispose();
    }
  }
  return {
    _generated_by: 'physical_ai_manager/src/components/Workshop/__tests__/blocklyConformance.cases.test.js '
      + '(EDUBOTICS_REGEN_BLOCKLY_CASES=1 to regenerate)',
    _checked_by: 'physical_ai_server/test/test_blockly_conformance.py (CPython runs the preview, '
      + 'workflow/interpreter.py runs the workspace)',
    cases: out,
  };
}

// One case per line: the fixture stays small and a regenerated corpus diffs
// case by case.
function serialize(now) {
  const head = Object.entries(now)
    .filter(([k]) => k !== 'cases')
    .map(([k, v]) => ` ${JSON.stringify(k)}: ${JSON.stringify(v)},`);
  const cases = now.cases.map((c, k) => `  ${JSON.stringify(c)}${k + 1 < now.cases.length ? ',' : ''}`);
  return `{\n${head.join('\n')}\n "cases": [\n${cases.join('\n')}\n ]\n}\n`;
}

beforeAll(() => {
  Blockly.setLocale(De);
});

describe('the Blockly conformance corpus (the preview is the meaning of a block)', () => {
  it('matches the fixture the Python conformance test executes', async () => {
    const now = await compute();
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, serialize(now));
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(now).toEqual(saved);
  });

  it('carries the named cases the semantics round depends on, each once', async () => {
    const names = buildCorpus().map((c) => c.name);
    expect(new Set(names).size).toBe(names.length);
    for (const name of [
      'for_lit_3_1_1', 'for_lit_1_3_-1', 'and_short', 'or_short', 'arith_textnum', 'len_num',
      'isempty_num', 'indexof_text', 'getidx_noat', 'sublist_text_at', 'repeat_text', 'math_change_text',
      'round_2.5', 'repeat_2.7', 'getidx_1.5',
      'constrain_low_gt_high', 'random_from_gt_to', 'isempty_empty_socket', 'join_float_and_list',
      'cap_for_loop', 'cap_lists_repeat', 'cap_lists_create_with', 'cap_text_join',
      // the review-fix round (2026-10-25)
      'proc_param_shadows_global', 'proc_nested_reads_global', 'proc_recursion_sum_20',
      'proc_param_write_is_local', 'proc_recursion_400',
      'cmp_eq_empty', 'cmp_lt_empty', 'and_empty_b', 'or_empty_a', 'neg_empty',
      'mod_empty_divisor', 'mod_empty_dividend', 'constrain_empty_high',
      'arith_unset_var', 'for_unset_to', 'repeat_unset_times', 'change_bool',
      'setidx_insert_fromend_0', 'sublist_fromstart_0', 'sublist_fromend_0',
      'sublist_text', 'getidx_text', 'sublist_number', 'getidx_text_remove',
      'repeat_list_2.7', 'randint_1.5',
    ]) {
      expect(names).toContain(name);
    }
  });

  it('every program is built from Blockly’s own blocks only', async () => {
    const { cases } = await compute();
    const walk = (b, out) => {
      if (!b || typeof b !== 'object') return;
      if (typeof b.type === 'string') out.add(b.type);
      for (const v of Object.values(b)) {
        if (Array.isArray(v)) v.forEach((x) => walk(x, out));
        else if (v && typeof v === 'object') walk(v, out);
      }
    };
    const types = new Set();
    for (const c of cases) walk(c.ws, types);
    for (const t of types) expect(t.startsWith('edubotics_')).toBe(false);
    // The preview of a corpus program never calls the robot.
    for (const c of cases) expect(c.py).not.toMatch(/\brobot\./);
  });
});
