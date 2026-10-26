/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The editor helpers of owner decision O7, as pure functions CodeEditor wires
// into CodeMirror: name completion inside an asset call's string, the German
// warning for a name the Sammlung does not have (the rules of
// sammlung/referenceValidators.js), and the hover text.

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';
import {
  ASSET_LINT_LANGUAGES,
  assetArgContext,
  assetAtOffset,
  assetDiagnostics,
  assetHoverText,
  assetOptions,
  buildCodeAssetKnowledge,
} from '../codeAssetCompletion';
import { CODE_LINT_LANGUAGES } from '../parseMarkers';
import { CODE_DE, formatCode } from '../codeMessagesDe';

const KNOWN = {
  recordings: [
    { name: 'Winken', duration_s: 4.2, versions: 3 },
    { name: 'Tanz', duration_s: 1, versions: 1 },
  ],
  recordingsStatus: 'ready',
  places: [
    { id: 'd_1', name: 'Ablage', kind: 'pin', x: 0.123, y: 0.04, z: 0 },
    { id: 'd_2', name: 'Hoch', kind: 'pose', x: 0.1, y: -0.02, z: 0.15 },
  ],
  codePinnedNames: ['Mitte'],
  counters: ['Punkte'],
  objects: ['wuerfel'],
  variables: ['punkte'],
};

describe('the two lint switches are separate', () => {
  it('ships the asset lint for both languages while the parse markers stay off', () => {
    expect(ASSET_LINT_LANGUAGES).toEqual(['python', 'java']);
    expect(CODE_LINT_LANGUAGES).toEqual([]);
  });
});

describe('assetArgContext', () => {
  it('detects the cursor inside the string argument of an asset call', () => {
    expect(assetArgContext('robot.move_to("Abl', 'python'))
      .toEqual({ asset: 'place', method: 'move_to', quote: '"', prefix: 'Abl', from: 15 });
    expect(assetArgContext("    robot.replay('", 'python'))
      .toMatchObject({ asset: 'recording', quote: "'", prefix: '', from: 18 });
    expect(assetArgContext('Robot.moveTo("', 'java')).toMatchObject({ asset: 'place', method: 'move_to' });
    expect(assetArgContext('x = robot.ziel("A', 'python')).toMatchObject({ asset: 'place', prefix: 'A' });
    expect(assetArgContext('robot.counter_add("P', 'python')).toMatchObject({ asset: 'counter' });
  });

  it('answers null outside a string, on a non-asset call and on a closed string', () => {
    expect(assetArgContext('robot.move_to(', 'python')).toBeNull();
    expect(assetArgContext('robot.log("Abl', 'python')).toBeNull();
    expect(assetArgContext('robot.move_to("Ablage")', 'python')).toBeNull();
    expect(assetArgContext('"abc".count("a', 'python')).toBeNull();
    expect(assetArgContext("Robot.moveTo('", 'java')).toBeNull();
  });
});

describe('assetOptions', () => {
  it('offers the recordings for replay, the Ziele/Positionen AND the code pins for a place', () => {
    expect(assetOptions('recording', KNOWN).map((o) => o.label)).toEqual(['Winken', 'Tanz']);
    const places = assetOptions('place', KNOWN);
    expect(places.map((o) => o.label)).toEqual(['Ablage', 'Hoch', 'Mitte']);
    expect(places[0].detail).toBe(CODE_DE.ASSET_KIND_PIN);
    expect(places[1].detail).toBe(CODE_DE.ASSET_KIND_POSE);
    expect(places[2].detail).toBe(CODE_DE.ASSET_KIND_CODE_PIN);
    expect(assetOptions('counter', KNOWN).map((o) => o.label)).toEqual(['Punkte']);
    expect(assetOptions('object', KNOWN).map((o) => o.label)).toEqual(['wuerfel']);
    expect(assetOptions('place_def', KNOWN)).toEqual([]);
    expect(assetOptions('recording', null)).toEqual([]);
  });

  it('never offers a name twice', () => {
    const k = { ...KNOWN, codePinnedNames: ['Ablage'], recordings: [{ name: 'A' }, { name: 'A' }] };
    expect(assetOptions('place', k).map((o) => o.label)).toEqual(['Ablage', 'Hoch']);
    expect(assetOptions('recording', k).map((o) => o.label)).toEqual(['A']);
  });
});

