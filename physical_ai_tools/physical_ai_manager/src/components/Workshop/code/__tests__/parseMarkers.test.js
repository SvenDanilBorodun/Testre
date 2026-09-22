/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A16 — the editor marks what its OWN parser could not read, as the student
// types, in both languages. The module under test is PURE: it takes the Lezer
// tree and the CodeMirror `Text` as arguments, so it is tested here against the
// REAL `@codemirror/lang-python` / `@codemirror/lang-java` parsers without an
// EditorView.
//
// The two language packages are loaded with dynamic `import()` on purpose: the
// eslint `no-restricted-imports` rule that keeps CodeMirror out of the entry
// bundle covers every file under src/ except the two lazy editor components,
// and a static import here would be flagged. A test never reaches a bundle, so
// the dynamic form costs nothing and keeps that rule's exclusion list at
// exactly the two shipped files.
//
// THE SHIP LIST IS EMPTY (§3.11's written exit — see the gate block at the
// bottom and docs/KNOWN-ISSUES.md). The mechanism is therefore tested through
// `collectUnparsedRegions` + `diagnosticsForRegions`, and `markersFor` — what
// the editor actually shows — is pinned to nothing for both languages, so
// re-enabling is a deliberate act that touches this file.

import fs from 'node:fs';
import path from 'node:path';
import {
  CODE_LINT_IDLE_MS,
  CODE_LINT_LANGUAGES,
  CODE_LINT_MAX_REGIONS,
  CODE_LINT_SEVERITY,
  CODE_PARSE_NOTICE_DE,
  collectUnparsedRegions,
  diagnosticsForRegions,
  markersFor,
} from '../parseMarkers';

let parsers;
let Text;

beforeAll(async () => {
  const [{ pythonLanguage }, { javaLanguage }, state] = await Promise.all([
    import('@codemirror/lang-python'),
    import('@codemirror/lang-java'),
    import('@codemirror/state'),
  ]);
  parsers = { python: pythonLanguage.parser, java: javaLanguage.parser };
  Text = state.Text;
});

function parse(language, source) {
  const tree = parsers[language].parse(source);
  const doc = Text.of(source.split('\n'));
  return { tree, doc };
}

const regionLines = (doc, r) => [doc.lineAt(r.from).number, doc.lineAt(r.to).number];
const markersOf = (language, source, cursorLine) => {
  const { tree, doc } = parse(language, source);
  return diagnosticsForRegions(collectUnparsedRegions(tree, doc), doc, cursorLine, language);
};

// ── Two realistic 20-line programs with umlauts in strings and comments. ──
const PYTHON_GOOD = [
  'import robot',                          // 1
  '',                                      // 2
  '# Würfel einsammeln und zählen',        // 3
  'anzahl = 0',                            // 4
  'robot.home()',                          // 5
  '',                                      // 6
  'if robot.sees("wuerfel"):',             // 7
  '    robot.grasp("wuerfel")',            // 8
  '    anzahl = anzahl + 1',               // 9
  '',                                      // 10
  'def melde(text):',                      // 11
  '    robot.log(text)',                   // 12
  '',                                      // 13
  'for i in range(3):',                    // 14
  '    melde("Runde " + str(i))',          // 15
  '    robot.wait(0.5)',                   // 16
  '',                                      // 17
  'robot.open_gripper()',                  // 18
  'robot.home()',                          // 19
  'melde("Fertig — schöne Grüße!")',       // 20
];

const JAVA_GOOD = [
  'import edubotics.Robot;',                       // 1
  '',                                              // 2
  'public class Main {',                           // 3
  '    // Würfel einsammeln und zählen',           // 4
  '    public static void main(String[] args) {',  // 5
  '        int anzahl = 0;',                       // 6
  '        Robot.home();',                         // 7
  '        if (Robot.sees("wuerfel")) {',          // 8
  '            Robot.grasp("wuerfel");',           // 9
  '            anzahl = anzahl + 1;',              // 10
  '        }',                                     // 11
  '        melde("Runde " + anzahl);',             // 12
  '        Robot.openGripper();',                  // 13
  '        Robot.home();',                         // 14
  '    }',                                         // 15
  '',                                              // 16
  '    static void melde(String text) {',          // 17
  '        Robot.log(text + " — schöne Grüße!");', // 18
  '    }',                                         // 19
  '}',                                             // 20
];

const withLine = (lines, n, text) => lines.map((l, i) => (i === n - 1 ? text : l)).join('\n');

