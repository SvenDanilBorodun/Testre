/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The editor half of the code Sammlung (owner decisions O5–O7), against a REAL
// EditorView in jsdom (the shape of CodeEditor.lint.test.jsx):
//
//   * an edit made OUTSIDE the editor (a rename, a drawer insertion) arrives as
//     the smallest change — the student's cursor survives and Strg+Z undoes it;
//   * the cursor line is reported to the page only for the student's own moves;
//   * a reveal request puts the caret on a line (a „Benutzt in" jump);
//   * name completion, German warnings and hover text for asset names;
//   * a Sammlung row dropped into the editor lands below the drop line.
//
// The CodeMirror packages are dynamic imports for the reason CodeEditor.lint.
// test.jsx gives: the entry-bundle eslint rule excludes exactly the two editor
// components, and a dynamic import is not a static one.

import React from 'react';
import { render, act, fireEvent } from '@testing-library/react';
import CodeEditor, {
  assetCompletionSource,
  assetHoverAt,
} from '../CodeEditor';
import { CODE_INDENT_UNIT, SNIPPET_MIME } from '../codeInsert';
import { CODE_LINT_IDLE_MS } from '../parseMarkers';
import { CODE_DE, formatCode } from '../codeMessagesDe';

let EditorView;
let EditorState;
let EditorSelection;
let CompletionContext;
let currentCompletions;
let startCompletion;
let undo;
let forEachDiagnostic;
let indentUnitFacet;
let insertNewlineAndIndent;

// jsdom has no layout, and its Range has no getClientRects — which the
// editor's cursor drawing calls on every selection change. An empty list is
// exactly what a 0×0 layout would answer; installed for this file only.
const hadRangeRects = typeof Range !== 'undefined' && typeof Range.prototype.getClientRects === 'function';
beforeAll(() => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    Range.prototype.getClientRects = function getClientRects() { return []; };
    Range.prototype.getBoundingClientRect = function getBoundingClientRect() {
      return {
        x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0,
      };
    };
  }
});
afterAll(() => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    delete Range.prototype.getClientRects;
    delete Range.prototype.getBoundingClientRect;
  }
});

beforeAll(async () => {
  const [view, state, autocomplete, commands, lint, language] = await Promise.all([
    import('@codemirror/view'),
    import('@codemirror/state'),
    import('@codemirror/autocomplete'),
    import('@codemirror/commands'),
    import('@codemirror/lint'),
    import('@codemirror/language'),
  ]);
  indentUnitFacet = language.indentUnit;
  insertNewlineAndIndent = commands.insertNewlineAndIndent;
  EditorView = view.EditorView;
  EditorState = state.EditorState;
  EditorSelection = state.EditorSelection;
  CompletionContext = autocomplete.CompletionContext;
  currentCompletions = autocomplete.currentCompletions;
  startCompletion = autocomplete.startCompletion;
  undo = commands.undo;
  forEachDiagnostic = lint.forEachDiagnostic;
});

/* eslint-disable testing-library/no-container, testing-library/no-node-access */
const viewOf = (container) => EditorView.findFromDOM(container.querySelector('.cm-editor'));
/* eslint-enable testing-library/no-container, testing-library/no-node-access */

const KNOWN = {
  recordings: [{ name: 'Winken', duration_s: 4.2, versions: 3 }],
  recordingsStatus: 'ready',
  places: [
    { id: 'd_1', name: 'Ablage', kind: 'pin', x: 0.123, y: 0.04, z: 0 },
    { id: 'd_2', name: 'Hoch', kind: 'pose', x: 0.1, y: -0.02, z: 0.15 },
  ],
  codePinnedNames: ['Mitte'],
  counters: [],
  objects: ['wuerfel'],
};

const PY = [
  'import robot',                    // 1
  'robot.move_to("Ablage")',         // 2
  'for i in range(3):',              // 3
  '    robot.replay("Winken")',      // 4
  'robot.home()',                    // 5
  '',
].join('\n');

