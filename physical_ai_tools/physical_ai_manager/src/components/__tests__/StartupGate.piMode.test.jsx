// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Pi startup-gate suppression rules (audit fix 3): the full-viewport blocking
// overlay may only show when the agent POSITIVELY reports the robot tier up.
// When the agent is unreachable (crashed / mid-restart / gateway not bound),
// its last robot_tier_up snapshot is stale and unknowable — the gate must
// treat that as 'unknown' and stay out of the way so the student can always
// reach the System route (the escape hatch + „Umgebung starten").

import React from 'react';
import { render, screen } from '@testing-library/react';
import StartupGate from '../StartupGate';
import PageType from '../../constants/pageType';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => vi.fn(),
}));

let mockPi;
vi.mock('../../utils/piMode', () => ({
  __esModule: true,
  usePiMode: () => mockPi,
  PI_PORT_BLOCKED_HINT: 'Verbindung zum Roboter blockiert? Netzwerk-Anleitung prüfen',
}));

vi.mock('../../utils/cloudMode', () => ({
  __esModule: true,
  isCloudOnlyMode: () => false,
}));

// rosbridge never connects in these tests — the overlay decision is what's
// under test, not the connect/settle progression.
vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { isConnected: () => false, resetReconnectCounter: vi.fn() },
}));

const OVERLAY_TEXT = 'Roboter wird verbunden...';

function renderGate() {
  return render(
    <StartupGate>
      <div>APP-INHALT</div>
    </StartupGate>
  );
}

beforeEach(() => {
  mockState = { ui: { currentPage: PageType.HOME } };
  mockPi = {
    piMode: true,
    piModeResolved: true,
    agentStatus: { robot_tier_up: true },
    agentReachable: true,
  };
});

describe('StartupGate — Pi overlay suppression (agentReachable)', () => {
  it('blocks while the agent reports the robot tier up and rosbridge is not connected', () => {
    renderGate();
    expect(screen.getByText(OVERLAY_TEXT)).toBeInTheDocument();
  });

  it('suppresses the overlay when the agent is UNREACHABLE, even with a stale robot_tier_up', () => {
    mockPi = { ...mockPi, agentReachable: false };
    renderGate();
    expect(screen.queryByText(OVERLAY_TEXT)).toBeNull();
    expect(screen.getByText('APP-INHALT')).toBeInTheDocument();
  });

  it('suppresses the overlay while the robot tier is down (fresh boot)', () => {
    mockPi = { ...mockPi, agentStatus: { robot_tier_up: false } };
    renderGate();
    expect(screen.queryByText(OVERLAY_TEXT)).toBeNull();
  });

  it('never covers the System tab (the escape hatch)', () => {
    mockState = { ui: { currentPage: PageType.SYSTEM } };
    renderGate();
    expect(screen.queryByText(OVERLAY_TEXT)).toBeNull();
  });
});
