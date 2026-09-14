// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Audit `docs/plans/audit-G10-G12.md` §7.5 — there is a THIRD destination-name
// validator and it was the un-improved one. The camera click is the PRIMARY way
// a destination is created, and it answered the bare „Ungültiger Ziel-Name." —
// precisely the sentence the server half of this same feature was written to
// replace.
//
// Since the Sammlung round the click no longer PROMPTS: the page hands the
// label over through `resolveMarkLabel` (the selected pin block's own name, or
// an automatic „Ziel n"), the overlay still validates it through the shared
// sentence before any service call, and a newly created Ziel opens an inline
// rename field at the click point. `window.prompt` is spied in every test and
// must never be called.
//
// The click path is driven for real (jsdom <img> + a stubbed layout box), so
// this fails if the wiring is removed as well as if the wording regresses.

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

let promptSpy;
beforeEach(() => {
  errors.length = 0;
  mockCallService.mockReset();
  mockState = { ros: { rosHost: '10.0.0.7' }, workshop: { detections: [] } };
  promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('Vom Prompt');
});

afterEach(() => {
  // The whole point of this round: the click never asks through a prompt.
  expect(promptSpy).not.toHaveBeenCalled();
  promptSpy.mockRestore();
});

const OK = { success: true, message: 'ok', world_x: 0.2, world_y: 0, world_z: 0.01 };

async function clickFeed(props) {
  render(<CameraFeedOverlay camera="scene" clickable data-testid="feed" {...props} />);
  await armTheStream();
  fireEvent.click(screen.getByTestId('feed'), { clientX: 320, clientY: 240 });
}