describe('assetDiagnostics — the rules of referenceValidators.js', () => {
  const src = [
    'import robot',
    'robot.replay("Winken")',
    'robot.replay("Fehlt")',
    'robot.move_to("Ablage")',
    'robot.move_to("Mitte")',
    'robot.move_to("Nirgends")',
    '# robot.move_to("Kommentar")',
    'robot.grasp("banane")',
    'robot.counter_add("Neu")',
    'robot.move_to("Tipp")',
    '',
  ].join('\n');

  it('warns in German on a missing recording and a missing place, and on nothing else', () => {
    const d = assetDiagnostics(src, 'python', KNOWN, { cursorLine: 1 });
    expect(d.map((x) => src.slice(x.from, x.to))).toEqual(['Fehlt', 'Nirgends', 'Tipp']);
    expect(d[0]).toMatchObject({
      severity: 'warning', source: 'asset', message: formatCode(CODE_DE.ASSET_MISSING_RECORDING, 'Fehlt'),
    });
    expect(d[1].message).toBe(formatCode(CODE_DE.ASSET_MISSING_PLACE, 'Nirgends'));
  });

  it('skips the cursor line (a half-typed name is not a mistake)', () => {
    const d = assetDiagnostics(src, 'python', KNOWN, { cursorLine: 10 });
    expect(d.map((x) => src.slice(x.from, x.to))).toEqual(['Fehlt', 'Nirgends']);
  });

  it('judges a recording only once the list is READY', () => {
    for (const status of ['none', 'idle', 'loading', 'error']) {
      const d = assetDiagnostics(src, 'python', { ...KNOWN, recordingsStatus: status }, { cursorLine: 0 });
      expect(d.map((x) => src.slice(x.from, x.to))).toEqual(['Nirgends', 'Tipp']);
    }
  });

  it('is off for a language outside the switch and total on garbage', () => {
    expect(assetDiagnostics(src, 'javascript', KNOWN, {})).toEqual([]);
    expect(assetDiagnostics(null, 'python', KNOWN, {})).toEqual([]);
    expect(assetDiagnostics(src, 'python', null, {}).length).toBeGreaterThan(0);
  });

  it('reads Java spellings', () => {
    const j = 'class Main {\n  void m() {\n    Robot.moveTo("Nirgends");\n    Robot.replay("Winken");\n  }\n}\n';
    expect(assetDiagnostics(j, 'java', KNOWN, { cursorLine: 1 }).map((x) => j.slice(x.from, x.to)))
      .toEqual(['Nirgends']);
  });
});

describe('assetAtOffset + assetHoverText', () => {
  const src = 'robot.move_to("Ablage")\nrobot.replay("Winken")\nrobot.move_to("Hoch")\nrobot.move_to("Mitte")\nrobot.replay("Weg")\n';

  it('finds the asset literal under the pointer', () => {
    expect(assetAtOffset(src, 'python', 17)).toMatchObject({ asset: 'place', name: 'Ablage' });
    expect(assetAtOffset(src, 'python', 3)).toBeNull();
  });

  it('describes a Ziel, a Position, a code pin and a recording in German', () => {
    expect(assetHoverText('place', 'Ablage', KNOWN)).toBe('Ziel „Ablage“ · x 12,3 cm · y 4,0 cm');
    expect(assetHoverText('place', 'Hoch', KNOWN)).toBe('Position „Hoch“ · x 10,0 cm · y −2,0 cm · z 15,0 cm');
    expect(assetHoverText('place', 'Mitte', KNOWN)).toBe(formatCode(CODE_DE.HOVER_CODE_PIN, 'Mitte'));
    expect(assetHoverText('recording', 'Winken', KNOWN)).toBe('Bewegung „Winken“ · 4,2 s · 3 Versionen');
    expect(assetHoverText('recording', 'Tanz', KNOWN)).toBe('Bewegung „Tanz“ · 1,0 s');
    expect(assetHoverText('recording', 'Weg', KNOWN)).toBe(formatCode(CODE_DE.ASSET_MISSING_RECORDING, 'Weg'));
    expect(assetHoverText('recording', 'Weg', { ...KNOWN, recordingsStatus: 'loading' })).toBeNull();
    expect(assetHoverText('object', 'wuerfel', KNOWN)).toBeNull();
  });
});

