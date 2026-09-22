/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Debug-Panel's „Haltepunkte" tab belongs to whichever notation the open
// workflow is written in. For a Blockly program that is `BreakpointList`, whose
// hint tells the student to Alt-click a BLOCK — advice a code program cannot
// follow, and whose per-id label resolves against a Blockly workspace that a
// code workflow does not have.
//
// So the tab switches on the language, and the Java asymmetry (A8) is SAID
// here rather than left as an empty list the student reads as „broken".
//
// This file lives under code/__tests__ because the behaviour it pins is the
// code editor's; the component under test is one directory up.

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import DebugPanel from '../../DebugPanel';
import { CODE_DE, formatCode } from '../codeMessagesDe';
import { DE } from '../../blocks/messages_de';
import workshopReducer from '../../../../features/workshop/workshopSlice';

vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({
    callService: vi.fn(() => Promise.resolve({})),
    setWorkflowBreakpoints: vi.fn(() => Promise.resolve({})),
  }),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const DEFAULTS = workshopReducer(undefined, { type: '@@init' });

async function openBreakpoints({ codeLanguage = '', breakpoints = [] } = {}) {
  const store = configureStore({
    reducer: { workshop: workshopReducer },
    preloadedState: { workshop: { ...DEFAULTS, breakpoints } },
  });
  render(
    <Provider store={store}>
      <DebugPanel workspace={null} codeLanguage={codeLanguage} />
    </Provider>,
  );
  await userEvent.click(screen.getByRole('tab', { name: DE.DEBUG_TAB_BREAKPOINTS }));
  return store;
}

/** The code panel only — so no assertion can be satisfied by the Blockly one. */
const codePanel = () => within(screen.getByTestId('code-breakpoints'));

describe('DebugPanel — the breakpoints tab of a code program', () => {
  test('Python says where breakpoints are set and lists the ones that are', async () => {
    await openBreakpoints({ codeLanguage: 'python', breakpoints: ['main.py:L12', 'hilfe.py:L9'] });
    expect(codePanel().getByText(CODE_DE.DEBUG_BP_HINT_PY)).toBeInTheDocument();
    expect(codePanel().getByText('main.py:L12')).toBeInTheDocument();
    expect(codePanel().getByText('hilfe.py:L9')).toBeInTheDocument();
    // The Blockly hint names an interaction a code program has no blocks for.
    expect(screen.queryByText(DE.DEBUG_BP_TOGGLE_HINT)).toBeNull();
  });

  test('a Blockly id left over from another document is not listed as a line', async () => {
    await openBreakpoints({ codeLanguage: 'python', breakpoints: ['b7', 'main.py:L12'] });
    expect(codePanel().getByText('main.py:L12')).toBeInTheDocument();
    expect(codePanel().queryByText('b7')).toBeNull();
  });

  test('an empty set says so, reusing the sentence the Blockly panel already has', async () => {
    await openBreakpoints({ codeLanguage: 'python' });
    expect(codePanel().getByText(DE.DEBUG_NO_BREAKPOINTS)).toBeInTheDocument();
  });

  test('a breakpoint can be removed from here, and the store is what changes', async () => {
    const store = await openBreakpoints({ codeLanguage: 'python', breakpoints: ['main.py:L12'] });
    await userEvent.click(
      codePanel().getByRole('button', { name: formatCode(CODE_DE.DEBUG_BP_REMOVE, 'main.py:L12') }),
    );
    expect(store.getState().workshop.breakpoints).toEqual([]);
  });

  test('„Alle entfernen" clears every id, the Blockly ones included', async () => {
    const store = await openBreakpoints({ codeLanguage: 'python', breakpoints: ['b7', 'main.py:L12'] });
    await userEvent.click(codePanel().getByRole('button', { name: CODE_DE.DEBUG_BP_CLEAR }));
    expect(store.getState().workshop.breakpoints).toEqual([]);
  });

  test('Java states the asymmetry instead of showing an empty list', async () => {
    await openBreakpoints({ codeLanguage: 'java' });
    expect(codePanel().getByText(CODE_DE.DEBUG_JAVA_NO_BREAKPOINTS)).toBeInTheDocument();
    expect(screen.queryByText(CODE_DE.DEBUG_BP_HINT_PY)).toBeNull();
    expect(screen.queryByText(DE.DEBUG_BP_TOGGLE_HINT)).toBeNull();
  });

  test('a Blockly workflow keeps the Blockly list, untouched', async () => {
    await openBreakpoints({ codeLanguage: '' });
    expect(screen.getByText(DE.DEBUG_BP_TOGGLE_HINT)).toBeInTheDocument();
    expect(screen.queryByTestId('code-breakpoints')).toBeNull();
    expect(screen.queryByText(CODE_DE.DEBUG_BP_HINT_PY)).toBeNull();
    expect(screen.queryByText(CODE_DE.DEBUG_JAVA_NO_BREAKPOINTS)).toBeNull();
  });
});
