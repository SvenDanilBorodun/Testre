/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code program's Sammlung scanner: which recordings, Ziele, counters,
// objects and shown variables the text names, where, and whether the call sits
// in a comment (the counterpart of a disabled block). Pure — no DOM, no
// CodeMirror — and the method set comes from robot_api.json's asset tags.

import { describe, it, expect } from 'vitest';
import robotApi from '../robot_api.json';
import {
  ASSET_CALLS,
  codeDefinedPlaceNames,
  codeUsageMaps,
  collectCodeVariables,
  findAssetCalls,
  renameCodeAssetRefs,
  scanCodeAssets,
  tokenizeCode,
  variableOccurrences,
  variableOccurrencesAll,
} from '../codeAssetUsage';
import { collectCodeReplayNames } from '../codeProject';

const PY = [
  'import robot',
  '',
  'robot.home()',
  'robot.replay("Winken")',
  'robot.replay("Winken", 1.5)',
  '# robot.replay("Alt")',
  'robot.move_to("Ablage")',
  "robot.drop_at('Kiste')",
  'robot.pin("Mitte", 0.2, -0.05, 0.0)',
  'robot.pin_current("Hier")',
  'robot.counter_add("Punkte")',
  'if robot.sees("wuerfel"):',
  '    ziel = robot.find("wuerfel")',
  'print(\'robot.replay("Nicht")\')',
  'text = "abc".count("a")',
  'robot.move_to(robot.ziel("Ablage"))',
  'robot.zeige("Anzahl Würfel", 3)',
  'robot.move_to(f"Abl{1}")',
  'robot.move_to("Abl" + "age")',
  '',
].join('\n');

const JAVA = [
  'import edubotics.Robot;',
  '',
  'public class Main {',
  '    public static void main(String[] args) {',
  '        Robot.replay("Winken");',
  '        // Robot.replay("Alt");',
  '        /* Robot.moveTo("Block"); */',
  '        Robot.moveTo("Ablage");',
  '        char c = \'"\';',
  '        Robot.dropAt("Kiste");',
  '        Robot.pin("Mitte", 0.2, -0.05, 0.0);',
  '        String s = "Robot.moveTo(\\"X\\")";',
  '        Robot.zeige("punkte", 3);',
  '    }',
  '}',
  '',
].join('\n');

describe('the asset call table comes from robot_api.json', () => {
  it('names every method whose first parameter carries an asset tag, per language', () => {
    const tagged = robotApi.methods.filter((m) => m.params && m.params[0] && m.params[0].asset);
    expect(tagged.length).toBeGreaterThan(10);
    for (const m of tagged) {
      // `param`: the keyword a Python call may name the asset by (round 2, ni1).
      const row = { method: m.name, asset: m.params[0].asset, param: m.params[0].name };
      expect(ASSET_CALLS.python.get(m.name)).toEqual(row);
      expect(ASSET_CALLS.java.get(m.java_name)).toEqual(row);
    }
    expect(ASSET_CALLS.python.get('home')).toBeUndefined();
    expect(ASSET_CALLS.java.get('moveTo').asset).toBe('place');
  });
});

describe('tokenizeCode', () => {
  it('splits Python into code, strings and comments, with prefixes and triple quotes', () => {
    const segs = tokenizeCode('x = r"a#b" # c\ny = """q\n"z"\n"""\n', 'python');
    expect(segs.map((s) => s.type)).toEqual(['code', 'string', 'code', 'comment', 'code', 'string', 'code']);
    expect(segs[1]).toMatchObject({ prefix: 'r', value: 'a#b', triple: false });
    expect(segs[5]).toMatchObject({ triple: true });
  });

  it('knows a Java char literal holding a quote is not a string opener', () => {
    const segs = tokenizeCode('char c = \'"\'; String s = "a"; // x\n/* y */', 'java');
    const strings = segs.filter((s) => s.type === 'string');
    expect(strings.map((s) => s.value)).toEqual(['a']);
    expect(segs.filter((s) => s.type === 'comment')).toHaveLength(2);
  });

  it('covers the whole text, in order, whatever it is handed', () => {
    for (const text of [PY, JAVA, '"unterminated', '/* open', '', '#']) {
      for (const lang of ['python', 'java']) {
        const segs = tokenizeCode(text, lang);
        expect(segs.map((s) => text.slice(s.start, s.end)).join('')).toBe(text);
      }
    }
  });
});

