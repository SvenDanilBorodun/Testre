/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import ModeBadge from '../ModeBadge';
import { MODE_DE } from '../modeCopy';

let mockState;
const mockSelectorCalls = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => {
    mockSelectorCalls();
    return sel(mockState);
  },
}));

const state = ({ runState = 'idle', paused = false, heartbeat = 'connected' } = {}) => ({
  workshop: { runState, paused },
  tasks: { heartbeatStatus: heartbeat },
});

beforeEach(() => {
  mockState = state();
  mockSelectorCalls.mockClear();
});

// eslint-disable-next-line testing-library/no-node-access
const dotOf = (badge) => badge.querySelector('[data-icon="dot"]');

describe('ModeBadge', () => {
  test('real mode: orange, „ECHTER ROBOTER", the arm icon and its title', () => {
    render(<ModeBadge simMode={false} />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(MODE_DE.BADGE_REAL);
    expect(badge.style.background).toBe('rgb(194, 65, 12)');
    expect(badge).toHaveAttribute('title', MODE_DE.BADGE_REAL_TITLE);
    // eslint-disable-next-line testing-library/no-node-access
    expect(badge.querySelector('[data-icon="robotArm"]')).not.toBeNull();
    // Literal uppercase, never CSS `uppercase`.
    expect(badge.className).not.toContain('uppercase');
    expect(MODE_DE.BADGE_REAL).toBe('ECHTER ROBOTER');
  });

  test('simulator: blue, „SIMULATOR", the simulator icon, no run state', () => {
    mockState = state({ runState: 'running' });
    render(<ModeBadge simMode />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(MODE_DE.BADGE_SIM);
    expect(badge.style.background).toBe('rgb(37, 99, 235)');
    expect(badge).toHaveAttribute('title', MODE_DE.BADGE_SIM_TITLE);
    // eslint-disable-next-line testing-library/no-node-access
    expect(badge.querySelector('[data-icon="simulator"]')).not.toBeNull();
    expect(badge).not.toHaveTextContent(MODE_DE.RUN_LIVE);
    expect(dotOf(badge)).toBeNull();
  });

  test('idle real mode carries no run state', () => {
    render(<ModeBadge simMode={false} />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(/^ECHTER ROBOTER$/);
    expect(dotOf(badge)).toBeNull();
  });

  test('a running program: „Programm läuft" with a PULSING dot while the link is alive', () => {
    mockState = state({ runState: 'running' });
    render(<ModeBadge simMode={false} />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(`${MODE_DE.BADGE_REAL} · ${MODE_DE.RUN_LIVE}`);
    expect(dotOf(badge).getAttribute('class')).toContain('motion-safe:animate-pulse');
    // The badge itself never animates.
    expect(badge.className).not.toContain('animate');
  });

  test('a running program on a dead link: the dot is steady', () => {
    mockState = state({ runState: 'running', heartbeat: 'disconnected' });
    render(<ModeBadge simMode={false} />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(MODE_DE.RUN_LIVE);
    expect(dotOf(badge).getAttribute('class')).not.toContain('animate-pulse');
  });

  test('paused at a breakpoint: „Angehalten" with a steady dot', () => {
    mockState = state({ runState: 'running', paused: true });
    render(<ModeBadge simMode={false} />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(`${MODE_DE.BADGE_REAL} · ${MODE_DE.RUN_PAUSED}`);
    expect(badge).not.toHaveTextContent(MODE_DE.RUN_LIVE);
    expect(dotOf(badge).getAttribute('class')).not.toContain('animate-pulse');
  });

  test('withRunState={false} renders no dot and reads no store', () => {
    mockState = {};
    render(<ModeBadge simMode={false} withRunState={false} />);
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(/^ECHTER ROBOTER$/);
    expect(dotOf(badge)).toBeNull();
    expect(mockSelectorCalls).not.toHaveBeenCalled();
  });

  test('getByText("Simulator") (exact) never matches the badge', () => {
    render(<ModeBadge simMode />);
    expect(screen.queryByText('Simulator')).toBeNull();
  });
});
