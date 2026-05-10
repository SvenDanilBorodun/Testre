/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  listWorkflowVersions,
  restoreWorkflowVersion,
} from '../../services/workflowApi';
import { DE } from './blocks/messages_de';

function fmtTs(iso) {
  if (!iso) return '–';
  try {
    return new Date(iso).toLocaleString('de-DE');
  } catch (e) {
    return iso;
  }
}

/**
 * "Verlauf" (history) dropdown — lists the last 20 saved snapshots of
 * the active workflow's blockly_json and lets the student restore one.
 * The restore endpoint (`POST /workflows/{id}/versions/{vid}/restore`)
 * itself triggers a fresh snapshot of the current state, so restoring
 * is reversible until the 20-cap rotates the snapshot out.
 *
 * The button is disabled when no workflow is selected (i.e. the editor
 * is on local-only autosave state). When clicked, it fetches the list
 * lazily and renders a popover.
 */
function VersionHistoryDropdown({ workflowId, onRestore }) {
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [restoringId, setRestoringId] = useState(null);

  const disabled = !workflowId || !accessToken;

  const refresh = useCallback(async () => {
    if (!workflowId || !accessToken) return;
    setLoading(true);
    try {
      const rows = await listWorkflowVersions(accessToken, workflowId);
      setVersions(Array.isArray(rows) ? rows : []);
    } catch (e) {
      toast.error(`${DE.VERSION_HISTORY}: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }, [accessToken, workflowId]);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  const handleRestore = useCallback(
    async (versionId) => {
      if (!accessToken || !workflowId) return;
      setRestoringId(versionId);
      try {
        const updated = await restoreWorkflowVersion(
          accessToken,
          workflowId,
          versionId,
        );
        if (typeof onRestore === 'function') {
          onRestore(updated);
        }
        toast.success('Version wiederhergestellt.');
        setOpen(false);
      } catch (e) {
        toast.error(`Wiederherstellen fehlgeschlagen: ${e.message || e}`);
      } finally {
        setRestoringId(null);
      }
    },
    [accessToken, workflowId, onRestore]
  );

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="menu"
        className={
          'inline-flex items-center justify-center min-h-[28px] '
          + 'px-3 py-1.5 rounded-md text-sm font-medium border border-[var(--line)] '
          + 'bg-white text-[var(--ink)] hover:bg-[var(--bg-sunk)] '
          + 'disabled:opacity-50 disabled:cursor-not-allowed '
          + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500'
        }
      >
        🕓 {DE.VERSION_HISTORY}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-1 z-10 w-72 bg-white border border-[var(--line)] rounded-md shadow-lg max-h-80 overflow-auto"
        >
          {loading ? (
            <p className="text-sm text-[var(--ink-3)] p-3">…</p>
          ) : versions.length === 0 ? (
            <p className="text-sm text-[var(--ink-3)] p-3">
              {DE.VERSION_NONE}
            </p>
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {versions.map((v) => (
                <li
                  key={v.id}
                  className="flex items-center justify-between gap-2 px-3 py-2 hover:bg-[var(--bg-sunk)]"
                >
                  <span className="text-xs text-[var(--ink)] font-mono">
                    {fmtTs(v.created_at)}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleRestore(v.id)}
                    disabled={restoringId === v.id}
                    className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                  >
                    {restoringId === v.id ? '…' : DE.VERSION_LOAD}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export default VersionHistoryDropdown;