describe('findAssetCalls', () => {
  it('finds exactly the string-literal first arguments of asset calls (Python)', () => {
    const calls = findAssetCalls(PY, 'python');
    const plain = calls.map((c) => [c.method, c.name, c.line, c.inComment]);
    expect(plain).toEqual([
      ['replay', 'Winken', 4, false],
      ['replay', 'Winken', 5, false],
      ['replay', 'Alt', 6, true],
      ['move_to', 'Ablage', 7, false],
      ['drop_at', 'Kiste', 8, false],
      ['pin', 'Mitte', 9, false],
      ['pin_current', 'Hier', 10, false],
      ['counter_add', 'Punkte', 11, false],
      ['sees', 'wuerfel', 12, false],
      ['find', 'wuerfel', 13, false],
      ['ziel', 'Ablage', 16, false],
      ['zeige', 'Anzahl Würfel', 17, false],
    ]);
    // Never: a string that only LOOKS like a call, str.count, an f-string, a
    // concatenation.
    expect(calls.some((c) => c.name === 'Nicht' || c.name === 'a')).toBe(false);
    expect(calls.some((c) => c.name.startsWith('Abl') && c.name !== 'Ablage')).toBe(false);
  });

  it('finds the Java spellings, and a comment of either kind is inComment', () => {
    const calls = findAssetCalls(JAVA, 'java');
    expect(calls.map((c) => [c.method, c.name, c.line, c.inComment])).toEqual([
      ['replay', 'Winken', 5, false],
      ['replay', 'Alt', 6, true],
      ['move_to', 'Block', 7, true],
      ['move_to', 'Ablage', 8, false],
      ['drop_at', 'Kiste', 10, false],
      ['pin', 'Mitte', 11, false],
      ['zeige', 'punkte', 13, false],
    ]);
  });

  it('points at the literal CONTENT, so a rename can rewrite exactly it', () => {
    const [call] = findAssetCalls('robot.replay( "Winken" )\n', 'python');
    expect('robot.replay( "Winken" )\n'.slice(call.valueStart, call.valueEnd)).toBe('Winken');
    expect(call.col).toBe(14);
  });

  it('reads the pinned coordinates of a literal pin(), NaN otherwise', () => {
    const calls = findAssetCalls('robot.pin("A", 0.2, -0.05, 1e-2)\nrobot.pin("B", x, 0, 0)\n', 'python');
    expect(calls[0].coords).toEqual({ x: 0.2, y: -0.05, z: 0.01 });
    expect(Number.isNaN(calls[1].coords.x)).toBe(true);
  });
});

