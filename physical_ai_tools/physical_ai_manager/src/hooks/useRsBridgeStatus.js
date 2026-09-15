/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { useEffect, useMemo, useState } from 'react';
import { rsControlBase, usePiMode } from '../utils/piMode';

// The Roboter-Studio control bridge probe, moved out of RunControls so every
// Roboter-Studio surface reads ONE answer to "is the leader arm on?".
//
// The follower's arm_controller subscribes to /leader/joint_trajectory; running
// a workflow publishes there too. While the leader arm is ON (both-arms mode),
// its broadcaster also floods that topic at ~100 Hz with the limp leader's pose,
// so the two writers fight and the follower jerks between poses. We probe the
// GUI's localhost control bridge (roboter_studio_control.py, the same one
// LeaderToggle uses). When the bridge is ABSENT (Jetson/cloud/old GUI), there is
// no leader to fight — Roboter Studio there is follower-only by construction —
// so the probe FAILS OPEN. The base is Windows-loopback (:8769) OR the Pi's
// same-origin /api/system proxy (rsControlBase, utils/piMode) — a remote browser
// can't reach the student PC's localhost. The base is derived from the LIVE
// `piMode` context value and the first probe waits for `piModeResolved`, so a
// boot-window probe on a Pi never mis-routes to the loopback base before the
// marker resolves; a missing provider resolves immediately (default context),
// so Windows behaviour is unchanged.
export const RS_STATUS_TIMEOUT_MS = 4000;
export const RS_STATUS_POLL_MS = 8000;

const UNAVAILABLE = Object.freeze({
  available: false, followerOnly: false, hasLeader: undefined, busy: false,
});

export async function probeRsStatus(base) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), RS_STATUS_TIMEOUT_MS);
  try {
    const res = await fetch(`${base}/roboter-studio/status`, { signal: ctrl.signal });
    if (!res.ok) return UNAVAILABLE;
    const body = await res.json().catch(() => ({}));
    // A `null` body throws on the property read and lands in the catch — the
    // fail-open answer, exactly as the RunControls original behaved.
    return {
      available: true,
      followerOnly: !!body.follower_only,
      // `undefined` when the bridge does not say (an older GUI): only an
      // EXPLICIT false means "this rig has no leader".
      hasLeader: typeof body.has_leader === 'boolean' ? body.has_leader : undefined,
      busy: !!body.busy,
    };
  } catch (e) {
    // No bridge reachable (Jetson/cloud/old GUI) → don't block.
    return UNAVAILABLE;
  } finally {
    clearTimeout(timer);
  }
}

function sameStatus(a, b) {
  return a.available === b.available && a.followerOnly === b.followerOnly
    && a.hasLeader === b.hasLeader && a.busy === b.busy;
}

/**
 * Poll the control bridge every RS_STATUS_POLL_MS.
 *
 * Returns `{ available, followerOnly, hasLeader, busy, leaderOn, probed }` with
 * `leaderOn = available && !followerOnly`. A failed probe yields
 * `available false, followerOnly false`, so `leaderOn` fails OPEN to false —
 * and a consumer that needs a POSITIVE follower-only answer must test
 * `available === true && followerOnly === true`, never `!leaderOn`.
 *
 * `probed` tells „not answered yet" from „answered: unavailable": it is false
 * until the FIRST probe of this mount has settled — a bridge answer, an HTTP
 * error, a network error and the RS_STATUS_TIMEOUT_MS abort all count — and
 * then stays true (a later failed poll is an answer, not a return to „pending").
 * Before that, `available false` means NOTHING about the bridge. A consumer that
 * ignores `probed` sees exactly the fields it saw before.
 * `enabled: false` stops polling and reports the fail-open answer with
 * `probed: false` (nothing is being asked).
 */
export default function useRsBridgeStatus({ enabled = true } = {}) {
  const [status, setStatus] = useState(UNAVAILABLE);
  const [probed, setProbed] = useState(false);
  const { piMode, piModeResolved } = usePiMode();

  useEffect(() => {
    if (!enabled || !piModeResolved) return undefined;
    let cancelled = false;
    const tick = async () => {
      const next = await probeRsStatus(rsControlBase(piMode));
      if (cancelled) return;
      // Keep the previous object on an unchanged answer, so a consumer does
      // not re-render every poll.
      setStatus((prev) => (sameStatus(prev, next) ? prev : next));
      // probeRsStatus never throws and always settles (its own abort timer), so
      // every first tick ends here. Setting true again is a no-op re-render.
      setProbed(true);
    };
    tick();
    const intervalId = setInterval(tick, RS_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, [enabled, piMode, piModeResolved]);

  const effective = enabled ? status : UNAVAILABLE;
  const effectiveProbed = enabled && probed;
  return useMemo(() => ({
    ...effective,
    leaderOn: effective.available && !effective.followerOnly,
    probed: effectiveProbed,
  }), [effective, effectiveProbed]);
}
