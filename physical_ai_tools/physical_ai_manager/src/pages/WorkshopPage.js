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
import { applyPinnedCoordinates } from '../components/Workshop/blocks/destinations';
import {
  setUnsavedBlocklyJson,
  setSelectedWorkflowId,
} from '../features/workshop/workshopSlice';
import { useRosTopicSubscription } from '../hooks/useRosTopicSubscription';
import { getWorkflow } from '../services/workflowApi';

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

  const [editorJson, setEditorJson] = useState(null);
  const [initialJsonForEditor, setInitialJsonForEditor] = useState(null);
  const [editorKey, setEditorKey] = useState(0);
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

  const handleWorkspaceReady = useCallback((workspace) => {
    workspaceRef.current = workspace;
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

  if (!isActive) return null;

  return (
    <div className="flex flex-col h-full w-full overflow-hidden">
      <header className="px-6 py-4 border-b border-[var(--line)] bg-white">
        <h1 className="text-xl font-semibold text-[var(--ink)]">Roboter Studio</h1>
        <p className="text-sm text-[var(--ink-3)]">
          {calibrated
            ? 'Bausteine ziehen, Aufgabe zusammenstellen und vom Roboter ausführen lassen.'
            : 'Bevor wir loslegen können, muss die Kamera eingerichtet werden.'}
        </p>
      </header>
      <main className="flex-1 overflow-hidden flex flex-col">
        {calibrated ? (
          <>
            <div className="px-4 pt-3">
              <TemplatePicker onPicked={handlePickWorkflow} />
            </div>
            <div className="flex-1 grid grid-cols-3 gap-4 p-4 overflow-hidden">
              <div className="col-span-2 bg-white rounded-lg border border-[var(--line)] overflow-hidden">
                <BlocklyWorkspace
                  key={editorKey}
                  initialJson={initialJsonForEditor}
                  onChange={handleEditorChange}
                  onWorkspaceReady={handleWorkspaceReady}
                />
              </div>
              <div className="col-span-1 flex flex-col gap-3 overflow-hidden">
                <CameraFeedOverlay
                  camera="scene"
                  clickable={true}
                  onMark={handleMarkDestination}
                />
              </div>
            </div>
            <RunControls workflowId={selectedWorkflowId} blocklyJson={editorJson || unsavedBlocklyJson} />
          </>
        ) : (
          <CalibrationWizard />
        )}
      </main>
    </div>
  );
}

export default WorkshopPage;
