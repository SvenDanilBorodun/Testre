// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import { CLOUD_API_URL, assertCloudApiConfigured } from './cloudConfig';

// Lightweight wrapper around the /jetson + /classrooms/{id}/jetson
// endpoints. Mirrors the cloudTrainingApi.js style: bearer-token auth,
// JSON in/out, error.detail bubbled up as the Error message.

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
 * Explicit "Trennen" — idempotent. Also called via navigator.sendBeacon
 * on tab unload (see useJetsonConnection).
 */
export async function releaseJetson(accessToken, jetsonId) {
  return _request(`/jetson/${jetsonId}/release`, 'POST', accessToken);
}

/**
 * navigator.sendBeacon is more reliable than fetch() during page unload
 * (it's spec'd to deliver even after the document is gone). Browser
 * support: all modern. We don't need a response — fire and forget.
 */
export function releaseJetsonBeacon(accessToken, jetsonId) {
  if (typeof navigator === 'undefined' || !navigator.sendBeacon) {
    return;
  }
  // sendBeacon doesn't accept custom headers — we encode the bearer
  // token as a query parameter ONLY for this fallback path. The Cloud
  // API's _request layer doesn't read tokens from query params today,
  // so the beacon path will fail authentication. Acceptable trade: the
  // 5-min sweeper will reap the lock anyway. Keeping the beacon call
  // for completeness so future Cloud API auth-via-query support
  // "just works" without a frontend change.
  try {
    navigator.sendBeacon(
      `${CLOUD_API_URL}/jetson/${jetsonId}/release`,
      JSON.stringify({ access_token: accessToken }),
    );
  } catch (_err) {
    // sendBeacon throws synchronously only when quota / payload-size
    // exceeds the browser limit (64 KB). Our payload is tiny; ignore.
  }
}