describe('scanCodeAssets', () => {
  const files = { 'main.py': PY, 'hilfe.py': 'import robot\nrobot.replay("Tanz")\n' };
  const scan = scanCodeAssets(files, 'python');

  it('groups the calls by kind and by name, with file, line, column and inComment', () => {
    expect([...scan.replay.keys()].sort()).toEqual(['Alt', 'Tanz', 'Winken']);
    expect(scan.replay.get('Winken').map((r) => r.line)).toEqual([4, 5]);
    expect(scan.replay.get('Tanz')[0]).toMatchObject({ file: 'hilfe.py', line: 2, inComment: false });
    expect(scan.replay.get('Alt')[0].inComment).toBe(true);
    expect([...scan.refs.keys()].sort()).toEqual(['Ablage', 'Kiste']);
    expect(scan.refs.get('Ablage')).toHaveLength(2);        // move_to + ziel
    expect([...scan.pinStatements.keys()]).toEqual(['Mitte']);
    expect(scan.pinStatements.get('Mitte')[0].coords).toEqual({ x: 0.2, y: -0.05, z: 0 });
    expect([...scan.currentStatements.keys()]).toEqual(['Hier']);
    expect([...scan.counters.keys()]).toEqual(['Punkte']);
    expect([...scan.objects.keys()]).toEqual(['wuerfel']);
    expect([...scan.variables.keys()]).toEqual(['Anzahl Würfel']);
  });

  it('is total on garbage', () => {
    for (const bad of [null, undefined, 3, [], { 'a.py': 5 }]) {
      const s = scanCodeAssets(bad, 'python');
      expect(s.replay.size).toBe(0);
    }
  });

  it('never replaces the run-time scan, which stays loose on purpose', () => {
    // collectCodeReplayNames still catches the comment and the string (a
    // skipped 404 costs one fetch); the UI scanner does not.
    expect(collectCodeReplayNames({ 'main.py': PY })).toEqual(['Winken', 'Alt', 'Nicht']);
  });
});

describe('codeUsageMaps — the assetIndex usage shape', () => {
  it('counts a call in a comment as disabled and ids rows by <file>:L<line>', () => {
    const scan = scanCodeAssets({ 'main.py': PY }, 'python');
    const usage = codeUsageMaps(scan, [{ id: 'punkte', name: 'punkte', uses: 2 }]);
    expect(usage.replay.get('Winken')).toEqual({ enabled: 2, disabled: 0, blockIds: ['main.py:L4', 'main.py:L5'] });
    expect(usage.replay.get('Alt')).toEqual({ enabled: 0, disabled: 1, blockIds: ['main.py:L6'] });
    expect(usage.pinStatements.get('Mitte')).toMatchObject({ blockId: 'main.py:L9', enabled: true, x: 0.2, y: -0.05, z: 0 });
    expect(usage.currentStatements.get('Hier')).toEqual({ blockId: 'main.py:L10', enabled: true });
    expect(usage.variableUses.get('punkte')).toBe(2);
  });
});

describe('renameCodeAssetRefs', () => {
  it('rewrites every reference of a recording, comments included, never another name', () => {
    const { files, count } = renameCodeAssetRefs({ 'main.py': PY }, 'python', 'recording', 'Winken', 'Gruß');
    expect(count).toBe(2);
    expect(files['main.py']).toContain('robot.replay("Gruß")');
    expect(files['main.py']).toContain('robot.replay("Gruß", 1.5)');
    expect(files['main.py']).toContain('# robot.replay("Alt")');
    const again = renameCodeAssetRefs(files, 'python', 'recording', 'Alt', 'Neu');
    expect(again.count).toBe(1);
    expect(again.files['main.py']).toContain('# robot.replay("Neu")');
  });

  it('rewrites the place REFERENCES (move_to/pickup/drop_at/ziel) and never a pin() definition', () => {
    const src = 'robot.pin("Ablage", 0.1, 0, 0)\nrobot.move_to("Ablage")\nrobot.move_to(robot.ziel("Ablage"))\nrobot.pickup(\'Ablage\')\n';
    const { files, count } = renameCodeAssetRefs({ 'main.py': src }, 'python', 'place', 'Ablage', 'Tisch');
    expect(count).toBe(3);
    expect(files['main.py']).toBe('robot.pin("Ablage", 0.1, 0, 0)\nrobot.move_to("Tisch")\nrobot.move_to(robot.ziel("Tisch"))\nrobot.pickup(\'Tisch\')\n');
  });

  it('keeps unchanged files as the SAME strings and returns the input on no match', () => {
    const input = { 'main.py': PY, 'b.py': 'x = 1\n' };
    const { files, count } = renameCodeAssetRefs(input, 'python', 'place', 'Kiste', 'Box');
    expect(count).toBe(1);
    expect(files['b.py']).toBe(input['b.py']);
    const none = renameCodeAssetRefs(input, 'python', 'place', 'Gibtsnicht', 'X');
    expect(none.count).toBe(0);
    expect(none.files).toBe(input);
  });

  it('works on Java and trims the stored name like a Blockly field', () => {
    const { files, count } = renameCodeAssetRefs({ 'Main.java': JAVA }, 'java', 'place', 'Ablage', 'Tisch');
    expect(count).toBe(1);
    expect(files['Main.java']).toContain('Robot.moveTo("Tisch");');
    const padded = renameCodeAssetRefs({ 'main.py': 'robot.move_to(" Ablage ")\n' }, 'python', 'place', 'Ablage', 'Tisch');
    expect(padded.files['main.py']).toBe('robot.move_to("Tisch")\n');
  });
});

