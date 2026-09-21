/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The wiring between the gutter and the wire: which id the toggle stores, when
// it is pushed to the runtime, and which line the server's `current_block_id`
// lights up. A REAL workshop reducer, because the id that ends up in
// `s.workshop.breakpoints` is the id `RunControls.handleStart` sends pre-start
// and the id the runner receives.
//
// The editor is mocked (the real one is a lazy CodeMirror chunk and has its own
// test): the mock renders the props the wiring computes, so a wrong line or a
// wrong language is visible as a value rather than as a pixel.

import React from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import CodeWorkspace from '../CodeWorkspace';
import { CODE_DE } from '../codeMessagesDe';
import workshopReducer, {
  setWorkflowStatus, setRunState, setPaused,
} from '../../../../features/workshop/workshopSlice';

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(),
  setWorkflowBreakpoints: vi.fn(() => Promise.resolve({ success: true })),
}));
vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
  useToasterStore: () => ({ toasts: [] }),
}));

// The editor's props ARE the assertion surface here.
vi.mock('../CodeEditor', () => ({
  __esModule: true,
  default: function MockCodeEditor({ path, breakpointLines, onToggleBreakpoint, highlightLine, highlightKind }) {
    return (
      <div
        data-testid="code-editor"
        data-path={path}
        data-breakpoints={(breakpointLines || []).join(',')}
        data-highlight-line={highlightLine == null ? '' : String(highlightLine)}
        data-highlight-kind={highlightKind || ''}
        data-gutter={typeof onToggleBreakpoint === 'function' ? 'yes' : 'no'}
      >
        <button type="button" onClick={() => onToggleBreakpoint && onToggleBreakpoint(12)}>
          press-gutter-12
        </button>
      </div>
    );
  },
}));

const FILES = Object.freeze({
  'main.py': 'import robot\nrobot.home()\n',
  'hilfe.py': '# Hilfe\n',
});
const JAVA_FILES = Object.freeze({ 'Main.java': 'class Main {}\n' });

const WORKSHOP_DEFAULTS = workshopReducer(undefined, { type: '@@init' });

function makeStore(workshop = {}) {
  return configureStore({
    reducer: { workshop: workshopReducer },
    preloadedState: { workshop: { ...WORKSHOP_DEFAULTS, ...workshop } },
  });
}

async function mount({ language = 'python', files = FILES, workshop = {} } = {}) {
  const store = makeStore(workshop);
  const utils = render(
    <Provider store={store}>
      <CodeWorkspace language={language} files={files} onFilesChange={() => {}} />
    </Provider>,
  );
  await screen.findByTestId('code-editor');
  return { store, ...utils };
}

const editor = () => screen.getByTestId('code-editor');

beforeEach(() => {
  vi.clearAllMocks();
  try { window.localStorage.clear(); } catch { /* private mode */ }
});

describe('CodeWorkspace — breakpoints on the wire', () => {
  test('a gutter press stores `<file>:L<line>` and nothing else', async () => {
    const { store } = await mount();
    await userEvent.click(screen.getByText('press-gutter-12'));
    expect(store.getState().workshop.breakpoints).toEqual(['main.py:L12']);
    await waitFor(() => expect(editor()).toHaveAttribute('data-breakpoints', '12'));
  });

  test('pressing the same line again removes it', async () => {
    const { store } = await mount();
    await userEvent.click(screen.getByText('press-gutter-12'));
    await userEvent.click(screen.getByText('press-gutter-12'));
    expect(store.getState().workshop.breakpoints).toEqual([]);
  });

  test('while a program runs, the whole set reaches setWorkflowBreakpoints', async () => {
    await mount({ workshop: { runState: 'running', phase: 'running' } });
    await userEvent.click(screen.getByText('press-gutter-12'));
    await waitFor(() => expect(mockRos.setWorkflowBreakpoints).toHaveBeenCalledWith(['main.py:L12']));
  });

  test('before Start nothing is pushed — RunControls sends the set with the run', async () => {
    await mount({ workshop: { runState: 'idle' } });
    await userEvent.click(screen.getByText('press-gutter-12'));
    await new Promise((r) => setTimeout(r, 400));
    expect(mockRos.setWorkflowBreakpoints).not.toHaveBeenCalled();
  });

  test('a Blockly id already in the set is carried along, never rewritten', async () => {
    const { store } = await mount({ workshop: { breakpoints: ['b7'], runState: 'running' } });
    await userEvent.click(screen.getByText('press-gutter-12'));
    expect(store.getState().workshop.breakpoints).toEqual(['b7', 'main.py:L12']);
    await waitFor(() => expect(mockRos.setWorkflowBreakpoints).toHaveBeenCalledWith(['b7', 'main.py:L12']));
  });

  test('only the open file`s lines reach the editor', async () => {
    await mount({ workshop: { breakpoints: ['main.py:L3', 'hilfe.py:L9', 'b7'] } });
    expect(editor()).toHaveAttribute('data-path', 'main.py');
    expect(editor()).toHaveAttribute('data-breakpoints', '3');
  });
});

