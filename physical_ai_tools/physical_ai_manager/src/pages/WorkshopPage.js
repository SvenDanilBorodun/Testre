/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import CalibrationWizard from '../components/Workshop/CalibrationWizard';
import BlocklyWorkspace from '../components/Workshop/BlocklyWorkspace';
import RunControls from '../components/Workshop/RunControls';
import CameraFeedOverlay from '../components/Workshop/CameraFeedOverlay';
import TemplatePicker from '../components/Workshop/TemplatePicker';
import ToolbarButtons from '../components/Workshop/ToolbarButtons';
import DebugPanel from '../components/Workshop/DebugPanel';
import GalleryTab from '../components/Workshop/GalleryTab';
import SkillmapPlayer from '../components/Workshop/SkillmapPlayer';
import VersionHistoryDropdown from '../components/Workshop/VersionHistoryDropdown';
import { applyPinnedCoordinates } from '../components/Workshop/blocks/destinations';
import { useAutosave } from '../components/Workshop/useAutosave';
import {
  setUnsavedBlocklyJson,
  setSelectedWorkflowId,
  markWorkflowSaved,
  requestRecalibration,
} from '../features/workshop/workshopSlice';
import { useRosTopicSubscription } from '../hooks/useRosTopicSubscription';
import {
  getWorkflow,
  createWorkflow,
  updateWorkflow,
} from '../services/workflowApi';

