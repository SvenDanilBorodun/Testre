/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// CodeView render contract. The heavy generator (pythonCodeGen → blockly/python)
// is mocked so this stays a fast, deps-light render test of the panel states:
// idle (no workspace) → placeholder, ready → the generated code, error → German
// error. Generation correctness lives in pythonCodeGen.test.js.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import CodeView from '../CodeView';

vi.mock('../pythonCodeGen', () => {
  const generatePython = vi.fn(async (ws) => {
    if (!ws) return '';
    if (ws.fail) throw new Error('boom');
    return 'robot.home()\nrobot.grasp("cube")\n';
  });
  return { generatePython, default: generatePython };
});

describe('CodeView', () => {
  test('shows the empty-state placeholder when there is no workspace', () => {
    render(<CodeView workspace={null} changeToken={null} />);
    expect(screen.getByText(/Noch keine Blöcke/)).toBeInTheDocument();
  });

  test('renders the generated Python for a workspace', async () => {
    render(<CodeView workspace={{ id: 'ws' }} changeToken={1} />);
    expect(await screen.findByText(/robot\.grasp\("cube"\)/)).toBeInTheDocument();
    expect(screen.getByText(/robot\.home\(\)/)).toBeInTheDocument();
  });

  test('shows a German error when generation fails', async () => {
    render(<CodeView workspace={{ fail: true }} changeToken={2} />);
    expect(
      await screen.findByText(/Code-Vorschau konnte nicht erzeugt werden/),
    ).toBeInTheDocument();
  });
});
