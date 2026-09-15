/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { useCallback, useEffect, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from './useRosServiceCaller';
// Namespace import, as in RunControls: tests mock only the members they need.
import * as workflowApi from '../services/workflowApi';
import { DE, formatDe } from '../components/Workshop/blocks/messages_de';
import { getDestinationStore } from '../components/Workshop/sammlung/destinationStore';
import {
  clearWorkflowError,
  clearWorkflowLog,
  setPaused,
  setRunState,
  setWorkflowStatus,
} from '../features/workshop/workshopSlice';
import {
  previewFailed,
  previewStarted,
  previewUnreachable,
  selectLastPreviewResult,
} from '../features/workshop/studioAssetsSlice';
import { compactTrajectoryPoints } from '../utils/trajectoryCompact';
import { normalizeTrajectory, trajectoryMatchesRig } from '../utils/trajectoryIdentity';
import { exceedsRunPayloadCap } from '../utils/runPayload';
import {
  PREVIEW_BLOCK_TITLES_DE,
  buildDestinationPreviewProgram,
  buildRecordingPreviewProgram,
  previewBlockReason,
  previewKeyForDestination,
  previewKeyForRecording,
  previewMessageDe,
  previewWorkflowIdForDestination,
  previewWorkflowIdForRecording,
} from '../utils/simPreview';

// Simulator previews („▶" on a Sammlung card or in the drawer).
//
// A preview is an ordinary SIM run of a tiny generated program
// (utils/simPreview.js): „spiele Bewegung" or „bewege zu" plus a short hold,
// started through /workflow/start with a `vorschau-…` workflow_id. It never
// calls /workshop/replay or /workshop/jog, so the real arm is never addressed.
// Every refusal is decided here, client-side and in German, BEFORE anything is
// sent; the result is recorded per asset in `studioAssets.lastPreviewResult`.
export default function useSimPreview({
  workspace,
  simScene,
  workflowId,
  accessToken,
  robotType,
  gates,
  ensureSimMode,
}) {
  const dispatch = useDispatch();
  const { callService } = useRosServiceCaller();
  const lastPreviewResult = useSelector(selectLastPreviewResult);

  // startPreview awaits (sim entry, the recording fetch), so it reads the
  // LATEST page state through this ref rather than a stale closure.
  const latest = useRef(null);
  latest.current = {
    workspace, simScene, workflowId, accessToken, robotType, gates, ensureSimMode, callService,
  };
  const resultsRef = useRef(lastPreviewResult);
  resultsRef.current = lastPreviewResult;
  const inFlightRef = useRef(false);
  // { key, prev } of the preview this hook started; cleared once its result lands.
  const startedRef = useRef(null);

  useEffect(() => {
    const started = startedRef.current;
    if (!started || !lastPreviewResult) return;
    const result = lastPreviewResult[started.key];
    if (!result || result === started.prev) return;
    startedRef.current = null;
    // `refused`: the status subscription (or this hook) already toasted it;
    // `stopped`: the student pressed Stopp — nothing to add.
    if (result.status === 'ok') toast.success(DE.PREVIEW_DONE);
  }, [lastPreviewResult]);

  const startPreview = useCallback(async (asset, options) => {
    if (!asset || typeof asset !== 'object') return;
    const isRecording = asset.kind === 'recording';
    // Any other kind (a variable, handled by the page itself) is not ours.
    if (!isRecording && asset.kind !== 'pin' && asset.kind !== 'pose') return;
    const tempo = options && typeof options === 'object' && options.tempo !== undefined
      ? options.tempo : 1.0;

    const args = latest.current;
    const reason = previewBlockReason({
      ...(args.gates || {}),
      asset,
      robotType: args.robotType,
      workflowId: args.workflowId,
      inFlight: inFlightRef.current,
    });
    if (reason === 'inFlight') return;
    if (reason) {
      toast.error(PREVIEW_BLOCK_TITLES_DE[reason]);
      return;
    }

    inFlightRef.current = true;
    const key = isRecording ? previewKeyForRecording(asset.id) : previewKeyForDestination(asset.id);
    try {
      if (!(await args.ensureSimMode({ showPath: true }))) return;
      // Sim entry re-rendered the page: read the scene and workspace afresh.
      const now = latest.current;
      let program;
      let wid;
      let name;
      let logTemplate;
      if (isRecording) {
        const row = await workflowApi.getTrajectory(now.accessToken, now.workflowId, asset.id);
        const norm = normalizeTrajectory(row);
        if (!norm) throw new Error(`Bewegung „${asset.name}" wurde nicht gefunden.`);
        if (!trajectoryMatchesRig(norm.robotProfile, now.robotType)) {
          toast.error(DE.PREVIEW_BLOCK_OTHER_ROBOT);
          return;
        }
        name = typeof row.name === 'string' && row.name ? row.name : asset.name;
        program = buildRecordingPreviewProgram({
          name,
          fps: norm.fps,
          points: compactTrajectoryPoints(norm.points),
          simScene: now.simScene,
          tempo,
        });
        wid = previewWorkflowIdForRecording(asset.id);
        logTemplate = DE.PREVIEW_LOG_RECORDING;
      } else {
        const entry = now.workspace ? getDestinationStore(now.workspace).getById(asset.id) : null;
        if (!entry) return;
        name = entry.name;
        program = buildDestinationPreviewProgram({ entry, simScene: now.simScene, tempo });
        wid = previewWorkflowIdForDestination(entry.id);
        logTemplate = entry.kind === 'pose' ? DE.PREVIEW_LOG_POSE : DE.PREVIEW_LOG_PLACE;
      }

      const json = JSON.stringify(program);
      if (exceedsRunPayloadCap(json)) {
        toast.error(DE.PREVIEW_TOO_BIG);
        return;
      }

      // The gates were judged before the sim-entry settle and the recording
      // fetch; the leader (or a teach session, a run …) may have come on since.
      const late = previewBlockReason({
        ...(now.gates || {}),
        asset,
        robotType: now.robotType,
        workflowId: now.workflowId,
        inFlight: false,
      });
      if (late) {
        toast.error(PREVIEW_BLOCK_TITLES_DE[late]);
        return;
      }

      dispatch(clearWorkflowLog());
      dispatch(clearWorkflowError());
      dispatch(setWorkflowStatus({ log_message: formatDe(logTemplate, name) }));
      startedRef.current = { key, prev: resultsRef.current ? resultsRef.current[key] : undefined };
      dispatch(previewStarted({ key, kind: asset.kind, name, workflowId: wid }));

      const r = await now.callService('/workflow/start', 'physical_ai_interfaces/srv/StartWorkflow', {
        workflow_json: json,
        workflow_id: wid,
      });
      if (!r || !r.success) {
        const message = (r && r.message) || 'Vorschau konnte nicht gestartet werden.';
        dispatch(previewFailed({ key, message }));
        toast.error(previewMessageDe(message));
        return;
      }
      // The ids are the generated `vorschau-*` blocks, never the student's —
      // so this is a result chip, NOT setDebuggerWarnings.
      const ids = Array.isArray(r.unreachable_block_ids) ? r.unreachable_block_ids : [];
      if (ids.length > 0) {
        const messages = Array.isArray(r.unreachable_messages) ? r.unreachable_messages : [];
        dispatch(previewUnreachable({ key, message: messages[0] || '' }));
      }
      // The terminal status can arrive BEFORE the start reply (a lead-in refused
      // at run start): a result already recorded means the run is over, and
      // 'running' would re-block previews and sim exit until a Stopp.
      const startedPrev = startedRef.current && startedRef.current.key === key
        ? startedRef.current.prev : undefined;
      const current = resultsRef.current ? resultsRef.current[key] : undefined;
      if (current !== undefined && current !== startedPrev) return;
      dispatch(setRunState('running'));
      dispatch(setPaused(false));
    } catch (e) {
      const message = (e && e.message) || String(e);
      dispatch(previewFailed({ key, message }));
      toast.error(formatDe(DE.PREVIEW_FAILED, message));
    } finally {
      inFlightRef.current = false;
    }
  }, [dispatch]);

  return { startPreview };
}

export { useSimPreview };
