/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Pieces the three Sammlung drawer detail views share: the „Rückgängig"
 * toast, the inline confirm, the „Benutzt in" list and the rename field.
 */

import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { DE, formatDe } from '../blocks/messages_de';
import { jumpToBlock } from './blockUsage';

export const UNDO_TOAST_MS = 8000;
export const RENAME_SPLIT_TOAST_ID = 'edubotics-rename-split';

/** A toast with a „Rückgängig" button that dismisses it and runs `undo`. */
export function showUndoToast(text, undo) {
  return toast((t) => (
    <span>
      {text}
      {' '}
      <button
        type="button"
        className="ml-1 font-semibold underline"
        onClick={() => { toast.dismiss(t.id); undo(); }}
      >
        {DE.UNDO}
      </button>
    </span>
  ), { duration: UNDO_TOAST_MS });
}

/**
 * The split-rename error toast. It stays until dismissed, but the page caps
 * the number of toasts — the drawer banner is the persistent carrier.
 */
export function showRenameSplitToast(error) {
  return toast.error((t) => (
    <span>
      {error}
      {' '}
      <button type="button" className="ml-1 font-semibold underline" onClick={() => toast.dismiss(t.id)}>
        {DE.TOAST_DISMISS}
      </button>
    </span>
  ), { id: RENAME_SPLIT_TOAST_ID, duration: Infinity });
}

/** The confirm sentence for deleting an asset that `count` blocks use. */
export function deleteUsedText(name, count) {
  return count === 1
    ? formatDe(DE.CONFIRM_DELETE_USED_ONE, name)
    : formatDe(DE.CONFIRM_DELETE_USED, name, count);
}

export function InlineConfirm({ text, yesLabel, onYes, onNo }) {
  return (
    <div role="alertdialog" aria-label={text} className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-2 text-sm">
      <p className="text-amber-900">{text}</p>
      <div className="mt-2 flex gap-2">
        <button type="button" onClick={onYes} className="rounded bg-red-600 px-2 py-1 text-white hover:bg-red-700">
          {yesLabel}
        </button>
        <button type="button" onClick={onNo} className="rounded border border-[var(--line)] px-2 py-1 hover:bg-gray-50">
          {DE.DRAWER_CANCEL}
        </button>
      </div>
    </div>
  );
}

export function DetailRow({ label, children }) {
  return (
    <div className="flex gap-2 text-sm">
      <dt className="w-28 shrink-0 text-gray-500">{label}</dt>
      <dd className="min-w-0 break-words text-gray-900">{children}</dd>
    </div>
  );
}

/** „Benutzt in": one button per block (click scrolls to it), or the kind's „Nirgends …". */
export function UsageList({ workspace, rows, nowhereText }) {
  return (
    <section aria-label={DE.DRAWER_USED_IN} className="mt-3">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{DE.DRAWER_USED_IN}</h4>
      {rows.length === 0 ? (
        <p className="mt-1 text-sm text-gray-600">{nowhereText}</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {rows.map((row) => (
            <li key={row.blockId}>
              <button
                type="button"
                onClick={() => jumpToBlock(workspace, row.blockId)}
                className="w-full truncate rounded px-2 py-1 text-left text-sm hover:bg-gray-100"
              >
                {row.disabled ? `${row.label} ${DE.DRAWER_DISABLED_SUFFIX}` : row.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Name + „Umbenennen". `sanitize` filters live typing (destination names);
 * `onCommit(draft)` returns a promise or value; the field closes on a truthy
 * result and stays open (keeping the draft) on a falsy one.
 */
export function RenameField({ name, maxLength, sanitize, onCommit, disabled }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [busy, setBusy] = useState(false);

  const start = () => { setDraft(name); setEditing(true); };
  const commit = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const done = await onCommit(draft);
      if (done) setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <div className="flex items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-base font-semibold text-gray-900" title={name}>{name}</h3>
        <button
          type="button"
          onClick={start}
          disabled={disabled}
          className="rounded border border-[var(--line)] px-2 py-0.5 text-sm hover:bg-gray-50 disabled:opacity-50"
        >
          {DE.DRAWER_RENAME}
        </button>
      </div>
    );
  }
  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => { e.preventDefault(); commit(); }}
    >
      <input
        type="text"
        aria-label={DE.DRAWER_NAME}
        value={draft}
        maxLength={maxLength}
        autoFocus
        disabled={busy}
        onChange={(e) => setDraft(sanitize ? sanitize(e.target.value) : e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation();
            setEditing(false);
          }
        }}
        className="min-w-0 flex-1 rounded border border-[var(--line)] px-2 py-1 text-sm"
      />
      <button type="submit" disabled={busy} className="rounded bg-gray-900 px-2 py-1 text-sm text-white disabled:opacity-50">
        {DE.DRAWER_SAVE_NAME}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => setEditing(false)}
        className="rounded border border-[var(--line)] px-2 py-1 text-sm"
      >
        {DE.DRAWER_CANCEL}
      </button>
    </form>
  );
}
