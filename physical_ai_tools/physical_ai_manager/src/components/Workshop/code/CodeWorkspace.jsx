/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code editor host: a file tree beside the (lazy) CodeMirror editor. Owns
// nothing the page does not — the project (`files`) and its language live in
// WorkshopPage and come in as props; every edit goes back through
// `onFilesChange(nextFiles)`. The §3.7 caps are enforced HERE, at the moment a
// file is created or renamed, with the German sentences of codeMessagesDe.js;
// content size is judged again at save and at run (codeProject.validateProject).
//
// The file last open is remembered under `edubotics_code_last_file`, a
// STUDENT-scoped key (utils/sessionScope.js): it is a view of the student's own
// program, not of the rig.

import React, { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { addBreakpoint, removeBreakpoint } from '../../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import { breakpointLinesForFile, codeBreakpointId, parseCodeBreakpointId } from './codeBreakpoints';
import { CODE_DE, formatCode } from './codeMessagesDe';
import { CODE_LIMITS, ENTRY_FILE, validateProjectPath } from './codeProject';

const CodeEditor = lazy(() => import('./CodeEditor'));

const CODE_LAST_FILE_KEY = 'edubotics_code_last_file';

/** Python debugs (A8); Java runs and stops. The list is the whole policy. */
const DEBUGGABLE_LANGUAGES = Object.freeze(['python']);

/**
 * Toggling ten breakpoints in a row must cost ONE service call, not ten — the
 * same 250 ms `BreakpointList` settled on, for the same reason: every call
 * contends for `useRosServiceCaller`'s 10 s timeout.
 */
const BREAKPOINT_PUSH_DEBOUNCE_MS = 250;

function readLastFile() {
  try {
    return window.localStorage.getItem(CODE_LAST_FILE_KEY);
  } catch (_) {
    return null;
  }
}

function writeLastFile(path) {
  try {
    window.localStorage.setItem(CODE_LAST_FILE_KEY, path);
  } catch (_) { /* quota / private mode — the in-memory choice still holds */ }
}

/**
 * The editor is a lazy chunk, and a chunk can fail to arrive: a WebView2 whose
 * cache survived an image update asks for a file the new image no longer
 * serves (the webview URL carries a cache-busting `_v=<IMAGE_TAG>` for exactly
 * that reason). Without a boundary that throw unmounts the whole Roboter-
 * Studio page — a white screen instead of a sentence the student can act on.
 * Scoped to the editor pane alone, so the file tree survives.
 */
class EditorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <p className="px-3 py-4 text-xs text-[var(--ink-3)]">{CODE_DE.EDITOR_FAILED}</p>
      );
    }
    return this.props.children;
  }
}

const sortPaths = (paths) => paths.slice().sort((a, b) => {
  const da = a.includes('/') ? 1 : 0;
  const db = b.includes('/') ? 1 : 0;
  return da - db || a.localeCompare(b, 'de');
});

