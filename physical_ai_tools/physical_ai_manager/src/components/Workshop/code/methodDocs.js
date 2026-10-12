/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The „Methoden" column's data and the editor's method hover, as pure
// functions over `robot_api_docs.json` (rendered from the ONE table,
// physical_ai_server/workflow/robot_api.py::export_docs_json).
//
// LAZY ONLY: imported by the lazy editor (CodeEditor.jsx, the hover) and the
// lazy MethodenPanel.jsx — never by a module of the entry bundle, so the docs
// JSON stays out of it (code/__tests__/methodDocs.lazy.test.js fences it).
//
// The example lines are rendered by PARAMETER KIND, exactly as the Python
// generator renders them (`_example_literal`): `JSON.parse` turns `440.0` into
// `440`, so a float is re-spelled from its kind, never from the number alone.
// methodDocs.test.js holds `renderExample` equal to the generator's own
// `example_py` / `example_java` for every row.

import docs from './robot_api_docs.json';
import { tokenizeCode } from './codeAssetUsage';
import { CODE_DE, formatCode } from './codeMessagesDe';

const METHODS = Object.freeze((docs.methods || []).map((m) => Object.freeze(m)));
const TYPES = Object.freeze((docs.types || []).map((t) => Object.freeze(t)));
const BY_NAME = new Map(METHODS.map((m) => [m.name, m]));
const BY_JAVA_NAME = new Map(METHODS.map((m) => [m.java_name, m]));

/**
 * Every public robot method (table order) and the two types. `byName` holds
 * the rows by their Python name.
 */
export const METHOD_DOCS = Object.freeze({
  methods: METHODS,
  types: TYPES,
  byName: Object.freeze(Object.fromEntries(METHODS.map((m) => [m.name, m]))),
});

/** A row by the name the student writes in `language`, or null. */
export function methodByName(language, name) {
  if (typeof name !== 'string') return null;
  return (language === 'java' ? BY_JAVA_NAME : BY_NAME).get(name) || null;
}

// What a value row's example declares its variable as, in Java
// (robot_api._JAVA_DOC_TYPE).
const JAVA_DOC_TYPE = Object.freeze({
  bool: 'boolean',
  int: 'int',
  point: 'double[]',
  name: 'String',
  ziel: 'Robot.Greifziel',
});

// Python's repr(float(v)) for the values the table uses: an integral float
// keeps its `.0`.
function floatLiteral(v) {
  const s = String(Number(v));
  return /^-?\d+$/.test(s) ? `${s}.0` : s;
}

function isRef(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value) && typeof value.ref === 'string';
}

/** One argument as source text, by the parameter's kind. */
export function literalFor(kind, value, language) {
  if (isRef(value)) return value.ref;
  switch (kind) {
    case 'float':
      return floatLiteral(value);
    case 'int':
      return String(Math.trunc(Number(value)));
    case 'str':
    case 'text':
    case 'obj':
      return JSON.stringify(value);
    case 'target':
      return literalFor(typeof value === 'string' ? 'str' : 'point', value, language);
    case 'point': {
      const inner = (Array.isArray(value) ? value : []).map(floatLiteral).join(', ');
      return language === 'java' ? `new double[] {${inner}}` : `[${inner}]`;
    }
    case 'bool':
      if (language === 'java') return value ? 'true' : 'false';
      return value ? 'True' : 'False';
    case 'value':
      if (typeof value === 'boolean') return literalFor('bool', value, language);
      if (typeof value === 'number') return literalFor(Number.isInteger(value) ? 'int' : 'float', value, language);
      return literalFor('str', String(value), language);
    default:
      return String(value);
  }
}

/**
 * The example line of `method` in `language` — its own example arguments, or
 * `args` (exampleArgsFor). Python `robot.name(…)` / `var = robot.name(…)`;
 * Java `Robot.javaName(…);` / `Type var = Robot.javaName(…);`.
 */
export function renderExample(method, language, args) {
  if (!method) return '';
  const values = Array.isArray(args) ? args : ((method.example && method.example.args) || []);
  const params = method.params || [];
  const list = values.map((v, i) => literalFor(params[i] ? params[i].kind : 'value', v, language)).join(', ');
  const value = method.table === 'value';
  const variable = method.example && method.example.var;
  if (language === 'java') {
    const line = `Robot.${method.java_name}(${list});`;
    return value && variable ? `${JAVA_DOC_TYPE[method.returns] || 'var'} ${variable} = ${line}` : line;
  }
  const line = `robot.${method.name}(${list})`;
  return value && variable ? `${variable} = ${line}` : line;
}