const BASE = {
  language: 'python',
  path: 'main.py',
  value: PY,
  onChange: () => {},
  assets: KNOWN,
};

function mount(props = {}) {
  const all = { ...BASE, ...props };
  const utils = render(<CodeEditor {...all} />);
  return {
    ...utils,
    view: viewOf(utils.container),
    show: (next) => utils.rerender(<CodeEditor {...all} {...next} />),
  };
}

const cursorAt = (view, line, col = 0) => view.state.doc.line(line).from + col;
const cursorLineCol = (view) => {
  const head = view.state.selection.main.head;
  const line = view.state.doc.lineAt(head);
  return [line.number, head - line.from];
};

describe('an external value arrives as the smallest change', () => {
  test('the cursor stays where it was, onChange is not echoed, Strg+Z undoes the edit', () => {
    const onChange = vi.fn();
    const { view, show } = mount({ onChange });
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(cursorAt(view, 4, 10)) });
    });
    expect(cursorLineCol(view)).toEqual([4, 10]);

    const renamed = PY.replace('robot.move_to("Ablage")', 'robot.move_to("Tischmitte")');
    show({ value: renamed });

    expect(view.state.doc.toString()).toBe(renamed);
    expect(cursorLineCol(view)).toEqual([4, 10]);
    expect(onChange).not.toHaveBeenCalled();

    act(() => { undo(view); });
    expect(view.state.doc.toString()).toBe(PY);
  });

  test('a file with Windows line breaks (CRLF) is the same lines — no extra line on mount or on a rename', () => {
    // The editor keeps "\n" between its lines; a CRLF value compared
    // character by character looked different, and its smallest change
    // inserted a stray "\r" that CodeMirror read as one more line break.
    const crlf = PY.replace(/\n/g, '\r\n');
    const onChange = vi.fn();
    const { view, show } = mount({ value: crlf, onChange });
    expect(view.state.doc.toString()).toBe(PY);
    const renamed = crlf.replace('robot.move_to("Ablage")', 'robot.move_to("Tischmitte")');
    show({ value: renamed });
    expect(view.state.doc.toString()).toBe(renamed.replace(/\r\n/g, '\n'));
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('onCursorChange', () => {
  test('reports the student’s own cursor moves and edits, never a programmatic one', () => {
    const onCursorChange = vi.fn();
    const { view, show } = mount({ onCursorChange });
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(cursorAt(view, 3)), userEvent: 'select.pointer' });
    });
    expect(onCursorChange).toHaveBeenLastCalledWith(3);

    act(() => {
      view.dispatch({
        changes: { from: cursorAt(view, 5), insert: 'x = 1\n' },
        selection: EditorSelection.cursor(cursorAt(view, 5) + 5),
        userEvent: 'input.type',
      });
    });
    expect(onCursorChange).toHaveBeenLastCalledWith(5);

    const calls = onCursorChange.mock.calls.length;
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(cursorAt(view, 1)) });
    });
    show({ value: `${view.state.doc.toString()}# Ende\n` });
    expect(onCursorChange.mock.calls.length).toBe(calls);
  });
});

describe('revealRequest', () => {
  test('puts the caret at the end of the requested line, once per nonce, clamped', () => {
    const { view, show } = mount({ revealRequest: null });
    show({ revealRequest: { line: 4, nonce: 1 } });
    expect(view.state.selection.main.head).toBe(view.state.doc.line(4).to);

    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(cursorAt(view, 1)) });
    });
    show({ revealRequest: { line: 4, nonce: 1 } });
    expect(cursorLineCol(view)).toEqual([1, 0]);

    show({ revealRequest: { line: 99, nonce: 2 } });
    expect(view.state.selection.main.head).toBe(view.state.doc.length);
  });
});

