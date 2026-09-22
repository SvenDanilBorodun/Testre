/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The CodeMirror 6 editor for ONE file of a code program. LAZY-loaded by
// CodeWorkspace (`React.lazy`), so CodeMirror and both Lezer parsers stay out
// of the entry bundle the white-screen CI greps watch. This file and the
// teacher's read-only viewer are the ONLY two allowed to import `codemirror`,
// `@codemirror/*` and `@lezer/*` (package.json `no-restricted-imports`).
//
// Deliberately NOT `basicSetup`: it brings the search panel and the lint panel,
// whose built-in strings are English (Rule §1). The extension list below is
// assembled by hand and every student-visible string it can produce is German
// (the parse notice, the API docs).

import React, { useEffect, useRef } from 'react';
import { Decoration, EditorView, GutterMarker, gutter, keymap, lineNumbers,
  highlightActiveLine, highlightActiveLineGutter,
  drawSelection, rectangularSelection } from '@codemirror/view';
import { EditorState, Compartment, StateEffect, StateField } from '@codemirror/state';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { bracketMatching, ensureSyntaxTree, indentOnInput, syntaxHighlighting,
  defaultHighlightStyle } from '@codemirror/language';
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from '@codemirror/autocomplete';
import { linter, lintGutter } from '@codemirror/lint';
import { python } from '@codemirror/lang-python';
import { java } from '@codemirror/lang-java';
import {
  CODE_LINT_IDLE_MS,
  CODE_LINT_LANGUAGES,
  CODE_LINT_PARSE_BUDGET_MS,
  collectUnparsedRegions,
  diagnosticsForRegions,
} from './parseMarkers';
import { robotApiCompletions, robotApiTriggerFrom } from './robotApiCompletion';

const LANGUAGE_SUPPORT = { python, java };

/**
 * A16, the mechanism: the parse markers as a lint source, plus the gutter that
 * renders them — unconditional, so it can be exercised for a language the ship
 * list has off (`parseLintExtensions` is the gate the editor uses).
 *
 * `linter` runs its source "whenever the editor is idle (after its content
 * changed)"; `delay` is the library's own debounce and is passed EXPLICITLY.
 * The source reads the tree through `ensureSyntaxTree` with a work budget and,
 * when the incremental parse has not reached the end of a large file within
 * it (`null`), keeps the previous diagnostics rather than judging a partial
 * tree. `needsRefresh` re-runs the source on a pure selection change once the
 * cursor's LINE moved: that is what reveals a half-typed line the student has
 * now left (parseMarkers.js, the cursor-line rule).
 */
export function buildParseLint(language) {
  let previous = [];
  let lastCursorLine = -1;
  const source = (view) => {
    const { state } = view;
    const tree = ensureSyntaxTree(state, state.doc.length, CODE_LINT_PARSE_BUDGET_MS);
    if (!tree) return previous;
    const cursorLine = state.doc.lineAt(state.selection.main.head).number;
    lastCursorLine = cursorLine;
    previous = diagnosticsForRegions(collectUnparsedRegions(tree, state.doc), state.doc, cursorLine, language);
    return previous;
  };
  const needsRefresh = (update) => {
    if (!update.selectionSet) return false;
    const line = update.state.doc.lineAt(update.state.selection.main.head).number;
    return line !== lastCursorLine;
  };
  return [linter(source, { delay: CODE_LINT_IDLE_MS, needsRefresh }), lintGutter()];
}

/** What the editor installs: the markers only for a language on the ship
 *  list (empty today — the §3.11 exit, docs/KNOWN-ISSUES.md), else nothing,
 *  so no idle parse work runs for a language whose markers are off. */
export function parseLintExtensions(language) {
  return CODE_LINT_LANGUAGES.includes(language) ? buildParseLint(language) : [];
}

/** The `robot.` / `Robot.` completion source over robot_api.json. */
export function robotApiCompletionSource(language) {
  const options = robotApiCompletions(language);
  return (context) => {
    const line = context.state.doc.lineAt(context.pos);
    const before = line.text.slice(0, context.pos - line.from);
    const from = robotApiTriggerFrom(before, language);
    if (from < 0) return null;
    return { from: line.from + from, options, validFor: /^[A-Za-z_][A-Za-z0-9_]*$/ };
  };
}

