// The editor's robot_api.json (rendered from the server's robot_api.py table)
// and pythonCodeGen.js (the Blockly display mirror) describe the same
// `robot.*` surface from two ends. They differ in EXACTLY three names, and
// every difference is deliberate:
//
//   json − pythonCodeGen == {'zeige', 'ziel'}
//                                          robot.ziel(name) is how a TEXT
//                                          program references a pinned
//                                          destination; Blockly drops the
//                                          value block into the socket.
//                                          robot.zeige(name, wert) is a
//                                          code-only row (robot_api
//                                          CODE_ONLY_METHODS, 2026-09-27):
//                                          a block program's variables reach
//                                          the Variablen panel by themselves
//   pythonCodeGen − json == {'broadcast'}  hats have no meaning in a text
//                                          program; a text program uses
//                                          functions
//
// Asserted as exact sets, so a third difference in either direction — a
// method added to the table without a Blockly mirror, or a generator
// spelling a method the server does not dispatch — fails here.
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

const API_JSON = path.resolve(__dirname, '../code/robot_api.json');
const CODEGEN_JS = path.resolve(__dirname, '../pythonCodeGen.js');

function jsonMethodNames() {
  const doc = JSON.parse(fs.readFileSync(API_JSON, 'utf8'));
  return new Set(doc.methods.map((m) => m.name));
}

function codeGenMethodNames() {
  const src = fs.readFileSync(CODEGEN_JS, 'utf8');
  return new Set([...src.matchAll(/robot\.([a-z_]+)\(/g)].map((m) => m[1]));
}

const minus = (a, b) => [...a].filter((x) => !b.has(x)).sort();

describe('robot_api.json vs pythonCodeGen.js', () => {
  it('the JSON names minus the generator names are exactly {zeige, ziel}', () => {
    expect(minus(jsonMethodNames(), codeGenMethodNames())).toEqual(['zeige', 'ziel']);
  });

  it('the generator names minus the JSON names are exactly {broadcast}', () => {
    expect(minus(codeGenMethodNames(), jsonMethodNames())).toEqual(['broadcast']);
  });

  it('the JSON carries 32 handler methods with a block type and one code-only method', () => {
    const doc = JSON.parse(fs.readFileSync(API_JSON, 'utf8'));
    expect(doc.methods).toHaveLength(33);
    const handlerRows = doc.methods.filter((m) => m.table !== 'code');
    const codeOnly = doc.methods.filter((m) => m.table === 'code');
    expect(handlerRows).toHaveLength(32);
    expect(codeOnly.map((m) => m.name)).toEqual(['zeige']);
    for (const m of doc.methods) {
      expect(typeof m.doc_de).toBe('string');
      expect(m.doc_de.length).toBeGreaterThan(0);
      expect(Array.isArray(m.params)).toBe(true);
    }
    for (const m of handlerRows) expect(m.block_type).toMatch(/^edubotics_/);
    for (const m of codeOnly) expect(m.block_type).toBeNull();
  });

  it('the JSON carries both frame bounds and the three project caps', () => {
    const { limits } = JSON.parse(fs.readFileSync(API_JSON, 'utf8'));
    expect(limits.MAX_FRAME_BYTES).toBe(65536);
    expect(limits.CONTROL_MAX_FRAME_BYTES).toBe(196608);
    expect(limits.MAX_CODE_FILES).toBe(32);
    expect(limits.MAX_CODE_FILE_BYTES).toBe(65536);
    expect(limits.MAX_CODE_PROJECT_BYTES).toBe(131072);
    expect(limits.CONTROL_MAX_FRAME_BYTES).toBeGreaterThanOrEqual(
      limits.MAX_CODE_PROJECT_BYTES + 8192,
    );
    expect(typeof limits.CODE_PATH_RE).toBe('string');
  });
});
