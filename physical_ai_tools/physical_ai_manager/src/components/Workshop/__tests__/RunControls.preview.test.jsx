/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// While a simulator preview plays, the run bar says so (chip + teal banner in
// place of the simulator info banner) and leaves the editor's block highlight
// alone: the running block ids are the generated `vorschau-*` ones.

import React from 'react';
import { render, screen } from '@testing-library/react';
import RunControls from '../RunControls';
import { DE, formatDe } from '../blocks/messages_de';

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
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));
vi.mock('../../../services/workflowApi', () => ({ __esModule: true, getTrajectoryByName: vi.fn() }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

const PREVIEW = {
  key: 'rec:t3', kind: 'recording', name: 'Greifen links', workflowId: 'vorschau-aufnahme-3f9a1c0d',
  startedAt: 1, sawOwnStatus: true, lastError: '', unreachable: false, unreachableMessage: '',
};

function state({ preview = null, runState = 'running', currentBlockId = 'vorschau-1' } = {}) {
  return {
    workshop: {
      runState,
      phase: runState === 'running' ? 'running' : '',
      currentBlockId,
      paused: false,
      log: [],
      workflowError: null,
      debuggerVisible: false,
      debuggerWarnings: [],
      breakpoints: [],
    },
    auth: { session: { access_token: 'jwt' } },
    studioAssets: { preview },
  };
}

function makeWorkspace() {
  return { highlightBlock: vi.fn(), getBlockById: vi.fn(() => null) };
}

beforeEach(() => {
  vi.clearAllMocks();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

describe('RunControls — a preview in flight', () => {
  test('chip and teal banner name the preview; the simulator info banner is replaced', () => {
    mockState = state({ preview: PREVIEW });
    render(<RunControls workflowId="wf-1" simMode simScene={null} workspace={makeWorkspace()} />);
    expect(screen.getByText(formatDe(DE.PREVIEW_RUNNING, 'Greifen links'))).toBeInTheDocument();
    const banner = screen.getByText(formatDe(DE.PREVIEW_BANNER, 'Greifen links'));
    expect(banner).toHaveAttribute('role', 'status');
    expect(banner.className).toContain('teal');
    expect(screen.queryByText(/Simulator-Modus/)).toBeNull();
    expect(screen.queryByText(DE.RUN_RUNNING)).toBeNull();
  });

  test('without a preview the chip and the simulator banner are unchanged', () => {
    mockState = state({ runState: 'idle', currentBlockId: null });
    render(<RunControls workflowId="wf-1" simMode simScene={null} workspace={makeWorkspace()} />);
    expect(screen.getByText(DE.RUN_READY)).toBeInTheDocument();
    expect(screen.getByText(/Simulator-Modus/)).toBeInTheDocument();
    expect(screen.queryByText(/Vorschau/)).toBeNull();
  });

  test('the block highlight is not touched while previewing and resumes after', () => {
    const ws = makeWorkspace();
    mockState = state({ preview: PREVIEW, currentBlockId: 'vorschau-1' });
    const { rerender } = render(
      <RunControls workflowId="wf-1" simMode simScene={null} workspace={ws} />);
    expect(ws.highlightBlock).not.toHaveBeenCalledWith('vorschau-1');
    expect(ws.highlightBlock).not.toHaveBeenCalled();

    // The student's own run afterwards: highlighting is live again.
    mockState = state({ preview: null, currentBlockId: 'b7' });
    rerender(<RunControls workflowId="wf-1" simMode simScene={null} workspace={ws} />);
    expect(ws.highlightBlock).toHaveBeenCalledWith('b7');
  });
});
