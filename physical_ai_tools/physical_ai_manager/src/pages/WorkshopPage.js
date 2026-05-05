/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import CalibrationWizard from '../components/Workshop/CalibrationWizard';
import BlocklyWorkspace from '../components/Workshop/BlocklyWorkspace';
import RunControls from '../components/Workshop/RunControls';
import CameraFeedOverlay from '../components/Workshop/CameraFeedOverlay';
import { setUnsavedBlocklyJson } from '../features/workshop/workshopSlice';
import { useRosTopicSubscription } from '../hooks/useRosTopicSubscription';

function WorkshopPage({ isActive }) {
  const dispatch = useDispatch();
  const hasIntrinsicGripper = useSelector((s) => s.workshop.hasIntrinsicGripper);
  const hasIntrinsicScene = useSelector((s) => s.workshop.hasIntrinsicScene);
  const hasHandeyeGripper = useSelector((s) => s.workshop.hasHandeyeGripper);
  const hasHandeyeScene = useSelector((s) => s.workshop.hasHandeyeScene);
  const hasColorProfile = useSelector((s) => s.workshop.hasColorProfile);
  const selectedWorkflowId = useSelector((s) => s.workshop.selectedWorkflowId);
  const unsavedBlocklyJson = useSelector((s) => s.workshop.unsavedBlocklyJson);

  const [editorJson, setEditorJson] = useState(null);
  const subscriptions = useRosTopicSubscription();

  const calibrated =
    hasIntrinsicGripper &&
    hasIntrinsicScene &&
    hasHandeyeGripper &&
    hasHandeyeScene &&
    hasColorProfile;

  useEffect(() => {
    if (!isActive) return undefined;
    if (subscriptions && typeof subscriptions.subscribeToWorkflowStatus === 'function') {
      subscriptions.subscribeToWorkflowStatus();
    }
    return undefined;
  }, [isActive, subscriptions]);

  const handleEditorChange = useCallback(
    (json) => {
      setEditorJson(json);
      dispatch(setUnsavedBlocklyJson(json));
    },
    [dispatch]
  );

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
            <div className="flex-1 grid grid-cols-3 gap-4 p-4 overflow-hidden">
              <div className="col-span-2 bg-white rounded-lg border border-[var(--line)] overflow-hidden">
                <BlocklyWorkspace
                  initialJson={null}
                  onChange={handleEditorChange}
                />
              </div>
              <div className="col-span-1 flex flex-col gap-3 overflow-hidden">
                <CameraFeedOverlay camera="scene" clickable={true} />
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
