/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code document's caps, judged in the editor BEFORE a save or a run, with
// the same numbers the server (`code_program.py`) and the cloud validator
// (`validators/workflow.py`) refuse on — all three read `robot_api.json.limits`
// or its Python source. A refusal here is a German sentence the student can act
// on; a refusal there is the same sentence after a round-trip.

import fs from 'node:fs';
import path from 'node:path';
import robotApi from '../robot_api.json';
import {
  CODE_LANGUAGES,
  CODE_LIMITS,
  ENTRY_FILE,
  STARTER_FILES,
  codeRunBlockReason,
  codeRunPayloadBase,
  isCodeLanguage,
  projectBytes,
  validateProject,
  validateProjectPath,
} from '../codeProject';

const KIB = 1024;

describe('codeProject — the caps come from robot_api.json, not from a copy', () => {
  test('the four limits are the JSON limits', () => {
    expect(CODE_LIMITS.MAX_CODE_FILES).toBe(robotApi.limits.MAX_CODE_FILES);
    expect(CODE_LIMITS.MAX_CODE_FILE_BYTES).toBe(robotApi.limits.MAX_CODE_FILE_BYTES);
    expect(CODE_LIMITS.MAX_CODE_PROJECT_BYTES).toBe(robotApi.limits.MAX_CODE_PROJECT_BYTES);
    // `.source` re-escapes `/` — compare against a RegExp built from the JSON.
    expect(CODE_LIMITS.CODE_PATH_RE.source).toBe(new RegExp(robotApi.limits.CODE_PATH_RE).source);
    // The numbers themselves, so a regenerated JSON that moved them fails here
    // rather than silently re-deriving every assertion below.
    expect(CODE_LIMITS.MAX_CODE_FILES).toBe(32);
    expect(CODE_LIMITS.MAX_CODE_FILE_BYTES).toBe(64 * KIB);
    expect(CODE_LIMITS.MAX_CODE_PROJECT_BYTES).toBe(128 * KIB);
  });

  test('the two languages and their entry files are the server’s', () => {
    expect([...CODE_LANGUAGES]).toEqual(['python', 'java']);
    expect(ENTRY_FILE).toEqual({ python: 'main.py', java: 'Main.java' });
    expect(isCodeLanguage('python')).toBe(true);
    expect(isCodeLanguage('java')).toBe(true);
    expect(isCodeLanguage('')).toBe(false);
    expect(isCodeLanguage('blocks')).toBe(false);
    expect(isCodeLanguage(null)).toBe(false);
  });

  test('every starter project passes its own validation and names the entry file', () => {
    for (const language of CODE_LANGUAGES) {
      const files = STARTER_FILES[language];
      expect(Object.keys(files)).toEqual([ENTRY_FILE[language]]);
      expect(validateProject(files, language)).toBeNull();
      // The starter talks to the robot the way the shipped stub is imported.
      expect(files[ENTRY_FILE[language]]).toMatch(language === 'python'
        ? /^import robot$/m
        : /^import edubotics\.Robot;$/m);
    }
  });
});

describe('projectBytes — measured the way the server measures', () => {
  test('equals Python’s json.dumps(files, ensure_ascii=False) byte count', () => {
    // Three cases measured with CPython 3.14 (scratch probe, 2026-09-21):
    // json.dumps' DEFAULT separators are ', ' and ': ', two bytes more per
    // file than JSON.stringify's ',' and ':' — hence the 2N-1 correction.
    expect(projectBytes({ 'main.py': 'x\nü "q" \\ \t €' })).toBe(36);
    expect(projectBytes({ 'main.py': 'a', 'a/b.py': 'ß\u2028z', 'c.py': '' })).toBe(48);
    expect(projectBytes({ 'm.py': '\x01\x7f\u00a0' })).toBe(21);
    expect(projectBytes({})).toBe(2);
  });
});

describe('validateProjectPath — CODE_PATH_RE plus the reserved stems', () => {
  test.each([
    ['main.py', 'python', null],
    ['hilfe/mathe.py', 'python', null],
    ['a/b/c/d.py', 'python', null],
    ['Main.java', 'java', null],
    ['edubotics/Helfer.java', 'java', null],
  ])('%s under %s is accepted', (p, language, want) => {
    expect(validateProjectPath(p, language)).toBe(want);
  });

  test.each([
    ['../main.py', 'python'],
    ['/main.py', 'python'],
    ['a/b/c/d/e.py', 'python'],
    ['1start.py', 'python'],
    ['mein programm.py', 'python'],
    ['äh.py', 'python'],
    ['main.txt', 'python'],
    ['main', 'python'],
    ['x'.repeat(41) + '.py', 'python'],
  ])('%s is refused in German as not allowed', (p, language) => {
    const msg = validateProjectPath(p, language);
    expect(msg).toContain('nicht erlaubt');
    expect(msg).toContain(p);
  });

  test('the shipped stub’s stems are reserved on both languages', () => {
    expect(validateProjectPath('robot.py', 'python')).toContain('reserviert');
    expect(validateProjectPath('hilfe/robot.py', 'python')).toContain('reserviert');
    expect(validateProjectPath('edubotics_debug.py', 'python')).toContain('reserviert');
    expect(validateProjectPath('EduboticsHelfer.java', 'java')).toContain('reserviert');
  });

  test('a file of the other language is refused by extension', () => {
    expect(validateProjectPath('Main.java', 'python')).toContain('passt nicht zur Sprache');
    expect(validateProjectPath('main.py', 'java')).toContain('passt nicht zur Sprache');
  });
});

