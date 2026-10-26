/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The names a Python file gives the robot at its TOP LEVEL (E1 of the
// Roboter Studio review fixes): `import robot as r` makes `r.` a receiver of
// the robot's methods, `from robot import replay as abspielen` makes a bare
// `abspielen(…)` the `replay` row. Every editor scanner that recognises a
// robot call follows them — the usage list, the rename, the asset lint, the
// completion, the hover and the replay scan RunControls sends first.
//
// INPUT: text whose strings and comments are ALREADY blanked (newlines kept),
// i.e. codeAssetUsage.codeOnlyText — so an alias inside a string or a comment
// is none. This module imports nothing from codeAssetUsage.js, which imports
// it (reading tokenizeCode here would be an import cycle).
//
// SCOPE: only a statement that starts in column 0 outside any bracket — an
// alias made inside a function, under a condition or in a loop is not
// followed. Java has no aliasing. Pure and small: it may reach the entry
// bundle (codeAssetUsage is in it already).

import robotApi from './robot_api.json';

// The public rows a function alias may name (robot_api.json `methods`).
const PUBLIC_ROWS = new Set((robotApi.methods || []).map((m) => m && m.name).filter((n) => typeof n === 'string'));

// Python identifiers are Unicode (`röbi`, `größe`).
const NAME = '[\\p{L}_][\\p{L}\\p{N}_]*';
const NAME_RE = new RegExp(`^${NAME}$`, 'u');
const IMPORT_RE = /^import\s+([\s\S]+)$/u;
const FROM_ROBOT_RE = /^from\s+robot\s+import\s+([\s\S]+)$/u;
const ITEM_RE = new RegExp(`^(${NAME}(?:\\s*\\.\\s*${NAME})*)(?:\\s+as\\s+(\\S+))?$`, 'u');

// The file's top-level logical statements: a logical line (physical lines
// joined while a bracket is open or a line ends in `\`) that starts in column
// 0, split at its `;`.
function topLevelStatements(code) {
  const out = [];
  const lines = code.split('\n');
  let depth = 0;
  let current = null;
  let continued = false;
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '');
    const startsStatement = depth === 0 && !continued;
    if (startsStatement) {
      if (current !== null) out.push(current);
      current = /^[^\s]/.test(line) ? line : null;
    } else if (current !== null) {
      current += ` ${line}`;
    }
    for (const ch of line) {
      if (ch === '(' || ch === '[' || ch === '{') depth += 1;
      else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
    }
    continued = /\\\s*$/.test(line);
    if (continued && current !== null) current = current.replace(/\\\s*$/, '');
  }
  if (current !== null) out.push(current);
  const statements = [];
  for (const logical of out) {
    let from = 0;
    let d = 0;
    for (let i = 0; i <= logical.length; i += 1) {
      const ch = logical[i];
      if (ch === '(' || ch === '[' || ch === '{') d += 1;
      else if (ch === ')' || ch === ']' || ch === '}') d = Math.max(0, d - 1);
      if (i === logical.length || (ch === ';' && d === 0)) {
        const s = logical.slice(from, i).trim();
        if (s) statements.push(s);
        from = i + 1;
      }
    }
  }
  return statements;
}

// `a as b, c` (one pair of parentheses around the whole list allowed) →
// [[dotted name, alias or null]], or null when an item is not one.
function importItems(list) {
  let body = list.trim();
  if (body.startsWith('(') && body.endsWith(')')) body = body.slice(1, -1);
  const items = [];
  for (const part of body.split(',')) {
    const item = part.trim();
    if (!item) continue;
    const m = ITEM_RE.exec(item);
    if (!m) return null;
    const alias = m[2] === undefined ? null : m[2];
    if (alias !== null && !NAME_RE.test(alias)) return null;
    items.push([m[1].replace(/\s+/g, ''), alias]);
  }
  return items;
}

/**
 * The robot's names in one Python file, from its code-only text:
 * `{receivers: ['robot', …module aliases], functions: Map(alias → row name)}`.
 * Total: anything that is not a string answers just `robot`.
 */
export function pythonRobotAliases(codeOnly) {
  const receivers = ['robot'];
  const functions = new Map();
  if (typeof codeOnly !== 'string' || !codeOnly) return { receivers, functions };
  for (const statement of topLevelStatements(codeOnly)) {
    const from = FROM_ROBOT_RE.exec(statement);
    if (from) {
      for (const [row, alias] of importItems(from[1]) || []) {
        if (alias && PUBLIC_ROWS.has(row)) functions.set(alias, row);
      }
      continue;
    }
    const imp = IMPORT_RE.exec(statement);
    if (!imp) continue;
    for (const [module, alias] of importItems(imp[1]) || []) {
      if (module === 'robot' && alias && !receivers.includes(alias)) receivers.push(alias);
    }
  }
  return { receivers, functions };
}

/** `s` as a literal inside a RegExp source. */
export function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** The receivers as one regex alternation, `robot|r` (each name escaped). */
export function receiverAlternation(aliases) {
  const receivers = aliases && Array.isArray(aliases.receivers) && aliases.receivers.length
    ? aliases.receivers : ['robot'];
  return receivers.map(escapeRegExp).join('|');
}

/** The bare names that call row `row` here: `row` itself unless an alias
 *  re-binds that name to another row, plus every alias of `row`. */
export function bareNamesOf(aliases, row) {
  const functions = aliases && aliases.functions instanceof Map ? aliases.functions : new Map();
  const out = [];
  if (!functions.has(row) || functions.get(row) === row) out.push(row);
  for (const [alias, target] of functions) if (target === row && !out.includes(alias)) out.push(alias);
  return out;
}

/** A key that is equal exactly when two alias sets are, for regex caches. */
export function aliasKey(aliases) {
  if (!aliases) return 'robot|';
  const functions = aliases.functions instanceof Map ? [...aliases.functions] : [];
  return `${receiverAlternation(aliases)}|${functions.map(([a, r]) => `${a}=${r}`).join(',')}`;
}

/** The row a call `spelling(…)` names: with a receiver its own name, bare
 *  through the file's function aliases first. */
export function rowNameOf(aliases, receiver, spelling) {
  if (receiver) return spelling;
  const functions = aliases && aliases.functions instanceof Map ? aliases.functions : null;
  return functions && functions.has(spelling) ? functions.get(spelling) : spelling;
}
