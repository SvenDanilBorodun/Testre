/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The warned glide to the Grundstellung. The arm used to JUMP home after
// „Tisch vermessen" and „Arm festsetzen"; the server now re-locks in place and
// this dialog is the ONLY way the arm is sent home afterwards. Pinned: nothing
// moves before the countdown ends (or „Jetzt fahren"), „Hier stehen lassen"
// never moves, exactly one glide per offer, „Stopp" is the manual abort, and a
// page-level provider keeps the countdown alive when the caller unmounts.

import React, { useEffect } from 'react';
import { render, screen, act, fireEvent, within } from '@testing-library/react';
import {
  HOME_GLIDE_COUNTDOWN_S,
  HomeGlideProvider,
  useHomeGlide,
} from '../HomeGlidePrompt';

const mockRos = vi.hoisted(() => ({
  homeArm: vi.fn(),
  handGuide: vi.fn(() => Promise.resolve({ success: true })),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

// A caller that offers the glide once on mount (like a successful touch-off).
function Offerer({ offerOnMount = true }) {
  const { offerHomeGlide, homeGlideDialog } = useHomeGlide();
  useEffect(() => {
    if (offerOnMount) offerHomeGlide();
  }, [offerOnMount, offerHomeGlide]);
  return (
    <div>
      <button type="button" onClick={offerHomeGlide}>anbieten</button>
      {homeGlideDialog}
    </div>
  );
}

// The countdown re-arms ONE timeout per rendered second, so time has to be
// advanced a second at a time with React committing in between — one big jump
// would only ever fire the first tick.
async function advance(ms) {
  for (let left = ms; left > 0; left -= 1000) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => {
      vi.advanceTimersByTime(Math.min(1000, left));
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  mockRos.homeArm.mockReset();
  mockRos.homeArm.mockImplementation(() => Promise.resolve({
    success: true, message: 'Der Arm steht in der Grundstellung.',
  }));
  mockRos.handGuide.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('HomeGlidePrompt', () => {
  test('warns first and glides only after the full countdown', async () => {
    render(<Offerer />);
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByText(/fährt gleich in die Grundstellung/)).toBeInTheDocument();
    expect(screen.getByText(`Start in ${HOME_GLIDE_COUNTDOWN_S} Sekunden`)).toBeInTheDocument();

    await advance((HOME_GLIDE_COUNTDOWN_S - 1) * 1000);
    expect(mockRos.homeArm).not.toHaveBeenCalled();
    expect(screen.getByText('Start in 1 Sekunde')).toBeInTheDocument();

    await advance(1000);
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
    await act(async () => { await Promise.resolve(); });
    expect(mockToast.success).toHaveBeenCalledWith('Der Arm steht in der Grundstellung.');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  test('„Hier stehen lassen" never moves the arm', async () => {
    render(<Offerer />);
    fireEvent.click(screen.getByRole('button', { name: 'Hier stehen lassen' }));
    await advance(HOME_GLIDE_COUNTDOWN_S * 3000);
    expect(mockRos.homeArm).not.toHaveBeenCalled();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  test('„Jetzt fahren" starts at once, and the countdown cannot start a second glide', async () => {
    let resolveGlide;
    mockRos.homeArm.mockImplementation(() => new Promise((r) => { resolveGlide = r; }));
    render(<Offerer />);
    fireEvent.click(screen.getByRole('button', { name: 'Jetzt fahren' }));
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
    // While moving the dialog stays up, offering Stopp.
    expect(screen.getByText(/fährt in die Grundstellung …/)).toBeInTheDocument();
    await advance(HOME_GLIDE_COUNTDOWN_S * 3000);
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
    await act(async () => { resolveGlide({ success: true }); });
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  test('a second offer while one is open does not restart or stack it', async () => {
    render(<Offerer />);
    await advance(3000);
    fireEvent.click(screen.getByRole('button', { name: 'anbieten' }));
    expect(screen.getAllByRole('alertdialog')).toHaveLength(1);
    await advance((HOME_GLIDE_COUNTDOWN_S - 3) * 1000);
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
  });

  test('„Stopp" during the glide sends the manual abort and reports a hold, not an error', async () => {
    let resolveGlide;
    mockRos.homeArm.mockImplementation(() => new Promise((r) => { resolveGlide = r; }));
    render(<Offerer />);
    fireEvent.click(screen.getByRole('button', { name: 'Jetzt fahren' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stopp' }));
    await act(async () => { await Promise.resolve(); });
    expect(mockRos.handGuide).toHaveBeenCalledWith(false);
    await act(async () => {
      resolveGlide({ success: false, message: 'Fahrt in die Grundstellung gestoppt — der Arm hält seine aktuelle Position.' });
    });
    expect(mockToast.error).not.toHaveBeenCalled();
    expect(mockToast).toHaveBeenCalledWith(
      'Fahrt in die Grundstellung gestoppt — der Arm hält seine aktuelle Position.',
      expect.anything());
  });

  test('a „Stopp" that LOST the race to the glide reports the arrival, not a stop', async () => {
    let resolveGlide;
    mockRos.homeArm.mockImplementation(() => new Promise((r) => { resolveGlide = r; }));
    render(<Offerer />);
    fireEvent.click(screen.getByRole('button', { name: 'Jetzt fahren' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stopp' }));
    await act(async () => {
      resolveGlide({ success: true, message: 'Der Arm steht in der Grundstellung.' });
    });
    expect(mockToast.success).toHaveBeenCalledWith('Der Arm steht in der Grundstellung.');
    expect(mockToast).not.toHaveBeenCalled();
  });

  test('two starts landing in the SAME tick (countdown end + click) run ONE glide', async () => {
    // The DOM cannot reproduce this — React drops a click on a button it has
    // already removed — so drive the dialog's own start handler twice before
    // React re-renders, which is exactly the same-tick case the guard exists for.
    let resolveGlide;
    mockRos.homeArm.mockImplementation(() => new Promise((r) => { resolveGlide = r; }));
    let latest;
    function Probe() {
      const glide = useHomeGlide();
      latest = glide;
      return null;
    }
    render(<Probe />);
    act(() => { latest.offerHomeGlide(); });
    const { onGoNow } = latest.homeGlideDialog.props;
    await act(async () => {
      onGoNow();
      onGoNow();
    });
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
    await act(async () => { resolveGlide({ success: true }); });
  });

  test('focus starts on „Hier stehen lassen" — a stray Enter never starts the glide', () => {
    render(<Offerer />);
    expect(screen.getByRole('button', { name: 'Hier stehen lassen' })).toHaveFocus();
  });

  test('a refusal from the server is shown in German', async () => {
    mockRos.homeArm.mockImplementation(() => Promise.resolve({
      success: false, message: 'Der Arm steht so, dass der Weg in die Grundstellung durch die Tischebene führen würde.',
    }));
    render(<Offerer />);
    fireEvent.click(screen.getByRole('button', { name: 'Jetzt fahren' }));
    await act(async () => { await Promise.resolve(); });
    expect(mockToast.error).toHaveBeenCalledWith(
      expect.stringContaining('Tischebene'));
  });

  test('WITHOUT a provider, unmounting during the countdown cancels it (nothing moves)', async () => {
    const { unmount } = render(<Offerer />);
    await advance(2000);
    unmount();
    await advance(HOME_GLIDE_COUNTDOWN_S * 2000);
    expect(mockRos.homeArm).not.toHaveBeenCalled();
  });

  test('UNDER a provider, the countdown survives the caller unmounting (the touch-off step switch)', async () => {
    function Page({ showCaller }) {
      return <HomeGlideProvider>{showCaller ? <Offerer /> : <span>Nächster Schritt</span>}</HomeGlideProvider>;
    }
    const { rerender } = render(<Page showCaller />);
    expect(screen.getAllByRole('alertdialog')).toHaveLength(1);
    rerender(<Page showCaller={false} />);
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    await advance(HOME_GLIDE_COUNTDOWN_S * 1000);
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
  });

  test('the dialog is portalled to <body>, so no dock/wizard layout CSS can clip it', () => {
    render(<section aria-label="Aufrufer"><Offerer /></section>);
    // The dialog exists on the page but NOT inside the component that offered
    // it — a portal. Rendered inline, any ancestor with a transform/filter/
    // contain would make its `fixed` overlay relative to that ancestor.
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Aufrufer' }))
      .queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});