describe('parseMarkers — a known defect, both languages (the mechanism)', () => {
  test('Python: a missing colon on line 7 yields exactly one region, on line 7', () => {
    const { tree, doc } = parse('python', withLine(PYTHON_GOOD, 7, 'if robot.sees("wuerfel")'));
    const regions = collectUnparsedRegions(tree, doc);
    expect(regions).toHaveLength(1);
    expect(doc.lineAt(regions[0].from).number).toBe(7);
    expect(diagnosticsForRegions(regions, doc, 1, 'python')).toHaveLength(1);
  });

  test('Java: a missing semicolon on a declaration (line 6) yields exactly one region — one line BELOW the omission', () => {
    // Measured 2026-09-21 (@lezer/java 1.1.4): the parser inserts the missing
    // token before the NEXT token it reads, so the zero-length error node
    // sits at the start of line 7, never on line 6. The mark lands where the
    // parser noticed, which for a missing token is by construction the line
    // after the omission. (The spec's "at the defect line" was a PLAUSIBLE
    // claim; for Java it is false and this test pins what is true.)
    const { tree, doc } = parse('java', withLine(JAVA_GOOD, 6, '        int anzahl = 0'));
    const regions = collectUnparsedRegions(tree, doc);
    expect(regions).toHaveLength(1);
    expect(doc.lineAt(regions[0].from).number).toBe(7);
    expect(diagnosticsForRegions(regions, doc, 1, 'java')).toHaveLength(1);
  });

  test('a known good file yields no region in both languages', () => {
    for (const [language, lines] of [['python', PYTHON_GOOD], ['java', JAVA_GOOD]]) {
      const { tree, doc } = parse(language, lines.join('\n'));
      expect(collectUnparsedRegions(tree, doc)).toEqual([]);
    }
  });
});

describe('parseMarkers — the notice is German and never a verdict', () => {
  // The three regexes of .github/scripts/german_detail_lint.py, loaded from the
  // script by path (the repo's precedent: tests/test_feetech_bus.py). There is
  // no `is_german` function in that script; the predicate is the one every
  // German-string test in this repo composes from these three.
  function lintRegexes() {
    const script = fs.readFileSync(
      path.resolve(__dirname, '..', '..', '..', '..', '..', '..', '..',
        '.github', 'scripts', 'german_detail_lint.py'),
      'utf8',
    );
    const pyRegex = (name) => {
      const m = script.match(new RegExp(`^${name} = re\\.compile\\(([\\s\\S]*?)\\)\\n`, 'm'));
      const parts = [...m[1].matchAll(/r"((?:[^"\\]|\\.)*)"/g)].map((x) => x[1]);
      return new RegExp(parts.join(''), 'i');
    };
    return {
      chars: pyRegex('GERMAN_CHARS'),
      words: pyRegex('GERMAN_WORDS'),
      translit: pyRegex('TRANSLITERATIONS'),
    };
  }

  test('every CODE_PARSE_NOTICE_DE passes the lint predicate, says „noch nicht lesen" and never „Fehler"', () => {
    const { chars, words, translit } = lintRegexes();
    expect(Object.keys(CODE_PARSE_NOTICE_DE).sort()).toEqual(['java', 'python']);
    for (const text of Object.values(CODE_PARSE_NOTICE_DE)) {
      expect(chars.test(text) || words.test(text)).toBe(true);
      expect(translit.test(text)).toBe(false);
      expect(text).toContain('noch nicht lesen');
      expect(text).not.toMatch(/Fehler/);
    }
  });

  test('every diagnostic carries one of the notices with severity warning', () => {
    const diags = markersOf('python', withLine(PYTHON_GOOD, 7, 'if robot.sees("wuerfel")'), 1);
    expect(diags.length).toBeGreaterThan(0);
    for (const d of diags) {
      expect(Object.values(CODE_PARSE_NOTICE_DE)).toContain(d.message);
      expect(d.severity).toBe('warning');
      expect(d.severity).toBe(CODE_LINT_SEVERITY);
      expect(d.source).toBe('parse');
      expect(d.markClass).toBe('cm-edubotics-unparsed');
      expect(d.to).toBeGreaterThanOrEqual(d.from);
    }
  });

  test('the constants are plain values, not knobs', () => {
    expect(CODE_LINT_IDLE_MS).toBe(750);
    expect(CODE_LINT_MAX_REGIONS).toBe(3);
    expect(CODE_LINT_SEVERITY).toBe('warning');
    expect(Object.isFrozen(CODE_LINT_LANGUAGES)).toBe(true);
    expect(Object.isFrozen(CODE_PARSE_NOTICE_DE)).toBe(true);
  });
});

