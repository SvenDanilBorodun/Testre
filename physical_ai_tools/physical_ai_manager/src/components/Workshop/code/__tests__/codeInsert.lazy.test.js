/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review round 2 (ni4): codeInsert.js — the insertion's structure analysis,
// with a Java statement parser — is not in the entry bundle. Only the lazy
// editor (CodeEditor.jsx, behind React.lazy) imports it statically; the code
// asset document loads it on demand; the drawer takes the drag type from its
// own tiny module. A failed load is not cached.

import fs from 'fs';
import path from 'path';
import {
  describe, it, expect, vi, afterEach,
} from 'vitest';

const SRC = path.resolve(__dirname, '../../../..');

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

describe('codeInsert.js rides the lazy chunks, not the entry bundle', () => {
  const files = sources(SRC).map((f) => [path.relative(SRC, f), fs.readFileSync(f, 'utf8')]);

  it('only CodeEditor.jsx imports it statically', () => {
    const importers = files
      .filter(([, text]) => /(?:from|import)\s+['"][^'"]*\/codeInsert['"]/.test(text))
      .map(([rel]) => rel);
    expect(importers).toEqual([path.join('components', 'Workshop', 'code', 'CodeEditor.jsx')]);
  });

  it('the code asset document loads it with a dynamic import, and nothing else does', () => {
    const dynamic = files
      .filter(([, text]) => /import\(\s*['"][^'"]*\/codeInsert['"]\s*\)/.test(text))
      .map(([rel]) => rel);
    expect(dynamic).toEqual([path.join('components', 'Workshop', 'code', 'codeAssetDocument.js')]);
  });

  it('the drag type comes from snippetMime.js, which imports nothing', () => {
    const mime = files.find(([rel]) => rel.endsWith(path.join('code', 'snippetMime.js')))[1];
    expect(mime).not.toMatch(/\bimport\b/);
    const drawer = files.find(([rel]) => rel.endsWith(path.join('sammlung', 'SammlungDrawer.jsx')))[1];
    expect(drawer).toMatch(/from\s+['"]\.\.\/code\/snippetMime['"]/);
  });
});

describe('loadCodeInsert', () => {
  afterEach(() => {
    vi.doUnmock('../codeInsert');
    vi.resetModules();
  });

  it('a failed load is not cached: the next insertion loads again', async () => {
    vi.resetModules();
    const state = { fail: true };
    vi.doMock('../codeInsert', async (importOriginal) => {
      if (state.fail) throw new Error('chunk nicht geladen');
      return importOriginal();
    });
    const { loadCodeInsert } = await import('../codeAssetDocument');
    // vitest wraps a throwing mock factory in its own error.
    await expect(loadCodeInsert()).rejects.toThrow();
    state.fail = false;
    vi.resetModules();
    const mod = await loadCodeInsert();
    expect(typeof mod.insertAtTarget).toBe('function');
    expect(await loadCodeInsert()).toBe(mod);
  });
});