// The Sammlung list an asset parameter takes its example name from.
const SAMMLUNG_LIST_OF = Object.freeze({ recording: 'recordings', place: 'places', object: 'objects' });
// A name that sits between double quotes in both languages as is.
const QUOTE_SAFE_RE = /^[^"\\\n\r]+$/;

/**
 * The example arguments, with the student's OWN first Bewegung / Ziel /
 * Objekt in place of the table's name where the method takes one (the first
 * parameter with an asset tag `recording`, `place` or `object`). A name the
 * parameter's pattern refuses, or one with a quote, a backslash or a line
 * break, keeps the table's example. `place_def`, `counter` and `variable`
 * name something NEW, so they always keep it.
 */
export function exampleArgsFor(method, sammlung) {
  const args = method && method.example && Array.isArray(method.example.args)
    ? method.example.args.slice() : [];
  if (!method || !sammlung || typeof sammlung !== 'object') return args;
  const params = method.params || [];
  const index = params.findIndex((p) => typeof p.asset === 'string');
  if (index < 0 || index >= args.length) return args;
  const param = params[index];
  const listKey = SAMMLUNG_LIST_OF[param.asset];
  if (!listKey) return args;
  const list = Array.isArray(sammlung[listKey]) ? sammlung[listKey] : [];
  const name = list.find((n) => typeof n === 'string' && n.length > 0);
  if (!name || !QUOTE_SAFE_RE.test(name)) return args;
  if (typeof param.max_len === 'number' && name.length > param.max_len) return args;
  if (typeof param.pattern === 'string') {
    let re = null;
    try {
      re = new RegExp(param.pattern, 'u');
    } catch (_) {
      return args;
    }
    if (!re.test(name)) return args;
  }
  args[index] = name;
  return args;
}

function boundLiteral(param, v) {
  return param.kind === 'int' ? String(Math.trunc(v)) : floatLiteral(v);
}

/** The parameter's range in German („0.25 bis 3.0", „ab 1", „höchstens 40 Zeichen"), or ''. */
export function rangeTextDe(param) {
  if (!param || typeof param !== 'object') return '';
  const lo = typeof param.lo === 'number' ? param.lo : null;
  const hi = typeof param.hi === 'number' ? param.hi : null;
  if (lo !== null && hi !== null) {
    return formatCode(CODE_DE.METHODS_RANGE, boundLiteral(param, lo), boundLiteral(param, hi));
  }
  if (lo !== null) return formatCode(CODE_DE.METHODS_MIN, boundLiteral(param, lo));
  if (typeof param.max_len === 'number') return formatCode(CODE_DE.METHODS_MAX_CHARS, param.max_len);
  return '';
}

/** A parameter's default as source text, or null when it has none. */
export function defaultTextFor(param, language) {
  if (!param || param.required !== false || !('default' in param)) return null;
  return literalFor(param.kind, param.default, language);
}

/**
 * The short form a row of the column shows: Python the signature
 * (`robot.move_to(target)`), Java `Robot.moveTo(…)` — its overloads differ in
 * their parameters, so no one list is THE list (the row adds „(+N Varianten)"
 * and the opened entry shows every overload); a method without parameters is
 * `Robot.home()`.
 */
export function methodShortForm(method, language) {
  if (!method) return '';
  if (language === 'java') {
    return `Robot.${method.java_name}(${(method.params || []).length > 0 ? '…' : ''})`;
  }
  return method.signature_py;
}

/** How many Java overloads a row has beyond its first (0 for Python). */
export function extraVariants(method, language) {
  if (!method || language !== 'java') return 0;
  return Math.max(0, (method.signatures_java || []).length - 1);
}

/**
 * What a hover over a method shows: `{signature, doc}`. Java: the first
 * overload, then „(+N Varianten)" when there are more; its Java sentence where
 * the row has one.
 */
export function methodHoverParts(method, language) {
  if (!method) return { signature: '', doc: '' };
  if (language === 'java') {
    const sigs = method.signatures_java || [];
    const n = extraVariants(method, 'java');
    return {
      signature: (sigs[0] || '') + (n > 0 ? ` ${formatCode(CODE_DE.METHODS_VARIANTS, n)}` : ''),
      doc: method.doc_de_java ?? method.doc_de,
    };
  }
  return { signature: method.signature_py, doc: method.doc_de };
}

// ── hover ──────────────────────────────────────────────────────────────────

const ASCII_IDENT_RE = /[A-Za-z0-9_]/;
const IDENT_CHAR_RE = /[\p{L}\p{N}_]/u;
// The receiver right before the method name, bounded like an identifier on
// its left (codeAssetUsage's RECEIVER): `robot.` / `Robot.` / `edubotics.Robot.`.
const RECEIVER_TAIL_RE = {
  python: /(?<![\p{L}\p{N}_.])robot\s*\.\s*$/u,
  java: /(?<![\p{L}\p{N}_.])(?:edubotics\s*\.\s*)?Robot\s*\.\s*$/u,
};
const RECEIVER_LOOKBACK = 120;

/**
 * The robot method under offset `offset` of `text`: `{from, to, method}` or
 * null. Only an identifier in CODE (never in a string or a comment —
 * tokenizeCode decides), preceded by the receiver (`robot.` in Python,
 * `Robot.` / `edubotics.Robot.` in Java) and naming a public row.
 */
export function methodAtOffset(text, language, offset) {
  if (typeof text !== 'string' || !Number.isInteger(offset) || offset < 0 || offset > text.length) return null;
  const receiver = RECEIVER_TAIL_RE[language];
  if (!receiver) return null;
  const segs = tokenizeCode(text, language);
  const seg = segs.find((s) => s.type === 'code' && s.start <= offset && offset <= s.end);
  if (!seg) return null;
  let from = offset;
  let to = offset;
  while (from > seg.start && ASCII_IDENT_RE.test(text[from - 1])) from -= 1;
  while (to < seg.end && ASCII_IDENT_RE.test(text[to])) to += 1;
  if (from === to) return null;
  // A whole word: no letter (Unicode) on either side.
  if (from > 0 && IDENT_CHAR_RE.test(text[from - 1])) return null;
  if (to < text.length && IDENT_CHAR_RE.test(text[to])) return null;
  const method = methodByName(language, text.slice(from, to));
  if (!method) return null;
  // The receiver must be code too: strings and comments are blanked.
  let before = '';
  for (const s of segs) {
    if (s.end <= from - RECEIVER_LOOKBACK || s.start >= from) continue;
    const a = Math.max(s.start, from - RECEIVER_LOOKBACK);
    const b = Math.min(s.end, from);
    const part = text.slice(a, b);
    before += s.type === 'code' ? part : part.replace(/[^\n]/g, ' ');
  }
  if (!receiver.test(before)) return null;
  return { from, to, method };
}