/*
 * The debugger's two editor-side surfaces (A8, §3.5). Both are driven by
 * StateEffects rather than by re-creating the view: the view is built once per
 * (language, path) and a breakpoint set or a paused line changes far more often
 * than that.
 *
 * `setBreakpointLines` carries the lines of the OPEN file only — CodeWorkspace
 * has already split the `<file>:L<line>` id space (codeBreakpoints.js), so the
 * editor never sees an id and can never mistake a Blockly one for a line.
 * `setRunHighlight` carries `{line, kind}` derived from
 * `WorkflowStatus.current_block_id` (CodeWorkspace), or null. The kind rides
 * through as a class so a future one needs no change here; the theme paints
 * every reachable kind the same amber, `running` and `paused` being the two
 * (CodeWorkspace says why there is no `error`).
 */
const setBreakpointLines = StateEffect.define();
const setRunHighlight = StateEffect.define();

const breakpointLinesField = StateField.define({
  create: () => [],
  update(value, tr) {
    let next = value;
    for (const e of tr.effects) if (e.is(setBreakpointLines)) next = e.value;
    return next;
  },
});

const runHighlightField = StateField.define({
  create: () => null,
  update(value, tr) {
    let next = value;
    for (const e of tr.effects) if (e.is(setRunHighlight)) next = e.value;
    return next;
  },
});

/**
 * The highlighted line, recomputed from the field alone. A line number the
 * document does not have is DROPPED rather than clamped: the student may have
 * edited the file while the run was live, and `Text.line` throws on an
 * out-of-range number — a throw inside a facet takes the whole editor down.
 */
const runHighlightDecorations = EditorView.decorations.compute(
  [runHighlightField],
  (state) => {
    const hl = state.field(runHighlightField);
    if (!hl || !Number.isInteger(hl.line) || hl.line < 1 || hl.line > state.doc.lines) {
      return Decoration.none;
    }
    const cls = `cm-edubotics-run-line cm-edubotics-run-${hl.kind || 'running'}`;
    return Decoration.set([Decoration.line({ class: cls }).range(state.doc.line(hl.line).from)]);
  },
);

/**
 * One marker per line, SET or not: an unset line still needs a press target,
 * because pressing it is how a breakpoint is set. The marker's DOM carries its
 * own line number, which is what the press handler reads.
 */
class BreakpointMarker extends GutterMarker {
  constructor(line, set) {
    super();
    this.line = line;
    this.set = set;
  }

  eq(other) {
    return other.line === this.line && other.set === this.set;
  }

  toDOM() {
    const dot = document.createElement('span');
    dot.className = this.set ? 'cm-edubotics-bp cm-edubotics-bp-set' : 'cm-edubotics-bp';
    dot.dataset.line = String(this.line);
    dot.textContent = this.set ? '●' : '';
    return dot;
  }
}
const spacerMarker = new BreakpointMarker(0, true);

/**
 * The breakpoint gutter. `renderEmptyElements` is NOT optional: without it a
 * line whose marker renders nothing gets no gutter element, so there would be
 * nothing to press. The toggle is read through a ref so a new callback identity
 * never has to reconfigure the view.
 *
 * The pressed LINE is resolved from the marker's `data-line` first and only
 * then from the height mapping CodeMirror hands in: `data-line` is the element
 * the student actually pressed, and the height mapping is unusable wherever
 * layout is absent (jsdom answers „line 1" for every Y, which is what the test
 * would otherwise be pinning).
 */
function breakpointGutter(onToggleRef) {
  return gutter({
    class: 'cm-edubotics-bp-gutter',
    renderEmptyElements: true,
    lineMarker: (view, line) => {
      const n = view.state.doc.lineAt(line.from).number;
      return new BreakpointMarker(n, view.state.field(breakpointLinesField).includes(n));
    },
    lineMarkerChange: (update) => (
      update.startState.field(breakpointLinesField) !== update.state.field(breakpointLinesField)
    ),
    initialSpacer: () => spacerMarker,
    domEventHandlers: {
      mousedown: (view, line, event) => {
        const pressed = event.target && typeof event.target.closest === 'function'
          ? event.target.closest('[data-line]')
          : null;
        const fromDom = pressed ? Number(pressed.dataset.line) : NaN;
        const n = Number.isInteger(fromDom) && fromDom >= 1
          ? fromDom
          : view.state.doc.lineAt(line.from).number;
        onToggleRef.current?.(n);
        return true;
      },
    },
  });
}

