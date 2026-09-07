/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// „Roboter aktivieren" — the only control in the product that puts a physical
// arm in motion from the Startseite, so its refusals matter more than its
// happy path.
//
// The agent refuses only a CONCURRENT activation; „not in the middle of a
// recording" and „not while a Roboter-Studio program runs" are decisions this
// client owns, exactly the position LeaderToggle documents for its own switch.
// If these tests go, that protection goes with them.

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

import ActivationCard, {
  blockReason, idleBody, pillTone, pillLabel, resolveHasLeader,
} from '../ActivationCard';

let mockActivation;
const activate = vi.fn(() => Promise.resolve(true));
vi.mock('../../../hooks/useRobotActivation', async () => {
  const actual = await vi.importActual('../../../hooks/useRobotActivation');
  return {
    ...actual,
    __esModule: true,
    default: () => mockActivation,
  };
});

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

function setUp({ status = null, calling = false, error = null,
  connected = true, running = false, workflowRunning = false,
  jetson = false, caps = null, calibration = null } = {}) {
  mockActivation = { status, activate, calling, error };
  mockState = {
    tasks: {
      heartbeatStatus: connected ? 'connected' : 'disconnected',
      taskStatus: { running, capabilities: caps },
    },
    workshop: {
      runState: workflowRunning ? 'running' : 'idle',
      paused: false,
      calibration: calibration || { framesCaptured: 0, calibrated: false },
    },
    jetson: { status: jetson ? 'connected' : 'idle' },
  };
}

beforeEach(() => {
  activate.mockClear();
  setUp();
});

