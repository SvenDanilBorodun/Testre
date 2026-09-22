/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The teacher's read-only view of one file of a student's code program. The
// SECOND and last file allowed to import `codemirror`, `@codemirror/*` and
// `@lezer/*` (package.json `no-restricted-imports`); the first is the
// student's `Workshop/code/CodeEditor.jsx`. It is loaded through `React.lazy`
// from `StudentProgramsDrawer`, so CodeMirror stays out of the teacher
// dashboard's own chunk.
//
// Read-only takes BOTH facets, and they are not the same thing:
// `EditorState.readOnly` refuses document changes, `EditorView.editable` is
// what keeps `contenteditable` off the DOM (and the caret out of it). A viewer
// that set only the first would still invite a teacher to type into a
// student's program and then silently drop what they typed.
//
// Deliberately NOT the student editor: no linter, no `robot.` autocomplete, no
// breakpoint gutter — a teacher reads this, they do not run it.

import React, { useEffect, useRef } from 'react';
import { EditorView, lineNumbers } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import { syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language';
import { python } from '@codemirror/lang-python';
import { java } from '@codemirror/lang-java';

const LANGUAGE_SUPPORT = { python, java };

const theme = EditorView.theme({
  '&': { height: '100%', fontSize: '12px' },
  '.cm-scroller': { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
});

/** Props: `language` ('python' | 'java'), `path` (a change swaps the
 *  document), `value` (its content). */
function CodeViewer({ language, path, value }) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);

  useEffect(() => {
    if (!hostRef.current) return undefined;
    const support = LANGUAGE_SUPPORT[language];
    const view = new EditorView({
      state: EditorState.create({
        doc: value || '',
        extensions: [
          lineNumbers(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          support ? support() : [],
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          theme,
        ],
      }),
      parent: hostRef.current,
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // `value` belongs to (language, path): a different file is a different
    // view, and nothing here ever edits the one that is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [language, path, value]);

  return <div ref={hostRef} className="h-full w-full min-h-0 overflow-auto" data-testid="code-viewer" />;
}

export default CodeViewer;