describe('buildCodeAssetKnowledge — what the editor helpers know', () => {
  it('groups the recordings by name (newest first), takes the store, the code’s pins and counters, the object types', () => {
    const k = buildCodeAssetKnowledge({
      files: { 'main.py': 'import robot\nrobot.pin("Mitte", 0, 0, 0)\nrobot.counter_add("Punkte")\nrobot.sees("banane")\n' },
      language: 'python',
      entries: [{ id: 'd_1', name: 'Ablage', kind: 'pin', x: 0.1, y: 0, z: 0 }],
      trajectories: {
        status: 'ready',
        items: [
          { id: 't1', name: 'Winken', duration_s: 3, created_at: '2026-09-27T09:00:00Z' },
          { id: 't2', name: 'Winken', duration_s: 4.2, created_at: '2026-09-27T10:00:00Z' },
          { id: 't3', name: 'Tanz', duration_s: 1, created_at: '2026-09-27T08:00:00Z' },
        ],
      },
      objectTypes: ['wuerfel'],
    });
    expect(k.recordingsStatus).toBe('ready');
    expect(k.recordings).toEqual([
      { name: 'Winken', duration_s: 4.2, versions: 2 },
      { name: 'Tanz', duration_s: 1, versions: 1 },
    ]);
    expect(k.places.map((e) => e.name)).toEqual(['Ablage']);
    expect(k.codePinnedNames).toEqual(['Mitte']);
    expect(k.counters).toEqual(['Punkte']);
    expect(k.objects).toEqual(['wuerfel', 'banane']);
  });

  it('is total on nothing', () => {
    expect(buildCodeAssetKnowledge({})).toMatchObject({
      recordings: [], recordingsStatus: 'none', places: [], codePinnedNames: [], counters: [], objects: [],
    });
  });
});

describe('n2: a pin() in a comment does not satisfy a move_to', () => {
  it('warns about the name and does not offer it', () => {
    const files = { 'main.py': 'import robot\n# robot.pin("Ablage", 0.1, 0.2, 0)\nrobot.move_to("Ablage")\n' };
    const known = buildCodeAssetKnowledge({ files, language: 'python' });
    expect(known.codePinnedNames).toEqual([]);
    const d = assetDiagnostics(files['main.py'], 'python', known, { cursorLine: 1 });
    expect(d.map((x) => x.message)).toEqual([formatCode(CODE_DE.ASSET_MISSING_PLACE, 'Ablage')]);
  });
});

describe('the editor helpers ride the lazy editor chunk, not the entry bundle (review n10)', () => {
  it('only CodeEditor.jsx (lazy-loaded) imports codeAssetCompletion.js', () => {
    const srcRoot = path.resolve(__dirname, '../../../..');
    const importers = [];
    const walk = (dir) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, e.name);
        if (e.isDirectory()) {
          if (e.name !== '__tests__' && e.name !== 'node_modules') walk(full);
        } else if (/\.(js|jsx)$/.test(e.name)
                   && /from\s+['"][^'"]*codeAssetCompletion['"]/.test(fs.readFileSync(full, 'utf8'))) {
          importers.push(path.relative(srcRoot, full));
        }
      }
    };
    walk(srcRoot);
    expect(importers).toEqual([path.join('components', 'Workshop', 'code', 'CodeEditor.jsx')]);
  });
});

describe('review round 2: completion knows the keyword form and a Unicode receiver boundary (ni1, ni2)', () => {
  it('offers inside `name="` / `target="` — the parameter the table names — and nowhere else', () => {
    expect(assetArgContext('robot.replay(name="Wi', 'python')).toMatchObject({ asset: 'recording', prefix: 'Wi' });
    expect(assetArgContext('robot.replay(speed=2, name="', 'python')).toMatchObject({ asset: 'recording', prefix: '' });
    expect(assetArgContext('robot.move_to(target=\'Ab', 'python')).toMatchObject({ asset: 'place', prefix: 'Ab' });
    expect(assetArgContext('robot.replay(speed="', 'python')).toBeNull();
    expect(assetArgContext('Größrobot.replay("', 'python')).toBeNull();
    expect(assetArgContext('ßmove_to("', 'python')).toBeNull();
    expect(assetArgContext('ÄRobot.replay("', 'java')).toBeNull();
    expect(assetArgContext('edubotics.Robot.replay("', 'java')).toMatchObject({ asset: 'recording' });
  });
});

