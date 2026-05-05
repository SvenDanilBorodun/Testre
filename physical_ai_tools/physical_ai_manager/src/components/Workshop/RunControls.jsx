/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { setRunState, clearWorkflowLog } from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';

function RunControls({ workflowId, blocklyJson }) {
  const dispatch = useDispatch();
  const { callService } = useRosServiceCaller();
  const runState = useSelector((s) => s.workshop.runState);
  const phase = useSelector((s) => s.workshop.phase);
  const log = useSelector((s) => s.workshop.log);
  const error = useSelector((s) => s.workshop.workflowError);
  const [busy, setBusy] = useState(false);

  const isRunning = runState === 'running' || phase === 'running';

  const handleStart = useCallback(async () => {
    if (!blocklyJson) {
      toast.error('Workflow ist leer.');
      return;
    }
    setBusy(true);
    try {
      dispatch(clearWorkflowLog());
      const r = await callService(
        '/workflow/start',
        'physical_ai_interfaces/srv/StartWorkflow',
        {
          workflow_json: JSON.stringify(blocklyJson),
          workflow_id: workflowId || `local-${Date.now()}`,
        }
      );
      if (!r.success) {
        toast.error(r.message || 'Workflow konnte nicht gestartet werden.');
        return;
      }
      dispatch(setRunState('running'));
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [blocklyJson, callService, dispatch, workflowId]);

  const handleStop = useCallback(async () => {
    setBusy(true);
    try {
      const r = await callService(
        '/workflow/stop',
        'physical_ai_interfaces/srv/StopWorkflow',
        {}
      );
      dispatch(setRunState('stopped'));
      if (!r.success) {
        toast.error(r.message || 'Stopp fehlgeschlagen.');
      } else {
        toast.success(r.message);
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [callService, dispatch]);

  return (
    <div className="border-t border-[var(--line)] bg-white p-4">
      <div className="flex items-center gap-3 mb-3">
        <button
          type="button"
          onClick={handleStart}
          disabled={busy || isRunning}
          className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
        >
          ▶ Start
        </button>
        <button
          type="button"
          onClick={handleStop}
          disabled={busy || !isRunning}
          className="px-4 py-2 rounded-md bg-red-500 text-white text-sm font-medium hover:bg-red-600 disabled:opacity-50"
        >
          ■ Stopp
        </button>
        <span className={
          'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ' +
          (isRunning
            ? 'bg-green-100 text-green-700'
            : phase === 'error'
            ? 'bg-red-100 text-red-700'
            : 'bg-gray-100 text-gray-600')
        }>
          <span className={'w-1.5 h-1.5 rounded-full ' + (isRunning ? 'bg-green-500 animate-pulse' : 'bg-gray-400')} />
          {phase || 'Bereit'}
        </span>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md p-2 mb-2">
          {error}
        </div>
      )}

      <div className="bg-[var(--bg-sunk)] rounded-md p-3 max-h-48 overflow-y-auto font-mono text-xs">
        {log.length === 0 ? (
          <p className="text-[var(--ink-4)]">Keine Meldungen.</p>
        ) : (
          log.map((entry, idx) => (
            <div key={idx} className="text-[var(--ink-3)]">
              <span className="text-[var(--ink-4)] mr-2">
                {new Date(entry.ts).toLocaleTimeString('de-DE')}
              </span>
              {entry.text}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default RunControls;
