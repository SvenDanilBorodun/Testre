/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The run bar's three code-only duties, each of which is about telling the
// truth rather than about a new control:
//
//  1. `current_block_id` now carries TWO id spaces. The Blockly highlight must
//     never be handed a code id. It is a no-op on today's page (a code workflow
//     mounts no canvas, so `workspace` is null) — which is exactly why it is
//     asserted: nothing else would notice if a later change gave the page a
//     workspace while a code program ran.
//  2. „Pause" on a code program takes effect at the next robot call or
//     breakpoint, NOT between two lines (§3.3). A student watching a paused
//     chip over a loop that keeps counting is owed that sentence.
//  3. The German error sentence leads and the raw tool line sits BENEATH it, in
//     monospace (decision A14). The raw line arrives as a `[TECHNIK] ` log
//     entry; the banner is where the student is already looking.

import React from 'react';
import { render, screen } from '@testing-library/react';
import RunControls from '../../RunControls';
import { CODE_DE } from '../codeMessagesDe';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(),
  pauseWorkflow: vi.fn(),
  stepWorkflow: vi.fn(),
  continueWorkflow: vi.fn(),
  setWorkflowBreakpoints: vi.fn(),
}));
vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));
vi.mock('../../../../services/workflowApi', () => ({
  __esModule: true,
  getTrajectoryByName: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

function state({
  runState = 'running', currentBlockId = null, paused = false,
  workflowError = null, log = [], phase = 'running',
} = {}) {
  return {
    workshop: {
      runState, phase, currentBlockId, paused, log,
      workflowError,
      debuggerVisible: false, debuggerWarnings: [], breakpoints: [],
    },
    auth: { session: { access_token: 'jwt' } },
    tasks: { taskStatus: { robotType: 'omx_f', capabilities: { code_languages: ['python'] } } },
    studioAssets: { preview: null },
  };
}

const makeWorkspace = () => ({ highlightBlock: vi.fn(), getBlockById: vi.fn(() => null) });

beforeEach(() => {
  vi.clearAllMocks();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

describe('RunControls — the Blockly highlight never sees a code id', () => {
  test('a `<file>:L<line>` id is not passed to highlightBlock', () => {
    const ws = makeWorkspace();
    mockState = state({ currentBlockId: 'main.py:L12' });
    render(<RunControls workflowId="wf-1" workspace={ws} codeLanguage="python" codeFiles={{}} />);
    expect(ws.highlightBlock).not.toHaveBeenCalledWith('main.py:L12');
  });

  test('a Blockly id still highlights, so the guard is a filter and not a switch-off', () => {
    const ws = makeWorkspace();
    mockState = state({ currentBlockId: 'b7' });
    render(<RunControls workflowId="wf-1" workspace={ws} />);
    expect(ws.highlightBlock).toHaveBeenCalledWith('b7');
  });
});

describe('RunControls — the pause asymmetry', () => {
  test('a paused CODE run says where the pause takes effect', () => {
    mockState = state({ paused: true });
    render(<RunControls workflowId="wf-1" workspace={null} codeLanguage="python" codeFiles={{}} />);
    expect(screen.getByText(CODE_DE.RUN_PAUSE_CODE_HINT)).toBeInTheDocument();
  });

  test('a paused BLOCKLY run does not — there the pause lands at the next block', () => {
    mockState = state({ paused: true });
    render(<RunControls workflowId="wf-1" workspace={makeWorkspace()} />);
    expect(screen.queryByText(CODE_DE.RUN_PAUSE_CODE_HINT)).toBeNull();
  });

  test('a code run that is NOT paused does not say it either', () => {
    mockState = state({ paused: false });
    render(<RunControls workflowId="wf-1" workspace={null} codeLanguage="python" codeFiles={{}} />);
    expect(screen.queryByText(CODE_DE.RUN_PAUSE_CODE_HINT)).toBeNull();
  });
});

describe('RunControls — the [TECHNIK] line under the German sentence', () => {
  const GERMAN = 'Zeile 7 in main.py: Hier stimmt etwas mit der Schreibweise nicht.';
  const RAW = "SyntaxError: expected ':'";

  test('the raw tool line is shown beneath the sentence, in monospace', () => {
    mockState = state({
      runState: 'idle',
      phase: 'error',
      workflowError: GERMAN,
      log: [{ ts: 1, text: `[TECHNIK] ${RAW}` }],
    });
    render(<RunControls workflowId="wf-1" workspace={null} codeLanguage="python" codeFiles={{}} />);
    const banner = screen.getByRole('alert');
    expect(banner).toHaveTextContent(GERMAN);
    expect(banner).toHaveTextContent(CODE_DE.ERROR_TECHNIK_LABEL);
    const raw = screen.getByTestId('technik-line');
    expect(raw).toHaveTextContent(RAW);
    expect(raw.className).toContain('font-mono');
    // The German sentence LEADS — the raw line is a separate element beneath
    // it, never spliced into the sentence (§3.6).
    expect(banner.textContent.indexOf(GERMAN)).toBeLessThan(banner.textContent.indexOf(RAW));
  });

  test('the NEWEST [TECHNIK] line wins — an earlier run`s must not be shown', () => {
    mockState = state({
      runState: 'idle',
      phase: 'error',
      workflowError: GERMAN,
      log: [
        { ts: 1, text: '[TECHNIK] NameError: name `robto` is not defined' },
        { ts: 2, text: 'Programm gestartet.' },
        { ts: 3, text: `[TECHNIK] ${RAW}` },
      ],
    });
    render(<RunControls workflowId="wf-1" workspace={null} codeLanguage="python" codeFiles={{}} />);
    expect(screen.getByTestId('technik-line')).toHaveTextContent(RAW);
    expect(screen.getByTestId('technik-line')).not.toHaveTextContent('robto');
  });

  test('no [TECHNIK] line, no label — an error without one shows the sentence alone', () => {
    mockState = state({ runState: 'idle', phase: 'error', workflowError: GERMAN, log: [] });
    render(<RunControls workflowId="wf-1" workspace={null} codeLanguage="python" codeFiles={{}} />);
    expect(screen.getByRole('alert')).toHaveTextContent(GERMAN);
    expect(screen.queryByTestId('technik-line')).toBeNull();
    expect(screen.queryByText(CODE_DE.ERROR_TECHNIK_LABEL)).toBeNull();
  });

  test('a Blockly run shows no [TECHNIK] block — nothing there emits one', () => {
    mockState = state({
      runState: 'idle',
      phase: 'error',
      workflowError: GERMAN,
      log: [{ ts: 1, text: `[TECHNIK] ${RAW}` }],
    });
    render(<RunControls workflowId="wf-1" workspace={makeWorkspace()} />);
    expect(screen.queryByTestId('technik-line')).toBeNull();
  });
});