describe('parseMarkers — the cursor-line rule and the cap (the mechanism)', () => {
  const BAD = withLine(PYTHON_GOOD, 7, 'if robot.sees("wuerfel")');

  test('a region on the cursor’s line is never returned; leaving the line reveals it', () => {
    const { tree, doc } = parse('python', BAD);
    const regions = collectUnparsedRegions(tree, doc);
    expect(regions).toHaveLength(1);
    const [fromLine, toLine] = regionLines(doc, regions[0]);
    for (let line = fromLine; line <= toLine; line += 1) {
      expect(diagnosticsForRegions(regions, doc, line, 'python')).toEqual([]);
    }
    expect(diagnosticsForRegions(regions, doc, 1, 'python')).toHaveLength(1);
    expect(diagnosticsForRegions(regions, doc, 20, 'python')).toHaveLength(1);
  });

  test('at most CODE_LINT_MAX_REGIONS regions are shown, the first by position', () => {
    // Five independent defects on five lines far apart.
    const src = PYTHON_GOOD.map((l, i) => ([4, 8, 12, 16, 19].includes(i + 1) ? `${l})` : l));
    const { tree, doc } = parse('python', src.join('\n'));
    const regions = collectUnparsedRegions(tree, doc);
    expect(regions.length).toBeGreaterThan(CODE_LINT_MAX_REGIONS);
    const diags = diagnosticsForRegions(regions, doc, 1, 'python');
    expect(diags).toHaveLength(CODE_LINT_MAX_REGIONS);
    expect(diags.map((d) => d.from)).toEqual(regions.slice(0, CODE_LINT_MAX_REGIONS).map((r) => r.from));
  });

  test('a language with no notice gets no diagnostics', () => {
    const { tree, doc } = parse('python', BAD);
    expect(diagnosticsForRegions(collectUnparsedRegions(tree, doc), doc, 1, 'brainfuck')).toEqual([]);
  });
});

describe('parseMarkers — what the editor SHOWS (the ship list)', () => {
  test('CODE_LINT_LANGUAGES is empty: the §3.11 exit, recorded in docs/KNOWN-ISSUES.md', () => {
    // Widening this list is an owner decision (the parsers cascade on unclosed
    // delimiters and on a missing `;` after a Java call — the gate below). A
    // widened list must flip the matching `test.fails` below to a plain test.
    expect([...CODE_LINT_LANGUAGES]).toEqual([]);
  });

  test('markersFor answers nothing for both languages while the list is empty', () => {
    for (const [language, lines, k, text] of [
      ['python', PYTHON_GOOD, 7, 'if robot.sees("wuerfel")'],
      ['java', JAVA_GOOD, 6, '        int anzahl = 0'],
    ]) {
      const { tree, doc } = parse(language, withLine(lines, k, text));
      expect(collectUnparsedRegions(tree, doc).length).toBeGreaterThan(0);
      expect(markersFor(tree, doc, 1, language)).toEqual([]);
    }
  });
});

// ── THE GATE (§3.11): one defect must not light more than lines k and k+1. ──
// Measured 2026-09-21 against @lezer/python 1.1.19 / @lezer/java 1.1.4: BOTH
// parsers cascade on part of the corpus (the raw error nodes are the parser's,
// not the region merge's — see docs/KNOWN-ISSUES.md for the dump). The written
// exit applies: CODE_LINT_LANGUAGES narrowed (to nothing), the corpus recorded,
// and this gate kept as an EXPECTED FAILURE naming it. `test.fails` is vitest's
// xfail: it passes while the gate fails and goes red the day a parser release
// makes the corpus pass — the signal to re-enable deliberately.
const PYTHON_DEFECTS = [
  [7, 'if robot.sees("wuerfel")', 'missing colon on if'],
  [11, 'def melde(text)', 'missing colon on def'],
  [14, 'for i in range(3)', 'missing colon on for'],
  [8, '    robot.grasp("wuerfel"', 'unclosed ('],
  [4, 'anzahl = [0', 'unclosed ['],
  [4, 'anzahl = {', 'unclosed {'],
  [15, '    melde("Runde  + str(i))', 'unclosed string'],
  [18, 'robot.open_gripper())', 'stray )'],
  [9, '   anzahl = anzahl + 1', 'bad dedent'],
  [5, 'robot.home()]', 'stray ]'],
  [12, '    robot.log(text text)', 'missing operator'],
  [16, '    robot.wait(0.5 0.5)', 'missing comma'],
];

