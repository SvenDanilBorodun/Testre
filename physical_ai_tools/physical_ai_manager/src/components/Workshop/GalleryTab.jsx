/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { setSelectedWorkflowId } from '../../features/workshop/workshopSlice';
import { listWorkflows, cloneWorkflow } from '../../services/workflowApi';
import { DE } from './blocks/messages_de';

function fmtDate(iso) {
  if (!iso) return '–';
  try {
    return new Date(iso).toLocaleDateString('de-DE');
  } catch (e) {
    return iso;
  }
}

/**
 * Gallery view of classroom templates + group-shared workflows. Each
 * card shows the workflow name + author + last-updated; the Klonen
 * button calls /workflows/{id}/clone, which produces a fresh non-
 * template copy under the caller's ownership.
 */
function GalleryTab({ onPicked }) {
  const dispatch = useDispatch();
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  // Audit fix: the Supabase user lives under `session.user`, not under
  // a top-level `user` key on the auth slice. The previous path was
  // always null → every workflow looked like "owned by null" so the
  // group-shared filter `w.owner_user_id !== userId` matched too many
  // rows and templates rendered duplicated.
  const userId = useSelector((s) => s.auth?.session?.user?.id || null);
  const [workflows, setWorkflows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [cloning, setCloning] = useState({});

  const refresh = useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const list = await listWorkflows(accessToken);
      setWorkflows(Array.isArray(list) ? list : []);
    } catch (e) {
      toast.error(`Galerie konnte nicht geladen werden: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }, [accessToken]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    if (!Array.isArray(workflows)) return [];
    return workflows.filter((w) => {
      // Templates from teacher OR group-shared from peers (not own).
      if (w.is_template) return true;
      if (w.workgroup_id && w.owner_user_id !== userId) return true;
      return false;
    });
  }, [workflows, userId]);

  const handleClone = useCallback(
    async (wf) => {
      if (!accessToken) return;
      setCloning((m) => ({ ...m, [wf.id]: true }));
      try {
        const created = await cloneWorkflow(accessToken, wf.id);
        if (created && created.id) {
          dispatch(setSelectedWorkflowId(created.id));
          toast.success('Geklont und im Editor geöffnet.');
          if (typeof onPicked === 'function') {
            onPicked(created);
          }
        }
      } catch (e) {
        toast.error(`Klonen fehlgeschlagen: ${e.message || e}`);
      } finally {
        setCloning((m) => {
          const next = { ...m };
          delete next[wf.id];
          return next;
        });
      }
    },
    [accessToken, dispatch, onPicked]
  );

  return (
    <section
      aria-label={DE.GALLERY_TITLE}
      className="bg-white rounded-lg border border-[var(--line)] p-3 sm:p-4 overflow-auto"
    >
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold text-[var(--ink)]">
          {DE.GALLERY_TITLE}
        </h2>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="text-xs text-[var(--ink-3)] hover:underline disabled:opacity-50"
        >
          {loading ? '…' : '↻ aktualisieren'}
        </button>
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-[var(--ink-4)]">{DE.GALLERY_EMPTY}</p>
      ) : (
        <ul className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {filtered.map((wf) => (
            <li
              key={wf.id}
              className="border border-[var(--line)] rounded-md p-3 hover:bg-[var(--bg-sunk)] transition-colors"
            >
              <div className="flex items-start justify-between gap-2 mb-2">
                <h3 className="text-sm font-medium text-[var(--ink)] truncate">
                  {wf.name || '(ohne Namen)'}
                </h3>
                {wf.is_template && (
                  <span className="text-[10px] uppercase tracking-wide bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded">
                    Vorlage
                  </span>
                )}
                {!wf.is_template && wf.workgroup_id && (
                  <span className="text-[10px] uppercase tracking-wide bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded">
                    Gruppe
                  </span>
                )}
              </div>
              {wf.description && (
                <p className="text-xs text-[var(--ink-3)] mb-2 line-clamp-2">
                  {wf.description}
                </p>
              )}
              <p className="text-xs text-[var(--ink-4)] mb-3">
                {fmtDate(wf.updated_at)}
              </p>
              <button
                type="button"
                onClick={() => handleClone(wf)}
                disabled={!!cloning[wf.id]}
                className="w-full text-xs px-2 py-1.5 rounded-md bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50"
              >
                {cloning[wf.id] ? '…' : DE.GALLERY_CLONE}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default GalleryTab;
