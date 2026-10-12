/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// ToolbarButtons (R0-1, R0-2): in a code document the undo/redo buttons act on
// the CodeMirror history through a ref and the Blockly-only tools are not
// rendered; the right end shows the CLOUD save status.

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import ToolbarButtons from '../ToolbarButtons';
import { DE } from '../blocks/messages_de';

function makeWorkspace() {
  return { undo: vi.fn(), zoomToFit: vi.fn() };
}

const press = (init) => { window.dispatchEvent(new KeyboardEvent('keydown', { ctrlKey: true, ...init })); };

describe('ToolbarButtons — Blockly mode', () => {
  test('offers every tool', () => {
    render(<ToolbarButtons workspace={makeWorkspace()} onSave={() => {}} />);
    [DE.TOOLBAR_UNDO, DE.TOOLBAR_REDO, DE.TOOLBAR_ZOOM_FIT, DE.TOOLBAR_SAVE, DE.TOOLBAR_EXPORT, DE.TOOLBAR_IMPORT]
      .forEach((name) => expect(screen.getByRole('button', { name })).toBeInTheDocument());
    expect(screen.getByRole('combobox', { name: DE.TOOLBAR_THEME })).toBeInTheDocument();
  });

  test('the buttons and Strg+Y / Strg+Umschalt+Z act on the workspace history', () => {
    const ws = makeWorkspace();
    render(<ToolbarButtons workspace={ws} onSave={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_UNDO }));
    expect(ws.undo).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_REDO }));
    expect(ws.undo).toHaveBeenLastCalledWith(true);
    ws.undo.mockClear();
    press({ key: 'y' });
    press({ key: 'Z', shiftKey: true });
    expect(ws.undo.mock.calls).toEqual([[true], [true]]);
  });

  test('Strg+S saves', () => {
    const onSave = vi.fn();
    render(<ToolbarButtons workspace={makeWorkspace()} onSave={onSave} />);
    press({ key: 's' });
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});

describe('ToolbarButtons — a code document', () => {
  function setup(commands = { undo: vi.fn(), redo: vi.fn() }) {
    const codeCommandsRef = { current: commands };
    const onSave = vi.fn();
    render(<ToolbarButtons workspace={null} codeMode codeCommandsRef={codeCommandsRef} onSave={onSave} />);
    return { commands, codeCommandsRef, onSave };
  }

  test('„Ansicht anpassen", „Exportieren", „Importieren" and the theme are not rendered', () => {
    setup();
    expect(screen.queryByRole('button', { name: DE.TOOLBAR_ZOOM_FIT })).toBeNull();
    expect(screen.queryByRole('button', { name: DE.TOOLBAR_EXPORT })).toBeNull();
    expect(screen.queryByRole('button', { name: DE.TOOLBAR_IMPORT })).toBeNull();
    expect(screen.queryByRole('combobox', { name: DE.TOOLBAR_THEME })).toBeNull();
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.querySelector('input[type="file"]')).toBeNull();
    // Undo, redo and save stay.
    expect(screen.getByRole('button', { name: DE.TOOLBAR_UNDO })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: DE.TOOLBAR_REDO })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: DE.TOOLBAR_SAVE })).toBeInTheDocument();
  });

  test('„Rückgängig" / „Wiederholen" call the CodeMirror handle', () => {
    const { commands } = setup();
    fireEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_UNDO }));
    expect(commands.undo).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_REDO }));
    expect(commands.redo).toHaveBeenCalledTimes(1);
  });

  test('without a handle yet (the editor still loading) the buttons do nothing and nothing throws', () => {
    setup(null);
    expect(() => fireEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_UNDO }))).not.toThrow();
    expect(() => fireEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_REDO }))).not.toThrow();
  });

  test('Strg+Y and Strg+Umschalt+Z do nothing on the window (m12); Strg+S still saves', () => {
    const { commands, onSave } = setup();
    const y = new KeyboardEvent('keydown', { key: 'y', ctrlKey: true, cancelable: true });
    window.dispatchEvent(y);
    const z = new KeyboardEvent('keydown', { key: 'Z', ctrlKey: true, shiftKey: true, cancelable: true });
    window.dispatchEvent(z);
    expect(commands.redo).not.toHaveBeenCalled();
    expect(y.defaultPrevented).toBe(false);
    expect(z.defaultPrevented).toBe(false);
    press({ key: 's' });
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});

describe('ToolbarButtons — the save status', () => {
  test.each([
    [{ savedAt: null, dirty: false }, 'Noch nicht gespeichert'],
    [{ savedAt: Date.now(), dirty: false }, 'Gespeichert: gerade eben'],
    [{ savedAt: Date.now() - 5 * 60 * 1000, dirty: false }, 'Gespeichert: vor 5 Min.'],
    [{ savedAt: Date.now(), dirty: true }, 'Nicht gespeichert'],
  ])('%j reads „%s"', (saveStatus, text) => {
    render(<ToolbarButtons workspace={null} saveStatus={saveStatus} onSave={() => {}} />);
    const status = screen.getByTestId('save-status');
    expect(status).toHaveTextContent(text);
    expect(status).toHaveAttribute('aria-live', 'polite');
  });

  test('no status at all reads „Noch nicht gespeichert" (never the local autosave)', () => {
    render(<ToolbarButtons workspace={null} onSave={() => {}} />);
    expect(screen.getByTestId('save-status')).toHaveTextContent('Noch nicht gespeichert');
    expect(screen.queryByText(/Letzte Speicherung/)).toBeNull();
  });
});
