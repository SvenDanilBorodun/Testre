/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code half of three small asset-document duties of the UX round:
//   * `flashInserted` (R0-5): after „Als Programm einfügen" the inserted lines
//     light up — a reveal of the last line carrying the whole range;
//   * `anchorLeft` (m-3): the drawer opens beside the file sidebar at its
//     APPLIED width, and at the default when nothing was measured;
//   * „Benutzt in" rows (R0-7) say WHERE („main.py, Zeile 2"); the line's own
//     text is the row's `detail`, cut with „…" only when it really was cut.

import { describe, it, expect } from 'vitest';
import { createCodeAssetDocument, CODE_SIDEBAR_WIDTH_PX } from '../codeAssetDocument';
import { createDetachedDestinationStore } from '../../sammlung/destinationStore';
import { CODE_DE, formatCode } from '../codeMessagesDe';

function make(over = {}) {
  const reveals = [];
  const files = over.files || { 'main.py': 'import robot\nrobot.replay("Winken")\n' };
  const doc = createCodeAssetDocument({
    language: 'python',
    store: createDetachedDestinationStore([]),
    getFiles: () => files,
    applyFiles: () => {},
    requestReveal: (at) => reveals.push(at),
    getCursor: () => null,
    ...over.args,
  });
  return { doc, reveals };
}

describe('flashInserted', () => {
  it('asks the editor to reveal the last inserted line and flash the whole range', () => {
    const { doc, reveals } = make();
    expect(doc.flashInserted({ file: 'main.py', firstLine: 3, lastLine: 5 })).toBe(true);
    expect(reveals).toEqual([{ file: 'main.py', line: 5, flash: { from: 3, to: 5 } }]);
  });

  it('a one-line insertion flashes that line', () => {
    const { doc, reveals } = make();
    expect(doc.flashInserted({ file: 'hilfe.py', firstLine: 2, lastLine: 2 })).toBe(true);
    expect(reveals).toEqual([{ file: 'hilfe.py', line: 2, flash: { from: 2, to: 2 } }]);
  });

  it('anything else is false and asks for nothing', () => {
    const { doc, reveals } = make();
    for (const bad of [
      null, undefined, 'main.py', {}, { count: 0 }, { blockIds: ['a'] },
      { file: '', firstLine: 1, lastLine: 1 },
      { file: 'main.py', firstLine: 0, lastLine: 1 },
      { file: 'main.py', firstLine: 3, lastLine: 2 },
      { file: 'main.py', firstLine: 1.5, lastLine: 2 },
      { file: 'main.py', firstLine: '1', lastLine: 2 },
    ]) {
      expect(doc.flashInserted(bad)).toBe(false);
    }
    expect(reveals).toEqual([]);
  });

  it('is false without a way to reveal', () => {
    const { doc } = make({ args: { requestReveal: undefined } });
    expect(doc.flashInserted({ file: 'main.py', firstLine: 1, lastLine: 1 })).toBe(false);
  });
});

describe('anchorLeft follows the sidebar’s applied width', () => {
  it('takes a measured positive width', () => {
    const { doc } = make({ args: { getSidebarWidth: () => 212 } });
    expect(doc.anchorLeft()).toBe(212);
  });

  it('falls back to the default without a function, at 0, negative, NaN or null', () => {
    expect(make().doc.anchorLeft()).toBe(CODE_SIDEBAR_WIDTH_PX);
    for (const w of [0, -5, NaN, null, undefined, 'breit']) {
      expect(make({ args: { getSidebarWidth: () => w } }).doc.anchorLeft()).toBe(CODE_SIDEBAR_WIDTH_PX);
    }
  });
});

describe('„Benutzt in" rows of a code program', () => {
  it('say file and line in German and carry the line’s text as detail', () => {
    const { doc } = make();
    expect(doc.usageRows('recording', 'Winken')).toEqual([{
      id: 'main.py:L2',
      label: formatCode(CODE_DE.USAGE_AT, 'main.py', 2),
      disabled: false,
      detail: 'robot.replay("Winken")',
    }]);
    expect(doc.usageRows('recording', 'Winken')[0].label).toBe('main.py, Zeile 2');
  });

  it('cut a long line at 60 characters with „…", and only then', () => {
    const long = `robot.replay("Winken")  ${'#'.repeat(10)} ${'x'.repeat(60)}`;
    const { doc } = make({ files: { 'main.py': `import robot\n${long}\n` } });
    const [row] = doc.usageRows('recording', 'Winken');
    expect(row.detail).toBe(`${long.slice(0, 60)}…`);
    const exact = `robot.replay("Winken")${' '.repeat(10)}${'y'.repeat(28)}`;
    expect(exact).toHaveLength(60);
    const { doc: doc2 } = make({ files: { 'main.py': `import robot\n${exact}\n` } });
    expect(doc2.usageRows('recording', 'Winken')[0].detail).toBe(exact);
  });
});
