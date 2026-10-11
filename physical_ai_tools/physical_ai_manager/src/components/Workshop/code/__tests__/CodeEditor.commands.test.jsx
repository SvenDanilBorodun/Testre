/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// R0-1 and R0-5 on a REAL EditorView in jsdom:
//   * `commandsRef` gets Rückgängig / Wiederholen for the live view (the
//     toolbar's buttons in code mode — CodeMirror is importable only in
//     CodeEditor.jsx), and loses them when the view goes;
//   * a reveal carrying `flash` lights up exactly the inserted lines and goes
//     dark after INSERT_FLASH_MS.

import React from 'react';
import { render, act } from '@testing-library/react';
import CodeEditor, { INSERT_FLASH_MS } from '../CodeEditor';

let EditorView;

const hadRangeRects = typeof Range !== 'undefined' && typeof Range.prototype.getClientRects === 'function';
beforeAll(async () => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    Range.prototype.getClientRects = function getClientRects() { return []; };
    Range.prototype.getBoundingClientRect = function getBoundingClientRect() {
      return {
        x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0,
      };
    };
  }
  ({ EditorView } = await import('@codemirror/view'));
});
afterAll(() => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    delete Range.prototype.getClientRects;
    delete Range.prototype.getBoundingClientRect;
  }
});

/* eslint-disable testing-library/no-container, testing-library/no-node-access */
const viewOf = (container) => EditorView.findFromDOM(container.querySelector('.cm-editor'));
const flashedLines = (container) => [...container.querySelectorAll('.cm-line.cm-edubotics-insert-flash')]
  .map((el) => el.textContent);
/* eslint-enable testing-library/no-container, testing-library/no-node-access */

const DOC = 'import robot\nrobot.home()\nrobot.lift()\nrobot.beep()\n';

function mount(props = {}) {
  const base = {
    language: 'python', path: 'main.py', value: DOC, onChange: () => {},
  };
  const utils = render(<CodeEditor {...base} {...props} />);
  return {
    ...utils,
    view: viewOf(utils.container),
    show: (next) => utils.rerender(<CodeEditor {...base} {...props} {...next} />),
  };
}

describe('commandsRef', () => {
  test('gets undo and redo for the live view; a typed change goes back and comes again', () => {
    const commandsRef = { current: null };
    const { view } = mount({ commandsRef });
    expect(typeof commandsRef.current.undo).toBe('function');
    expect(typeof commandsRef.current.redo).toBe('function');
    act(() => {
      view.dispatch({ changes: { from: 0, insert: 'x = 1\n' }, userEvent: 'input' });
    });
    expect(view.state.doc.toString()).toBe(`x = 1\n${DOC}`);
    let done;
    act(() => { done = commandsRef.current.undo(); });
    expect(done).toBe(true);
    expect(view.state.doc.toString()).toBe(DOC);
    act(() => { done = commandsRef.current.redo(); });
    expect(done).toBe(true);
    expect(view.state.doc.toString()).toBe(`x = 1\n${DOC}`);
  });

  test('an empty history answers false and changes nothing', () => {
    const commandsRef = { current: null };
    const { view } = mount({ commandsRef });
    expect(commandsRef.current.undo()).toBe(false);
    expect(view.state.doc.toString()).toBe(DOC);
  });

  test('is cleared when the editor goes, and replaced by the next view’s', () => {
    const commandsRef = { current: null };
    const { show, unmount } = mount({ commandsRef });
    const first = commandsRef.current;
    show({ path: 'hilfe.py', value: 'a = 1\n' });
    expect(commandsRef.current).not.toBe(first);
    expect(commandsRef.current).not.toBeNull();
    unmount();
    expect(commandsRef.current).toBeNull();
  });
});

describe('the insert flash', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  test('a reveal with flash lights exactly those lines and clears after the timer', () => {
    const { container, show } = mount();
    expect(flashedLines(container)).toEqual([]);
    show({ revealRequest: { line: 3, nonce: 1, flash: { from: 2, to: 3 } } });
    expect(flashedLines(container)).toEqual(['robot.home()', 'robot.lift()']);
    act(() => { vi.advanceTimersByTime(INSERT_FLASH_MS - 1); });
    expect(flashedLines(container)).toHaveLength(2);
    act(() => { vi.advanceTimersByTime(1); });
    expect(flashedLines(container)).toEqual([]);
  });

  test('a plain reveal flashes nothing; lines the document lacks are dropped', () => {
    const { container, show } = mount();
    show({ revealRequest: { line: 2, nonce: 1 } });
    expect(flashedLines(container)).toEqual([]);
    // The document has five lines (the last one empty): 6…99 do not exist.
    show({ revealRequest: { line: 4, nonce: 2, flash: { from: 4, to: 99 } } });
    expect(flashedLines(container)).toEqual(['robot.beep()', '']);
  });

  test('a newer flash keeps its full time', () => {
    const { container, show } = mount();
    show({ revealRequest: { line: 2, nonce: 1, flash: { from: 2, to: 2 } } });
    act(() => { vi.advanceTimersByTime(INSERT_FLASH_MS - 100); });
    show({ revealRequest: { line: 4, nonce: 2, flash: { from: 4, to: 4 } } });
    act(() => { vi.advanceTimersByTime(200); });
    expect(flashedLines(container)).toEqual(['robot.beep()']);
    act(() => { vi.advanceTimersByTime(INSERT_FLASH_MS); });
    expect(flashedLines(container)).toEqual([]);
  });
});
