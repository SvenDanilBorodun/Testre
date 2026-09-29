/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// What a teacher sees of one student's Roboter-Studio work (decision A3): the
// programs as they stand right now, and the snapshots the student deliberately
// handed in („Abgeben"). The drawer shape is `StudentTrainingHistoryDrawer`'s,
// down to the backdrop and the close button, because a teacher should not have
// to learn a second one.
//
// It ships in the TEACHER build (Railway W3), which has no rosbridge, no robot
// and no student session: everything here comes from the four Cloud-API reads
// of §3.12 and nothing else. It also stores NOTHING in the browser — a
// staffroom PC is the same handover problem as a shared student PC
// (utils/sessionScope.js), and the simplest answer to it is to keep no key.
//
// CodeMirror arrives through `React.lazy`: the viewer is one of exactly two
// files allowed to import it (package.json `no-restricted-imports`), and a
// static import here would put it in the teacher dashboard's own chunk. The
// Blockly preview is lazy for the same reason.

import React, { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { useSelector } from 'react-redux';
import {
  getStudentSubmission,
  getStudentWorkflow,
  listStudentSubmissions,
  listStudentWorkflows,
} from '../../services/teacherApi';
import { Avatar, Pill } from '../EbUI';
import Icon from '../icons/Icon';

const CodeViewer = lazy(() => import('./CodeViewer'));
const BlocklyPreview = lazy(() => import('../Workshop/BlocklyWorkspace'));

const DE = Object.freeze({
  SUBTITLE: 'Programme · Abgaben',
  CLOSE: 'Schließen',
  LOADING: 'Laden…',
  PROGRAMS: 'Aktuelle Programme',
  SUBMISSIONS: 'Abgaben',
  EMPTY: 'Diese Schülerin/dieser Schüler hat noch kein Programm angelegt.',
  NO_SUBMISSIONS: 'Noch nichts abgegeben.',
  LIST_FAILED: 'Programme konnten nicht geladen werden.',
  DOC_FAILED: 'Das Programm konnte nicht geladen werden.',
  DOC_GONE: 'Dieses Programm gibt es nicht mehr — vielleicht wurde es gelöscht.',
  FORBIDDEN: 'Keine Berechtigung — bitte neu anmelden.',
  VIEWER_LOADING: 'Ansicht wird geladen …',
  LANG_BLOCKS: 'Blöcke',
  LANG_PYTHON: 'Python',
  LANG_JAVA: 'Java',
  SUBMITTED: 'Abgabe vom {0}',
  EMPTY_PROGRAM: 'Dieses Programm ist noch leer.',
  DOCUMENT: 'Ausgewähltes Programm',
});

const LANGUAGE_LABEL = { python: DE.LANG_PYTHON, java: DE.LANG_JAVA };

/**
 * Every failure the teacher is shown is ONE of these sentences, chosen by the
 * HTTP status. The thrown `Error.message` is never rendered: `apiClient`
 * carries the API's own `detail` there, but it also carries „Failed to fetch"
 * from a dead network, and a teacher can act on neither.
 */
function failureDe(err, fallback) {
  const status = err && err.status;
  if (status === 404) return DE.DOC_GONE;
  if (status === 401 || status === 403) return DE.FORBIDDEN;
  return fallback;
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('de-DE');
  } catch {
    return iso;
  }
}

/** The entry file first, then the rest the way the editor orders them. */
function sortFiles(paths) {
  return paths.slice().sort((a, b) => {
    const ea = a === 'main.py' || a === 'Main.java' ? 0 : 1;
    const eb = b === 'main.py' || b === 'Main.java' ? 0 : 1;
    const da = a.includes('/') ? 1 : 0;
    const db = b.includes('/') ? 1 : 0;
    return ea - eb || da - db || a.localeCompare(b, 'de');
  });
}

function LanguagePill({ language }) {
  const label = LANGUAGE_LABEL[language] || DE.LANG_BLOCKS;
  return <Pill tone={language ? 'accent' : 'neutral'}>{label}</Pill>;
}

/** One row of either list — the whole card is the button. */
function ProgramButton({ title, subtitle, language, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'w-full text-left p-3 border rounded-[var(--radius)] transition '
        + (active
          ? 'border-[var(--accent)] bg-[var(--accent-wash)]'
          : 'border-[var(--line)] bg-white hover:bg-[var(--bg-sunk)]')
      }
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-[var(--ink)] truncate">{title}</div>
          <div className="text-[11px] text-[var(--ink-3)] font-mono truncate">{subtitle}</div>
        </div>
        <LanguagePill language={language} />
      </div>
    </button>
  );
}