// F10 (semantics round 2026-10-18): a run sends only the recordings whose
// names stand in the program as whole literals (codeProject's scan), so a
// `replay` whose name is a variable, an f-string or a concatenation plays a
// recording that never reaches the robot. The editor says so where it is
// written.
describe('F10: a replay whose name is not written directly in quotes', () => {
  const lineOf = (src, offset) => src.slice(0, offset).split('\n').length;

  it('non_literal_replay_is_flagged', () => {
    const src = [
      'import robot',
      'name = "Winken"',
      'robot.replay(name)',
      'robot.replay(f"Win{1}")',
      'robot.replay("Win" + "ken")',
      'robot.replay("Winken")',
      'robot.replay(speed=2, name="Winken")',
      'robot.replay(speed=2, name=name)',
      'robot.replay("Winken", 2)',
      '# robot.replay(name)',
      'print("robot.replay(name)")',
      'robot.replay(name)',
      '',
    ].join('\n');
    const flagged = assetDiagnostics(src, 'python', KNOWN, { cursorLine: 12 })
      .filter((d) => d.message === CODE_DE.REPLAY_NAME_NOT_LITERAL);
    expect(flagged.map((d) => [lineOf(src, d.from), src.slice(d.from, d.to)])).toEqual([
      [3, 'name'],
      [4, 'f"Win{1}"'],
      [5, '"Win" + "ken"'],
      [8, 'name=name'],
    ]);
    for (const d of flagged) expect(d.severity).toBe('warning');
  });

  it('reads Java, and only the robot’s replay', () => {
    const j = [
      'import edubotics.Robot;',
      'public class Main {',
      '  static void replay(String n) {}',
      '  public static void main(String[] args) {',
      '    String n = "Winken";',
      '    Robot.replay(n);',
      '    Robot.replay("Winken");',
      '    Robot.replay("Win" + "ken", 2.0);',
      '    replay(n);',
      '  }',
      '}',
      '',
    ].join('\n');
    const flagged = assetDiagnostics(j, 'java', KNOWN, { cursorLine: 0 })
      .filter((d) => d.message === CODE_DE.REPLAY_NAME_NOT_LITERAL);
    expect(flagged.map((d) => [lineOf(j, d.from), j.slice(d.from, d.to)])).toEqual([
      [6, 'n'],
      [8, '"Win" + "ken"'],
    ]);
  });

  it('the sentence is German and says why', () => {
    expect(CODE_DE.REPLAY_NAME_NOT_LITERAL).toBe(
      'Der Name einer Bewegung muss direkt in Anführungszeichen stehen, sonst wird sie nicht mitgeschickt.',
    );
  });
});

// E1 (Roboter Studio review fixes): a top-level alias of `robot` or of one of
// its functions is the robot for the completion and the not-a-literal warning
// too. The completion reads the aliases from the whole document (its third
// argument); without it only `robot.` is the robot, as before.
describe('E1: aliases of `robot` in the completion and the warning', () => {
  const lineOf = (src, offset) => src.slice(0, offset).split('\n').length;
  const notLiteral = (src) => assetDiagnostics(src, 'python', KNOWN, { cursorLine: 0 })
    .filter((d) => d.message === CODE_DE.REPLAY_NAME_NOT_LITERAL)
    .map((d) => [lineOf(src, d.from), src.slice(d.from, d.to)]);

  it('completes inside r.replay(" and a function alias', () => {
    const doc = 'import robot as r\nfrom robot import replay as abspielen\nr.replay("Wi';
    expect(assetArgContext('r.replay("Wi', 'python', doc))
      .toMatchObject({ asset: 'recording', method: 'replay', prefix: 'Wi' });
    expect(assetArgContext('abspielen("', 'python', doc)).toMatchObject({ asset: 'recording', method: 'replay' });
    expect(assetArgContext('r.move_to(speed=2, target="A', 'python', doc))
      .toMatchObject({ asset: 'place', prefix: 'A' });
    // Without the document, or in another document, `r` is not the robot.
    expect(assetArgContext('r.replay("Wi', 'python')).toBeNull();
    expect(assetArgContext('r.replay("Wi', 'python', 'import math as r\n')).toBeNull();
    expect(assetArgContext('r.abspielen("', 'python', doc)).toBeNull();
  });

  it('flags a not-literal name on r.replay(x) and on a bare replay(x)', () => {
    const src = [
      'import robot as r',
      'from robot import replay, replay as abspielen',
      'n = "Winken"',
      'r.replay(n)',
      'replay(n)',
      'abspielen(n)',
      'r.replay("Winken")',
      'abspielen("Winken")',
      'def replay(name):',
      '    pass',
      'self.replay(n)',
      '',
    ].join('\n');
    expect(notLiteral(src)).toEqual([[4, 'n'], [5, 'n'], [6, 'n']]);
  });

  it('a bare replay re-bound to another row, and an alias in another file, are not judged', () => {
    expect(notLiteral('from robot import home as replay\nreplay(1)\n')).toEqual([]);
    expect(notLiteral('r.replay(n)\nabspielen(n)\n')).toEqual([]);
  });
});