describe('asset completion', () => {
  test('offers the Sammlung names inside a place call’s string, and nothing elsewhere', () => {
    const source = assetCompletionSource('python', { current: KNOWN });
    const doc = 'import robot\nrobot.move_to("Ab';
    const state = EditorState.create({ doc });
    const result = source(new CompletionContext(state, doc.length, false));
    expect(result.from).toBe(doc.length - 2);
    expect(result.options.map((o) => o.label)).toEqual(['Ablage', 'Hoch', 'Mitte']);
    expect(result.options[1].detail).toBe(CODE_DE.ASSET_KIND_POSE);

    const plain = EditorState.create({ doc: 'robot.log("Ab' });
    expect(source(new CompletionContext(plain, 13, false))).toBeNull();
  });

  test('reads the names through the ref, so new Sammlung items need no editor rebuild', () => {
    const ref = { current: KNOWN };
    const source = assetCompletionSource('python', ref);
    const doc = 'robot.replay("';
    ref.current = { ...KNOWN, recordings: [...KNOWN.recordings, { name: 'Tanz' }] };
    const result = source(new CompletionContext(EditorState.create({ doc }), doc.length, true));
    expect(result.options.map((o) => o.label)).toEqual(['Winken', 'Tanz']);
  });

  test('the running editor offers them too (alongside the robot API)', async () => {
    const doc = 'import robot\nrobot.move_to("';
    const { view } = mount({ value: doc });
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(doc.length) });
    });
    await act(async () => {
      startCompletion(view);
      await new Promise((r) => { setTimeout(r, 120); });
    });
    expect(currentCompletions(view.state).map((c) => c.label)).toEqual(['Ablage', 'Hoch', 'Mitte']);
  });
});

describe('asset warnings', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  const messages = (view) => {
    const out = [];
    forEachDiagnostic(view.state, (d) => out.push(d));
    return out;
  };

  test('a German warning on a missing name, no English source label, gone once the name exists', async () => {
    const doc = 'import robot\nrobot.replay("Fehlt")\nrobot.move_to("Ablage")\n';
    const { view, show } = mount({ value: doc });
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(0) });
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50); });
    const found = messages(view);
    expect(found.map((d) => d.message)).toEqual([formatCode(CODE_DE.ASSET_MISSING_RECORDING, 'Fehlt')]);
    expect(found[0].severity).toBe('warning');
    expect(found[0].source).toBeUndefined();

    show({ assets: { ...KNOWN, recordings: [...KNOWN.recordings, { name: 'Fehlt' }] } });
    await act(async () => { await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50); });
    expect(messages(view)).toEqual([]);
  });

  test('Java gets the warnings too', async () => {
    const doc = 'class Main {\n  public static void main(String[] a) {\n    Robot.moveTo("Nirgends");\n  }\n}\n';
    const { view } = mount({ language: 'java', path: 'Main.java', value: doc });
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(0) });
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50); });
    expect(messages(view).map((d) => d.message)).toEqual([formatCode(CODE_DE.ASSET_MISSING_PLACE, 'Nirgends')]);
  });
});

describe('assetHoverAt', () => {
  test('describes the asset literal under the pointer in German', () => {
    const state = EditorState.create({ doc: PY });
    const pos = PY.indexOf('Ablage') + 2;
    expect(assetHoverAt(state, pos, 'python', KNOWN)).toEqual({
      pos: PY.indexOf('Ablage'),
      end: PY.indexOf('Ablage') + 'Ablage'.length,
      text: 'Ziel „Ablage“ · x 12,3 cm · y 4,0 cm',
    });
    expect(assetHoverAt(state, 2, 'python', KNOWN)).toBeNull();
  });
});

