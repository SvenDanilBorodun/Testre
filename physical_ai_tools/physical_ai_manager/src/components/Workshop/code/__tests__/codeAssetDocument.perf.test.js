/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review m8: the Variablen list of a code document re-tokenized the whole
// project once PER VARIABLE (93 ms for 120 variables, on every render). The
// document now asks the scanner for every name's uses in ONE pass. Counted
// at the module boundary, so the test does not depend on the machine's speed.

import { describe, it, expect, vi } from 'vitest';
import { createCodeAssetDocument } from '../codeAssetDocument';
import { createDetachedDestinationStore } from '../../sammlung/destinationStore';
import * as usage from '../codeAssetUsage';

vi.mock('../codeAssetUsage', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    variableOccurrences: vi.fn(actual.variableOccurrences),
    variableOccurrencesAll: actual.variableOccurrencesAll
      ? vi.fn(actual.variableOccurrencesAll) : undefined,
  };
});

describe('buildIndex of a code document with many variables', () => {
  it('scans the project for all variable uses once, not once per variable', () => {
    const lines = ['import robot'];
    for (let i = 0; i < 120; i += 1) lines.push(`wert${i} = ${i}`);
    const files = { 'main.py': `${lines.join('\n')}\n` };
    const doc = createCodeAssetDocument({
      language: 'python',
      store: createDetachedDestinationStore([]),
      getFiles: () => files,
      applyFiles: () => {},
      requestReveal: () => {},
      getCursor: () => null,
    });
    const index = doc.buildIndex({});
    expect(index.variables).toHaveLength(120);
    expect(usage.variableOccurrences).not.toHaveBeenCalled();
    expect(usage.variableOccurrencesAll).toHaveBeenCalledTimes(1);
  });
});
