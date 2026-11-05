// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import useReducedMotion, { readReducedMotion, REDUCED_MOTION_QUERY } from '../useReducedMotion';

function Probe() {
  return <span data-testid="rm">{String(useReducedMotion())}</span>;
}

describe('useReducedMotion', () => {
  const orig = window.matchMedia;
  afterEach(() => { window.matchMedia = orig; });

  it('without matchMedia the answer is false', () => {
    window.matchMedia = undefined;
    expect(readReducedMotion()).toBe(false);
    render(<Probe />);
    expect(screen.getByTestId('rm')).toHaveTextContent('false');
  });

  it('follows the media query live and unsubscribes', () => {
    const listeners = new Set();
    const mq = {
      matches: false,
      media: REDUCED_MOTION_QUERY,
      addEventListener: (_t, fn) => listeners.add(fn),
      removeEventListener: (_t, fn) => listeners.delete(fn),
    };
    window.matchMedia = vi.fn(() => mq);
    const { unmount } = render(<Probe />);
    expect(window.matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)');
    expect(screen.getByTestId('rm')).toHaveTextContent('false');
    act(() => { mq.matches = true; listeners.forEach((fn) => fn()); });
    expect(screen.getByTestId('rm')).toHaveTextContent('true');
    unmount();
    expect(listeners.size).toBe(0);
  });

  it('an old-style MediaQueryList (addListener) works too', () => {
    const listeners = new Set();
    const mq = { matches: true, addListener: (fn) => listeners.add(fn), removeListener: (fn) => listeners.delete(fn) };
    window.matchMedia = () => mq;
    const { unmount } = render(<Probe />);
    expect(screen.getByTestId('rm')).toHaveTextContent('true');
    expect(listeners.size).toBe(1);
    unmount();
    expect(listeners.size).toBe(0);
  });

  it('subscribe = false: no read, no listener, answer false', () => {
    window.matchMedia = vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
    function Off() {
      return <span data-testid="off">{String(useReducedMotion(false))}</span>;
    }
    render(<Off />);
    expect(screen.getByTestId('off')).toHaveTextContent('false');
    expect(window.matchMedia).not.toHaveBeenCalled();
  });
});
