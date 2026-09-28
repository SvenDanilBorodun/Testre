/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen host: turns a `studioAssets.teach.requested` (the toolbar chooser,
// a Sammlung flyout or drawer „… vormachen", a code sidebar row) into an open
// overlay — one focused window of the requested kind (owner decision D1) — or
// into a German refusal. The entry gates are judged HERE, when the request is
// processed, because a request can be queued from a flyout while the rig
// changed. A request without a valid kind is dropped silently: there is no
// kind-less window (the slice already drops one; this is the second fence).
//
// It works on the page's asset document (`assetDoc`, sammlung/assetDocument.js)
// — a Blockly workspace or a Python/Java program — so Vormachen opens for a
// code program too (owner decision O4). A caller that still hands a bare
// `workspace` gets that workspace's Blockly document.

import React, { useEffect, useMemo } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  selectTeachState,
  teachClosed,
  teachModeResolved,
  teachOpened,
  teachRequestHandled,
} from '../../../features/workshop/studioAssetsSlice';
import { useHomeGlide } from '../HomeGlidePrompt';
import { assetDocumentOf } from '../sammlung/assetDocument';
import {
  TEACH_BLOCK_TITLES_DE, isTeachKind, resolveTeachMode, teachEntryBlockReason,
} from './teachGates';
import TeachOverlay from './TeachOverlay';

function TeachHost({
  isActive, workspace, assetDoc = null, accessToken, workflowId, robotType, caps, heartbeatStatus, runState, paused,
  simMode, jogHandGuideOn, previewActive, rsBridge, saveWorkflowNow, refetchTrajectories,
}) {
  const dispatch = useDispatch();
  const doc = useMemo(() => assetDocumentOf(assetDoc, workspace), [assetDoc, workspace]);
  const teach = useSelector(selectTeachState) || {};
  const { homeGlideActive } = useHomeGlide();
  const token = teach.requested ? teach.requested.token : null;

  useEffect(() => {
    if (token === null || token === undefined) return;
    if (teach.open) {
      dispatch(teachRequestHandled());
      return;
    }
    const kind = teach.requested ? teach.requested.kind : null;
    if (!isTeachKind(kind)) {
      dispatch(teachRequestHandled());
      return;
    }
    // No editor on screen (calibration, gallery): nothing to teach into.
    if (!isActive || !doc) {
      dispatch(teachRequestHandled());
      return;
    }
    const reason = teachEntryBlockReason({
      heartbeatStatus,
      runState,
      paused,
      simMode,
      jogHandGuideOn,
      previewActive,
    }) || (homeGlideActive ? 'glide' : null);
    if (reason) {
      toast.error(TEACH_BLOCK_TITLES_DE[reason]);
      dispatch(teachRequestHandled());
      return;
    }
    // Close the flyout the request came from, so Blockly holds no focus (a
    // code document has none: a no-op).
    try { doc.hideChaff(); } catch (_) { /* a disposed workspace */ }
    // D8: a live leader (a POSITIVE bridge answer) on a leader-capable profile
    // teaches with the leader arm; the mode is fixed for the whole session.
    // R7: while the bridge cannot report the leader state (not answered yet, or
    // unavailable) on a rig that may have a leader, the session opens UNRESOLVED
    // (`mode: null`) — the overlay shows the German notice and no teaching —
    // and the effect below resolves it when the bridge answers.
    const mode = resolveTeachMode({ rsBridge, caps });
    dispatch(teachOpened({ mode, kind }));
    // Only a NEW token is a new request; the gate inputs are read as they are now.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // R7: resolve an unresolved session on the bridge's first definite answer (the
  // hook re-polls every RS_STATUS_POLL_MS). Hand when the leader is proven off or
  // the rig follower-only, leader when it is on — exactly what an open with that
  // answer would have chosen. The overlay is keyed on the mode, so it remounts
  // with a fresh session (of the same kind); the unresolved one never sent a call.
  const resolvedMode = teach.open && !teach.mode ? resolveTeachMode({ rsBridge, caps }) : null;
  useEffect(() => {
    if (resolvedMode) dispatch(teachModeResolved({ mode: resolvedMode }));
  }, [resolvedMode, dispatch]);

  // Redux keeps `teach.open` across a tab change; the overlay's own teardown
  // has already released the arm by the time this runs.
  useEffect(() => () => { dispatch(teachClosed()); }, [dispatch]);

  if (!teach.open || !isActive || !isTeachKind(teach.kind)) return null;
  return (
    <TeachOverlay
      key={teach.mode || 'pending'}
      mode={teach.mode || 'pending'}
      kind={teach.kind}
      onClose={() => dispatch(teachClosed())}
      workspace={workspace}
      assetDoc={doc}
      accessToken={accessToken}
      workflowId={workflowId}
      robotType={robotType}
      caps={caps}
      heartbeatOk={heartbeatStatus === 'connected'}
      rsBridge={rsBridge}
      saveWorkflowNow={saveWorkflowNow}
      refetchTrajectories={refetchTrajectories}
    />
  );
}

export default TeachHost;
