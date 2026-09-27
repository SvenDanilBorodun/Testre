/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Autocomplete for the `robot.*` / `Robot.*` API (each entry's German
// docstring rides as its `info`; there is no hover help on a command already
// in the text — owner decision R3-O3), from
// `robot_api.json` and nothing else (the one generated table). PURE: it builds
// plain completion objects in the shape `@codemirror/autocomplete` consumes
// (`label`, `type`, `detail`, `info`, `apply`); the CodeMirror wiring lives in
// CodeEditor.jsx, the one file that may import the editor packages.

import robotApi from './robot_api.json';

const RECEIVER = Object.freeze({ python: 'robot', java: 'Robot' });

const paramSignature = (params, language) => params
  .map((p) => (language === 'python' && p.required === false && p.default !== undefined
    ? `${p.name}=${JSON.stringify(p.default)}`
    : p.name))
  .join(', ');

/** Every public method as a completion entry for `language`. */
export function robotApiCompletions(language) {
  return robotApi.methods.map((m) => {
    const name = language === 'java' ? m.java_name : m.name;
    const signature = paramSignature(m.params || [], language);
    return {
      label: name,
      type: 'function',
      detail: `(${signature})${m.returns && m.returns !== 'none' ? ` → ${m.returns}` : ''}`,
      info: m.doc_de,
      apply: `${name}(${signature ? '' : ')'}`,
      boost: m.table === 'statement' ? 1 : 0,
    };
  });
}

/**
 * The trigger this source answers to: the text right before the cursor is
 * `robot.` (Python) / `Robot.` (Java) followed by an optional partial name.
 * Returns the match's `from` for the word being completed, or -1.
 */
export function robotApiTriggerFrom(textBeforeCursor, language) {
  const receiver = RECEIVER[language];
  if (!receiver) return -1;
  const m = new RegExp(`${receiver}\\.([A-Za-z_][A-Za-z0-9_]*)?$`).exec(textBeforeCursor);
  if (!m) return -1;
  return textBeforeCursor.length - (m[1] ? m[1].length : 0);
}
