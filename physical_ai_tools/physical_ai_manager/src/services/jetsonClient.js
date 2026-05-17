// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import { CLOUD_API_URL, assertCloudApiConfigured } from './cloudConfig';

// Lightweight wrapper around the /jetson + /classrooms/{id}/jetson +
// /teacher/classrooms/{id}/jetson/* endpoints. Mirrors the
// cloudTrainingApi.js style: bearer-token auth, JSON in/out,
// error.detail bubbled up as the Error message.

async function _request(endpoint, method, accessToken, body = null) {
  assertCloudApiConfigured();
  const headers = {
    Authorization: `Bearer ${accessToken}`,
  };
  const options = { method, headers };
  if (body !== null) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`${CLOUD_API_URL}${endpoint}`, options);
  if (response.status === 404) {
    // Distinguish a missing-Jetson 404 from a generic failure so callers
    // can map it to the "no_jetson" state without throwing.
    const err = new Error('no jetson');
    err.status = 404;
    throw err;
  }
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    const e = new Error(err.detail || `Request failed: ${response.status}`);
    e.status = response.status;
    throw e;
  }
  // Some endpoints (heartbeat, release) return {ok: true} with 200; others
  // (claim) return a full JetsonInfo. Same response.json() handles both.
  return response.json();
}

/**
 * Look up the paired Jetson for the caller's classroom. Returns null if
 * no Jetson is paired (404 from the API).
 */
export async function getClassroomJetson(accessToken, classroomId) {
  try {
    return await _request(`/classrooms/${classroomId}/jetson`, 'GET', accessToken);
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

/**
 * Atomic claim. 409 P0030 → throws with status 409 (caller should flip
 * UI to busy + show "Jetson belegt von <name>").
 */
export async function claimJetson(accessToken, jetsonId) {
  return _request(`/jetson/${jetsonId}/claim`, 'POST', accessToken);
}

/**
 * 30 s heartbeat to keep the lock alive. 410 P0031 means the lock was
 * released server-side (sweep, teacher force-release) — caller should
 * disconnect locally.
 */
export async function heartbeatJetson(accessToken, jetsonId) {
  return _request(`/jetson/${jetsonId}/heartbeat`, 'POST', accessToken);
}

/**
 * Explicit "Trennen" — idempotent. The Bearer-token authenticated path
 * for normal disconnects.
 */
export async function releaseJetson(accessToken, jetsonId) {
  return _request(`/jetson/${jetsonId}/release`, 'POST', accessToken);
}

/**
 * sendBeacon-friendly release. Called from beforeunload to release the
 * lock when the student closes their tab. v2.3.0 added a dedicated
 * /jetson/{id}/release-beacon endpoint on the Cloud API that accepts
 * the JWT in the body (sendBeacon CAN'T set Authorization headers).
 *
 * Without this, every tab-close in a 15-student classroom would orphan
 * the Jetson lock for the full 5-min sweeper window — chronic
 * mid-lesson lockouts. With it, the lock releases within milliseconds
 * of the tab closing.
 *
 * Fire-and-forget — sendBeacon is spec'd to deliver even after the
 * document is gone, but doesn't return a response. The Cloud API
 * revalidates the JWT and returns 200 / 401 / 410; we never see it.
 */
export function releaseJetsonBeacon(accessToken, jetsonId) {
  if (typeof navigator === 'undefined' || !navigator.sendBeacon) {
    return;
  }
  // sendBeacon requires the body to be a Blob/ArrayBuffer/FormData/
  // URLSearchParams/string. To get Content-Type: application/json on
  // the server side we wrap the JSON in a Blob with an explicit type.
  // The Cloud API's release-beacon endpoint reads the access_token
  // from the JSON body, revalidates it via supabase.auth.get_user,
  // and then calls release_jetson — same logic as /jetson/{id}/release
  // but token comes from body instead of header.
  try {
    const body = new Blob(
      [JSON.stringify({ access_token: accessToken })],
      { type: 'application/json' }
    );
    navigator.sendBeacon(
      `${CLOUD_API_URL}/jetson/${jetsonId}/release-beacon`,
      body
    );
  } catch (_err) {
    // sendBeacon throws synchronously only when quota / payload-size
    // exceeds the browser limit (64 KB). Our payload is < 100 bytes;
    // ignore.
  }
}

// ---------- Teacher-only endpoints (v2.3.0) ----------

/**
 * Teacher pairs an unbound Jetson to one of their classrooms via the
 * 6-digit pairing code from setup.sh stdout. 404 if the code is
 * invalid or expired; 403 if the classroom doesn't belong to the
 * caller; 409 if the Jetson is already bound to a classroom.
 */
export async function pairJetson(accessToken, classroomId, pairingCode, mdnsName = null) {
  const body = { pairing_code: pairingCode };
  if (mdnsName) body.mdns_name = mdnsName;
  return _request(
    `/teacher/classrooms/${classroomId}/jetson/pair`,
    'POST',
    accessToken,
    body
  );
}

/**
 * Teacher emergency unlock — releases the current student's lock
 * without waiting for the 5-min sweeper. Used between consecutive
 * class periods when a student walked out without clicking Trennen.
 */
export async function forceReleaseJetson(accessToken, classroomId) {
  return _request(
    `/teacher/classrooms/${classroomId}/jetson/force-release`,
    'POST',
    accessToken
  );
}

/**
 * Generate a fresh 6-digit pairing code without SSHing back to the
 * Jetson. Returns {jetson_id, pairing_code, pairing_code_expires_at}.
 * The teacher then enters the new code in their other browser tab
 * (the classroom dashboard) to complete the re-pair.
 */
export async function regeneratePairingCode(accessToken, classroomId) {
  return _request(
    `/teacher/classrooms/${classroomId}/jetson/regenerate-code`,
    'POST',
    accessToken
  );
}

/**
 * Teacher unbinds the Jetson from the classroom. Sets classroom_id
 * back to NULL and force-releases any active session. The Jetson's
 * agent_token is preserved so the same physical device can be
 * re-paired to another classroom without SSH access.
 */
export async function unpairJetson(accessToken, classroomId) {
  return _request(
    `/teacher/classrooms/${classroomId}/jetson/unpair`,
    'POST',
    accessToken
  );
}