function WorkshopPage({ isActive }) {
  const dispatch = useDispatch();
  const hasIntrinsicGripper = useSelector((s) => s.workshop.hasIntrinsicGripper);
  const hasIntrinsicScene = useSelector((s) => s.workshop.hasIntrinsicScene);
  const hasHandeyeGripper = useSelector((s) => s.workshop.hasHandeyeGripper);
  const hasHandeyeScene = useSelector((s) => s.workshop.hasHandeyeScene);
  const hasColorProfile = useSelector((s) => s.workshop.hasColorProfile);
  const selectedWorkflowId = useSelector((s) => s.workshop.selectedWorkflowId);
  const unsavedBlocklyJson = useSelector((s) => s.workshop.unsavedBlocklyJson);
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  // Audit fix: prior path `s.auth?.user?.id` was always null (no
  // top-level `user` field on the auth slice). The Supabase user lives
  // under `session.user.id`. Without this fix the autosave scopeKey
  // was the same string for every student on a shared browser →
  // cross-student autosave bleed.
  const userId = useSelector((s) => s.auth?.session?.user?.id || null);
  const debuggerVisible = useSelector((s) => s.workshop.debuggerVisible);
  const restrictedBlocks = useSelector((s) => s.workshop.restrictedBlocks);
  const activeTutorialId = useSelector((s) => s.workshop.activeTutorialId);

  const [editorJson, setEditorJson] = useState(null);
  const [initialJsonForEditor, setInitialJsonForEditor] = useState(null);
  const [editorKey, setEditorKey] = useState(0);
  const [workspace, setWorkspace] = useState(null);
  const [saving, setSaving] = useState(false);
  const [view, setView] = useState('editor'); // 'editor' | 'gallery'
  const subscriptions = useRosTopicSubscription();
  const workspaceRef = useRef(null);

  const calibrated =
    hasIntrinsicGripper &&
    hasIntrinsicScene &&
    hasHandeyeGripper &&
    hasHandeyeScene &&
    hasColorProfile;

  // Re-subscribe to /workflow/status whenever this page is active OR
  // the rosbridge connection state flips back to connected. The v1
  // ship subscribed once on mount and lost the feed after any
  // rosbridge reconnect (audit §3.8). The hook's subscribe call is
  // re-entrant: it tears down a stale topic before binding a new one.
  useEffect(() => {
    if (!isActive) return undefined;
    if (subscriptions && typeof subscriptions.subscribeToWorkflowStatus === 'function') {
      subscriptions.subscribeToWorkflowStatus();
    }
    if (subscriptions && typeof subscriptions.subscribeToWorkflowSensors === 'function') {
      subscriptions.subscribeToWorkflowSensors();
    }
    return undefined;
  }, [isActive, subscriptions, subscriptions?.connected]);

  // Hydrate the editor with: (a) selected cloud workflow, then
  // (b) unsavedBlocklyJson from Redux, then (c) blank workspace.
  // Bumping editorKey forces BlocklyWorkspace to remount so initialJson
  // is re-applied — Blockly only consumes initialJson on mount.
  useEffect(() => {
    let cancelled = false;
    if (!isActive) return undefined;
    if (selectedWorkflowId && accessToken) {
      getWorkflow(accessToken, selectedWorkflowId)
        .then((w) => {
          if (cancelled) return;
          setInitialJsonForEditor(w?.blockly_json || null);
          setEditorKey((k) => k + 1);
        })
        .catch((e) => {
          if (cancelled) return;
          toast.error(`Workflow konnte nicht geladen werden: ${e.message || e}`);
          setInitialJsonForEditor(unsavedBlocklyJson || null);
          setEditorKey((k) => k + 1);
        });
      return () => { cancelled = true; };
    }
    setInitialJsonForEditor(unsavedBlocklyJson || null);
    setEditorKey((k) => k + 1);
    return () => { cancelled = true; };
    // unsavedBlocklyJson intentionally omitted from deps: we only want
    // to seed once per workflow-id change. The change-listener inside
    // BlocklyWorkspace keeps Redux in sync after that.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, selectedWorkflowId, accessToken]);

  const handleEditorChange = useCallback(
    (json) => {
      setEditorJson(json);
      dispatch(setUnsavedBlocklyJson(json));
    },
    [dispatch]
  );

  const handleWorkspaceReady = useCallback((ws) => {
    workspaceRef.current = ws;
    setWorkspace(ws);
  }, []);

  // TemplatePicker calls onPicked(workflowObject) — the full row, not
  // just the id. We extract the id and store it in Redux; the editor's
  // load effect picks up the change and hydrates initialJsonForEditor.
  const handlePickWorkflow = useCallback(
    (workflow) => {
      if (!workflow || !workflow.id) return;
      dispatch(setSelectedWorkflowId(workflow.id));
    },
    [dispatch]
  );

  // Click-to-pin handler: when the student clicks the scene camera and
  // a destination_pin block is selected, write the world coordinates
  // returned by /workshop/mark_destination into that block's X/Y/Z
  // fields. Without this, the destination_pin handler at runtime would
  // overwrite the click data with zeros (audit §1.4).
  const handleMarkDestination = useCallback(({ label, world_x, world_y, world_z }) => {
    const ws = workspaceRef.current;
    if (!ws) return;
    const selected = (typeof ws.getSelected === 'function') ? ws.getSelected() : null;
    if (!selected || selected.type !== 'edubotics_destination_pin') {
      toast(
        'Tipp: Wähle zuerst einen "setze Ziel = Pin"-Block aus, '
        + 'dann klicke in die Szenen-Kamera, damit die Koordinaten '
        + `in den Block geschrieben werden. (Ziel "${label}" wurde `
        + 'serverseitig gespeichert, aber kein Block aktualisiert.)',
        { icon: '💡' },
      );
      return;
    }
    applyPinnedCoordinates(selected, world_x, world_y, world_z);
  }, []);

  // Autosave hook. Restores the most-recent local state if the parent
  // hasn't already loaded a server workflow. Scoped per user so two
  // students sharing a browser don't see each other's drafts.
  const handleAutosaveRestore = useCallback(
    (state) => {
      if (selectedWorkflowId) return;  // server workflow takes precedence
      if (initialJsonForEditor) return;
      setInitialJsonForEditor(state);
      setEditorKey((k) => k + 1);
      dispatch(setUnsavedBlocklyJson(state));
    },
    [selectedWorkflowId, initialJsonForEditor, dispatch]
  );
  const { lastSavedAt } = useAutosave({
    workspace,
    enabled: isActive && calibrated,
    scopeKey: userId,
    onRestore: handleAutosaveRestore,
  });

  const handleSave = useCallback(async () => {
    if (!accessToken) {
      toast.error('Nicht angemeldet — Speichern nicht möglich.');
      return;
    }
    const json = editorJson || unsavedBlocklyJson;
    if (!json) {
      toast.error('Workflow ist leer.');
      return;
    }
    setSaving(true);
    try {
      if (selectedWorkflowId) {
        await updateWorkflow(accessToken, selectedWorkflowId, {
          blockly_json: json,
        });
      } else {
        const created = await createWorkflow(accessToken, {
          name: 'Neuer Workflow',
          description: '',
          blockly_json: json,
        });
        if (created && created.id) {
          dispatch(setSelectedWorkflowId(created.id));
        }
      }
      dispatch(markWorkflowSaved());
      toast.success('Gespeichert.');
    } catch (e) {
      toast.error(`Speichern fehlgeschlagen: ${e.message || e}`);
    } finally {
      setSaving(false);
    }
  }, [accessToken, editorJson, unsavedBlocklyJson, selectedWorkflowId, dispatch]);

  if (!isActive) return null;

  return (
    <div className="flex flex-col h-full w-full overflow-hidden">
      <header className="px-4 sm:px-6 py-3 sm:py-4 border-b border-[var(--line)] bg-white">
        <h1 className="text-lg sm:text-xl font-semibold text-[var(--ink)]">
          Roboter Studio
        </h1>
        <p className="text-xs sm:text-sm text-[var(--ink-3)]">
          {calibrated
            ? 'Bausteine ziehen, Aufgabe zusammenstellen und vom Roboter ausführen lassen.'
            : 'Bevor wir loslegen können, muss die Kamera eingerichtet werden.'}
        </p>
      </header>
      <main className="flex-1 overflow-hidden flex flex-col">
        {calibrated ? (
          <>
            <div className="px-3 sm:px-4 pt-2 sm:pt-3 flex items-center gap-2 flex-wrap">
              <div className="flex items-center gap-1 rounded-md border border-[var(--line)] bg-white p-0.5">
                <button
                  type="button"
                  onClick={() => setView('editor')}
                  aria-pressed={view === 'editor'}
                  className={
                    'px-3 py-1 text-xs rounded '
                    + (view === 'editor'
                      ? 'bg-[var(--accent)] text-white'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]')
                  }
                >
                  Editor
                </button>
                <button
                  type="button"
                  onClick={() => setView('gallery')}
                  aria-pressed={view === 'gallery'}
                  className={
                    'px-3 py-1 text-xs rounded '
                    + (view === 'gallery'
                      ? 'bg-[var(--accent)] text-white'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]')
                  }
                >
                  Galerie
                </button>
              </div>
              <div className="flex-1 min-w-0">
                <TemplatePicker onPicked={handlePickWorkflow} />
              </div>
              {/* Audit U3: a calibrated student can re-enter the wizard
                  (e.g. cameras moved between sessions) without having
                  to wipe the named volume from a terminal. Resets
                  per-step badges; on-disk YAMLs survive and are
                  overwritten as the student re-runs each step. */}
              <button
                type="button"
                onClick={() => {
                  if (typeof window !== 'undefined'
                      && window.confirm('Kalibrierung neu starten? Die bisherigen Werte werden beim nächsten Schritt überschrieben.')) {
                    dispatch(requestRecalibration());
                  }
                }}
                className="text-xs px-2 py-1 rounded-md border border-[var(--line)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]"
                title="Kalibrierung erneut durchlaufen — Schritt-Marker werden zurückgesetzt"
              >
                Kalibrierung neu starten
              </button>
            </div>
            {view === 'gallery' ? (
              <div className="flex-1 p-3 sm:p-4 overflow-auto">
                <GalleryTab
                  onPicked={(wf) => {
                    handlePickWorkflow(wf);
                    setView('editor');
                  }}
                />
              </div>
            ) : (
              <>
                <ToolbarButtons
                  workspace={workspace}
                  lastSavedAt={lastSavedAt}
                  onSave={handleSave}
                  saving={saving}
                  extra={
                    <VersionHistoryDropdown
                      workflowId={selectedWorkflowId}
                      onRestore={(updated) => {
                        if (updated && updated.blockly_json) {
                          setInitialJsonForEditor(updated.blockly_json);
                          setEditorKey((k) => k + 1);
                          dispatch(setUnsavedBlocklyJson(updated.blockly_json));
                        }
                      }}
                    />
                  }
                />
                <div
                  className={
                    'flex-1 flex flex-col gap-3 p-3 sm:p-4 overflow-auto '
                    + 'md:grid md:gap-4 md:overflow-hidden '
                    + (debuggerVisible || activeTutorialId
                      ? 'md:grid-cols-12'
                      : 'md:grid-cols-3')
                  }
                >
                  <div
                    className={
                      'bg-white rounded-lg border border-[var(--line)] '
                      + 'overflow-hidden min-h-[420px] '
                      + (debuggerVisible
                        ? 'md:col-span-6'
                        : activeTutorialId
                        ? 'md:col-span-7'
                        : 'md:col-span-2')
                    }
                  >
                    <BlocklyWorkspace
                      key={editorKey}
                      initialJson={initialJsonForEditor}
                      onChange={handleEditorChange}
                      onWorkspaceReady={handleWorkspaceReady}
                      restrictedBlocks={restrictedBlocks}
                    />
                  </div>
                  <div
                    className={
                      'flex flex-col gap-3 overflow-auto md:overflow-hidden '
                      + 'min-w-0 '
                      + (debuggerVisible
                        ? 'md:col-span-3'
                        : activeTutorialId
                        ? 'md:col-span-2'
                        : 'md:col-span-1')
                    }
                  >
                    <CameraFeedOverlay
                      camera="scene"
                      clickable={true}
                      onMark={handleMarkDestination}
                    />
                    <SkillmapPlayer />
                  </div>
                  {debuggerVisible && (
                    <div className="md:col-span-3 overflow-auto md:overflow-hidden min-w-0">
                      <DebugPanel workspace={workspace} />
                    </div>
                  )}
                </div>
                <RunControls
                  workflowId={selectedWorkflowId}
                  blocklyJson={editorJson || unsavedBlocklyJson}
                  workspace={workspace}
                />
              </>
            )}
          </>
        ) : (
          <CalibrationWizard />
        )}
      </main>
    </div>
  );
}

export default WorkshopPage;
