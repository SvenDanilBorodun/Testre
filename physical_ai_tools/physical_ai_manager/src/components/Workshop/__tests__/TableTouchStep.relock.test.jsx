/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// „Tisch vermessen" used to end with the arm SNAPPING to its home pose. The
// server now re-locks it in place (tip still on the table); the step then offers
// the warned glide. Under the page provider the offer must outlive this step,
// because the same success advances the wizard and unmounts it.

import React, { useState } from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TableTouchStep from '../TableTouchStep';
import { HomeGlideProvider } from '../HomeGlidePrompt';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const mockRos = vi.hoisted(() => ({
  startCalibration: vi.fn(() => Promise.resolve({ success: true, message: 'Arm frei.' })),
  calibrationCaptureFrame: vi.fn(),
  calibrationSolve: vi.fn(),
  cancelCalibration: vi.fn(() => Promise.resolve({ success: true })),
  homeArm: vi.fn(() => Promise.resolve({ success: true })),
  handGuide: vi.fn(() => Promise.resolve({ success: true })),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

beforeEach(() => {
  mockState = {
    tasks: { taskStatus: { capabilities: null } },
    workshop: { framesCaptured: 4, framesRequired: 4, calibError: null },
  };
  mockDispatch.mockClear();
  mockRos.calibrationSolve.mockReset();
  mockRos.homeArm.mockClear();
});

async function startAndSolve() {
  await userEvent.click(screen.getByRole('button', { name: 'Tischvermessung starten' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Berechnen & speichern' }));
}

describe('TableTouchStep — after the solve', () => {
  it('a successful solve offers the warned glide instead of moving the arm', async () => {
    mockRos.calibrationSolve.mockResolvedValue({ success: true, message: 'Tisch gespeichert.' });
    render(<TableTouchStep />);
    await startAndSolve();
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
    expect(mockRos.homeArm).not.toHaveBeenCalled();
  });

  it('a failed solve (arm still limp, taps continue) offers nothing', async () => {
    mockRos.calibrationSolve.mockResolvedValue({ success: false, message: 'Zu wenige Punkte.' });
    render(<TableTouchStep />);
    await startAndSolve();
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('under the page provider the warning survives the step being unmounted by the wizard', async () => {
    mockRos.calibrationSolve.mockResolvedValue({ success: true, message: 'Tisch gespeichert.' });
    let advanceWizard;
    function Wizard() {
      const [step, setStep] = useState('table_touch');
      advanceWizard = () => setStep('accuracy_verify');
      return (
        <HomeGlideProvider>
          {step === 'table_touch' ? <TableTouchStep /> : <p>Genauigkeit prüfen</p>}
        </HomeGlideProvider>
      );
    }
    render(<Wizard />);
    await startAndSolve();
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
    act(() => advanceWizard());
    expect(screen.getByText('Genauigkeit prüfen')).toBeInTheDocument();
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });
});