const theme = EditorView.theme({
  '&': { height: '100%', fontSize: '13px' },
  '.cm-scroller': { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
  '.cm-edubotics-unparsed': { textDecoration: 'underline wavy #d97706', textUnderlineOffset: '3px' },
  '.cm-edubotics-bp-gutter': { width: '14px', cursor: 'pointer' },
  // `display: block` + full width: the marker IS the press target, including
  // on a line that has no breakpoint yet.
  '.cm-edubotics-bp': { display: 'block', width: '100%', fontSize: '11px', lineHeight: 'inherit' },
  '.cm-edubotics-bp-set': { color: '#dc2626' },
  '.cm-edubotics-run-line': { backgroundColor: '#fef3c7' },
});

function editorExtensions(language, onDocChange, readOnlyCompartment, onToggleRef, withGutter) {
  return [
    lineNumbers(),
    breakpointLinesField,
    runHighlightField,
    runHighlightDecorations,
    withGutter ? breakpointGutter(onToggleRef) : [],
    highlightActiveLineGutter(),
    highlightActiveLine(),
    history(),
    drawSelection(),
    rectangularSelection(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    autocompletion({ override: [robotApiCompletionSource(language)] }),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...completionKeymap, indentWithTab]),
    LANGUAGE_SUPPORT[language](),
    parseLintExtensions(language),
    readOnlyCompartment.of(EditorState.readOnly.of(false)),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) onDocChange(update.state.doc.toString());
    }),
    theme,
  ];
}

/**
 * Props: `language` ('python' | 'java'), `path` (the file shown — a change
 * swaps the document), `value` (its content), `onChange(content)`, `readOnly`,
 * plus the debugger's four: `breakpointLines` (the lines of THIS file),
 * `onToggleBreakpoint(line)`, `highlightLine` and `highlightKind`.
 *
 * The gutter exists exactly when `onToggleBreakpoint` is a function — that is
 * the whole Java asymmetry (A8): CodeWorkspace hands one for Python only, so
 * there is no gutter to click and none to mis-read. It is read at view
 * creation, which is fine because the language decides it and a language
 * change re-creates the view.
 *
 * The view is created once per (language, path); typing flows out through
 * onChange, and an external `value` that differs from the document (a rename,
 * a version restore) is applied without moving the cursor more than needed.
 */
function CodeEditor({
  language, path, value, onChange, readOnly = false,
  breakpointLines = null, onToggleBreakpoint = null,
  highlightLine = null, highlightKind = null,
}) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const onChangeRef = useRef(onChange);
  const onToggleRef = useRef(onToggleBreakpoint);
  const readOnlyRef = useRef(new Compartment());
  useEffect(() => { onChangeRef.current = onChange; }, [onChange]);
  useEffect(() => { onToggleRef.current = onToggleBreakpoint; }, [onToggleBreakpoint]);

  useEffect(() => {
    if (!hostRef.current) return undefined;
    const view = new EditorView({
      state: EditorState.create({
        doc: value || '',
        extensions: editorExtensions(
          language,
          (doc) => onChangeRef.current?.(doc),
          readOnlyRef.current,
          onToggleRef,
          typeof onToggleRef.current === 'function',
        ),
      }),
      parent: hostRef.current,
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // `value` is deliberately not a dependency: the view owns the document
    // between mounts, and the effect below applies an external replacement.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [language, path]);

  // The breakpoint set and the run line are STATE, not configuration: they
  // travel as effects so a paused line never costs the student their editor.
  const bpKey = Array.isArray(breakpointLines) ? breakpointLines.join(',') : '';
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: setBreakpointLines.of(Array.isArray(breakpointLines) ? breakpointLines : []),
    });
    // The ARRAY identity changes on every parent render; its contents are what
    // matter, so the joined key is the dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bpKey, path]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const hl = Number.isInteger(highlightLine) && highlightLine >= 1
      ? { line: highlightLine, kind: highlightKind || 'running' }
      : null;
    view.dispatch({ effects: setRunHighlight.of(hl) });
  }, [highlightLine, highlightKind, path]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (value !== undefined && value !== current) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value || '' } });
    }
  }, [value]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({ effects: readOnlyRef.current.reconfigure(EditorState.readOnly.of(!!readOnly)) });
  }, [readOnly]);

  return <div ref={hostRef} className="h-full w-full min-h-0 overflow-hidden" data-testid="code-editor" />;
}

export default CodeEditor;
