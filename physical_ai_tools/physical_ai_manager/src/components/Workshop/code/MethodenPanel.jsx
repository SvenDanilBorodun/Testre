/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The „Methoden" column beside the code editor (owner decision C): every
// robot command of the program's language, grouped like the Blockly toolbox
// (methodGroups.js), each with its German explanation, its signature(s), its
// parameters (doc, range, default), what it gives back, and an example line —
// filled with the student's own first Ziel / Bewegung / Objekt where the
// command takes one (methodDocs.exampleArgsFor).
//
// „Einfügen" writes the example below the line the cursor is on, through the
// code document's ONE checked path (`assetDoc.insertSnippet({kind: 'call'})` →
// codeInsert.snippetLines + insertionTarget): every refusal and the
// „Klicke zuerst in deinen Code" hint are the drawer's, handled the drawer's
// way. The example row can also be dragged onto the text (the drawer's
// SNIPPET_MIME, the editor's drop). The two types (Greifziel, Greifobjekt)
// are explanations only: their examples are more than one line.
//
// LAZY: CodeWorkspace loads this file through React.lazy, so the docs JSON
// (methodDocs.js) and the toolbox walk (methodGroups.js) never reach the
// entry bundle (methodDocs.lazy.test.js).

import React, { useId, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import Icon from '../../icons/Icon';
import { CODE_DE, formatCode } from './codeMessagesDe';
import { SNIPPET_MIME } from './snippetMime';
import {
  METHOD_DOCS,
  defaultTextFor,
  exampleArgsFor,
  extraVariants,
  methodShortForm,
  rangeTextDe,
  renderExample,
} from './methodDocs';
import { methodGroups } from './methodGroups';

const MONO = 'font-mono text-[12px]';

function docOf(method, java) {
  return java ? (method.doc_de_java ?? method.doc_de) : method.doc_de;
}

function ParamRow({ param, java }) {
  const name = java ? (param.java_name || param.name) : param.name;
  const range = rangeTextDe(param);
  const def = defaultTextFor(param, java ? 'java' : 'python');
  const meta = [
    range || null,
    def !== null ? formatCode(CODE_DE.METHODS_DEFAULT, def) : null,
    param.required === false ? CODE_DE.METHODS_OPTIONAL : null,
  ].filter(Boolean).join(' · ');
  return (
    <li className="leading-snug">
      <code className={`${MONO} font-semibold text-[var(--ink)]`}>{name}</code>
      {' — '}
      <span>{param.doc_de}</span>
      {meta && <span className="block text-[11px] text-[var(--ink-4)]">{meta}</span>}
    </li>
  );
}

function MethodEntry({
  method, java, language, open, onToggle, line, canInsert, canDrag, inserting, onInsert,
}) {
  const detailId = useId();
  const short = methodShortForm(method, language);
  const variants = extraVariants(method, language);
  const doc = docOf(method, java);
  const signatures = java ? (method.signatures_java || []) : [method.signature_py];
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? detailId : undefined}
        onClick={onToggle}
        className="w-full rounded px-2 py-1 text-left hover:bg-[var(--bg-sunk)]"
      >
        <code className={`block truncate ${MONO} text-[var(--ink)]`}>
          {short}
          {variants > 0 && (
            <span className="text-[var(--ink-4)]">{` ${formatCode(CODE_DE.METHODS_VARIANTS, variants)}`}</span>
          )}
        </code>
        <span className="block truncate text-[11px] text-[var(--ink-3)]">{doc}</span>
      </button>
      {open && (
        <div id={detailId} className="flex flex-col gap-1.5 px-2 pb-2 pt-1 text-xs text-[var(--ink-2)]">
          <div className="flex flex-col">
            {signatures.map((sig) => (
              <code key={sig} className={`${MONO} break-words text-[var(--ink)]`}>{sig}</code>
            ))}
          </div>
          <p className="leading-snug">{doc}</p>
          {method.params.length > 0 && (
            <div>
              <h4 className="text-[11px] font-semibold text-[var(--ink-3)]">{CODE_DE.METHODS_PARAMS}</h4>
              <ul className="flex flex-col gap-1">
                {method.params.map((p) => <ParamRow key={p.name} param={p} java={java} />)}
              </ul>
            </div>
          )}
          {method.returns_de && (
            <div>
              <h4 className="text-[11px] font-semibold text-[var(--ink-3)]">{CODE_DE.METHODS_RETURNS}</h4>
              <p className="leading-snug">{method.returns_de}</p>
            </div>
          )}
          <div>
            <h4 className="text-[11px] font-semibold text-[var(--ink-3)]">{CODE_DE.METHODS_EXAMPLE}</h4>
            <div
              className="flex items-center gap-1.5 rounded border border-[var(--line)] bg-[var(--bg-sunk)] px-1.5 py-1"
              draggable={canDrag ? 'true' : undefined}
              onDragStart={canDrag ? (e) => {
                e.dataTransfer.setData(SNIPPET_MIME, JSON.stringify({ kind: 'call', method: method.name, line }));
                e.dataTransfer.effectAllowed = 'copy';
              } : undefined}
              data-testid={`method-example-${method.name}`}
            >
              <code className={`min-w-0 flex-1 break-all ${MONO} text-[var(--ink)]`}>{line}</code>
              {canInsert && (
                <button
                  type="button"
                  onClick={() => onInsert(method, line)}
                  disabled={inserting}
                  aria-busy={inserting || undefined}
                  aria-label={formatCode(CODE_DE.METHODS_INSERT_ARIA, short)}
                  title={CODE_DE.SAMMLUNG_INSERT_TITLE}
                  className="shrink-0 rounded px-1.5 py-0.5 text-xs font-medium text-[var(--accent)] hover:bg-white disabled:cursor-wait disabled:opacity-50"
                >
                  {CODE_DE.SAMMLUNG_INSERT}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </li>
  );
}

function TypeEntry({
  type, java, open, onToggle,
}) {
  const detailId = useId();
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? detailId : undefined}
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left hover:bg-[var(--bg-sunk)]"
      >
        <code className={`min-w-0 flex-1 truncate ${MONO} text-[var(--ink)]`}>{java ? type.java_name : type.name}</code>
        <span className="shrink-0 rounded bg-[var(--bg-sunk)] px-1 text-[10px] font-semibold text-[var(--ink-3)]">
          {CODE_DE.METHODS_TYPE}
        </span>
      </button>
      {open && (
        <div id={detailId} className="flex flex-col gap-1.5 px-2 pb-2 pt-1 text-xs">
          <p className="leading-snug">{type.doc_de}</p>
          <div>
            <h4 className="text-[11px] font-semibold text-[var(--ink-3)]">{CODE_DE.METHODS_EXAMPLE}</h4>
            <pre className={`overflow-x-auto whitespace-pre rounded border border-[var(--line)] bg-[var(--bg-sunk)] px-1.5 py-1 ${MONO} text-[var(--ink)]`}>
              {java ? type.example_java : type.example_py}
            </pre>
          </div>
        </div>
      )}
    </li>
  );
}

/**
 * Props: `language` ('python' | 'java'), `assetDoc` (the code document — its
 * `insertSnippet` is „Einfügen"; none, no button), `readOnly` (no „Einfügen",
 * no drag), `sammlung` (`{places, recordings, objects}`: the student's names
 * for the examples), `floating` (the panel lies over the code: Esc closes it),
 * `onClose`.
 */
export default function MethodenPanel({
  language, assetDoc = null, readOnly = false, sammlung = null, floating = false, onClose = null,
}) {
  const java = language === 'java';
  const groups = useMemo(() => methodGroups(METHOD_DOCS), []);
  const [open, setOpen] = useState(null);
  const canInsert = !readOnly && !!assetDoc && typeof assetDoc.insertSnippet === 'function';
  const canDrag = !readOnly;

  // One insertion at a time, handled exactly like the drawer's „Einfügen":
  // a thrown load is the module failing to arrive; an `error` is the German
  // reason the spot was refused (or the click-first hint); a count is done.
  const [inserting, setInserting] = useState(false);
  const insert = async (method, line) => {
    if (inserting) return;
    setInserting(true);
    try {
      let result;
      try {
        result = await assetDoc.insertSnippet({ kind: 'call', method: method.name, line });
      } catch (err) {
        console.error('insertSnippet failed:', err);
        toast.error(CODE_DE.SAMMLUNG_INSERT_FAILED);
        return;
      }
      if (result && result.error) {
        toast.error(result.error);
        return;
      }
      if (result && result.count > 0) {
        toast.success(formatCode(CODE_DE.INSERTED_AT, result.file, result.firstLine));
      }
    } finally {
      setInserting(false);
    }
  };

  const toggle = (key) => setOpen((prev) => (prev === key ? null : key));

  const handleKeyDown = (e) => {
    if (!floating || e.key !== 'Escape') return;
    e.stopPropagation();
    onClose?.();
  };

  return (
    <section
      aria-label={CODE_DE.METHODS_TITLE}
      onKeyDown={handleKeyDown}
      className="flex flex-1 flex-col overflow-hidden bg-white"
      data-testid="methoden-panel"
    >
      <header className="flex items-center gap-1 border-b border-[var(--line)] px-2 py-1">
        <Icon name="methods" className="text-[var(--ink-3)]" />
        <h2 className="min-w-0 flex-1 truncate text-[11px] font-semibold uppercase tracking-wide text-[var(--ink-3)]">
          {CODE_DE.METHODS_TITLE}
        </h2>
        <button
          type="button"
          onClick={() => onClose?.()}
          aria-label={CODE_DE.METHODS_CLOSE}
          title={CODE_DE.METHODS_CLOSE}
          className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)]"
        >
          <Icon name="close" />
        </button>
      </header>
      <p className="border-b border-[var(--line)] px-2 py-1 text-[10px] leading-snug text-[var(--ink-4)]">
        {CODE_DE.METHODS_HELP}
      </p>
      <div className="flex-1 overflow-y-auto px-1 py-1">
        {groups.map((group) => (
          <section key={group.id} aria-label={group.label} className="mb-1.5">
            <h3
              className="mx-1 mb-0.5 border-l-4 pl-1.5 text-[11px] font-semibold text-[var(--ink-2)]"
              style={{ borderLeftColor: group.colour || 'transparent' }}
            >
              {group.label}
            </h3>
            <ul>
              {group.methods.map((m) => (
                <MethodEntry
                  key={m.name}
                  method={m}
                  java={java}
                  language={language}
                  open={open === `m:${m.name}`}
                  onToggle={() => toggle(`m:${m.name}`)}
                  line={renderExample(m, language, exampleArgsFor(m, sammlung))}
                  canInsert={canInsert}
                  canDrag={canDrag}
                  inserting={inserting}
                  onInsert={insert}
                />
              ))}
              {group.types.map((t) => (
                <TypeEntry
                  key={t.name}
                  type={t}
                  java={java}
                  open={open === `t:${t.name}`}
                  onToggle={() => toggle(`t:${t.name}`)}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </section>
  );
}
