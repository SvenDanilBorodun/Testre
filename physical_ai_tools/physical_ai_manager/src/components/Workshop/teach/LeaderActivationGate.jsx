/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Leader-arm Vormachen (D8): the teleop broadcaster is spawned only by the
// Startseite activation, so on an un-activated rig the leader moves and the
// follower does not — every take would be „no motion". This gate replaces the
// overlay's teaching content with a German panel until the agent reports
// `active`, and tells the session (onBlockedChange) so every key except Esc
// waits too.
//
// Rendered by TeachOverlay ONLY in leader mode: useRobotActivation reads
// `s.ros.rosbridgeUrl` unconditionally, and no other surface (hand mode, the
// WorkshopPage tests) has or needs that slice.
//
// `status === null` is UNKNOWN (an older image has no activation agent), never
// „not activated" — the children render, exactly like ActivationCard.

import React, { useEffect, useRef } from 'react';
import useRobotActivation, { ACTIVE } from '../../../hooks/useRobotActivation';
import { DE } from '../blocks/messages_de';

export function leaderActivationBlocked(status) {
  return !!status && status.state !== ACTIVE;
}

function LeaderActivationGate({ children, onBlockedChange }) {
  const { status } = useRobotActivation({ enabled: true });
  const blocked = leaderActivationBlocked(status);

  const onChangeRef = useRef(onBlockedChange);
  onChangeRef.current = onBlockedChange;
  useEffect(() => {
    if (typeof onChangeRef.current === 'function') onChangeRef.current(blocked);
  }, [blocked]);
  // Unmounted (overlay closed or mode changed): nothing blocks any more.
  useEffect(() => () => {
    if (typeof onChangeRef.current === 'function') onChangeRef.current(false);
  }, []);

  if (!blocked) return children;
  return (
    <div
      role="alert"
      data-testid="teach-leader-not-active"
      className="rounded-xl border border-amber-300 bg-amber-50 p-6 text-amber-900"
    >
      <p className="text-2xl font-semibold">{DE.TEACH_BLOCK_NOT_ACTIVE}</p>
    </div>
  );
}

export default LeaderActivationGate;
