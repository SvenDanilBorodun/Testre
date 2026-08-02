/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Type-aware LeaderToggle self-hide: even when the GUI :8769 bridge answers the
// status probe (available === true), the toggle must render NOTHING on a
// leader-less profile (capabilities.has_leader === false). omx_full / undefined
// caps (pre-capability image, pre-first-tick) still render (`=== false` only).

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import LeaderToggle from '../LeaderToggle';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});

// The readiness effect only touches this while `preparing` — never in these
// tests — so a bare stub suffices.
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { ros: null },
}));

vi.mock('roslib', () => ({
  __esModule: true,
  default: { Topic: function () { this.subscribe = () => {}; this.unsubscribe = () => {}; } },
}));

function setCaps(capabilities) {
  mockState = { tasks: { taskStatus: { phase: 0, capabilities } } };
}

beforeEach(() => {
  // The status probe succeeds → available becomes true (the interesting case:
  // a working bridge that must STILL be hidden on a leader-less rig).
  global.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({ follower_only: false, busy: false }) })
  );
});

describe('LeaderToggle — has_leader capability hide', () => {
  it('renders the toggle when caps are undefined (pre-capability image)', async () => {
    setCaps(undefined);
    render(<LeaderToggle isActive />);
    expect(await screen.findByText(/Leader abschalten/)).toBeTruthy();
  });

  it('renders the toggle when has_leader is true (omx_full)', async () => {
    setCaps({ has_leader: true });
    render(<LeaderToggle isActive />);
    expect(await screen.findByText(/Leader abschalten/)).toBeTruthy();
  });

  it('renders NOTHING when has_leader is false (omx_follower), even with a live bridge', async () => {
    setCaps({ has_leader: false });
    const { container } = render(<LeaderToggle isActive />);
    // Let the status probe resolve (proves available would be true).
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByText(/Leader abschalten/)).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });
});

// ── the Pi's second hide: the control bridge's own answer ───────────────────
//
// On Windows a follower-only rig NEVER constructs the :8769 bridge
// (gui_app._start_rs_control_server), so the probe fails and the component is
// already hidden. On an Orange Pi the identical contract is served by the
// always-on agent, so the probe ALWAYS succeeds — leaving a window between
// mount and the first /task/status capability tick in which `caps` is undefined
// and this toggle would render on a rig that has no leader arm at all. The
// agent therefore reports `has_leader` on /roboter-studio/status, and the
// Windows bridge simply omits it.

function bridgeStatus(body) {
  global.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
  );
}

describe('LeaderToggle — has_leader from the control bridge (Pi)', () => {
  it('renders NOTHING when the bridge says has_leader:false, with caps still unknown', async () => {
    setCaps(undefined); // the pre-first-tick window this closes
    bridgeStatus({ follower_only: true, busy: false, has_leader: false });
    const { container } = render(<LeaderToggle isActive />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(screen.queryByText(/Leader verbinden/)).toBeNull();
  });

  it('renders NOTHING even when the ROS caps disagree and say has_leader:true', async () => {
    // Stale capabilities from a previous rig must not un-hide it.
    setCaps({ has_leader: true });
    bridgeStatus({ follower_only: true, busy: false, has_leader: false });
    const { container } = render(<LeaderToggle isActive />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('renders when the bridge OMITS the key (Windows GUI bridge — unchanged)', async () => {
    setCaps(undefined);
    bridgeStatus({ follower_only: false, busy: false });
    render(<LeaderToggle isActive />);
    expect(await screen.findByText(/Leader abschalten/)).toBeTruthy();
  });

  it('renders when the bridge says has_leader:true (omx_full on a Pi)', async () => {
    setCaps(undefined);
    bridgeStatus({ follower_only: false, busy: false, has_leader: true });
    render(<LeaderToggle isActive />);
    expect(await screen.findByText(/Leader abschalten/)).toBeTruthy();
  });
});
