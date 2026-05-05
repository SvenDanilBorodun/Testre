/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useState } from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import useSupabaseWorkflows from '../../hooks/useSupabaseWorkflows';
import { cloneWorkflow } from '../../services/workflowApi';

function TemplatePicker({ onPicked }) {
  const { workflows, loading } = useSupabaseWorkflows();
  const session = useSelector((s) => s.auth.session);
  const accessToken = session?.access_token;
  const [busyId, setBusyId] = useState(null);

  const templates = workflows.filter((w) => w.is_template);
  const own = workflows.filter((w) => !w.is_template);

  const handleClone = async (workflowId) => {
    if (!accessToken) return;
    setBusyId(workflowId);
    try {
      const cloned = await cloneWorkflow(accessToken, workflowId);
      toast.success(`Vorlage geklont: ${cloned.name}`);
      if (onPicked) onPicked(cloned);
    } catch (e) {
      toast.error(`Klon fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusyId(null);
    }
  };

  if (loading) {
    return <p className="text-sm text-[var(--ink-3)]">Workflows werden geladen ...</p>;
  }

  return (
    <div className="space-y-4">
      {templates.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-[var(--ink)] mb-2">Klassenraum-Vorlagen</h3>
          <ul className="space-y-2">
            {templates.map((w) => (
              <li
                key={w.id}
                className="bg-white border border-[var(--line)] rounded-md p-3 flex items-center gap-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-[var(--ink)] truncate">{w.name}</div>
                  {w.description && (
                    <div className="text-xs text-[var(--ink-4)] truncate">{w.description}</div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => handleClone(w.id)}
                  disabled={busyId === w.id}
                  className="text-xs px-3 py-1.5 rounded-md bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50"
                >
                  Klonen
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {own.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-[var(--ink)] mb-2">Meine Workflows</h3>
          <ul className="space-y-2">
            {own.map((w) => (
              <li
                key={w.id}
                className="bg-white border border-[var(--line)] rounded-md p-3 flex items-center gap-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-[var(--ink)] truncate">{w.name}</div>
                  <div className="text-xs text-[var(--ink-4)]">
                    Aktualisiert: {new Date(w.updated_at).toLocaleString('de-DE')}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => onPicked && onPicked(w)}
                  className="text-xs px-3 py-1.5 rounded-md bg-[var(--accent-wash)] text-[var(--accent-ink)] hover:bg-[var(--accent)] hover:text-white"
                >
                  Öffnen
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {templates.length === 0 && own.length === 0 && (
        <p className="text-sm text-[var(--ink-3)]">
          Noch keine Workflows. Erstelle einen leeren Workflow, um zu beginnen.
        </p>
      )}
    </div>
  );
}

export default TemplatePicker;
