// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The browser half of the Cloud-Inferenz contract with the robot (spec §5.2,
// frozen contract C4/C8). The robot half is
// physical_ai_server/inference/cloud_contract.py;
// robotis_ai_setup/tests/test_cloud_inference_lockstep.py reads both, so every
// constant stays a plain `export const NAME = <literal>;`.
//
//   START  /edubotics/cloud_inference/start   physical_ai_interfaces/srv/StartCloudInference
//          request: task_info, session_token, edge_token (the two one-time
//          credentials of this run; nothing else ever carries them)
//   STOP   /edubotics/cloud_inference/stop    std_srvs/srv/Trigger (ends only a cloud run)
//   STATE  /edubotics/cloud_inference_state   std_msgs/String JSON, latched + 1 Hz, facts only

export const START_SERVICE = '/edubotics/cloud_inference/start';
export const START_SERVICE_TYPE = 'physical_ai_interfaces/srv/StartCloudInference';
export const STOP_SERVICE = '/edubotics/cloud_inference/stop';
export const STOP_SERVICE_TYPE = 'std_srvs/srv/Trigger';
export const STATE_TOPIC = '/edubotics/cloud_inference_state';
export const STATE_SCHEMA_VERSION = 1;
export const STATE_KEYS = Object.freeze(['v', 'seq', 'active', 'phase', 'rtt_ms_p50', 'plan_age_ms', 'gpu_ms_p50', 'hold_s', 'ends_in_s', 'stale_camera', 'end_text_de']);
export const PHASES = Object.freeze(['idle', 'loading', 'running', 'holding', 'ended']);
// The robot publishes at 1 Hz; a state older than this is no state at all.
export const STATE_STALE_MS = 6000;
// The request keys of StartCloudInference.srv, in its field order.
export const START_REQUEST_KEYS = Object.freeze(['task_info', 'session_token', 'edge_token']);

const NUMBER_KEYS = ['rtt_ms_p50', 'plan_age_ms', 'gpu_ms_p50', 'hold_s', 'ends_in_s'];

function numberOrNull(value) {
  if (value === null) return { ok: true, value: null };
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return { ok: true, value };
  return { ok: false };
}

/**
 * Parse one /edubotics/cloud_inference_state payload.
 *
 * Returns null for anything malformed (not JSON, not an object, another schema
 * version, a missing key, a value of the wrong type): an unknown state shows no
 * facts, it never shows a made-up one.
 *
 * @param {string} data raw std_msgs/String payload
 * @returns {null|object}
 */
export function parseCloudState(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  for (const key of STATE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(raw, key)) return null;
  }
  if (raw.v !== STATE_SCHEMA_VERSION) return null;
  if (typeof raw.seq !== 'number' || !Number.isFinite(raw.seq)) return null;
  if (typeof raw.active !== 'boolean') return null;
  if (typeof raw.phase !== 'string' || !PHASES.includes(raw.phase)) return null;
  const out = { v: raw.v, seq: raw.seq, active: raw.active, phase: raw.phase };
  for (const key of NUMBER_KEYS) {
    const n = numberOrNull(raw[key]);
    if (!n.ok) return null;
    out[key] = n.value;
  }
  if (raw.stale_camera !== null && typeof raw.stale_camera !== 'string') return null;
  if (raw.end_text_de !== null && typeof raw.end_text_de !== 'string') return null;
  out.stale_camera = raw.stale_camera;
  out.end_text_de = raw.end_text_de;
  return out;
}