function CodeWorkspace({ language, files, onFilesChange, readOnly = false }) {
  const entry = ENTRY_FILE[language];
  const paths = useMemo(() => sortPaths(Object.keys(files || {})), [files]);
  const [active, setActive] = useState(() => {
    const remembered = readLastFile();
    return remembered && files && Object.prototype.hasOwnProperty.call(files, remembered)
      ? remembered
      : entry;
  });
  // A deleted or renamed active file, or a project swap, falls back to the entry.
  useEffect(() => {
    if (!files || !Object.prototype.hasOwnProperty.call(files, active)) {
      setActive(entry);
    }
  }, [files, active, entry]);

  const open = useCallback((path) => {
    setActive(path);
    writeLastFile(path);
  }, []);

  const handleContentChange = useCallback((content) => {
    if (!files || files[active] === content) return;
    onFilesChange({ ...files, [active]: content });
  }, [files, active, onFilesChange]);

  const handleNewFile = useCallback(() => {
    if (paths.length >= CODE_LIMITS.MAX_CODE_FILES) {
      toast.error(formatCode(CODE_DE.ERR_TOO_MANY_FILES, CODE_LIMITS.MAX_CODE_FILES));
      return;
    }
    const raw = typeof window !== 'undefined' ? window.prompt(CODE_DE.FILE_NEW_PROMPT, '') : null;
    if (raw == null) return;
    const path = raw.trim();
    const error = validateProjectPath(path, language);
    if (error) { toast.error(error); return; }
    if (Object.prototype.hasOwnProperty.call(files, path)) {
      toast.error(formatCode(CODE_DE.ERR_FILE_EXISTS, path));
      return;
    }
    onFilesChange({ ...files, [path]: '' });
    open(path);
  }, [paths.length, language, files, onFilesChange, open]);

  const handleRename = useCallback(() => {
    if (active === entry) { toast.error(formatCode(CODE_DE.ERR_ENTRY_RENAME, entry)); return; }
    const raw = typeof window !== 'undefined'
      ? window.prompt(formatCode(CODE_DE.FILE_RENAME_PROMPT, active), active) : null;
    if (raw == null) return;
    const path = raw.trim();
    if (path === active) return;
    const error = validateProjectPath(path, language);
    if (error) { toast.error(error); return; }
    if (Object.prototype.hasOwnProperty.call(files, path)) {
      toast.error(formatCode(CODE_DE.ERR_FILE_EXISTS, path));
      return;
    }
    const next = {};
    for (const p of Object.keys(files)) next[p === active ? path : p] = files[p];
    onFilesChange(next);
    open(path);
  }, [active, entry, language, files, onFilesChange, open]);

  const handleDelete = useCallback(() => {
    if (active === entry) { toast.error(formatCode(CODE_DE.ERR_ENTRY_DELETE, entry)); return; }
    if (typeof window !== 'undefined' && !window.confirm(formatCode(CODE_DE.FILE_DELETE_CONFIRM, active))) {
      return;
    }
    const next = { ...files };
    delete next[active];
    onFilesChange(next);
    open(entry);
  }, [active, entry, files, onFilesChange, open]);

  /*
   * The debugger (§3.5). The slice gets NO new state: the breakpoint ids live
   * where the Blockly ones already do (`s.workshop.breakpoints`, through the
   * existing add/remove reducers) and the highlighted line is DERIVED here from
   * `currentBlockId` + `phase`/`paused`. Both id spaces share those two fields,
   * so `parseCodeBreakpointId` is what tells them apart — a Blockly id resolves
   * to nothing and lights up no line.
   */
  const dispatch = useDispatch();
  const { setWorkflowBreakpoints } = useRosServiceCaller();
  const breakpoints = useSelector((s) => s.workshop.breakpoints);
  const currentBlockId = useSelector((s) => s.workshop.currentBlockId);
  const runState = useSelector((s) => s.workshop.runState);
  const phase = useSelector((s) => s.workshop.phase);
  const paused = useSelector((s) => s.workshop.paused);

  const debuggable = !readOnly && DEBUGGABLE_LANGUAGES.includes(language);
  const breakpointLines = useMemo(
    () => (debuggable ? breakpointLinesForFile(breakpoints, active) : []),
    [debuggable, breakpoints, active],
  );

  const handleToggleBreakpoint = useCallback((line) => {
    if (!Number.isInteger(line) || line < 1) return;
    const id = codeBreakpointId(active, line);
    dispatch(breakpoints.includes(id) ? removeBreakpoint(id) : addBreakpoint(id));
  }, [active, breakpoints, dispatch]);

  // Push to the runtime only while a program is LIVE. Before Start the set
  // rides `RunControls.handleStart`, which pushes it before `/workflow/start`
  // so the first blocks cannot outrun it; pushing here too would answer „Es
  // läuft kein Workflow." once per toggle. Best-effort by design: a failure is
  // reported by the run itself, not by a toast per keystroke.
  const live = runState === 'running' || paused;
  useEffect(() => {
    if (!debuggable || !live) return undefined;
    const timer = setTimeout(() => {
      Promise.resolve(setWorkflowBreakpoints(breakpoints)).catch(() => {});
    }, BREAKPOINT_PUSH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [debuggable, live, breakpoints, setWorkflowBreakpoints]);

  const highlight = useMemo(() => {
    const parsed = parseCodeBreakpointId(currentBlockId);
    if (!parsed || parsed.path !== active) return { line: null, kind: null };
    if (phase === 'error') return { line: parsed.line, kind: 'error' };
    if (paused) return { line: parsed.line, kind: 'paused' };
    if (runState === 'running') return { line: parsed.line, kind: 'running' };
    return { line: null, kind: null };
  }, [currentBlockId, active, phase, paused, runState]);

  const smallButton = 'text-xs px-2 py-1 rounded-md border border-[var(--line)] bg-white '
    + 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] disabled:opacity-50 disabled:cursor-not-allowed';

  return (
    <div className="flex h-full w-full min-h-0" data-testid="code-workspace" data-language={language}>
      <aside className="w-44 shrink-0 border-r border-[var(--line)] bg-[var(--bg-sunk)] flex flex-col min-h-0">
        <div className="px-2 py-1.5 text-[11px] font-semibold text-[var(--ink-3)] uppercase tracking-wide">
          {CODE_DE.FILES_TITLE}
        </div>
        <ul className="flex-1 min-h-0 overflow-auto px-1" role="listbox" aria-label={CODE_DE.FILES_TITLE}>
          {paths.map((path) => (
            <li key={path}>
              <button
                type="button"
                role="option"
                aria-selected={path === active}
                onClick={() => open(path)}
                title={path === entry ? CODE_DE.FILE_ENTRY_TITLE : path}
                className={
                  'w-full text-left truncate text-xs px-2 py-1 rounded '
                  + (path === active
                    ? 'bg-[var(--accent)] text-white'
                    : 'text-[var(--ink)] hover:bg-white')
                }
              >
                {path === entry ? '▶ ' : ''}{path}
              </button>
            </li>
          ))}
        </ul>
        {!readOnly && (
          <>
            <p className="px-2 py-1.5 text-[10px] leading-snug text-[var(--ink-4)] border-t border-[var(--line)]">
              {debuggable ? CODE_DE.DEBUG_BP_HINT_PY : CODE_DE.DEBUG_JAVA_NO_BREAKPOINTS}
            </p>
            <div className="p-1.5 flex flex-col gap-1 border-t border-[var(--line)]">
              <button type="button" onClick={handleNewFile} className={smallButton}>
                + {CODE_DE.FILE_NEW}
              </button>
              <div className="flex gap-1">
                <button type="button" onClick={handleRename} disabled={active === entry} className={smallButton + ' flex-1'}>
                  {CODE_DE.FILE_RENAME}
                </button>
                <button type="button" onClick={handleDelete} disabled={active === entry} className={smallButton + ' flex-1'}>
                  {CODE_DE.FILE_DELETE}
                </button>
              </div>
            </div>
          </>
        )}
      </aside>
      <div className="flex-1 min-w-0 min-h-0">
        <EditorBoundary>
          <Suspense
            fallback={
              <p className="px-3 py-4 text-xs text-[var(--ink-3)]">{CODE_DE.EDITOR_LOADING}</p>
            }
          >
            <CodeEditor
              language={language}
              path={active}
              value={files ? files[active] : ''}
              onChange={handleContentChange}
              readOnly={readOnly}
              breakpointLines={breakpointLines}
              onToggleBreakpoint={debuggable ? handleToggleBreakpoint : null}
              highlightLine={highlight.line}
              highlightKind={highlight.kind}
            />
          </Suspense>
        </EditorBoundary>
      </div>
    </div>
  );
}

export default CodeWorkspace;
