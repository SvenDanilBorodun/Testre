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
//
// The code Sammlung (owner decisions O4–O7), when the page hands an asset
// document (`assetDoc`) and its Sammlung provider: a „Sammlung" section under
// the files — four counts that open the drawer on their tab, and „+ Neu" with
// the creation actions the flyout cards offer a Blockly program — plus the
// cursor „Einfügen" writes below. The last cursor line is remembered PER FILE
// (in memory, never stored) and reported to the page as `{file, line}`; a file
// the student opened but never clicked into reports null, so an insertion then
// goes to the end of main (codeInsert.insertionTarget). A reveal request from
// the page (`{file, line, nonce}`: a „Benutzt in" jump, an insertion) switches
// to that file and puts the caret on that line.

import React, {
  Suspense, lazy, useCallback, useDeferredValue, useEffect, useMemo, useReducer, useRef, useState,
} from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { addBreakpoint, removeBreakpoint } from '../../../features/workshop/workshopSlice';
import { openDrawer } from '../../../features/workshop/studioAssetsSlice';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import { DE } from '../blocks/messages_de';
import { newActionsFor } from '../sammlung/newActions';
import { breakpointLinesForFile, codeBreakpointId, parseCodeBreakpointId } from './codeBreakpoints';
import { buildCodeAssetKnowledge } from './codeAssetCompletion';
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

// The sidebar's Sammlung rows: [drawer tab, label, index count key].
const SAMMLUNG_ROWS = Object.freeze([
  ['aufnahmen', DE.CATEGORY_AUFNAHMEN],
  ['ziele', DE.CATEGORY_ZIELE],
  ['positionen', DE.CATEGORY_POSITIONEN],
  ['variablen', DE.CATEGORY_VARIABLEN],
]);

const NO_OBJECT_TYPES = Object.freeze([]);

function snapshotOf(provider) {
  try {
    return provider && typeof provider.getSnapshot === 'function' ? provider.getSnapshot() : null;
  } catch (_) {
    return null;
  }
}

/**
 * The „Sammlung" section of the sidebar: the counts (each opens the drawer on
 * its tab) and „+ Neu". Rendered only with an asset document.
 */