describe('CodeWorkspace — the run line', () => {
  test('a paused `<file>:L<line>` highlights that line', async () => {
    await mount({ workshop: { currentBlockId: 'main.py:L12', runState: 'running', paused: true } });
    expect(editor()).toHaveAttribute('data-highlight-line', '12');
    expect(editor()).toHaveAttribute('data-highlight-kind', 'paused');
  });

  // The error line is NOT highlighted, and this test exists so nobody re-adds
  // the claim without changing what makes it false. The server does publish the
  // id (`code_program._raise_error` → `<file>:L<line>` with phase `error`), but
  // one topic callback dispatches BOTH `setWorkflowStatus` and
  // `setRunState('error')`, and the terminal branch of the latter nulls
  // `currentBlockId` and blanks `phase`. Driving the REAL reducers through that
  // real sequence is the whole point: a preloaded `{phase:'error',
  // currentBlockId:'main.py:L4'}` store is a state production cannot reach, and
  // asserting against one is how the unreachable branch shipped green.
  test('a terminal error tick leaves no line highlighted — the id is gone by then', async () => {
    const { store } = await mount({ workshop: { runState: 'running', phase: 'running' } });
    await act(async () => {
      store.dispatch(setWorkflowStatus({
        current_block_id: 'main.py:L4',
        phase: 'error',
        progress: 1,
        error: 'Zeile 4 in main.py',
        log_message: '',
        workflow_id: 'wf-1',
      }));
      store.dispatch(setRunState('error'));
      store.dispatch(setPaused(false));
    });
    // The cause, pinned: if this ever holds the id again, revisit the highlight.
    expect(store.getState().workshop.currentBlockId).toBeNull();
    expect(store.getState().workshop.phase).toBe('');
    expect(editor()).toHaveAttribute('data-highlight-line', '');
    expect(editor()).toHaveAttribute('data-highlight-kind', '');
  });

  // The positive control for the test above: the two kinds that DO survive the
  // real dispatch sequence. A breakpoint tick dispatches only `setPaused(true)`
  // and a per-line tick re-dispatches `setRunState('running')` while runState is
  // ALREADY 'running', so its transition guard does not null the id.
  test('a paused tick and a running tick both keep their line, through the real dispatches', async () => {
    const { store } = await mount({ workshop: { runState: 'running', phase: 'running' } });
    const tick = (block, phase) => ({
      current_block_id: block, phase, progress: 0, error: '', log_message: '', workflow_id: 'wf-1',
    });
    await act(async () => {
      store.dispatch(setWorkflowStatus(tick('main.py:L2', 'running')));
      store.dispatch(setRunState('running'));
      store.dispatch(setPaused(false));
    });
    expect(editor()).toHaveAttribute('data-highlight-line', '2');
    expect(editor()).toHaveAttribute('data-highlight-kind', 'running');
    await act(async () => {
      store.dispatch(setWorkflowStatus(tick('main.py:L9', 'paused')));
      store.dispatch(setPaused(true));
    });
    expect(editor()).toHaveAttribute('data-highlight-line', '9');
    expect(editor()).toHaveAttribute('data-highlight-kind', 'paused');
  });

  test('an id of an UNKNOWN shape is ignored — a Blockly id is not a line', async () => {
    await mount({ workshop: { currentBlockId: 'b7', runState: 'running' } });
    expect(editor()).toHaveAttribute('data-highlight-line', '');
  });

  test('an id naming another FILE does not light up this one', async () => {
    await mount({ workshop: { currentBlockId: 'hilfe.py:L2', runState: 'running' } });
    expect(editor()).toHaveAttribute('data-highlight-line', '');
  });

  test('nothing running, nothing highlighted', async () => {
    await mount({ workshop: { currentBlockId: null, runState: 'idle' } });
    expect(editor()).toHaveAttribute('data-highlight-line', '');
  });
});

describe('CodeWorkspace — the Java asymmetry', () => {
  test('Java gets no gutter and says why, in German', async () => {
    await mount({ language: 'java', files: JAVA_FILES });
    expect(editor()).toHaveAttribute('data-gutter', 'no');
    expect(screen.getByText(CODE_DE.DEBUG_JAVA_NO_BREAKPOINTS)).toBeInTheDocument();
  });

  test('Python gets the gutter and no such notice', async () => {
    await mount();
    expect(editor()).toHaveAttribute('data-gutter', 'yes');
    expect(screen.queryByText(CODE_DE.DEBUG_JAVA_NO_BREAKPOINTS)).toBeNull();
  });

  test('a read-only workspace has no gutter either — it is nobody`s program to debug', async () => {
    const store = makeStore();
    render(
      <Provider store={store}>
        <CodeWorkspace language="python" files={FILES} onFilesChange={() => {}} readOnly />
      </Provider>,
    );
    await screen.findByTestId('code-editor');
    expect(editor()).toHaveAttribute('data-gutter', 'no');
  });
});
