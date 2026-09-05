/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// The touch-off defines z_table by FK'ing the arm to the point the student
// presses on the desk, and the FK model puts the TCP at the CLOSED fingertip.
// On a ROTATING claw that point swings BACK as the jaws open (86.25 mm below
// the tool frame closed, 68.8 mm at 0.9 rad open on the Edu:1), so a touch-off
// taught with the claw open measures the table ~17 mm too low and every later
// grasp inherits it. Nothing can GUARD that — the readback is a legal pose — so
// it is an instruction, and it must appear ONLY on an arm it is true for.

import React from 'react';
import { render, screen } from '@testing-library/react';
import TableTouchStep from '../TableTouchStep';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => vi.fn(),
}));

vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});

vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({
    calibrationCapture: vi.fn(),
    calibrationSolve: vi.fn(),
    setHandGuide: vi.fn(),
    callService: vi.fn(),
  }),
}));

function setCaps(capabilities) {
  mockState = {
    tasks: { taskStatus: { capabilities } },
    workshop: { framesCaptured: 0, framesRequired: 3, calibError: null },
  };
}

const CLAW_LINE = /Greifer ganz schließen/;

describe('TableTouchStep — close-the-claw instruction', () => {
  it('is shown on a rotating-claw manifest', () => {
    setCaps({ urdf_asset_id: 'edu1', tool_tip_tracks_gripper: true });
    render(<TableTouchStep />);
    expect(screen.getByText(CLAW_LINE)).toBeTruthy();
  });

  it('is HIDDEN on every parallel-jaw arm', () => {
    for (const caps of [
      null,                                   // pre-first-tick / cloud mode
      undefined,
      {},                                     // an OLD server: no such key
      { urdf_asset_id: 'omx_f' },
      { urdf_asset_id: 'edu6', tool_tip_tracks_gripper: false },
    ]) {
      setCaps(caps);
      const { unmount } = render(<TableTouchStep />);
      expect(screen.queryByText(CLAW_LINE)).toBeNull();
      unmount();
    }
  });

  it('does not replace the shared instructions, it adds to them', () => {
    setCaps({ urdf_asset_id: 'edu1', tool_tip_tracks_gripper: true });
    render(<TableTouchStep />);
    expect(screen.getByText(/mindestens 3 verschiedenen Stellen/)).toBeTruthy();
    expect(screen.getByText(/senkrecht nach/)).toBeTruthy();
  });
});
