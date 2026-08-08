// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// THE CLIPBOARD LEAK, and the one class of key this repo's storage registry is
// structurally blind to.
//
// `@mit-app-inventor/blockly-plugin-workspace-multiselect` implements
// cross-tab copy/paste by writing the copied blocks — the student's PROGRAM, in
// plain text — into localStorage, and reading them back on Ctrl+V. Three keys:
// `blocklyStashMulti`, `blocklyStashConnection`, `blocklyStashTime`.
//
// Student A copies blocks in Roboter Studio and walks away; student B gets a
// freshly spawned, correctly scrubbed window; B presses Ctrl+V and pastes A's
// program. The `rmtree` this repo replaced with a targeted scrub DELETED these
// keys, so leaving them behind was a REGRESSION of that change, not an
// inherited residual.
//
// WHY NO EXISTING GUARD COULD HAVE CAUGHT IT — three independent reasons, and
// the fix has to answer all three:
//   1. the writer is a DEPENDENCY. `sessionScope.test.js`'s coverage scan walks
//      `src/` and skips `node_modules`.
//   2. the keys do not begin with `edubotics`, and that scan's `isEdubotics`
//      filter judges only keys that do.
//   3. the feature is ON BY DEFAULT, so nothing in our source mentions it.
//
// This file is therefore the one test in the suite that reads NODE_MODULES. It
// is deliberate: the invariant being protected is a fact about the DEPENDENCY
// ("it still writes these three names, and cross-tab is still on"), and a
// dependency bump that renames a key or changes the default would otherwise
// reopen the leak in total silence.

import fs from 'node:fs';
import path from 'node:path';
import { STUDENT_SCOPED_KEYS, clearStudentScopedStorage } from '../sessionScope';
import { bootScrubOnce, BOOT_SCRUB_PARAM } from '../bootScrub';

const PKG_ROOT = path.resolve(__dirname, '..', '..', '..');
const PLUGIN_DIST = path.join(
  PKG_ROOT, 'node_modules', '@mit-app-inventor',
  'blockly-plugin-workspace-multiselect', 'dist', 'index.js'
);

const STASH_KEYS = ['blocklyStashMulti', 'blocklyStashConnection', 'blocklyStashTime'];

function withSearch(search, fn) {
  const original = Object.getOwnPropertyDescriptor(window, 'location');
  delete window.location;
  window.location = { ...original?.value, search };
  try {
    return fn();
  } finally {
    if (original) Object.defineProperty(window, 'location', original);
  }
}

describe('the Blockly cross-tab clipboard is student data and is scrubbed', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it('a fresh spawn clears the previous student’s copied blocks', () => {
    STASH_KEYS.forEach((k) => localStorage.setItem(k, '[{"student":"A"}]'));
    expect(withSearch(`?${BOOT_SCRUB_PARAM}=n1`, bootScrubOnce)).toBe('scrubbed');
    for (const k of STASH_KEYS) {
      expect(localStorage.getItem(k)).toBeNull();
    }
  });

  it('signing out clears them too', () => {
    // The same keys reached through the other entry point — they must be in
    // STUDENT_SCOPED_KEYS itself, not special-cased inside the boot scrub.
    STASH_KEYS.forEach((k) => localStorage.setItem(k, 'A'));
    clearStudentScopedStorage();
    for (const k of STASH_KEYS) {
      expect(localStorage.getItem(k)).toBeNull();
    }
  });

  it('lists them as student-scoped, not machine-scoped', () => {
    for (const k of STASH_KEYS) {
      expect(STUDENT_SCOPED_KEYS).toContain(k);
    }
  });
});

describe('the DEPENDENCY still behaves the way those keys assume', () => {
  // A bump that renames a key or flips the default would reopen the leak with
  // every test above still green, because they only ever exercise names WE
  // chose. These read the shipped plugin instead.
  const dist = fs.existsSync(PLUGIN_DIST)
    ? fs.readFileSync(PLUGIN_DIST, 'utf8')
    : null;

  it('the plugin is actually installed where this test looks', () => {
    // Zero-file floor: a moved or renamed dist would make everything below pass
    // having read nothing.
    if (!dist) {
      throw new Error(`multiselect plugin dist not found at ${PLUGIN_DIST}`);
    }
    expect(dist.length).toBeGreaterThan(1000);
  });

  it('still writes exactly the three key names we scrub', () => {
    for (const k of STASH_KEYS) {
      if (!dist.includes(k)) {
        throw new Error(
          `the multiselect plugin no longer writes '${k}'. Either it was `
          + `renamed — in which case STUDENT_SCOPED_KEYS is now scrubbing a `
          + `dead name while the live one leaks — or cross-tab copy/paste was `
          + `removed. Check dataCopyToStorage and update both.`
        );
      }
    }
  });

  it('still writes them to localStorage specifically', () => {
    // If it ever moved to IndexedDB or cookies, clearStudentScopedStorage — a
    // localStorage loop — would silently stop reaching them.
    expect(dist).toMatch(/localStorage\.setItem\(\s*["']blocklyStashMulti["']/);
  });

  it('still defaults cross-tab copy/paste to ON, which is why this matters', () => {
    // `useCopyPasteCrossTab_ = true` in the constructor, disabled only by an
    // explicit `multiselectCopyPaste.crossTab === false`. Our call site passes
    // `{}`, so the disable branch is unreachable and the feature is live.
    expect(dist).toMatch(/useCopyPasteCrossTab_\s*=\s*!0|useCopyPasteCrossTab_\s*=\s*true/);
    expect(dist).toMatch(/multiselectCopyPaste/);
  });

  it('our call site really does leave the default in place', () => {
    const ws = fs.readFileSync(
      path.join(PKG_ROOT, 'src', 'components', 'Workshop', 'BlocklyWorkspace.jsx'),
      'utf8'
    );
    expect(ws).toContain('blockly-plugin-workspace-multiselect');
    // If this ever becomes `ms.init({ multiselectCopyPaste: { crossTab: false } })`
    // the keys stop being written and the scrub entries become dead — harmless,
    // but the comment in sessionScope.js would then be wrong.
    expect(ws).toMatch(/ms\.init\(\{\s*\}\)/);
  });
});