function SammlungSection({ counts, actions, onOpen, onAction, buttonClass }) {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <section
      aria-label={DE.SAMMLUNG_TITLE}
      className="border-t border-[var(--line)] px-1 py-1.5"
    >
      <div className="px-1 pb-1 text-[11px] font-semibold text-[var(--ink-3)] uppercase tracking-wide">
        {DE.SAMMLUNG_TITLE}
      </div>
      {SAMMLUNG_ROWS.map(([tab, label]) => (
        <button
          key={tab}
          type="button"
          onClick={() => onOpen(tab)}
          title={formatCode(CODE_DE.SAMMLUNG_OPEN_TAB, label)}
          className="w-full text-left text-xs px-2 py-0.5 rounded text-[var(--ink)] hover:bg-white"
        >
          {`${label} ${counts[tab] ?? 0}`}
        </button>
      ))}
      {actions.length > 0 && (
        <div className="relative px-1 pt-1">
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            className={buttonClass + ' w-full'}
          >
            {`+ ${CODE_DE.SAMMLUNG_NEW}`}
          </button>
          {menuOpen && (
            <ul
              role="menu"
              aria-label={CODE_DE.SAMMLUNG_NEW_MENU}
              className="absolute left-1 right-1 bottom-full mb-1 z-10 rounded-md border border-[var(--line)] bg-white py-1 shadow"
            >
              {actions.map(({ label, action }) => (
                <li key={label} role="none">
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuOpen(false);
                      onAction(action);
                    }}
                    className="w-full text-left text-xs px-2 py-1 text-[var(--ink)] hover:bg-[var(--bg-sunk)]"
                  >
                    {label}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

const sortPaths = (paths) => paths.slice().sort((a, b) => {
  const da = a.includes('/') ? 1 : 0;
  const db = b.includes('/') ? 1 : 0;
  return da - db || a.localeCompare(b, 'de');
});

function CodeWorkspace({
  language, files, onFilesChange, readOnly = false,
  assetDoc = null, provider = null, onCursorChange = null, revealRequest = null,
  objectTypes = NO_OBJECT_TYPES,
}) {
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

  /*
   * The cursor „Einfügen" writes below. `cursorByFile` is the last line the
   * student's cursor was on, per file; `editorReveal` is what the editor is
   * asked to show (`{line, nonce}`, a fresh nonce per request).
   */
  const cursorByFile = useRef(new Map());
  const revealSeq = useRef(0);
  const [editorReveal, setEditorReveal] = useState(null);
  const onCursorRef = useRef(onCursorChange);
  useEffect(() => { onCursorRef.current = onCursorChange; }, [onCursorChange]);
  const reportCursor = useCallback((at) => { onCursorRef.current?.(at); }, []);
  const revealInEditor = useCallback((line) => {
    revealSeq.current += 1;
    setEditorReveal({ line, nonce: revealSeq.current });
  }, []);

  const open = useCallback((path) => {
    setActive(path);
    writeLastFile(path);
    const line = cursorByFile.current.get(path);
    if (Number.isInteger(line)) {
      reportCursor({ file: path, line });
      revealInEditor(line);
    } else {
      // No remembered line: no caret request either — the previous file's
      // reveal must not land in this one while the page's cursor is null
      // (review m5: the caret and the insertion point must agree).
      setEditorReveal(null);
      reportCursor(null);
    }
  }, [reportCursor, revealInEditor]);

  const handleEditorCursor = useCallback((line) => {
    if (!Number.isInteger(line) || line < 1) return;
    cursorByFile.current.set(active, line);
    reportCursor({ file: active, line });
  }, [active, reportCursor]);

  // The page's reveal: switch to its file (if the program has it), caret there.
  const pageRevealNonce = revealRequest ? revealRequest.nonce : null;
  useEffect(() => {
    if (!revealRequest || typeof revealRequest.file !== 'string' || !Number.isInteger(revealRequest.line)) return;
    if (!files || !Object.prototype.hasOwnProperty.call(files, revealRequest.file)) return;
    const { file, line } = revealRequest;
    setActive(file);
    cursorByFile.current.set(file, line);
    reportCursor({ file, line });
    revealInEditor(line);
    // Once per nonce — the files change on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageRevealNonce]);

  // An UPDATER, not `{...files}`: the page applies it to its latest files
  // (codeFilesRef), so a keystroke landing between an edit the page applied
  // (a drawer rename) and React's next render keeps that edit (review n8).
  const handleContentChange = useCallback((content) => {
    if (!files || files[active] === content) return;
    onFilesChange((latest) => {
      const base = latest && typeof latest === 'object' ? latest : files;
      return base[active] === content ? base : { ...base, [active]: content };
    });
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
    const remembered = cursorByFile.current.get(active);
    cursorByFile.current.delete(active);
    if (Number.isInteger(remembered)) cursorByFile.current.set(path, remembered);
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
    cursorByFile.current.delete(active);
    onFilesChange(next);
    open(entry);
  }, [active, entry, files, onFilesChange, open]);

  /*
   * The debugger (§3.5). The slice gets NO new state: the breakpoint ids live
   * where the Blockly ones already do (`s.workshop.breakpoints`, through the
   * existing add/remove reducers) and the highlighted line is DERIVED here from
   * `currentBlockId` + `runState`/`paused`. Both id spaces share those fields,
   * so `parseCodeBreakpointId` is what tells them apart — a Blockly id resolves
   * to nothing and lights up no line.
   */
  const dispatch = useDispatch();
  const { setWorkflowBreakpoints } = useRosServiceCaller();
  const breakpoints = useSelector((s) => s.workshop.breakpoints);
  const currentBlockId = useSelector((s) => s.workshop.currentBlockId);
  const runState = useSelector((s) => s.workshop.runState);
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

  // Only the two kinds a LIVE run can reach. There is deliberately no `error`
  // kind: the server does publish `<file>:L<line>` with phase `error`
  // (`code_program._raise_error`), but `useRosTopicSubscription` answers that
  // tick with `setWorkflowStatus` AND `setRunState('error')` in one callback,
  // and the terminal branch of `setRunState` nulls `currentBlockId` and blanks
  // `phase` — so the committed state after ANY error tick carries no id. A
  // branch on it would be dead code claiming a feature the student never gets;
  // the line is named in the German error banner instead. Reviving it means
  // changing that reducer first (see docs/KNOWN-ISSUES.md).
  const highlight = useMemo(() => {
    const parsed = parseCodeBreakpointId(currentBlockId);
    if (!parsed || parsed.path !== active) return { line: null, kind: null };
    if (paused) return { line: parsed.line, kind: 'paused' };
    if (runState === 'running') return { line: parsed.line, kind: 'running' };
    return { line: null, kind: null };
  }, [currentBlockId, active, paused, runState]);

  /*
   * The Sammlung: counts for the section and the names the editor completes,
   * warns about and describes. Both re-derive on a store or provider change
   * (a new Ziel, a recording list arriving) and on the files — deferred, so a
   * keystroke never waits for a whole-project scan.
   */
  const [sammlungTick, bumpSammlung] = useReducer((n) => n + 1, 0);
  useEffect(() => {
    const offs = [];
    if (assetDoc && typeof assetDoc.subscribe === 'function') offs.push(assetDoc.subscribe(bumpSammlung));
    if (provider && typeof provider.subscribe === 'function') offs.push(provider.subscribe(bumpSammlung));
    return () => offs.forEach((off) => off());
  }, [assetDoc, provider]);
  const deferredFiles = useDeferredValue(files);
  const sammlungCounts = useMemo(() => {
    if (!assetDoc) return null;
    try {
      return assetDoc.buildIndex(snapshotOf(provider) || undefined).counts || {};
    } catch (_) {
      return {};
    }
    // sammlungTick and deferredFiles are the change signals of what
    // buildIndex reads through the document.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetDoc, provider, sammlungTick, deferredFiles]);
  const assets = useMemo(() => {
    if (!assetDoc) return null;
    const store = assetDoc.getStore();
    const snapshot = snapshotOf(provider);
    return buildCodeAssetKnowledge({
      files: deferredFiles,
      language,
      entries: store && typeof store.getEntries === 'function' ? store.getEntries() : [],
      trajectories: snapshot ? snapshot.trajectories : null,
      objectTypes,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetDoc, provider, sammlungTick, deferredFiles, language, objectTypes]);
  const newActions = useMemo(
    () => (readOnly || !assetDoc ? [] : newActionsFor((snapshotOf(provider) || {}).capabilities)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [readOnly, assetDoc, provider, sammlungTick],
  );
  const openSammlungTab = useCallback((tab) => {
    dispatch(openDrawer({ tab, focusId: null }));
  }, [dispatch]);
  const dispatchSammlungAction = useCallback((action) => {
    if (provider && typeof provider.dispatchAction === 'function') provider.dispatchAction(action);
  }, [provider]);

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
        {sammlungCounts && (
          <SammlungSection
            counts={sammlungCounts}
            actions={newActions}
            onOpen={openSammlungTab}
            onAction={dispatchSammlungAction}
            buttonClass={smallButton}
          />
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
              assets={assets}
              revealRequest={editorReveal}
              onCursorChange={handleEditorCursor}
            />
          </Suspense>
        </EditorBoundary>
      </div>
    </div>
  );
}

export default CodeWorkspace;