describe('CameraFeedOverlay — the destination-name refusal a student reads', () => {
  it('names the offending name and the alphabet instead of the bare sentence', async () => {
    await clickFeed({ resolveMarkLabel: () => ({ label: 'Ziel!', target: 'block', blockId: 'b1' }) });

    await waitFor(() => expect(errors.length).toBe(1));
    // LITERALS, not the symbol under test on both sides.
    expect(errors[0]).toBe(
      'Ungültiger Ziel-Name: „Ziel!". Erlaubt sind Buchstaben (auch ä ö ü ß), '
      + 'Ziffern, Leerzeichen, Unterstrich und Bindestrich.',
    );
    expect(errors[0]).toBe(destinationNameErrorDe('Ziel!'));
    expect(mockCallService).not.toHaveBeenCalled();
  });

  it('does not offer a character count the editor contradicts', () => {
    // The Blockly field caps at 24, the camera label and the server regex at
    // 40, so any number in the shared sentence is wrong on a surface that shows it.
    expect(NAME_ALPHABET_DE).toBe(
      'Erlaubt sind Buchstaben (auch ä ö ü ß), Ziffern, Leerzeichen, '
      + 'Unterstrich und Bindestrich.',
    );
  });

  it('refuses a whitespace-only name, which the alphabet regex alone accepts', async () => {
    // '   ' matches /^[A-Za-zÄÖÜäöüß0-9 _-]{1,40}$/ — space IS in the alphabet.
    await clickFeed({ resolveMarkLabel: () => ({ label: '   ', target: 'store' }) });
    await waitFor(() => expect(errors.length).toBe(1));
    expect(errors[0]).toContain('Ungültiger Ziel-Name');
    expect(mockCallService).not.toHaveBeenCalled();
  });

  it('sends a padded but valid name TRIMMED, so the block can match the key', async () => {
    mockCallService.mockResolvedValue(OK);
    await clickFeed({ resolveMarkLabel: () => ({ label: '  Ablage  ', target: 'block', blockId: 'b1' }) });
    await waitFor(() => expect(mockCallService).toHaveBeenCalledTimes(1));
    expect(mockCallService.mock.calls[0][2].label).toBe('Ablage');
  });

  it('still accepts the alphabet it always accepted', async () => {
    mockCallService.mockResolvedValue(OK);
    await clickFeed({ resolveMarkLabel: () => ({ label: 'Ablage über Tisch-2_A', target: 'store' }) });
    await waitFor(() => expect(mockCallService).toHaveBeenCalledTimes(1));
    expect(mockCallService.mock.calls[0][2].label).toBe('Ablage über Tisch-2_A');
    expect(errors).toEqual([]);
  });

  it('a resolver answering null aborts before any service call', async () => {
    const resolveMarkLabel = vi.fn(() => null);
    await clickFeed({ resolveMarkLabel });
    await act(async () => { await Promise.resolve(); });
    expect(resolveMarkLabel).toHaveBeenCalledTimes(1);
    expect(mockCallService).not.toHaveBeenCalled();
    expect(errors).toEqual([]);
  });

  it('hands onMark the intent with the TRIMMED label and the world point, and toasts nothing itself', async () => {
    mockCallService.mockResolvedValue(OK);
    const onMark = vi.fn(() => null);
    await clickFeed({
      resolveMarkLabel: () => ({ label: ' A ', target: 'block', blockId: 'b7' }),
      onMark,
    });
    await waitFor(() => expect(onMark).toHaveBeenCalledTimes(1));
    expect(onMark).toHaveBeenCalledWith({
      label: 'A', target: 'block', blockId: 'b7', world_x: 0.2, world_y: 0, world_z: 0.01,
    });
    expect(errors).toEqual([]);
    // No rename field for a block target.
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});

describe('CameraFeedOverlay — inline rename of a new Ziel', () => {
  async function openRename(extra = {}) {
    mockCallService.mockResolvedValue(OK);
    const onRenameMark = vi.fn(() => ({ ok: true }));
    await clickFeed({
      resolveMarkLabel: () => ({ label: 'Ziel 1', target: 'store' }),
      onMark: () => ({ entryId: 'd_00000001', name: 'Ziel 1' }),
      onRenameMark,
      ...extra,
    });
    const field = await screen.findByRole('textbox', { name: 'Ziel umbenennen' });
    return { field, onRenameMark: extra.onRenameMark || onRenameMark };
  }

  it('opens a field prefilled with the new name, labelled for a screen reader', async () => {
    const { field } = await openRename();
    expect(field).toHaveValue('Ziel 1');
    expect(field).toHaveAttribute('aria-label', 'Ziel umbenennen');
    expect(field).toHaveAttribute('title', 'Enter speichert den Namen, Esc behält ihn.');
    expect(field).toHaveFocus();
  });

  it('sanitises like the Blockly field: „Kiste!" shows „Kiste"', async () => {
    const { field } = await openRename();
    await userEvent.clear(field);
    await userEvent.type(field, 'Kiste!');
    expect(field).toHaveValue('Kiste');
  });

  it('Enter renames the entry and closes the field', async () => {
    const { field, onRenameMark } = await openRename();
    await userEvent.clear(field);
    await userEvent.type(field, 'Kiste!{Enter}');
    expect(onRenameMark).toHaveBeenCalledWith('d_00000001', 'Kiste');
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('a refused rename keeps the field open', async () => {
    const onRenameMark = vi.fn(() => ({ ok: false, error: 'Der Name „Ablage" ist schon vergeben.' }));
    const { field } = await openRename({ onRenameMark });
    await userEvent.clear(field);
    await userEvent.type(field, 'Ablage{Enter}');
    expect(onRenameMark).toHaveBeenCalledWith('d_00000001', 'Ablage');
    expect(screen.getByRole('textbox', { name: 'Ziel umbenennen' })).toBeInTheDocument();
  });

  it('Escape closes without renaming', async () => {
    const { field, onRenameMark } = await openRename();
    await userEvent.type(field, '{Escape}');
    expect(onRenameMark).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('blur closes without renaming', async () => {
    const { field, onRenameMark } = await openRename();
    act(() => { field.blur(); });
    expect(onRenameMark).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('a click inside the field does not trigger a second service call', async () => {
    const { field } = await openRename();
    fireEvent.pointerDown(field);
    fireEvent.click(field, { clientX: 320, clientY: 240 });
    await act(async () => { await Promise.resolve(); });
    expect(mockCallService).toHaveBeenCalledTimes(1);
  });

  it('while the field is open, a click on the image marks nothing', async () => {
    await openRename();
    fireEvent.click(screen.getByTestId('feed'), { clientX: 100, clientY: 100 });
    await act(async () => { await Promise.resolve(); });
    expect(mockCallService).toHaveBeenCalledTimes(1);
  });
});

describe('CameraFeedOverlay — one service call per mark', () => {
  it('a second click while the first call is still pending marks nothing', async () => {
    let resolveCall;
    mockCallService.mockImplementation(() => new Promise((r) => { resolveCall = r; }));
    const resolveMarkLabel = vi.fn(() => ({ label: 'Ziel 1', target: 'store' }));
    await clickFeed({ resolveMarkLabel, onMark: () => null });
    fireEvent.click(screen.getByTestId('feed'), { clientX: 100, clientY: 100 });
    expect(mockCallService).toHaveBeenCalledTimes(1);
    expect(resolveMarkLabel).toHaveBeenCalledTimes(1);
    await act(async () => { resolveCall(OK); });
    // Once answered, the next click marks again.
    fireEvent.click(screen.getByTestId('feed'), { clientX: 100, clientY: 100 });
    await waitFor(() => expect(mockCallService).toHaveBeenCalledTimes(2));
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
