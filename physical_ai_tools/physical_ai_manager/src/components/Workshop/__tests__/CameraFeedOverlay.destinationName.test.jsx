// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Audit `docs/plans/audit-G10-G12.md` §7.5 — there is a THIRD destination-name
// validator and it was the un-improved one. The camera click is the PRIMARY way
// a destination is created, and it answered the bare „Ungültiger Ziel-Name." —
// precisely the sentence the server half of this same feature was written to
// replace. Same round, same feature, two opposite German answers to one
// alphabet.
//
// The click path is driven for real (jsdom <img> + a stubbed layout box), so
// this fails if the wiring is removed as well as if the wording regresses.

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import CameraFeedOverlay from '../CameraFeedOverlay';
import {
  destinationNameErrorDe,
  NAME_ALPHABET_DE,
} from '../blocks/destinations';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

vi.mock('../../../utils/piMode', async () => {
  const actual = await vi.importActual('../../../utils/piMode');
  return { ...actual, usePiMode: () => ({ piMode: false, piModeResolved: true }) };
});

const mockCallService = vi.fn();
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ callService: mockCallService }),
}));

vi.mock('../../../utils/cloudMode', () => ({
  __esModule: true,
  isCloudOnlyMode: () => false,
}));

vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { ros: null },
}));

const errors = [];
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: {
    error: (m) => errors.push(m),
    success: (m) => { errors.push(`SUCCESS:${m}`); },
  },
}));

// The MJPEG <img> is created imperatively and its size arrives via `onload`.
// jsdom reports naturalWidth 0, which short-circuits `handleClick`, so the load
// is simulated with a real 640x480 frame and a real layout box.
async function armTheStream() {
  const img = await screen.findByRole('img');
  Object.defineProperty(img, 'naturalWidth', { value: 640, configurable: true });
  Object.defineProperty(img, 'naturalHeight', { value: 480, configurable: true });
  img.getBoundingClientRect = () => ({
    left: 0, top: 0, width: 640, height: 480, right: 640, bottom: 480,
  });
  await act(async () => { img.onload(); });
  return img;
}

beforeEach(() => {
  errors.length = 0;
  mockCallService.mockReset();
  mockState = { ros: { rosHost: '10.0.0.7' }, workshop: { detections: [] } };
});

describe('CameraFeedOverlay — the destination-name refusal a student reads', () => {
  it('names the offending name and the alphabet instead of the bare sentence', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('Ziel!');
    render(<CameraFeedOverlay camera="scene" clickable data-testid="feed" />);
    await armTheStream();
    fireEvent.click(screen.getByTestId('feed'), { clientX: 320, clientY: 240 });

    await waitFor(() => expect(errors.length).toBe(1));
    // LITERALS, not the symbol under test on both sides.
    expect(errors[0]).toBe(
      'Ungültiger Ziel-Name: „Ziel!". Erlaubt sind Buchstaben (auch ä ö ü ß), '
      + 'Ziffern, Leerzeichen, Unterstrich und Bindestrich.',
    );
    expect(errors[0]).not.toBe('Ungültiger Ziel-Name.');
    expect(mockCallService).not.toHaveBeenCalled();
  });

  it('does not offer a character count the editor contradicts', () => {
    // The Blockly field caps at 24, this prompt and the server regex at 40, so
    // any number in the shared sentence is wrong on a surface that shows it.
    expect(NAME_ALPHABET_DE).toBe(
      'Erlaubt sind Buchstaben (auch ä ö ü ß), Ziffern, Leerzeichen, '
      + 'Unterstrich und Bindestrich.',
    );
  });

  it('refuses a whitespace-only name, which the alphabet regex alone accepts', async () => {
    // '   ' matches /^[A-Za-zÄÖÜäöüß0-9 _-]{1,40}$/ — space IS in the alphabet.
    vi.spyOn(window, 'prompt').mockReturnValue('   ');
    render(<CameraFeedOverlay camera="scene" clickable data-testid="feed" />);
    await armTheStream();
    fireEvent.click(screen.getByTestId('feed'), { clientX: 320, clientY: 240 });
    await waitFor(() => expect(errors.length).toBe(1));
    expect(errors[0]).toContain('Ungültiger Ziel-Name');
    expect(mockCallService).not.toHaveBeenCalled();
  });

  it('sends a padded but valid name TRIMMED, so the block can match the key', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('  Ablage  ');
    mockCallService.mockResolvedValue({
      success: true, message: 'ok', world_x: 0.2, world_y: 0, world_z: 0.01,
    });
    render(<CameraFeedOverlay camera="scene" clickable data-testid="feed" />);
    await armTheStream();
    fireEvent.click(screen.getByTestId('feed'), { clientX: 320, clientY: 240 });
    await waitFor(() => expect(mockCallService).toHaveBeenCalledTimes(1));
    expect(mockCallService.mock.calls[0][2].label).toBe('Ablage');
  });

  it('still accepts the alphabet it always accepted', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('Ablage über Tisch-2_A');
    mockCallService.mockResolvedValue({
      success: true, message: 'ok', world_x: 0.2, world_y: 0, world_z: 0.01,
    });
    render(<CameraFeedOverlay camera="scene" clickable data-testid="feed" />);
    await armTheStream();
    fireEvent.click(screen.getByTestId('feed'), { clientX: 320, clientY: 240 });
    await waitFor(() => expect(mockCallService).toHaveBeenCalledTimes(1));
    expect(errors.filter((e) => !e.startsWith('SUCCESS:'))).toEqual([]);
  });
});

describe('destinationNameErrorDe — the one shared wording', () => {
  it('neutralises a log sentinel and collapses whitespace in the echo', () => {
    expect(destinationNameErrorDe('A\n[FEHLER] x')).toBe(
      'Ungültiger Ziel-Name: „A (FEHLER) x". Erlaubt sind Buchstaben '
      + '(auch ä ö ü ß), Ziffern, Leerzeichen, Unterstrich und Bindestrich.',
    );
  });

  it('caps a pathological paste so the toast stays a sentence', () => {
    const msg = destinationNameErrorDe('x'.repeat(4000));
    expect(msg).toContain(`„${'x'.repeat(40)}"`);
    expect(msg).not.toContain('x'.repeat(41));
  });
});
