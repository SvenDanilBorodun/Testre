/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as Blockly from 'blockly/core';
import toast from 'react-hot-toast';
import { DE } from './blocks/messages_de';
import { formatAutosaveAge } from './useAutosave';
import { THEME_KEYS, applyTheme } from './themes';

const BUTTON_BASE =
  'inline-flex items-center justify-center min-h-[28px] min-w-[28px] '
  + 'px-3 py-1.5 rounded-md text-sm font-medium border border-[var(--line)] '
  + 'bg-white text-[var(--ink)] hover:bg-[var(--bg-sunk)] '
  + 'disabled:opacity-50 disabled:cursor-not-allowed '
  + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 '
  + 'focus-visible:ring-offset-1';

const PRIMARY_BUTTON =
  'inline-flex items-center justify-center min-h-[28px] '
  + 'px-3 py-1.5 rounded-md text-sm font-medium '
  + 'bg-[var(--accent)] text-white hover:opacity-90 '
  + 'disabled:opacity-50 disabled:cursor-not-allowed '
  + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 '
  + 'focus-visible:ring-offset-1';

function readSavedTheme() {
  try {
    return localStorage.getItem('edubotics:workshop:theme') || 'standard';
  } catch (e) {
    return 'standard';
  }
}

function persistTheme(name) {
  try {
    localStorage.setItem('edubotics:workshop:theme', name);
  } catch (e) {
    /* ignore quota errors — non-essential setting */
  }
}

/**
 * Toolbar above the Blockly workspace. Provides undo/redo, save,
 * export, import, theme switcher, and an autosave status chip. Acts on
 * the workspace via the ref; keeps no internal state about the
 * workflow.
 */
