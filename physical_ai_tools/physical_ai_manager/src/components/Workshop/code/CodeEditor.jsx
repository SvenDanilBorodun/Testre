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
// (the parse notice, the API docs, the Sammlung warnings and hover text).
//
// The code Sammlung (owner decisions O5–O7) reaches the editor through four
// props and the pure modules it wires in: the asset names (`assets`, read
// through a ref, so a new Ziel never rebuilds the view) feed a second
// completion source, a warning linter and a hover tooltip
// (codeAssetCompletion.js); a Sammlung row dropped onto the text becomes the
// line(s) that use it (codeInsert.js); `revealRequest` puts the caret on a
// line; `onCursorChange` tells the page where the student's cursor is, which
// is where „Einfügen" writes.

import React, { useEffect, useRef } from 'react';
import { Decoration, EditorView, GutterMarker, gutter, hoverTooltip, keymap, lineNumbers,
  highlightActiveLine, highlightActiveLineGutter,
  drawSelection, rectangularSelection } from '@codemirror/view';
import {
  Annotation, EditorSelection, EditorState, Compartment, StateEffect, StateField,
} from '@codemirror/state';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { bracketMatching, ensureSyntaxTree, indentOnInput, syntaxHighlighting,
  defaultHighlightStyle } from '@codemirror/language';
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from '@codemirror/autocomplete';
import { forceLinting, linter, lintGutter } from '@codemirror/lint';
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
import {
  ASSET_LINT_LANGUAGES,
  assetArgContext,
  assetAtOffset,
  assetDiagnostics,
  assetHoverText,
  assetOptions,
} from './codeAssetCompletion';
import { SNIPPET_MIME, insertionEdit, minimalChange, snippetLines } from './codeInsert';

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

/**
 * The Sammlung completion source: inside the string argument of an asset call
 * (`robot.move_to("`, `Robot.replay("`), the names the Sammlung and the
 * program know. `knownRef.current` is read on every query.
 */
