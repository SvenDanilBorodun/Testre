/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Methoden column's data rides LAZY chunks, never the entry bundle (the
// white-screen greps watch the entry, and its size): robot_api_docs.json is
// imported only by methodDocs.js; methodDocs.js only by the two lazy modules
// (the editor's hover, the column); methodGroups.js only by the column; and
// CodeWorkspace.jsx — which IS in the entry — reaches the column only through
// `lazy(() => import('./MethodenPanel'))`.

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';

const SRC = path.resolve(__dirname, '../../../..');
const CODE = path.join('components', 'Workshop', 'code');

function sources(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== '__tests__' && e.name !== 'node_modules') sources(full, out);
    } else if (/\.(js|jsx)$/.test(e.name)) {
      out.push(full);
    }
  }
  return out;
}

const FILES = sources(SRC).map((f) => [path.relative(SRC, f), fs.readFileSync(f, 'utf8')]);
// Every module specifier a file names: a static import / re-export, or a
// dynamic import().
const importersOf = (stem) => FILES
  .filter(([, text]) => new RegExp(`(?:from|import)\\s*\\(?\\s*['"][^'"]*/${stem}['"]`).test(text))
  .map(([rel]) => rel)
  .sort();

describe('the Methoden data stays out of the entry bundle', () => {
  it('scanned the tree', () => {
    expect(FILES.length).toBeGreaterThan(100);
  });

  it('only methodDocs.js imports robot_api_docs.json', () => {
    expect(importersOf('robot_api_docs\\.json')).toEqual([path.join(CODE, 'methodDocs.js')]);
  });

  it('only CodeEditor.jsx and MethodenPanel.jsx import methodDocs', () => {
    expect(importersOf('methodDocs')).toEqual([
      path.join(CODE, 'CodeEditor.jsx'),
      path.join(CODE, 'MethodenPanel.jsx'),
    ]);
  });

  it('only MethodenPanel.jsx imports methodGroups', () => {
    expect(importersOf('methodGroups')).toEqual([path.join(CODE, 'MethodenPanel.jsx')]);
  });

  it('CodeWorkspace reaches MethodenPanel only through React.lazy, and nothing else imports it', () => {
    expect(importersOf('MethodenPanel')).toEqual([path.join(CODE, 'CodeWorkspace.jsx')]);
    const workspace = FILES.find(([rel]) => rel === path.join(CODE, 'CodeWorkspace.jsx'))[1];
    expect(workspace).toMatch(/lazy\(\s*\(\)\s*=>\s*import\(\s*['"]\.\/MethodenPanel['"]\s*\)\s*\)/);
    expect(workspace).not.toMatch(/from\s+['"]\.\/MethodenPanel['"]/);
    // And CodeEditor (CodeMirror) the same way, as before.
    expect(workspace).toMatch(/lazy\(\s*\(\)\s*=>\s*import\(\s*['"]\.\/CodeEditor['"]\s*\)\s*\)/);
  });

  it('the entry-side modules name neither the docs nor the groups', () => {
    for (const [rel, text] of FILES) {
      if ([path.join(CODE, 'methodDocs.js'), path.join(CODE, 'methodGroups.js'),
        path.join(CODE, 'CodeEditor.jsx'), path.join(CODE, 'MethodenPanel.jsx')].includes(rel)) continue;
      expect([rel, /robot_api_docs|methodDocs|methodGroups/.test(text.replace(/\/\/.*$|\/\*[\s\S]*?\*\//gm, ''))])
        .toEqual([rel, false]);
    }
  });
});