/** The document itself: code files with a file switcher, or the block preview. */
function DocumentView({ doc }) {
  // Memoised off `doc` rather than off `doc.code_files`: the `|| {}` fallback
  // would be a fresh object on every render and re-sort the list each time.
  const files = useMemo(() => (doc && doc.code_files) || {}, [doc]);
  const paths = useMemo(() => sortFiles(Object.keys(files)), [files]);
  const [active, setActive] = useState('');
  useEffect(() => { setActive(paths[0] || ''); }, [paths]);

  if (!doc) return null;

  if (!doc.code_language) {
    const json = doc.blockly_json;
    if (!json || Object.keys(json).length === 0) {
      return <p className="text-sm text-[var(--ink-3)]">{DE.EMPTY_PROGRAM}</p>;
    }
    return (
      <div className="h-72 border border-[var(--line)] rounded-md overflow-hidden">
        <Suspense fallback={<p className="p-3 text-xs text-[var(--ink-3)]">{DE.VIEWER_LOADING}</p>}>
          <BlocklyPreview key={doc.id} initialJson={json} readOnly />
        </Suspense>
      </div>
    );
  }

  if (paths.length === 0) {
    return <p className="text-sm text-[var(--ink-3)]">{DE.EMPTY_PROGRAM}</p>;
  }

  return (
    <div>
      <div className="flex flex-wrap gap-1 mb-2">
        {paths.map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => setActive(p)}
            className={
              'text-[11px] font-mono px-2 py-1 rounded-md border '
              + (p === active
                ? 'border-[var(--accent)] bg-[var(--accent-wash)] text-[var(--accent-ink)]'
                : 'border-[var(--line)] bg-white text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]')
            }
          >
            {p}
          </button>
        ))}
      </div>
      <div className="h-72 border border-[var(--line)] rounded-md overflow-hidden">
        <Suspense fallback={<p className="p-3 text-xs text-[var(--ink-3)]">{DE.VIEWER_LOADING}</p>}>
          <CodeViewer language={doc.code_language} path={active} value={files[active] || ''} />
        </Suspense>
      </div>
    </div>
  );
}

export default function StudentProgramsDrawer({ student, onClose }) {
  const token = useSelector((s) => s.auth.session?.access_token);
  const [programs, setPrograms] = useState([]);
  const [submissions, setSubmissions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState('');
  // The open document: {kind: 'workflow'|'submission', id} plus what came back.
  const [selected, setSelected] = useState(null);
  const [doc, setDoc] = useState(null);
  const [docError, setDocError] = useState('');
  const [docLoading, setDocLoading] = useState(false);

  useEffect(() => {
    if (!token || !student) return;
    let alive = true;
    setLoading(true);
    setListError('');
    Promise.all([
      listStudentWorkflows(token, student.id),
      listStudentSubmissions(token, student.id),
    ])
      .then(([w, s]) => {
        if (!alive) return;
        setPrograms(Array.isArray(w) ? w : []);
        setSubmissions(Array.isArray(s) ? s : []);
      })
      .catch((err) => {
        if (!alive) return;
        setPrograms([]);
        setSubmissions([]);
        setListError(failureDe(err, DE.LIST_FAILED));
      })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [token, student]);

  const openDocument = useCallback((kind, id) => {
    setSelected({ kind, id });
    setDoc(null);
    setDocError('');
    setDocLoading(true);
    const read = kind === 'submission'
      ? getStudentSubmission(token, student.id, id)
      : getStudentWorkflow(token, student.id, id);
    read
      .then((d) => setDoc(d))
      .catch((err) => setDocError(failureDe(err, DE.DOC_FAILED)))
      .finally(() => setDocLoading(false));
  }, [token, student]);

  if (!student) return null;

  const nothingAtAll = !loading && !listError
    && programs.length === 0 && submissions.length === 0;

  return (
    <div className="fixed inset-0 z-40 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/30 backdrop-blur-sm" />
      <aside
        className="relative w-[560px] max-w-full h-full bg-white border-l border-[var(--line)] shadow-pop flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-5 border-b border-[var(--line)] flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Avatar name={student.full_name || student.username} />
            <div className="min-w-0">
              <h3 className="font-semibold text-[var(--ink)] truncate">
                {student.full_name || student.username}
              </h3>
              <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
                {DE.SUBTITLE} · {student.username}
              </div>
            </div>
          </div>
          <button
            className="w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)] flex items-center justify-center transition shrink-0"
            onClick={onClose}
            aria-label={DE.CLOSE}
          >
            <Icon name="close" size={20} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {loading && <div className="text-[var(--ink-3)] text-sm">{DE.LOADING}</div>}
          {listError && <div className="text-sm text-[color:var(--danger)]">{listError}</div>}
          {nothingAtAll && <div className="text-[var(--ink-3)] text-sm">{DE.EMPTY}</div>}

          {!loading && !listError && programs.length > 0 && (
            <section aria-label={DE.PROGRAMS}>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)] mb-2">
                {DE.PROGRAMS}
              </h4>
              <div className="space-y-2">
                {programs.map((p) => (
                  <ProgramButton
                    key={p.id}
                    title={p.name}
                    subtitle={formatDate(p.updated_at)}
                    language={p.code_language}
                    active={!!selected && selected.kind === 'workflow' && selected.id === p.id}
                    onClick={() => openDocument('workflow', p.id)}
                  />
                ))}
              </div>
            </section>
          )}

          {!loading && !listError && !nothingAtAll && (
            <section aria-label={DE.SUBMISSIONS}>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)] mb-2">
                {DE.SUBMISSIONS}
              </h4>
              {submissions.length === 0 ? (
                <div className="text-[var(--ink-3)] text-sm">{DE.NO_SUBMISSIONS}</div>
              ) : (
                <div className="space-y-2">
                  {submissions.map((s) => (
                    <ProgramButton
                      key={s.id}
                      title={DE.SUBMITTED.replace('{0}', formatDate(s.submitted_at))}
                      subtitle={s.note ? `${s.name} · ${s.note}` : s.name}
                      language={s.code_language}
                      active={!!selected && selected.kind === 'submission' && selected.id === s.id}
                      onClick={() => openDocument('submission', s.id)}
                    />
                  ))}
                </div>
              )}
            </section>
          )}

          {selected && (
            <section aria-label={DE.DOCUMENT}>
              {docLoading && <div className="text-[var(--ink-3)] text-sm">{DE.LOADING}</div>}
              {docError && <div className="text-sm text-[color:var(--danger)]">{docError}</div>}
              {!docLoading && !docError && <DocumentView doc={doc} />}
            </section>
          )}
        </div>
      </aside>
    </div>
  );
}