describe('codeDefinedPlaceNames', () => {
  it('lists the names pin() and pin_current() define, in every file', () => {
    expect(codeDefinedPlaceNames({ 'main.py': PY, 'x.py': 'robot.pin("Z", 0, 0, 0)\n' }, 'python'))
      .toEqual(['Mitte', 'Hier', 'Z']);
    expect(codeDefinedPlaceNames({ 'Main.java': JAVA }, 'java')).toEqual(['Mitte']);
  });
});

describe('collectCodeVariables (display only)', () => {
  it('lists Python assignment and for-loop targets, first occurrence, no dunders', () => {
    const src = 'import robot\n__x = 1\npunkte = 0\na, b = 1, 2\nfor i, w in enumerate([]):\n    punkte += 1\nif punkte == 3:\n    pass\nliste[0] = 5\n# kommentar = 1\ns = "c = 2"\n';
    expect(collectCodeVariables({ 'main.py': src }, 'python').map((v) => [v.name, v.line]))
      .toEqual([['punkte', 3], ['a', 4], ['b', 4], ['i', 5], ['w', 5], ['s', 11]]);
  });

  it('lists Java local and field declarations, for and for-each variables', () => {
    const src = 'public class Main {\n  static int zaehler = 0;\n  public static void main(String[] args) {\n    double x = 1.5;\n    Robot.Greifziel ziel = Robot.find("w");\n    for (int i = 0; i < 3; i++) {}\n    for (String s : liste) {}\n    // int alt = 1;\n  }\n}\n';
    expect(collectCodeVariables({ 'Main.java': src }, 'java').map((v) => v.name))
      .toEqual(['zaehler', 'args', 'x', 'ziel', 'i', 's']);
  });
});

describe('variableOccurrences', () => {
  it('finds whole-word uses outside strings and comments, never an attribute', () => {
    const src = 'punkte = 0\npunkte += 1\nprint("punkte")\n# punkte\nobj.punkte = 3\nzeige(punkte2)\n';
    expect(variableOccurrences({ 'main.py': src }, 'python', 'punkte').map((o) => o.line)).toEqual([1, 2]);
  });
});