const JAVA_DEFECTS = [
  [7, '        Robot.home()', 'missing semicolon after a call'],
  [6, '        int anzahl = 0', 'missing semicolon on a declaration'],
  [13, '        Robot.openGripper()', 'missing semicolon late'],
  [9, '            Robot.grasp("wuerfel";', 'unclosed ('],
  [8, '        if (Robot.sees("wuerfel") {', 'missing ) in if ('],
  [12, '        melde("Runde  + anzahl);', 'unclosed string'],
  [14, '        Robot.home());', 'stray )'],
  [6, '        int[] anzahl = {0;', 'unclosed {'],
  [6, '        int[] anzahl = new int[3;', 'unclosed ['],
  [10, '            anzahl = anzahl + ;', 'missing operand'],
  [12, '        melde("Runde " anzahl);', 'missing operator'],
  [17, '    static void melde(String text {', 'missing ) in a signature'],
];

// What was measured: the classes that cascade (lines beyond k+1) and the one
// the Python parser does not see at all. Pinned so a parser bump that changes
// the picture is noticed here, not on a student's screen.
const CASCADING = {
  python: ['unclosed (', 'unclosed [', 'unclosed {', 'unclosed string'],
  java: ['missing semicolon after a call', 'missing operator'],
};
const UNSEEN = { python: ['bad dedent'], java: [] };

function litLines(language, good, k, replacement) {
  const { tree, doc } = parse(language, withLine(good, k, replacement));
  const lines = new Set();
  for (const r of collectUnparsedRegions(tree, doc)) {
    const [a, b] = regionLines(doc, r);
    for (let n = a; n <= b; n += 1) lines.add(n);
  }
  return [...lines].sort((x, y) => x - y);
}

describe.each([
  ['python', PYTHON_GOOD, PYTHON_DEFECTS],
  ['java', JAVA_GOOD, JAVA_DEFECTS],
])('parseMarkers — %s noise gate', (language, good, defects) => {
  test('the corpus has twelve entries, each a one-line mutation of the good file', () => {
    expect(defects).toHaveLength(12);
    const { tree, doc } = parse(language, good.join('\n'));
    expect(collectUnparsedRegions(tree, doc)).toEqual([]);
  });

  test.fails('GATE (expected failure until the parser resyncs sooner): a single defect marks at most lines k and k+1', () => {
    const violations = [];
    for (const [k, replacement, label] of defects) {
      const lines = litLines(language, good, k, replacement);
      if (lines.length === 0) violations.push(`${label}: not seen`);
      else if (lines.some((n) => n !== k && n !== k + 1)) violations.push(`${label}: lines ${lines.join(',')} (k=${k})`);
    }
    expect(violations).toEqual([]);
  });

  test('the measured picture: exactly these classes cascade or go unseen, every other class holds', () => {
    const cascading = [];
    const unseen = [];
    for (const [k, replacement, label] of defects) {
      const lines = litLines(language, good, k, replacement);
      if (lines.length === 0) unseen.push(label);
      else if (lines.some((n) => n !== k && n !== k + 1)) cascading.push(label);
    }
    expect(cascading).toEqual(CASCADING[language]);
    expect(unseen).toEqual(UNSEEN[language]);
  });
});

describe('parseMarkers — source-level fences', () => {
  const src = fs.readFileSync(path.resolve(__dirname, '..', 'parseMarkers.js'), 'utf8');

  test('imports nothing from codemirror, @codemirror/*, @lezer/* or robot_api.json', () => {
    expect(src).not.toMatch(/from\s+['"](codemirror|@codemirror\/|@lezer\/)/);
    expect(src).not.toMatch(/robot_api/);
    expect(src).not.toMatch(/ROBOT_API/);
    expect(src).not.toMatch(/import\(/);
  });

  test('the four constants are plain module constants, never env reads', () => {
    for (const name of ['CODE_LINT_IDLE_MS', 'CODE_LINT_MAX_REGIONS', 'CODE_LINT_SEVERITY', 'CODE_PARSE_NOTICE_DE']) {
      expect(src).toMatch(new RegExp(`^export const ${name} = `, 'm'));
    }
    expect(src).not.toMatch(/process\.env/);
    expect(src).not.toMatch(/EDUBOTICS_/);
  });
});
