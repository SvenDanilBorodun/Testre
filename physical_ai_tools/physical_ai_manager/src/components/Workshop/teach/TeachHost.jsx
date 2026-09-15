/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen host: turns a `studioAssets.teach.requested` (the toolbar button or
// a Sammlung flyout „✋ … vormachen") into an open overlay — or into a German
// refusal. The entry gates are judged HERE, when the request is processed,
// because a request can be queued from a flyout while the rig changed.

import React, { useEffect } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  selectTeachState,
  teachClosed,
  teachOpened,
  teachRequestHandled,
} from '../../../features/workshop/studioAssetsSlice';
import { useHomeGlide } from '../HomeGlidePrompt';
import { TEACH_BLOCK_TITLES_DE, teachEntryBlockReason, teachModeFor } from './teachGates';
import TeachOverlay from './TeachOverlay';

function TeachHost({
  isActive, workspace, accessToken, workflowId, robotType, caps, heartbeatStatus, runState, paused,
  simMode, jogHandGuideOn, previewActive, rsBridge, saveWorkflowNow, refetchTrajectories,
}) {
  const dispatch = useDispatch();
  const teach = useSelector(selectTeachState) || {};
  const { homeGlideActive } = useHomeGlide();
  const token = teach.requested ? teach.requested.token : null;

  useEffect(() => {
    if (token === null || token === undefined) return;
    if (teach.open) {
      dispatch(teachRequestHandled());
      return;
    }
    // No editor on screen (calibration, gallery): nothing to teach into.
    if (!isActive || !workspace) {
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
    // Close the flyout the request came from, so Blockly holds no focus.
    try { workspace.hideChaff(); } catch (_) { /* a disposed workspace */ }
    // D8: a live leader (a POSITIVE bridge answer) on a leader-capable profile
    // teaches with the leader arm; the mode is fixed for the whole session.
    const mode = teachModeFor({ rsLeaderOn: !!(rsBridge && rsBridge.leaderOn), caps });
    dispatch(teachOpened({ mode, focus: teach.requested.focus || null }));
    // Only a NEW token is a new request; the gate inputs are read as they are now.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // Redux keeps `teach.open` across a tab change; the overlay's own teardown
  // has already released the arm by the time this runs.
  useEffect(() => () => { dispatch(teachClosed()); }, [dispatch]);

  if (!teach.open || !isActive) return null;
  return (
    <TeachOverlay
      mode={teach.mode || 'hand'}
      focus={teach.focus || null}
      onClose={() => dispatch(teachClosed())}
      workspace={workspace}
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