export function assetCompletionSource(language, knownRef) {
  return (context) => {
    const line = context.state.doc.lineAt(context.pos);
    const before = line.text.slice(0, context.pos - line.from);
    const hit = assetArgContext(before, language);
    if (!hit) return null;
    const options = assetOptions(hit.asset, knownRef && knownRef.current)
      .map((o) => ({ label: o.label, detail: o.detail, type: 'constant' }));
    if (options.length === 0) return null;
    return { from: line.from + hit.from, options, validFor: /^[^"'\\\n]*$/ };
  };
}

// Dispatched when the Sammlung's names change. `forceLinting` alone only
// hurries a lint that is already PENDING, so the source is asked to re-run
// through `needsRefresh` first.
const assetsChanged = StateEffect.define();

/**
 * The Sammlung warnings as a lint extension (only for a language on
 * ASSET_LINT_LANGUAGES). The cursor's line is skipped (a half-typed name is on
 * it by construction), so moving to another line re-runs the source, and so
 * does a change of the Sammlung's names. A diagnostic carries no `source`: the
 * lint tooltip would print it, and it is an English word.
 */
function assetLintExtensions(language, knownRef, withGutter) {
  if (!ASSET_LINT_LANGUAGES.includes(language)) return [];
  let lastCursorLine = -1;
  const source = (view) => {
    const { state } = view;
    const cursorLine = state.doc.lineAt(state.selection.main.head).number;
    lastCursorLine = cursorLine;
    return assetDiagnostics(state.doc.toString(), language, knownRef.current, { cursorLine })
      .map((d) => ({
        from: d.from, to: d.to, severity: d.severity, message: d.message,
      }));
  };
  const needsRefresh = (update) => {
    if (update.transactions.some((tr) => tr.effects.some((e) => e.is(assetsChanged)))) return true;
    if (!update.selectionSet) return false;
    return update.state.doc.lineAt(update.state.selection.main.head).number !== lastCursorLine;
  };
  return [
    linter(source, { delay: CODE_LINT_IDLE_MS, needsRefresh }),
    withGutter ? lintGutter() : [],
  ];
}

/**
 * What a hover over position `pos` shows: the asset literal under it and its
 * German description, `{pos, end, text}`, or null.
 */
export function assetHoverAt(state, pos, language, known) {
  const hit = assetAtOffset(state.doc.toString(), language, pos);
  if (!hit) return null;
  const text = assetHoverText(hit.asset, hit.name, known);
  return text ? { pos: hit.from, end: hit.to, text } : null;
}

function assetHover(language, knownRef) {
  return hoverTooltip((view, pos) => {
    const hit = assetHoverAt(view.state, pos, language, knownRef.current);
    if (!hit) return null;
    return {
      pos: hit.pos,
      end: hit.end,
      above: true,
      create() {
        const dom = document.createElement('div');
        dom.className = 'cm-edubotics-asset-hover';
        dom.textContent = hit.text;
        return { dom };
      },
    };
  });
}

/**
 * A Sammlung row dropped onto the text (the drawer's `SNIPPET_MIME` payload
 * `{kind, name}`) becomes the line(s) that use it, on new lines BELOW the drop
 * line and indented like it (codeInsert.insertionEdit — the same rule as
 * „Einfügen"). Without drop coordinates the cursor's line is the anchor.
 * Anything else — ordinary text — is left to CodeMirror's own handler.
 */
function snippetDrop(language) {
  return EditorView.domEventHandlers({
    dragover(event) {
      const types = event.dataTransfer && event.dataTransfer.types;
      if (types && Array.from(types).includes(SNIPPET_MIME)) {
        event.preventDefault();
        return true;
      }
      return false;
    },
    drop(event, view) {
      const raw = event.dataTransfer ? event.dataTransfer.getData(SNIPPET_MIME) : '';
      if (!raw) return false;
      event.preventDefault();
      if (view.state.readOnly) return true;
      let asset = null;
      try {
        asset = JSON.parse(raw);
      } catch (_) {
        return true;
      }
      const lines = snippetLines(asset, language);
      if (lines.length === 0) return true;
      let at = null;
      try {
        at = view.posAtCoords({ x: event.clientX, y: event.clientY });
      } catch (_) {
        at = null;
      }
      const pos = Number.isInteger(at) ? at : view.state.selection.main.head;
      const dropLine = view.state.doc.lineAt(pos).number;
      const edit = insertionEdit(view.state.doc.toString(), dropLine, lines, { language });
      if (!edit.insert) return true;
      // The end of the last inserted line: the insertion either ends with the
      // newline that separates it from the drop line's successor, or (at the
      // very end of a file without one) starts with it.
      const caret = edit.insert.endsWith('\n')
        ? edit.from + edit.insert.length - 1
        : edit.from + edit.insert.length;
      view.dispatch({
        changes: { from: edit.from, insert: edit.insert },
        selection: EditorSelection.cursor(caret),
        scrollIntoView: true,
        userEvent: 'input.drop',
      });
      return true;
    },
  });
}

// Marks the transaction that applies an external `value` (a rename, an
// insertion from the drawer): the page already holds that text, so it is not
// echoed back through onChange, and it is not a cursor move of the student's.
const externalSync = Annotation.define();

// The transactions that are the student's own doing: typing, deleting,
// pasting, a drop, undo/redo, a click or an arrow key.
const STUDENT_EVENTS = ['input', 'delete', 'move', 'select', 'undo', 'redo'];
function isStudentTransaction(tr) {
  return !tr.annotation(externalSync) && STUDENT_EVENTS.some((e) => tr.isUserEvent(e));
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
  '.cm-edubotics-asset-hover': { padding: '2px 6px', fontSize: '12px' },
});

function editorExtensions(language, callbacks, readOnlyCompartment, onToggleRef, withGutter, assetsRef) {
  const parseLint = parseLintExtensions(language);
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
    autocompletion({
      override: [robotApiCompletionSource(language), assetCompletionSource(language, assetsRef)],
    }),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...completionKeymap, indentWithTab]),
    LANGUAGE_SUPPORT[language](),
    parseLint,
    // One lint gutter: the parse markers bring their own when they are on.
    assetLintExtensions(language, assetsRef, parseLint.length === 0),
    assetHover(language, assetsRef),
    snippetDrop(language),
    readOnlyCompartment.of(EditorState.readOnly.of(false)),
    EditorView.updateListener.of((update) => {
      const external = update.transactions.some((tr) => tr.annotation(externalSync));
      if (update.docChanged && !external) callbacks.onDocChange(update.state.doc.toString());
      const own = update.transactions.some(isStudentTransaction);
      if ((own && (update.selectionSet || update.docChanged)) || (update.focusChanged && update.view.hasFocus)) {
        callbacks.onCursor(update.state.doc.lineAt(update.state.selection.main.head).number);
      }
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
 * The code Sammlung's four: `assets` (what codeAssetCompletion.js knows —
 * recordings, places, the program's pins, counters, objects), `revealRequest`
 * (`{line, nonce}`: put the caret at the end of that line, once per nonce,
 * clamped to the document), `onCursorChange(line)` (the student's own cursor
 * moves and edits only — never a programmatic selection or an external value).
 *
 * The view is created once per (language, path); typing flows out through
 * onChange, and an external `value` that differs from the document (a rename,
 * an insertion from the drawer, a version restore) is applied as the SMALLEST
 * change (codeInsert.minimalChange), so the cursor stays where the text around
 * it did not change and Strg+Z undoes exactly that edit. It is not echoed back
 * through onChange: the page already holds it.
 */
function CodeEditor({
  language, path, value, onChange, readOnly = false,
  breakpointLines = null, onToggleBreakpoint = null,
  highlightLine = null, highlightKind = null,
  assets = null, revealRequest = null, onCursorChange = null,
}) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const onChangeRef = useRef(onChange);
  const onToggleRef = useRef(onToggleBreakpoint);
  const onCursorRef = useRef(onCursorChange);
  const assetsRef = useRef(assets);
  const readOnlyRef = useRef(new Compartment());
  useEffect(() => { onChangeRef.current = onChange; }, [onChange]);
  useEffect(() => { onToggleRef.current = onToggleBreakpoint; }, [onToggleBreakpoint]);
  useEffect(() => { onCursorRef.current = onCursorChange; }, [onCursorChange]);

  useEffect(() => {
    if (!hostRef.current) return undefined;
    const view = new EditorView({
      state: EditorState.create({
        doc: value || '',
        extensions: editorExtensions(
          language,
          {
            onDocChange: (doc) => onChangeRef.current?.(doc),
            onCursor: (line) => onCursorRef.current?.(line),
          },
          readOnlyRef.current,
          onToggleRef,
          typeof onToggleRef.current === 'function',
          assetsRef,
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
    if (!view || value === undefined) return;
    const change = minimalChange(view.state.doc.toString(), value || '');
    if (change) view.dispatch({ changes: change, annotations: externalSync.of(true) });
  }, [value]);

  // New Sammlung names: the warnings are re-judged at once (completion and
  // hover read the ref on their own next query).
  useEffect(() => {
    assetsRef.current = assets;
    const view = viewRef.current;
    if (view && ASSET_LINT_LANGUAGES.includes(language)) {
      view.dispatch({ effects: assetsChanged.of(null) });
      forceLinting(view);
    }
  }, [assets, language]);

  // A „Benutzt in" jump or an insertion: the caret at the end of the line.
  const revealNonce = revealRequest ? revealRequest.nonce : null;
  useEffect(() => {
    const view = viewRef.current;
    if (!view || !revealRequest || !Number.isInteger(revealRequest.line)) return;
    const n = Math.min(Math.max(revealRequest.line, 1), view.state.doc.lines);
    const caret = view.state.doc.line(n).to;
    view.dispatch({
      selection: EditorSelection.cursor(caret),
      effects: EditorView.scrollIntoView(caret, { y: 'center' }),
    });
    // One reveal per nonce: the request object's identity and line are not
    // the trigger, its nonce is (a repeated jump to the same line gets a new
    // nonce from the page).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealNonce, path]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({ effects: readOnlyRef.current.reconfigure(EditorState.readOnly.of(!!readOnly)) });
  }, [readOnly]);

  return <div ref={hostRef} className="h-full w-full min-h-0 overflow-hidden" data-testid="code-editor" />;
}

export default CodeEditor;
