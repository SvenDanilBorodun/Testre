// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// `/edubotics/daten_state` (spec §J.5: std_msgs/String JSON, latched + 1 Hz):
// which datasets are busy and with what, the jobs and the HF worker's running
// upload. ONE rosbridge subscription shared by every reader (ref-counted): the
// Daten page, the Training banner's hook and the Aufnahme page's STARTING-only
// wait (useDatenStartWait). The subscription ends with its last reader and the
// snapshot is forgotten then, so a page that mounts again starts from „no
// message yet" (the crashed card waits for a fresh message, T-1 a).
//
// Local state, not Redux: nothing outside the readers must re-render on it.

import { useCallback, useSyncExternalStore } from 'react';
import { useSelector } from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../../../utils/rosConnectionManager';
import { BUSY_KINDS, JOB_OPS, JOB_STATES, STATE_TOPIC } from '../datenContract';

export const EMPTY_DATEN_STATE = Object.freeze({ received: false, payload: null, receivedAt: null });

/** The JSON of one message → `{v, seq, busy, jobs, transfer}` with only well-formed rows, or null. */
export function parseDatenState(text) {
  let v;
  try {
    v = JSON.parse(String(text || ''));
  } catch {
    return null;
  }
  if (!v || typeof v !== 'object' || v.v !== 1) return null;
  const busy = (Array.isArray(v.busy) ? v.busy : [])
    .filter((b) => b && typeof b.id === 'string' && BUSY_KINDS.includes(b.kind))
    .map((b) => ({ id: b.id, kind: b.kind }));
  const jobs = (Array.isArray(v.jobs) ? v.jobs : [])
    .filter((j) => j && typeof j.job_id === 'string' && JOB_OPS.includes(j.op) && JOB_STATES.includes(j.state))
    .map((j) => ({
      ...j,
      datasets: Array.isArray(j.datasets) ? j.datasets.filter((x) => typeof x === 'string') : [],
      outputs: Array.isArray(j.outputs) ? j.outputs.filter((x) => typeof x === 'string') : [],
    }));
  const t = v.transfer;
  const transfer = t && typeof t === 'object' && typeof t.repo_id === 'string'
    ? { kind: String(t.kind || ''), repo_id: t.repo_id, target: typeof t.target === 'string' ? t.target : null }
    : null;
  return { v: 1, seq: Number(v.seq) || 0, busy, jobs, transfer };
}

/** `{id: kind}` of a payload's busy list. */
export function busyMap(payload) {
  const out = {};
  if (payload && Array.isArray(payload.busy)) payload.busy.forEach((b) => { out[b.id] = b.kind; });
  return out;
}

// ---------------------------------------------------------------- the shared subscription

const shared = {
  url: null,
  topic: null,
  listeners: new Set(),
  snapshot: EMPTY_DATEN_STATE,
  generation: 0,
};

function emit() {
  shared.listeners.forEach((fn) => {
    try { fn(shared.snapshot); } catch (err) { console.error('daten_state listener failed:', err); }
  });
}

function closeTopic() {
  shared.generation += 1;
  if (shared.topic) {
    try { shared.topic.unsubscribe(); } catch { /* the socket may already be gone */ }
  }
  shared.topic = null;
  shared.url = null;
  shared.snapshot = EMPTY_DATEN_STATE;
}

async function openTopic(url) {
  closeTopic();
  shared.url = url;
  const gen = shared.generation;
  let ros;
  try {
    ros = await rosConnectionManager.getConnection(url);
  } catch {
    return;
  }
  if (gen !== shared.generation || !ros) return;
  const topic = new ROSLIB.Topic({
    ros,
    name: STATE_TOPIC,
    messageType: 'std_msgs/msg/String',
    queue_length: 1,
  });
  shared.topic = topic;
  topic.subscribe((msg) => {
    if (gen !== shared.generation) return;
    const payload = parseDatenState(msg && msg.data);
    if (!payload) return;
    shared.snapshot = { received: true, payload, receivedAt: Date.now() };
    emit();
  });
}

/**
 * Listen to `/edubotics/daten_state` on `url`. Returns the unsubscribe; the
 * topic closes with the last listener. `listener(snapshot)` is called on
 * every message (never for the current snapshot: read it with
 * getDatenStateSnapshot).
 */
export function subscribeDatenState(url, listener) {
  if (!url) return () => {};
  shared.listeners.add(listener);
  if (shared.url !== url) openTopic(url);
  return () => {
    shared.listeners.delete(listener);
    if (shared.listeners.size === 0) closeTopic();
  };
}

export function getDatenStateSnapshot() {
  return shared.snapshot;
}

/** Test seam: forget everything (no listener survives). */
export function resetDatenStateForTests() {
  shared.listeners.clear();
  closeTopic();
}

/**
 * The latest `/edubotics/daten_state` while `enabled` (and the robot link
 * has a URL): `{received, payload, receivedAt}`. Re-renders on every message.
 */
export default function useDatenState({ enabled = true } = {}) {
  const url = useSelector((s) => s.ros.rosbridgeUrl);
  const active = !!(enabled && url);
  // Stable per (active, url): a new subscribe function would make React
  // unsubscribe and subscribe again on every render, and the last
  // unsubscribe closes the shared topic.
  const subscribe = useCallback(
    (onChange) => (active ? subscribeDatenState(url, () => onChange()) : () => {}),
    [active, url],
  );
  const getSnapshot = useCallback(() => (active ? shared.snapshot : EMPTY_DATEN_STATE), [active]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