describe('review round fixes (2026-09-27)', () => {
  it('n1: a backslash escapes the quote in a RAW string too (r\'\\\'\' is one string)', () => {
    const src = "x = r'\\''; robot.replay(\"Winken\")\n";
    expect(findAssetCalls(src, 'python').map((c) => `${c.method}:${c.name}`)).toEqual(['replay:Winken']);
    const triple = "y = r'''\\''' '''\nrobot.replay(\"Tanz\")\n";
    expect(findAssetCalls(triple, 'python').map((c) => c.name)).toEqual(['Tanz']);
  });

  it('n2: a pin() inside a comment defines nothing — but its name stays reserved, like a disabled block', () => {
    const files = { 'main.py': 'import robot\n# robot.pin("Ablage", 0.1, 0.2, 0)\nrobot.pin("Mitte", 0, 0, 0)\n' };
    expect(codeDefinedPlaceNames(files, 'python')).toEqual(['Mitte']);
    expect(codeDefinedPlaceNames(files, 'python', { includeComments: true })).toEqual(['Ablage', 'Mitte']);
  });

  it('m3: Unicode identifiers are variables too, in Python and in Java', () => {
    const py = 'größe = 3\nPunkte = 0\nfor stück in range(größe):\n    Punkte += größe\n';
    expect(collectCodeVariables({ 'main.py': py }, 'python').map((v) => v.name))
      .toEqual(['größe', 'Punkte', 'stück']);
    expect(variableOccurrences({ 'main.py': py }, 'python', 'größe').map((o) => o.line)).toEqual([1, 3, 4]);
    expect(variableOccurrences({ 'main.py': 'xgröße = 1\ngröße2 = 2\n' }, 'python', 'größe')).toEqual([]);
    const java = 'public class Main {\n  public static void main(String[] args) {\n    int größe = 3;\n    Würfel w = null;\n    größe += 1;\n  }\n}\n';
    expect(collectCodeVariables({ 'Main.java': java }, 'java').map((v) => v.name)).toEqual(['args', 'größe', 'w']);
    expect(variableOccurrences({ 'Main.java': java }, 'java', 'größe').map((o) => o.line)).toEqual([3, 5]);
  });

  it('m8: variableOccurrencesAll scans every name in one pass and agrees with the one-name scan', () => {
    const files = {
      'main.py': 'punkte = 0\ngröße = 2\npunkte += größe\nprint("punkte")\nobj.punkte = 1\n',
      'hilfe.py': 'def f():\n    return größe\n',
    };
    const all = variableOccurrencesAll(files, 'python', ['punkte', 'größe', 'fehlt']);
    for (const name of ['punkte', 'größe', 'fehlt']) {
      expect(all.get(name)).toEqual(variableOccurrences(files, 'python', name));
    }
  });
});