describe('dropping a Sammlung row', () => {
  const dropOn = (view, payload) => {
    const data = { [SNIPPET_MIME]: JSON.stringify(payload) };
    fireEvent.drop(view.contentDOM, {
      dataTransfer: {
        types: Object.keys(data),
        getData: (t) => data[t] || '',
      },
    });
  };

  test('lands on a new line below the drop line, indented like the block it is in', () => {
    const onChange = vi.fn();
    const { view } = mount({ onChange });
    view.posAtCoords = () => view.state.doc.line(4).from + 2;
    act(() => { dropOn(view, { kind: 'pose', name: 'Hoch' }); });
    const lines = view.state.doc.toString().split('\n');
    expect(lines[4]).toBe('    robot.move_to("Hoch")');
    expect(onChange).toHaveBeenLastCalledWith(view.state.doc.toString());
    expect(cursorLineCol(view)[0]).toBe(5);
  });

  test('without drop coordinates it lands below the cursor line', () => {
    const { view } = mount();
    view.posAtCoords = () => null;
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(cursorAt(view, 3, 4)) });
    });
    act(() => { dropOn(view, { kind: 'recording', name: 'Winken' }); });
    expect(view.state.doc.toString().split('\n')[3]).toBe('    robot.replay("Winken")');
  });

  test('a read-only editor takes nothing, and ordinary text is not intercepted', () => {
    const { view } = mount({ readOnly: true });
    view.posAtCoords = () => view.state.doc.line(1).from;
    act(() => { dropOn(view, { kind: 'pose', name: 'Hoch' }); });
    expect(view.state.doc.toString()).toBe(PY);
  });
});

describe('one indentation unit for the editor and the insertion (review M2)', () => {
  test('the editor indents with CODE_INDENT_UNIT, so Enter after a block opener agrees', () => {
    const doc = 'for i in range(3):';
    const { view } = mount({ value: doc });
    expect(view.state.facet(indentUnitFacet)).toBe(CODE_INDENT_UNIT);
    act(() => {
      view.dispatch({ selection: EditorSelection.cursor(doc.length) });
      insertNewlineAndIndent(view);
    });
    expect(view.state.doc.toString()).toBe(`for i in range(3):\n${CODE_INDENT_UNIT}`);
  });

  test('a row dropped into a 2-space body lands with 2 spaces', () => {
    const two = 'import robot\nfor i in range(3):\n  robot.home()\n';
    const { view } = mount({ value: two });
    view.posAtCoords = () => view.state.doc.line(2).from + 3;
    const data = { [SNIPPET_MIME]: JSON.stringify({ kind: 'pose', name: 'Hoch' }) };
    fireEvent.drop(view.contentDOM, {
      dataTransfer: { types: Object.keys(data), getData: (t) => data[t] || '' },
    });
    expect(view.state.doc.toString())
      .toBe('import robot\nfor i in range(3):\n  robot.move_to("Hoch")\n  robot.home()\n');
  });
});

describe('a reveal belongs to its nonce, not to the file on screen (review m5)', () => {
  test('switching the file with the same request leaves the caret alone', () => {
    const { view, show } = mount({ revealRequest: { line: 4, nonce: 1 } });
    expect(view.state.selection.main.head).toBe(view.state.doc.line(4).to);
    show({ path: 'hilfe.py', value: 'a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n', revealRequest: { line: 4, nonce: 1 } });
    // eslint-disable-next-line testing-library/no-node-access
    const next = EditorView.findFromDOM(document.querySelector('.cm-editor'));
    expect(next.state.selection.main.head).toBe(0);
  });
});

describe('the knowledge is built inside the lazy editor from its inputs (review n10)', () => {
  test('assetSources become the same completion options as ready-made assets', async () => {
    const doc = 'import robot\nrobot.move_to("';
    const { view } = mount({
      value: doc,
      assets: null,
      assetSources: {
        files: { 'main.py': doc }, language: 'python', entries: KNOWN.places, trajectories: null, objectTypes: [],
      },
    });
    act(() => { view.dispatch({ selection: EditorSelection.cursor(doc.length) }); });
    await act(async () => {
      startCompletion(view);
      await new Promise((r) => { setTimeout(r, 120); });
    });
    expect(currentCompletions(view.state).map((c) => c.label)).toEqual(['Ablage', 'Hoch']);
  });
});
