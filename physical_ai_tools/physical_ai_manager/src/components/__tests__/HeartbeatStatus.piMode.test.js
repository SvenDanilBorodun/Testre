// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Network-aware disconnect wording (Pi mode): when the agent REPORTS the robot
// tier running but rosbridge (:9090) is unreachable from the browser, the
// „Getrennt" pill must name the port-9090/network cause — never the Windows-era
// „Docker prüfen". Non-Pi must stay byte-identical (bare pill, no hint).

import React from 'react';
import { render, screen } from '@testing-library/react';
import HeartbeatStatus from '../HeartbeatStatus';

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
  PI_PORT_BLOCKED_HINT: 'Verbindung zu Port 9090 blockiert? Netzwerk-Anleitung prüfen',
}));

beforeEach(() => {
  mockState = { tasks: { heartbeatStatus: 'disconnected', lastHeartbeatTime: 0 } };
  mockPi = { piMode: false, agentStatus: null };
});

describe('HeartbeatStatus — Pi network-aware disconnect', () => {
  it('shows the plain „Getrennt" pill and NO port hint outside Pi mode', () => {
    render(<HeartbeatStatus />);
    expect(screen.getByText('Getrennt')).toBeInTheDocument();
    expect(screen.queryByText(/Port 9090 blockiert/)).not.toBeInTheDocument();
  });

  it('adds the port-9090 network hint when the Pi robot tier is up but rosbridge is unreachable', () => {
    mockPi = { piMode: true, agentStatus: { robot_tier_up: true } };
    render(<HeartbeatStatus />);
    expect(screen.getByText('Getrennt')).toBeInTheDocument();
    expect(screen.getByText(/Port 9090 blockiert/)).toBeInTheDocument();
  });

  it('does NOT show the hint when the Pi robot tier is down (fresh boot / stopped)', () => {
    mockPi = { piMode: true, agentStatus: { robot_tier_up: false } };
    render(<HeartbeatStatus />);
    expect(screen.queryByText(/Port 9090 blockiert/)).not.toBeInTheDocument();
  });
});