describe('review round 2 (2026-09-27): keyword arguments, Unicode boundaries, the variable list', () => {
  const names = (content, language = 'python') => findAssetCalls(content, language)
    .map((c) => `${c.method}:${c.name}${c.inComment ? '(c)' : ''}`);

  it('ni1: a keyword first argument is the same asset — every tagged method, its own parameter name', () => {
    const src = [
      'import robot',
      'robot.replay(name="Winken")',
      'robot.replay(speed=2.0, name="Tanz")',
      'robot.move_to(target="Ablage")',
      'robot.pickup(target = "Kiste")',
      'robot.drop_at(target="Kiste")',
      'x = robot.ziel(name="Mitte")',
      'robot.pin(name="Neu", x=0.1, y=0.2, z=0.0)',
      'robot.counter_add(name="Punkte")',
      'robot.grasp(obj="wuerfel")',
      'robot.zeige(name="punkte", wert=3)',
      '# robot.replay(name="Alt")',
      'robot.replay(speed="Falsch")',
      '',
    ].join('\n');
    expect(names(src)).toEqual([
      'replay:Winken', 'replay:Tanz', 'move_to:Ablage', 'pickup:Kiste', 'drop_at:Kiste', 'ziel:Mitte',
      'pin:Neu', 'counter_add:Punkte', 'grasp:wuerfel', 'zeige:punkte', 'replay:Alt(c)',
    ]);
    const pin = findAssetCalls(src, 'python').find((c) => c.method === 'pin');
    expect(pin.coords).toEqual({ x: 0.1, y: 0.2, z: 0 });
    // Java has no keyword arguments: `name = "W"` is an assignment expression.
    expect(names('Robot.replay(name = "W");', 'java')).toEqual([]);
  });

  it('ni1: rename, usage and the defined names see the keyword form too', () => {
    const files = { 'main.py': 'import robot\nrobot.replay(name="Winken")\nrobot.move_to(target="Ablage")\n' };
    const rec = renameCodeAssetRefs(files, 'python', 'recording', 'Winken', 'Gruss');
    expect(rec.count).toBe(1);
    expect(rec.files['main.py']).toBe('import robot\nrobot.replay(name="Gruss")\nrobot.move_to(target="Ablage")\n');
    const place = renameCodeAssetRefs(files, 'python', 'place', 'Ablage', 'Tisch');
    expect(place.files['main.py']).toContain('robot.move_to(target="Tisch")');
    const scan = scanCodeAssets(files, 'python');
    expect([...scan.replay.keys()]).toEqual(['Winken']);
    expect([...scan.refs.keys()]).toEqual(['Ablage']);
    expect(codeDefinedPlaceNames({ 'main.py': 'robot.pin(name="P", x=0, y=0, z=0)\n' }, 'python')).toEqual(['P']);
  });

  it('nb3 (round 3): in a comment, the keyword argument counts wherever it stands, like in code', () => {
    const src = [
      'import robot',
      '# robot.replay(speed=2, name="Alt")',
      '# robot.move_to(target = "Ablage")',
      '# robot.pin(x=0.1, y=0.2, z=0.0, name="Mitte")',
      '# robot.replay(speed=2, other="Nein")',
      '# robot.replay(speed="Falsch")',
      '# robot.replay(x, "Nein2")',
      '# robot.zeige(wert="x", name="punkte")',
      'robot.replay(speed=2, name="Alt")',
      '',
    ].join('\n');
    expect(names(src)).toEqual([
      'replay:Alt(c)', 'move_to:Ablage(c)', 'pin:Mitte(c)', 'zeige:punkte(c)', 'replay:Alt',
    ]);
    const pin = findAssetCalls(src, 'python').find((c) => c.method === 'pin');
    expect(pin.coords).toEqual({ x: 0.1, y: 0.2, z: 0 });
    const files = { 'main.py': src };
    const rec = renameCodeAssetRefs(files, 'python', 'recording', 'Alt', 'Neu');
    expect(rec.count).toBe(2);
    expect(rec.files['main.py']).toContain('# robot.replay(speed=2, name="Neu")');
    expect(rec.files['main.py']).toContain('\nrobot.replay(speed=2, name="Neu")');
    // Java has no keyword arguments; a comment's first positional literal still counts.
    expect(names('// Robot.replay("A", 2.0);\n// Robot.replay(x, "B");\n', 'java')).toEqual(['replay:A(c)']);
  });

  it('ni1: the run-time scan fetches a keyword-form recording as well', () => {
    expect(collectCodeReplayNames({ 'main.py': 'robot.replay(name="A")\nrobot.replay(speed=2, name=\'B\')\n' }))
      .toEqual(['A', 'B']);
  });

  it('ni2: the receiver needs a Unicode identifier boundary', () => {
    expect(names('Größrobot.replay("W")\nßreplay("X")\nüber.robot.replay("Y")\n')).toEqual([]);
    expect(names('größe = 1\nrobot.replay("W")\n')).toEqual(['replay:W']);
    expect(names('class Gruß {\n  void f() { ÄRobot.replay("W"); }\n}\n', 'java')).toEqual([]);
  });

  it('ni2: the tokenizer’s identifier characters are Unicode — `ür"x"` is a name then a plain string', () => {
    const segs = tokenizeCode('ür"x"', 'python');
    expect(segs.map((s) => [s.type, s.start, s.end, s.prefix ?? null])).toEqual([
      ['code', 0, 2, null], ['string', 2, 5, ''],
    ]);
  });

  it('ni2: annotated and chained assignments are listed; keyword arguments and dict keys are not', () => {
    const files = {
      'main.py': [
        'import robot',
        'x: int = 5',
        'a = b = 1',
        'liste: list[int] = []',
        'robot.pin("A",',
        '          x2=0.1,',
        '          y2=0.2, z2=0.0)',
        'd = {',
        '    "k": 1,',
        '}',
        'else_ = 3',
        '',
      ].join('\n'),
    };
    expect(collectCodeVariables(files, 'python').map((v) => v.name))
      .toEqual(['x', 'a', 'b', 'liste', 'd', 'else_']);
  });
});
