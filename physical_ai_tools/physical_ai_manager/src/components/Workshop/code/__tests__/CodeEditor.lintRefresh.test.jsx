/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review round 2 (ni3): CodeWorkspace hands the editor a NEW `assetSources`
// object on every deferred keystroke, and the editor used to force the
// Sammlung lint on each one — skipping the linter's own idle debounce. The
// lint is forced only when the names it judges by really changed.

import React from 'react';
import { render, act } from '@testing-library/react';
import CodeEditor from '../CodeEditor';
import { assetDiagnostics } from '../codeAssetCompletion';

vi.mock('../codeAssetCompletion', async (importOriginal) => {
  const original = await importOriginal();
  return { ...original, assetDiagnostics: vi.fn(original.assetDiagnostics) };
});

const hadRangeRects = typeof Range !== 'undefined' && typeof Range.prototype.getClientRects === 'function';
beforeAll(() => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    Range.prototype.getClientRects = function getClientRects() { return []; };
    Range.prototype.getBoundingClientRect = function getBoundingClientRect() {
      return {
        x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0,
      };
    };
  }
});
afterAll(() => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    delete Range.prototype.getClientRects;
    delete Range.prototype.getBoundingClientRect;
  }
});
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

const DOC = 'import robot\nrobot.move_to("Ablage")\nrobot.replay("Winken")\n';
const sources = (entries) => ({
  files: { 'main.py': DOC }, language: 'python', entries, trajectories: null, objectTypes: [],
});
const ABLAGE = [{ id: 'd_1', name: 'Ablage', kind: 'pin', x: 0.1, y: 0, z: 0 }];

test('a new sources object with the same names forces no lint; a real change does', async () => {
  const { rerender } = render(
    <CodeEditor language="python" path="main.py" value={DOC} onChange={() => {}} assetSources={sources(ABLAGE)} />,
  );
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  const settled = assetDiagnostics.mock.calls.length;
  expect(settled).toBeGreaterThan(0);

  for (let i = 0; i < 5; i += 1) {
    rerender(
      <CodeEditor language="python" path="main.py" value={DOC} onChange={() => {}} assetSources={sources([...ABLAGE])} />,
    );
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
  }
  expect(assetDiagnostics.mock.calls.length).toBe(settled);

  rerender(
    <CodeEditor
      language="python"
      path="main.py"
      value={DOC}
      onChange={() => {}}
      assetSources={sources([...ABLAGE, { id: 'd_2', name: 'Kiste', kind: 'pin', x: 0, y: 0.1, z: 0 }])}
    />,
  );
  await act(async () => { await vi.advanceTimersByTimeAsync(5); });
  expect(assetDiagnostics.mock.calls.length).toBeGreaterThan(settled);
});
