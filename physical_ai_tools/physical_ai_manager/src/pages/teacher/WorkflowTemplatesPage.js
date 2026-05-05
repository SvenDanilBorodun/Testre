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
import BlocklyWorkspace from '../../components/Workshop/BlocklyWorkspace';
import {
  listClassroomTemplates,
  publishClassroomTemplate,
  deleteClassroomTemplate,
} from '../../services/workflowApi';

function WorkflowTemplatesPage({ classroomId }) {
  const accessToken = useSelector((s) => s.auth.session?.access_token);
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [draftName, setDraftName] = useState('');
  const [draftDescription, setDraftDescription] = useState('');
  const [draftJson, setDraftJson] = useState(null);
  const [previewTemplate, setPreviewTemplate] = useState(null);
  const [busy, setBusy] = useState(false);

  const refetch = useCallback(async () => {
    if (!accessToken || !classroomId) return;
    setLoading(true);
    try {
      const data = await listClassroomTemplates(accessToken, classroomId);
      setTemplates(data);
    } catch (e) {
      toast.error(`Vorlagen konnten nicht geladen werden: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }, [accessToken, classroomId]);

  useEffect(() => {
    refetch();
  }, [refetch]);

  const handlePublish = async () => {
    if (!accessToken || !classroomId) return;
    if (!draftName.trim()) {
      toast.error('Bitte einen Namen eingeben.');
      return;
    }
    if (!draftJson) {
      toast.error('Bitte zuerst Bausteine in den Editor ziehen.');
      return;
    }
    setBusy(true);
    try {
      await publishClassroomTemplate(accessToken, classroomId, {
        name: draftName.trim(),
        description: draftDescription.trim(),
        blockly_json: draftJson,
      });
      setDraftName('');
      setDraftDescription('');
      setDraftJson(null);
      await refetch();
      toast.success('Vorlage veröffentlicht.');
    } catch (e) {
      toast.error(`Veröffentlichen fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (templateId) => {
    if (!accessToken || !classroomId) return;
    if (!window.confirm('Vorlage wirklich löschen?')) return;
    setBusy(true);
    try {
      await deleteClassroomTemplate(accessToken, classroomId, templateId);
      await refetch();
      toast.success('Vorlage gelöscht.');
    } catch (e) {
      toast.error(`Löschen fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  if (!classroomId) {
    return <p className="text-sm text-[var(--ink-3)]">Bitte zuerst eine Klasse auswählen.</p>;
  }

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-[var(--ink)]">Workflow-Vorlagen</h2>
        <p className="text-sm text-[var(--ink-3)]">
          Veröffentliche fertige Workflows, die Schülerinnen und Schüler dieser Klasse
          klonen und ausführen können. Hier kannst du nicht testen — die Hardware steht
          nur am Schüler-Rechner.
        </p>
      </header>

      <section className="bg-white border border-[var(--line)] rounded-lg p-4 space-y-3">
        <h3 className="text-sm font-semibold text-[var(--ink)]">Neue Vorlage</h3>
        <input
          type="text"
          placeholder="Name der Vorlage"
          value={draftName}
          onChange={(e) => setDraftName(e.target.value)}
          className="w-full border border-[var(--line)] rounded-md px-3 py-2 text-sm"
        />
        <textarea
          placeholder="Beschreibung (optional)"
          value={draftDescription}
          onChange={(e) => setDraftDescription(e.target.value)}
          rows={2}
          className="w-full border border-[var(--line)] rounded-md px-3 py-2 text-sm"
        />
        <div className="h-96 border border-[var(--line)] rounded-md overflow-hidden">
          <BlocklyWorkspace onChange={setDraftJson} />
        </div>
        <button
          type="button"
          onClick={handlePublish}
          disabled={busy}
          className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
        >
          Veröffentlichen
        </button>
      </section>

      <section>
        <h3 className="text-sm font-semibold text-[var(--ink)] mb-2">Veröffentlichte Vorlagen</h3>
        {loading ? (
          <p className="text-sm text-[var(--ink-3)]">Lädt ...</p>
        ) : templates.length === 0 ? (
          <p className="text-sm text-[var(--ink-3)]">Noch keine Vorlagen.</p>
        ) : (
          <ul className="space-y-2">
            {templates.map((t) => (
              <li
                key={t.id}
                className="bg-white border border-[var(--line)] rounded-md p-3 flex items-center gap-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-[var(--ink)] truncate">{t.name}</div>
                  {t.description && (
                    <div className="text-xs text-[var(--ink-4)] truncate">{t.description}</div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setPreviewTemplate(t)}
                  className="text-xs px-3 py-1.5 rounded-md bg-[var(--bg-sunk)] text-[var(--ink)] hover:bg-[var(--accent-wash)]"
                >
                  Vorschau
                </button>
                <button
                  type="button"
                  onClick={() => handleDelete(t.id)}
                  disabled={busy}
                  className="text-xs px-3 py-1.5 rounded-md bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50"
                >
                  Löschen
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {previewTemplate && (
        <section className="bg-white border border-[var(--line)] rounded-lg p-4 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-[var(--ink)]">
              Vorschau: {previewTemplate.name}
            </h3>
            <button
              type="button"
              onClick={() => setPreviewTemplate(null)}
              className="text-xs px-2 py-1 rounded-md bg-[var(--bg-sunk)]"
            >
              Schließen
            </button>
          </div>
          <div className="h-80 border border-[var(--line)] rounded-md overflow-hidden">
            <BlocklyWorkspace
              key={previewTemplate.id}
              initialJson={previewTemplate.blockly_json}
              readOnly={true}
            />
          </div>
        </section>
      )}
    </div>
  );
}

export default WorkflowTemplatesPage;