describe('ActivationCard — the button', () => {
  it('offers activation even when the agent has not answered yet', () => {
    // status === null is UNKNOWN, not „not activated". An older server image
    // has no agent at all; greying the button out would strand that rig.
    setUp({ status: null });
    render(<ActivationCard />);
    const btn = screen.getByRole('button', { name: 'Roboter aktivieren' });
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
    expect(screen.getByText('unbekannt')).toBeInTheDocument();
  });

  it('calls the service on click', () => {
    setUp({ status: { state: 'idle', hasLeader: false, required: true, message: '' } });
    render(<ActivationCard />);
    fireEvent.click(screen.getByRole('button', { name: 'Roboter aktivieren' }));
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it('is refused while a recording or an inference runs', () => {
    setUp({ status: { state: 'idle', hasLeader: true, required: true }, running: true });
    render(<ActivationCard />);
    expect(screen.getByRole('button', { name: 'Roboter aktivieren' })).toBeDisabled();
    expect(screen.getByText(/Aufnahme oder eine Inferenz/)).toBeInTheDocument();
  });

  it('is refused while a Roboter-Studio program runs', () => {
    setUp({ status: { state: 'idle', hasLeader: true, required: true }, workflowRunning: true });
    render(<ActivationCard />);
    expect(screen.getByRole('button', { name: 'Roboter aktivieren' })).toBeDisabled();
    expect(screen.getByText(/Roboter Studio läuft gerade ein Programm/)).toBeInTheDocument();
  });

  it('is refused with no bridge', () => {
    setUp({ status: null, connected: false });
    render(<ActivationCard />);
    expect(screen.getByRole('button', { name: 'Roboter aktivieren' })).toBeDisabled();
    expect(screen.getByText(/Keine Verbindung zum Roboter/)).toBeInTheDocument();
  });

  it('shows progress and disables itself while the sequence runs', () => {
    setUp({ status: { state: 'activating', step: 'home', hasLeader: true, required: true, message: 'Der Roboter fährt in die Grundstellung …' } });
    render(<ActivationCard />);
    const btn = screen.getByRole('button', { name: /wird aktiviert/ });
    expect(btn).toBeDisabled();
    expect(screen.getByText('Der Roboter fährt in die Grundstellung …')).toBeInTheDocument();
  });

  it('offers NO re-home once teleop owns the arm', () => {
    // The leader broadcaster republishes its pose at 100 Hz onto the very rail
    // a home trajectory would use, so a re-home there is overwritten within
    // 10 ms and the follower snaps back. The agent refuses it too; this is the
    // half that keeps the button from ever suggesting it.
    setUp({ status: { state: 'active', hasLeader: true, required: true, message: '' } });
    render(<ActivationCard />);
    expect(screen.queryByRole('button', { name: 'Grundstellung erneut anfahren' }))
      .toBeNull();
    expect(screen.getByText(/führst du den Leader-Arm von Hand/)).toBeInTheDocument();
    expect(screen.getByText(/folgt dem Leader-Arm/)).toBeInTheDocument();
    expect(screen.getByText('aktiv')).toBeInTheDocument();
  });

  it('DOES offer a re-home on a follower-only rig, where nothing else owns the rail', () => {
    setUp({ status: { state: 'active', hasLeader: false, required: true, message: '' } });
    render(<ActivationCard />);
    expect(screen.getByRole('button', { name: 'Grundstellung erneut anfahren' }))
      .toBeInTheDocument();
    expect(screen.queryByText(/Leader-Arm/)).toBeNull();
    // Deliberately NOT „…und steht in der Grundstellung": arrival is verified
    // softly and an incomplete home still ends in STATE_ACTIVE, so claiming
    // the pose would be a claim the page cannot back up.
    expect(screen.getByText('Der Roboter ist aktiv.')).toBeInTheDocument();
    expect(screen.queryByText(/steht in der Grundstellung/)).toBeNull();
  });

  it('does not show a green „aktiv" over a home that did not arrive', () => {
    // _home_omx returns (True, MSG_HOME_INCOMPLETE) when arrival_decision
    // fails, so the sequence reaches STATE_ACTIVE with a warning attached. A
    // green pill there is exactly the claim this page forbids.
    setUp({
      status: {
        state: 'active', hasLeader: false, required: true,
        message: '[WARNUNG] Der Roboter hat die Grundstellung nicht ganz erreicht.',
      },
    });
    render(<ActivationCard />);
    expect(screen.getByText('aktiv mit Hinweis')).toBeInTheDocument();
    expect(screen.queryByText('aktiv')).toBeNull();
    expect(pillTone('active', false, true)).toBe('amber');
  });

  it('never shows the log tags to the student', () => {
    // [FEHLER]/[WARNUNG] are this codebase's LOG convention — literally what
    // german-strings-lint greps for. No other student-facing SPA string shows
    // them; these are stripped with the Health-Check's own helper.
    setUp({
      status: {
        state: 'failed', hasLeader: false, required: true,
        message: '[FEHLER] Vom Roboterarm kommen keine Gelenkdaten.',
      },
    });
    render(<ActivationCard />);
    expect(screen.getByText('Vom Roboterarm kommen keine Gelenkdaten.'))
      .toBeInTheDocument();
    expect(screen.queryByText(/\[FEHLER\]/)).toBeNull();
  });

  it('is NOT gated on the latching calibration signal', () => {
    // selectCalibrationHasUnsolvedCaptures latches and its own docstring says
    // the caller must also establish that the wizard is on screen — which is
    // never true on the Startseite. Wiring it here would kill the button for
    // good after one abandoned wizard. Pinned so nobody "fixes" it back.
    setUp({
      status: { state: 'idle', hasLeader: false, required: true },
      calibration: { framesCaptured: 3, calibrated: false },
    });
    render(<ActivationCard />);
    expect(screen.getByRole('button', { name: 'Roboter aktivieren' }))
      .not.toBeDisabled();
  });

  it("surfaces the agent's own German failure reason", () => {
    setUp({
      status: {
        state: 'failed', hasLeader: false, required: true,
        message: '[FEHLER] Vom Roboterarm kommen keine Gelenkdaten.',
      },
    });
    render(<ActivationCard />);
    expect(screen.getByText(/keine Gelenkdaten/)).toBeInTheDocument();
    expect(screen.getByText('fehlgeschlagen')).toBeInTheDocument();
    // …and a retry is still offered.
    expect(screen.getByRole('button', { name: 'Roboter aktivieren' })).not.toBeDisabled();
  });

  it("prefers the client's own error when it could not reach the agent at all", () => {
    setUp({ status: { state: 'idle', hasLeader: false, required: true, message: 'x' }, error: 'Der Roboter konnte nicht erreicht werden. Läuft die Umgebung noch?' });
    render(<ActivationCard />);
    expect(screen.getByText(/konnte nicht erreicht werden/)).toBeInTheDocument();
    expect(screen.queryByText('x')).toBeNull();
  });
});

describe('ActivationCard — the Jetson owns its own lifecycle', () => {
  it('renders nothing at all while a classroom Jetson is claimed', () => {
    // The Jetson agent homes the follower on every CLAIM — itself a logged-in
    // student's action — and its compose keeps the stack up. Offering a second
    // activation here would report „nicht aktiv" over an already-homed arm.
    setUp({ status: null, jetson: true });
    const { container } = render(<ActivationCard />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('ActivationCard — pure helpers', () => {
  it('names the whole sequence, in order, on a leader rig', () => {
    const body = idleBody(true);
    expect(body.indexOf('Grundstellung')).toBeLessThan(body.indexOf('Leader-Arm an'));
    expect(body).toMatch(/erst danach folgt er deinen Bewegungen/);
  });

  it('never mentions a leader on a follower-only rig', () => {
    expect(idleBody(false)).not.toMatch(/Leader/);
    expect(idleBody(false)).toMatch(/Grundstellung/);
  });

  it('asks the capability manifest when the agent did not say', () => {
    // The old default was an unchecked `true`. `has_leader` is on /task/status
    // and is a measured fact.
    expect(resolveHasLeader({ hasLeader: null }, { has_leader: false })).toBe(false);
    expect(resolveHasLeader({ hasLeader: null }, { has_leader: true })).toBe(true);
    // The agent's own answer still wins when it gave one.
    expect(resolveHasLeader({ hasLeader: false }, { has_leader: true })).toBe(false);
    // Neither answered → assume a leader, which HIDES the re-home rather than
    // offering a home into a rail the broadcaster owns.
    expect(resolveHasLeader({ hasLeader: null }, null)).toBe(true);
    expect(resolveHasLeader(null, null)).toBe(true);
  });

  it('orders the refusals so the most specific one wins', () => {
    expect(blockReason({ connected: false, taskRunning: true })).toMatch(/Keine Verbindung/);
    expect(blockReason({ connected: true, taskRunning: true, busy: true })).toMatch(/Aufnahme/);
    expect(blockReason({ connected: true, workflowRunning: true, busy: true }))
      .toMatch(/Roboter Studio/);
    expect(blockReason({ connected: true })).toBeNull();
  });

  it('renders UNKNOWN as neutral, never as a finding', () => {
    // amber on this page means „we looked and found something".
    expect(pillTone(null, false)).toBe('neutral');
    expect(pillLabel(null, false)).toBe('unbekannt');
    expect(pillTone('idle', false)).toBe('amber');
    expect(pillLabel('idle', false)).toBe('nicht aktiv');
    expect(pillTone('active', false)).toBe('success');
    expect(pillTone('failed', false)).toBe('danger');
  });
});
