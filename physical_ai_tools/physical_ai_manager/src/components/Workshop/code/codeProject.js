/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code DOCUMENT: two languages, one entry file each, a `{ path: content }
// }` project, and the caps the server (`workflow/code_program.py`) and the
// cloud (`validators/workflow.py`) both refuse on. The numbers are read from
// `robot_api.json.limits`, which is generated from the one Python table, so the
// editor never carries a copy that can drift. PURE: no CodeMirror, no React —
// it rides the entry bundle (RunControls + WorkshopPage import it).

import robotApi from './robot_api.json';
import { CODE_DE, formatCode } from './codeMessagesDe';

export const CODE_LANGUAGES = Object.freeze(['python', 'java']);

export const ENTRY_FILE = Object.freeze({ python: 'main.py', java: 'Main.java' });
const EXT_FOR = Object.freeze({ python: '.py', java: '.java' });
// The shipped stub's stems: a student file named like them would shadow the
// `robot` module / the reserved `edubotics*` namespace on the runner's path.
const RESERVED_STEMS = Object.freeze(['robot']);
const RESERVED_PREFIX = 'edubotics';

export const CODE_LIMITS = Object.freeze({
  MAX_CODE_FILES: robotApi.limits.MAX_CODE_FILES,
  MAX_CODE_FILE_BYTES: robotApi.limits.MAX_CODE_FILE_BYTES,
  MAX_CODE_PROJECT_BYTES: robotApi.limits.MAX_CODE_PROJECT_BYTES,
  CODE_PATH_RE: new RegExp(robotApi.limits.CODE_PATH_RE),
});

// What a new project opens with. German comments, the stub imported exactly
// the way the runner ships it (`import robot` / `edubotics.Robot`).
export const STARTER_FILES = Object.freeze({
  python: Object.freeze({
    'main.py': [
      '# Dein Python-Programm für den Roboter.',
      '# Alles, was der Roboter kann, steckt im Modul „robot“ — tippe robot. für die Liste.',
      'import robot',
      '',
      'robot.home()',
      'robot.log("Hallo Roboter!")',
      '',
    ].join('\n'),
  }),
  java: Object.freeze({
    'Main.java': [
      '// Dein Java-Programm für den Roboter.',
      '// Alles, was der Roboter kann, steckt in der Klasse Robot — tippe Robot. für die Liste.',
      'import edubotics.Robot;',
      '',
      'public class Main {',
      '    public static void main(String[] args) {',
      '        Robot.home();',
      '        Robot.log("Hallo Roboter!");',
      '    }',
      '}',
      '',
    ].join('\n'),
  }),
});

export function isCodeLanguage(value) {
  return typeof value === 'string' && CODE_LANGUAGES.includes(value);
}

const utf8Bytes = (s) => new TextEncoder().encode(s).length;

/**
 * The project's size as the SERVER measures it: the UTF-8 bytes of Python's
 * `json.dumps(files, ensure_ascii=False)`. Python's default separators are
 * `', '` and `': '`, one byte more each than `JSON.stringify`'s, so the JS
 * figure is corrected by `2N - 1` for N files (measured equal on control
 * characters, U+2028, umlauts and an empty file — codeProject.test.js).
 */
export function projectBytes(files) {
  const n = Object.keys(files || {}).length;
  return utf8Bytes(JSON.stringify(files || {})) + (n > 0 ? 2 * n - 1 : 0);
}

/**
 * The German refusal for a path under `language`, or null when it is fine:
 * CODE_PATH_RE, the reserved stems, the language's extension.
 */
export function validateProjectPath(path, language) {
  if (typeof path !== 'string' || !CODE_LIMITS.CODE_PATH_RE.test(path)) {
    return formatCode(CODE_DE.ERR_BAD_PATH, path);
  }
  const stem = path.split('/').pop().replace(/\.[^.]+$/, '');
  if (RESERVED_STEMS.includes(stem) || stem.toLowerCase().startsWith(RESERVED_PREFIX)) {
    return formatCode(CODE_DE.ERR_RESERVED, path);
  }
  if (!path.endsWith(EXT_FOR[language])) {
    return formatCode(CODE_DE.ERR_WRONG_EXT, path, language);
  }
  return null;
}

/**
 * The German refusal for a whole project, or null: the same ladder as
 * `CodeProgram.from_payload` — files present, count, each path, each file's
 * UTF-8 bytes, the entry file, the project bytes.
 */
