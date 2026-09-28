/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RunControls draws icons, never glyphs (owner decision D7): the run buttons,
// the Protokoll toggle, Debug, the out-of-reach toast — and the pause icon in
// front of a breakpoint's Protokoll line, whose text the server sends plain
// (BREAKPOINT_LOG_PREFIX, lockstep-tested against the server's f-strings).

/* eslint-disable testing-library/no-node-access */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls, { BREAKPOINT_LOG_PREFIX } from '../RunControls';
import { DE } from '../blocks/messages_de';
import { toastIcon } from '../../icons/toast';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(() => Promise.resolve({
    success: true, message: 'gestartet', unreachable_block_ids: ['b1'], unreachable_messages: ['zu weit'],
  })),
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
const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

function state(over = {}) {
  return {
    workshop: {
      runState: 'idle',
      phase: '',
      currentBlockId: null,
      paused: false,
      log: [],
      workflowError: null,
      debuggerVisible: false,
      debuggerWarnings: [],
      breakpoints: [],
      ...over,
    },
    auth: { session: { access_token: 'jwt-1' } },
  };
}

const PROGRAM = { blocks: { blocks: [{ type: 'edubotics_home' }] } };
const iconOf = (el) => el.querySelector('svg[data-icon]').getAttribute('data-icon');

beforeEach(() => {
  mockState = state();
  mockDispatch.mockClear();
  mockToast.mockClear();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

describe('RunControls icons', () => {
  test('the breakpoint prefix is the server\'s plain German sentence start', () => {
    expect(BREAKPOINT_LOG_PREFIX).toBe('Haltepunkt erreicht: ');
  });

  test('Start, Stopp, the Protokoll toggle and Debug carry their icons', () => {
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    expect(iconOf(screen.getByRole('button', { name: DE.RUN_START }))).toBe('play');
    expect(iconOf(screen.getByRole('button', { name: DE.RUN_STOP }))).toBe('stop');
    expect(iconOf(screen.getByRole('button', { name: new RegExp(DE.DOCK_LOG_LABEL) }))).toBe('chevronRight');
    expect(iconOf(screen.getByRole('button', { name: DE.DOCK_TAB_DEBUG }))).toBe('debug');
  });

  test('paused: Schritt and Weiter carry their icons; running: Pause', () => {
    mockState = state({ runState: 'running', paused: true });
    const { unmount } = render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    expect(iconOf(screen.getByRole('button', { name: DE.RUN_STEP }))).toBe('step');
    expect(iconOf(screen.getByRole('button', { name: DE.RUN_CONTINUE }))).toBe('play');
    unmount();
    mockState = state({ runState: 'running', paused: false });
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    expect(iconOf(screen.getByRole('button', { name: DE.RUN_PAUSE }))).toBe('pause');
  });

  test('a breakpoint line in the Protokoll gets the pause icon; other lines none', async () => {
    mockState = state({
      log: [
        { ts: 1, text: 'Workflow läuft.' },
        { ts: 2, text: `${BREAKPOINT_LOG_PREFIX}main.py:L3` },
      ],
    });
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: new RegExp(DE.DOCK_LOG_LABEL) }));
    const log = screen.getByLabelText('Workflow-Log');
    const lines = Array.from(log.children);
    expect(lines).toHaveLength(2);
    expect(lines[0].querySelector('svg')).toBeNull();
    expect(lines[1].querySelector('svg[data-icon="pause"]')).toHaveAttribute('aria-hidden', 'true');
    expect(within(log).getByText(/Haltepunkt erreicht: main\.py:L3/)).toBeInTheDocument();
  });

  test('the out-of-reach toast shows the warning icon', async () => {
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: DE.RUN_START }));
    await waitFor(() => expect(mockToast).toHaveBeenCalled());
    expect(mockToast).toHaveBeenCalledWith(
      '1 Block markiert: außerhalb des Arbeitsbereichs.', { icon: toastIcon('warning') },
    );
  });
});
