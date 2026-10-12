// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The robot's /edubotics/cloud_inference_state (latched + 1 Hz): facts about the
// running cloud run (Antwortzeit, Plan-Alter, GPU-Zeit, „Endet in", a hold, a
// stale camera, the end sentence). Nothing is inferred client-side: a state
// older than STATE_STALE_MS, a malformed one or no topic at all (an older image)
// is null, and the page then shows no facts.

import { useEffect, useState } from 'react';
import { useSelector } from 'react-redux';

import { subscribeCloudState } from '../features/cloudInference/robotCalls';
import { STATE_STALE_MS } from '../features/cloudInference/robotContract';

const TICK_MS = 1000;

/**
 * @param {object} [opts]
 * @param {boolean} [opts.enabled=true] subscribe only while the cloud view is shown
 * @returns {null|object} the last valid state, or null when unknown / stale
 */
export default function useCloudInferenceState({ enabled = true } = {}) {
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const [entry, setEntry] = useState(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) {
      setEntry(null);
      return undefined;
    }
    let cancelled = false;
    let unsubscribe = null;
    subscribeCloudState(rosbridgeUrl, (state) => {
      if (!cancelled) setEntry({ state, at: Date.now() });
    }).then(
      (unsub) => {
        if (cancelled) unsub();
        else unsubscribe = unsub;
      },
      () => { /* no link: the heartbeat chip tells that story */ },
    );
    return () => {
      cancelled = true;
      if (unsubscribe) unsubscribe();
    };
  }, [enabled, rosbridgeUrl]);

  useEffect(() => {
    if (!enabled) return undefined;
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, [enabled]);

  if (!enabled || !entry) return null;
  if (now - entry.at > STATE_STALE_MS) return null;
  return entry.state;
}
