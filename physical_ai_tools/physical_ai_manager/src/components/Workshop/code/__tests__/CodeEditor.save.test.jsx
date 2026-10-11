/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Owner decision K-1: Strg+S while the cursor is in the code editor saves the
// program — once, through the page's own save — and never opens the
// browser's „Seite speichern", also when there is nothing to save with (a
// read-only viewer). A new save callback reaches the live view without a
// rebuild.

import React from 'react';
import { render } from '@testing-library/react';
import CodeEditor from '../CodeEditor';

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
const editorDom = (container) => container.querySelector('.cm-editor');
/* eslint-enable testing-library/no-container, testing-library/no-node-access */

const DOC = 'import robot\nrobot.home()\n';

function ctrlS(view) {
  const event = new KeyboardEvent('keydown', {
    key: 's', code: 'KeyS', keyCode: 83, ctrlKey: true, bubbles: true, cancelable: true,
  });
  view.contentDOM.dispatchEvent(event);
  return event;
}

function mount(props = {}) {
  const base = {
    language: 'python', path: 'main.py', value: DOC, onChange: () => {},
  };
  const utils = render(<CodeEditor {...base} {...props} />);
  const view = EditorView.findFromDOM(editorDom(utils.container));
  return {
    ...utils, view, show: (next) => utils.rerender(<CodeEditor {...base} {...props} {...next} />),
  };
}

describe('Strg+S inside the editor', () => {
  test('calls onSave exactly once and keeps the browser’s dialog away', () => {
    const onSave = vi.fn();
    const { view } = mount({ onSave });
    const event = ctrlS(view);
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
    expect(view.state.doc.toString()).toBe(DOC);
  });

  test('without onSave it is still prevented, and nothing throws', () => {
    const { view } = mount();
    let event;
    expect(() => { event = ctrlS(view); }).not.toThrow();
    expect(event.defaultPrevented).toBe(true);
    expect(view.state.doc.toString()).toBe(DOC);
  });

  test('a NEW onSave is the one called, with the same view', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { container, view, show } = mount({ onSave: first });
    const dom = editorDom(container);
    show({ onSave: second });
    expect(editorDom(container)).toBe(dom);
    ctrlS(view);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