export function validateProject(files, language) {
  if (!files || typeof files !== 'object' || Array.isArray(files)
      || Object.keys(files).length === 0) {
    return CODE_DE.ERR_NO_FILES;
  }
  const paths = Object.keys(files);
  if (paths.length > CODE_LIMITS.MAX_CODE_FILES) {
    return formatCode(CODE_DE.ERR_TOO_MANY_FILES, CODE_LIMITS.MAX_CODE_FILES);
  }
  for (const path of paths) {
    if (typeof files[path] !== 'string') return formatCode(CODE_DE.ERR_BAD_PATH, path);
    const pathError = validateProjectPath(path, language);
    if (pathError) return pathError;
    if (utf8Bytes(files[path]) > CODE_LIMITS.MAX_CODE_FILE_BYTES) {
      return formatCode(CODE_DE.ERR_FILE_TOO_BIG, path, CODE_LIMITS.MAX_CODE_FILE_BYTES / 1024);
    }
  }
  const entry = ENTRY_FILE[language];
  if (!Object.prototype.hasOwnProperty.call(files, entry)) {
    return formatCode(CODE_DE.ERR_MISSING_ENTRY, entry);
  }
  if (projectBytes(files) > CODE_LIMITS.MAX_CODE_PROJECT_BYTES) {
    return formatCode(CODE_DE.ERR_PROJECT_TOO_BIG, CODE_LIMITS.MAX_CODE_PROJECT_BYTES / 1024);
  }
  return null;
}

/**
 * Fail CLOSED (the `previewLeaderGate` discipline): a code run needs the robot
 * to have SAID it runs this language. `null` / `{}` / a manifest without
 * `code_languages` is unknown, and unknown is not permission — an old image
 * would otherwise receive a program it cannot run.
 */
export function codeRunBlockReason(caps, language) {
  const list = caps && typeof caps === 'object' ? caps.code_languages : null;
  if (!Array.isArray(list) || !list.includes(language)) return 'unsupported';
  return null;
}

export const CODE_RUN_BLOCK_TITLES_DE = Object.freeze({
  unsupported: CODE_DE.RUN_UNSUPPORTED,
});

// The poison block: an old server's `Interpreter.from_json` reads only
// `blocks` and raises „Unbekannter Block-Typ: edubotics_code_program_v1" —
// loud, in German, never a silent green run. The new server routes on
// `language` before the interpreter ever sees it.
export const CODE_POISON_BLOCK_TYPE = 'edubotics_code_program_v1';

// ── which recordings a code run has to carry ──────────────────────────────
// A Blockly run collects them from the block tree (`collectReplayNames`); a
// code program names them in TEXT, so they are scanned out of the files. The
// scan is deliberately LOOSE — over-collecting costs one fetch that comes back
// empty and is then skipped, while under-collecting leaves `robot.replay` with
// no data and the run aborts on the server's „Unbekannte Aufnahme: …". Both
// call shapes a student writes are matched: `robot.replay("X")` /
// `Robot.replay("X", 1.5)` and a bare `replay("X")` after
// `from robot import replay`.
const REPLAY_CALL_RE = /\breplay\s*\(\s*(["'])([^"'\\\r\n]*)\1/g;
// The name rule is the one the generated table already carries for this
// method's first parameter — never a second copy of the pattern.
const replayNameParam = (robotApi.methods || [])
  .find((m) => m && m.name === 'replay')?.params?.[0] || {};
const REPLAY_NAME_RE = new RegExp(replayNameParam.pattern);
// `workflow_trajectories` is pruned to 16 rows per workflow (migration 034),
// so a 17th DISTINCT name cannot name a recording that exists — and an
// unbounded list would turn one pathological file into that many cloud reads.
export const MAX_CODE_REPLAY_NAMES = 16;

/**
 * Every recording name a code project's `replay(...)` calls reference, trimmed,
 * de-duplicated, in first-seen order, capped at `MAX_CODE_REPLAY_NAMES`. Pure;
 * never throws on a malformed project.
 *
 * @param {object|null} files - `{ path: content }`.
 * @returns {string[]}
 */
export function collectCodeReplayNames(files) {
  if (!files || typeof files !== 'object' || Array.isArray(files)) return [];
  const seen = new Set();
  const order = [];
  for (const content of Object.values(files)) {
    if (typeof content !== 'string') continue;
    REPLAY_CALL_RE.lastIndex = 0;
    let match = REPLAY_CALL_RE.exec(content);
    while (match !== null) {
      const name = match[2].trim();
      if (name && REPLAY_NAME_RE.test(name) && !seen.has(name)) {
        seen.add(name);
        order.push(name);
      }
      match = REPLAY_CALL_RE.exec(content);
    }
    if (order.length >= MAX_CODE_REPLAY_NAMES) break;
  }
  return order.slice(0, MAX_CODE_REPLAY_NAMES);
}

export function codeRunPayloadBase(language, files) {
  return {
    blocks: { blocks: [{ type: CODE_POISON_BLOCK_TYPE, id: 'code' }] },
    variables: [],
    language,
    files,
  };
}
