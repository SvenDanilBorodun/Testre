/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// LeaderActivationGate — leader-arm Vormachen waits for the Startseite
// activation. Unknown (null, an older image with no agent) is NOT „not
// activated"; every other non-active state is.

import React from 'react';
import { render, screen } from '@testing-library/react';
import LeaderActivationGate, { leaderActivationBlocked } from '../LeaderActivationGate';
import { DE } from '../../blocks/messages_de';

const mockActivation = vi.hoisted(() => ({ status: null, calls: [] }));
vi.mock('../../../../hooks/useRobotActivation', async () => {
  const actual = await vi.importActual('../../../../hooks/useRobotActivation');
  return {
    ...actual,
    __esModule: true,
    default: (opts) => {
      mockActivation.calls.push(opts);
      return { status: mockActivation.status, activate: vi.fn(), calling: false, error: null };
    },
  };
});

const status = (state) => (state === null ? null : {
  state, step: '', message: '', robotType: 'omx_full', hasLeader: true, required: true,
});

function renderGate(state, onBlockedChange = vi.fn()) {
  mockActivation.status = status(state);
  const view = render(
    <LeaderActivationGate onBlockedChange={onBlockedChange}>
      <p data-testid="teach-content">Inhalt</p>
    </LeaderActivationGate>,
  );
  return { ...view, onBlockedChange };
}

beforeEach(() => {
  mockActivation.status = null;
  mockActivation.calls = [];
});

describe('LeaderActivationGate', () => {
  test.each([null, 'active'])('renders the children for %s and reports not blocked', (state) => {
    const { onBlockedChange } = renderGate(state);
    expect(screen.getByTestId('teach-content')).toBeInTheDocument();
    expect(screen.queryByText(DE.TEACH_BLOCK_NOT_ACTIVE)).toBeNull();
    expect(onBlockedChange).toHaveBeenLastCalledWith(false);
    expect(onBlockedChange).not.toHaveBeenCalledWith(true);
  });

  test.each(['idle', 'activating', 'failed'])('shows the blocked panel for %s and reports blocked', (state) => {
    const { onBlockedChange } = renderGate(state);
    expect(screen.queryByTestId('teach-content')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent(DE.TEACH_BLOCK_NOT_ACTIVE);
    expect(DE.TEACH_BLOCK_NOT_ACTIVE).toBe('Aktiviere den Roboter auf der Startseite, bevor du ihn bewegst.');
    expect(onBlockedChange).toHaveBeenLastCalledWith(true);
  });

  test('subscribes with enabled: true', () => {
    renderGate('active');
    expect(mockActivation.calls[0]).toEqual({ enabled: true });
  });

  test('activation arriving mid-session unblocks; unmount reports not blocked', () => {
    const onBlockedChange = vi.fn();
    const { rerender, unmount } = renderGate('idle', onBlockedChange);
    expect(onBlockedChange).toHaveBeenLastCalledWith(true);
    mockActivation.status = status('active');
    rerender(
      <LeaderActivationGate onBlockedChange={onBlockedChange}>
        <p data-testid="teach-content">Inhalt</p>
      </LeaderActivationGate>,
    );
    expect(screen.getByTestId('teach-content')).toBeInTheDocument();
    expect(onBlockedChange).toHaveBeenLastCalledWith(false);
    mockActivation.status = status('failed');
    rerender(
      <LeaderActivationGate onBlockedChange={onBlockedChange}>
        <p data-testid="teach-content">Inhalt</p>
      </LeaderActivationGate>,
    );
    expect(onBlockedChange).toHaveBeenLastCalledWith(true);
    unmount();
    expect(onBlockedChange).toHaveBeenLastCalledWith(false);
  });

  test('leaderActivationBlocked: unknown is not blocked', () => {
    expect(leaderActivationBlocked(null)).toBe(false);
    expect(leaderActivationBlocked(undefined)).toBe(false);
    expect(leaderActivationBlocked(status('active'))).toBe(false);
    expect(leaderActivationBlocked(status('idle'))).toBe(true);
  });
});