describe('validateProject — the three caps and the entry file', () => {
  const py = (files) => validateProject(files, 'python');

  test('a healthy project is null', () => {
    expect(py({ 'main.py': 'import robot\nrobot.home()\n', 'hilfe.py': 'x = 1\n' })).toBeNull();
  });

  test('no files / a missing entry file', () => {
    expect(py({})).toContain('keine Dateien');
    expect(py(null)).toContain('keine Dateien');
    expect(py({ 'hilfe.py': 'x = 1\n' })).toContain('main.py');
    expect(validateProject({ 'Helfer.java': 'class Helfer {}' }, 'java')).toContain('Main.java');
  });

  test('33 files is one too many', () => {
    const files = { 'main.py': '' };
    for (let i = 0; i < 32; i += 1) files[`f${i}.py`] = '';
    expect(Object.keys(files)).toHaveLength(33);
    expect(py(files)).toContain('zu viele Dateien');
    expect(py(files)).toContain('32');
  });

  test('a 64 KiB + 1 file is too big, 64 KiB exactly is not', () => {
    expect(py({ 'main.py': 'a'.repeat(64 * KIB) })).toBeNull();
    const msg = py({ 'main.py': 'a'.repeat(64 * KIB + 1) });
    expect(msg).toContain('zu groß');
    expect(msg).toContain('main.py');
    expect(msg).toContain('64');
  });

  test('bytes are UTF-8 bytes, not characters', () => {
    // 'ü' is two bytes: 32 768 of them already exceed the 64 KiB file cap.
    expect(py({ 'main.py': 'ü'.repeat(32 * KIB + 1) })).toContain('zu groß');
  });

  test('a 128 KiB + 1 project is too big even when every file fits', () => {
    const files = { 'main.py': 'a'.repeat(60 * KIB) };
    files['b.py'] = 'a'.repeat(60 * KIB);
    files['c.py'] = 'a'.repeat(8 * KIB);
    // Measured: three 60/60/8 KiB files serialise past the 128 KiB cap.
    expect(projectBytes(files)).toBeGreaterThan(128 * KIB);
    const msg = py(files);
    expect(msg).toContain('insgesamt zu groß');
    expect(msg).toContain('128');
  });

  test('a bad path or a wrong extension is refused with the path sentence', () => {
    expect(py({ 'main.py': '', '../x.py': '' })).toContain('nicht erlaubt');
    expect(py({ 'main.py': '', 'X.java': '' })).toContain('passt nicht zur Sprache');
    expect(py({ 'main.py': '', 'robot.py': '' })).toContain('reserviert');
  });

  test('a non-string content is refused, never counted', () => {
    expect(py({ 'main.py': 42 })).toContain('nicht erlaubt');
  });
});

describe('codeRunBlockReason — fail CLOSED on an unknown robot', () => {
  const full = { code_languages: ['python', 'java'] };
  test('unknown is not permission', () => {
    expect(codeRunBlockReason(null, 'python')).toBe('unsupported');
    expect(codeRunBlockReason(undefined, 'python')).toBe('unsupported');
    expect(codeRunBlockReason({}, 'python')).toBe('unsupported');
    expect(codeRunBlockReason({ code_languages: 'python' }, 'python')).toBe('unsupported');
  });
  test('a manifest that lacks the language refuses that language only', () => {
    expect(codeRunBlockReason({ code_languages: ['python'] }, 'java')).toBe('unsupported');
    expect(codeRunBlockReason({ code_languages: ['python'] }, 'python')).toBeNull();
  });
  test('a full manifest permits both', () => {
    expect(codeRunBlockReason(full, 'python')).toBeNull();
    expect(codeRunBlockReason(full, 'java')).toBeNull();
  });
});

describe('codeRunPayloadBase — the poison block an old image trips over', () => {
  test('carries exactly the poison block, no variables, the language and the files', () => {
    const files = { 'main.py': 'import robot\n' };
    const base = codeRunPayloadBase('python', files);
    expect(Object.keys(base).sort()).toEqual(['blocks', 'files', 'language', 'variables']);
    expect(base.blocks).toEqual({ blocks: [{ type: 'edubotics_code_program_v1', id: 'code' }] });
    expect(base.variables).toEqual([]);
    expect(base.language).toBe('python');
    expect(base.files).toBe(files);
  });
});

describe('the module is pure and reads only robot_api.json', () => {
  test('imports nothing from codemirror', () => {
    const src = fs.readFileSync(path.resolve(__dirname, '..', 'codeProject.js'), 'utf8');
    expect(src).not.toMatch(/from ['"](codemirror|@codemirror\/|@lezer\/)/);
    expect(src).not.toMatch(/import\(/);
  });
});