function ToolbarButtons({
  workspace,
  lastSavedAt,
  onSave,
  saving = false,
  onExportPdf = null,
  extra = null,
}) {
  const [_, setTick] = useState(0);  // eslint-disable-line no-unused-vars
  const [theme, setTheme] = useState(readSavedTheme);
  const fileInputRef = useRef(null);

  // Apply theme on mount and whenever changed.
  useEffect(() => {
    if (workspace) applyTheme(workspace, theme);
  }, [workspace, theme]);

  // Re-render every 5 s so the autosave-age label stays current.
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 5_000);
    return () => clearInterval(id);
  }, []);

  const handleUndo = useCallback(() => {
    if (workspace) workspace.undo(false);
  }, [workspace]);

  const handleRedo = useCallback(() => {
    if (workspace) workspace.undo(true);
  }, [workspace]);

  // Keyboard shortcuts: Ctrl+S → save, Ctrl+Y / Ctrl+Shift+Z → redo.
  // Audit round-3 §U / §V — the toolbar's redo tooltip used to claim
  // Ctrl+Y but no handler was wired, and the save shortcut fired even
  // while the student was typing inside a text block field. Now we
  // skip the global shortcut when focus is inside a form/contentEditable
  // element so Blockly's own field input remains untouched.
  useEffect(() => {
    const handler = (e) => {
      const ctrlOrMeta = e.ctrlKey || e.metaKey;
      if (!ctrlOrMeta) return;
      // Don't fire global shortcuts while the user is editing a field.
      const t = e.target;
      if (t && typeof t.matches === 'function' && t.matches('input, textarea, [contenteditable=""], [contenteditable="true"]')) {
        return;
      }
      if (e.key === 's' || e.key === 'S') {
        e.preventDefault();
        if (typeof onSave === 'function') onSave();
        return;
      }
      // Ctrl+Y → redo (Windows/Linux convention)
      if (e.key === 'y' || e.key === 'Y') {
        e.preventDefault();
        handleRedo();
        return;
      }
      // Ctrl+Shift+Z → redo (Mac convention)
      if (e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
        e.preventDefault();
        handleRedo();
        return;
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onSave, handleRedo]);

  const handleZoomFit = useCallback(() => {
    if (!workspace) return;
    if (typeof workspace.zoomToFit === 'function') {
      workspace.zoomToFit();
    } else {
      // Older Blockly fallback — no zoom-to-fit exposed.
      workspace.scrollCenter();
    }
  }, [workspace]);

  const handleExport = useCallback(() => {
    if (!workspace) return;
    let state;
    try {
      state = Blockly.serialization.workspaces.save(workspace);
    } catch (e) {
      toast.error('Export fehlgeschlagen.');
      return;
    }
    const blob = new Blob([JSON.stringify(state, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `roboter-studio-${ts}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [workspace]);

  const handleImport = useCallback(() => {
    if (fileInputRef.current) fileInputRef.current.click();
  }, []);

  const handleFile = useCallback((e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file || !workspace) return;
    const reader = new FileReader();
    reader.onerror = () => toast.error('Datei konnte nicht gelesen werden.');
    reader.onload = () => {
      let state;
      try {
        state = JSON.parse(reader.result);
      } catch (err) {
        toast.error('Ungültige JSON-Datei.');
        return;
      }
      try {
        Blockly.serialization.workspaces.load(state, workspace);
        toast.success('Workflow importiert.');
      } catch (err) {
        toast.error(`Import fehlgeschlagen: ${err.message || err}`);
      }
    };
    reader.readAsText(file);
  }, [workspace]);

  const handleThemeChange = useCallback((e) => {
    const name = e.target.value;
    setTheme(name);
    persistTheme(name);
  }, []);

  const ageLabel = formatAutosaveAge(lastSavedAt);

  return (
    <div
      role="toolbar"
      aria-label="Workshop Werkzeugleiste"
      className="flex flex-wrap items-center gap-2 px-3 py-2 bg-white border-b border-[var(--line)]"
    >
      <button
        type="button"
        className={BUTTON_BASE}
        onClick={handleUndo}
        title={`${DE.TOOLBAR_UNDO} (Strg+Z)`}
        aria-label={DE.TOOLBAR_UNDO}
      >
        ↶ {DE.TOOLBAR_UNDO}
      </button>
      <button
        type="button"
        className={BUTTON_BASE}
        onClick={handleRedo}
        title={`${DE.TOOLBAR_REDO} (Strg+Y)`}
        aria-label={DE.TOOLBAR_REDO}
      >
        ↷ {DE.TOOLBAR_REDO}
      </button>
      <button
        type="button"
        className={BUTTON_BASE}
        onClick={handleZoomFit}
        title={DE.TOOLBAR_ZOOM_FIT}
        aria-label={DE.TOOLBAR_ZOOM_FIT}
      >
        ⤢ {DE.TOOLBAR_ZOOM_FIT}
      </button>

      <span className="mx-1 h-6 w-px bg-[var(--line)]" aria-hidden="true" />

      <button
        type="button"
        className={PRIMARY_BUTTON}
        onClick={onSave}
        disabled={saving || !onSave}
        title={`${DE.TOOLBAR_SAVE} (Strg+S)`}
        aria-label={DE.TOOLBAR_SAVE}
      >
        💾 {saving ? '…' : DE.TOOLBAR_SAVE}
      </button>
      <button
        type="button"
        className={BUTTON_BASE}
        onClick={handleExport}
        title={DE.TOOLBAR_EXPORT}
        aria-label={DE.TOOLBAR_EXPORT}
      >
        ⇪ {DE.TOOLBAR_EXPORT}
      </button>
      <button
        type="button"
        className={BUTTON_BASE}
        onClick={handleImport}
        title={DE.TOOLBAR_IMPORT}
        aria-label={DE.TOOLBAR_IMPORT}
      >
        ⇲ {DE.TOOLBAR_IMPORT}
      </button>
      {onExportPdf && (
        <button
          type="button"
          className={BUTTON_BASE}
          onClick={onExportPdf}
          title={DE.TOOLBAR_PDF_EXPORT}
          aria-label={DE.TOOLBAR_PDF_EXPORT}
        >
          📄 PDF
        </button>
      )}
      {extra}
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={handleFile}
      />

      <span className="mx-1 h-6 w-px bg-[var(--line)]" aria-hidden="true" />

      <label className="text-sm text-[var(--ink-3)] flex items-center gap-1.5">
        <span className="sr-only">{DE.TOOLBAR_THEME}</span>
        <span aria-hidden="true">🎨</span>
        <select
          value={theme}
          onChange={handleThemeChange}
          className="text-sm border border-[var(--line)] rounded-md px-1 py-0.5 bg-white"
          aria-label={DE.TOOLBAR_THEME}
        >
          <option value={THEME_KEYS.STANDARD}>{DE.THEME_STANDARD}</option>
          <option value={THEME_KEYS.TRITANOPIA}>{DE.THEME_TRITANOPIA}</option>
          <option value={THEME_KEYS.DEUTERANOPIA}>{DE.THEME_DEUTERANOPIA}</option>
          <option value={THEME_KEYS.HIGHCONTRAST}>{DE.THEME_HIGHCONTRAST}</option>
        </select>
      </label>

      <span className="ml-auto text-xs text-[var(--ink-4)]" aria-live="polite">
        <span aria-hidden="true">●</span> {DE.AUTOSAVE_LABEL}: {ageLabel}
      </span>
    </div>
  );
}

export default ToolbarButtons;
