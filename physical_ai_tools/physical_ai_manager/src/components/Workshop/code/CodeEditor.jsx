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
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  drawSelection, rectangularSelection } from '@codemirror/view';
import { EditorState, Compartment } from '@codemirror/state';
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

const theme = EditorView.theme({
  '&': { height: '100%', fontSize: '13px' },
  '.cm-scroller': { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
  '.cm-edubotics-unparsed': { textDecoration: 'underline wavy #d97706', textUnderlineOffset: '3px' },
});

function editorExtensions(language, onDocChange, readOnlyCompartment) {
  return [
    lineNumbers(),
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
 * swaps the document), `value` (its content), `onChange(content)`, `readOnly`.
 * The view is created once per (language, path); typing flows out through
 * onChange, and an external `value` that differs from the document (a rename,
 * a version restore) is applied without moving the cursor more than needed.
 */
function CodeEditor({ language, path, value, onChange, readOnly = false }) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const onChangeRef = useRef(onChange);
  const readOnlyRef = useRef(new Compartment());
  useEffect(() => { onChangeRef.current = onChange; }, [onChange]);

  useEffect(() => {
    if (!hostRef.current) return undefined;
    const view = new EditorView({
      state: EditorState.create({
        doc: value || '',
        extensions: editorExtensions(language, (doc) => onChangeRef.current?.(doc), readOnlyRef.current),
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
