/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The workflow-creation dialog: „Neu ▾" opens a popover with the ONE choice a
// workflow makes at birth — Blöcke, Python or Java. One workflow is one
// language and the choice is immutable afterwards (D3), which the popover
// says. Before this dialog a workflow was created implicitly on its first save;
// a Blockly document still is, so „Blöcke" here only clears the editor.
//
// Same popover shape as WorkshopPage's OpenWorkflowPopover (closes on pick or
// outside click).

import React, { useEffect, useRef, useState } from 'react';
import { CODE_DE } from './codeMessagesDe';

const CHOICES = [
  { id: 'blocks', label: CODE_DE.LANG_BLOCKS, hint: CODE_DE.LANG_BLOCKS_HINT, icon: '🧩' },
  { id: 'python', label: CODE_DE.LANG_PYTHON, hint: CODE_DE.LANG_PYTHON_HINT, icon: '🐍' },
  { id: 'java', label: CODE_DE.LANG_JAVA, hint: CODE_DE.LANG_JAVA_HINT, icon: '☕' },
];

function NewProgramDialog({ onCreate, disabled = false }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={CODE_DE.NEW_TITLE}
        disabled={disabled}
        title={CODE_DE.NEW_TITLE}
        className={
          'inline-flex items-center gap-1 min-h-[28px] px-3 py-1.5 rounded-md '
          + 'text-sm font-medium border border-[var(--line)] bg-white text-[var(--ink)] '
          + 'hover:bg-[var(--bg-sunk)] focus:outline-none focus-visible:ring-2 '
          + 'focus-visible:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed'
        }
      >
        ✨ {CODE_DE.NEW}
        <span className="text-[10px]" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={CODE_DE.NEW_TITLE}
          className="absolute z-30 mt-1 left-0 w-72 rounded-md border border-[var(--line)] bg-white shadow-lg p-2"
        >
          <p className="px-1 pb-1.5 text-[11px] text-[var(--ink-3)]">{CODE_DE.NEW_LANGUAGE_FIXED}</p>
          <ul className="flex flex-col gap-1">
            {CHOICES.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    if (onCreate) onCreate(c.id);
                  }}
                  className="w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md hover:bg-[var(--bg-sunk)]"
                >
                  <span className="text-base leading-5" aria-hidden="true">{c.icon}</span>
                  <span className="min-w-0">
                    <span className="block text-sm font-medium text-[var(--ink)]">{c.label}</span>
                    <span className="block text-[11px] text-[var(--ink-3)]">{c.hint}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default NewProgramDialog;
