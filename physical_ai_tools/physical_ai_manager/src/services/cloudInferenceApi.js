// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The cloud API's /inference/* routes (Cloud-Inferenz, spec §4.3).
//
// `apiRequest`'s contract: a refusal throws an Error carrying `status` and the
// German `detail`. Only `armInferenceSession` answers secrets (the connect token
// and the edge token of the run); its caller (features/cloudInference/
// controller.js) keeps them in local variables for one robot call and nowhere
// else.

import { apiRequest } from './apiClient';
import { CLOUD_API_URL } from './cloudConfig';

/** POST /inference/sessions — reserve a cloud GPU for a model (no secret in the answer). */
export function reserveInferenceSession(accessToken, trainingId) {
  return apiRequest('/inference/sessions', 'POST', accessToken, { training_id: trainingId });
}

/** POST /inference/sessions/{id}/arm — {token, edge_token, session_s}: the ONLY answer with secrets. */
export function armInferenceSession(accessToken, sessionId) {
  return apiRequest(`/inference/sessions/${encodeURIComponent(sessionId)}/arm`, 'POST', accessToken);
}

/** POST /inference/sessions/{id}/heartbeat — {status, end_reason, end_reason_de, max_end_in_s}. */
export function heartbeatInferenceSession(accessToken, sessionId) {
  return apiRequest(`/inference/sessions/${encodeURIComponent(sessionId)}/heartbeat`, 'POST', accessToken);
}

/** POST /inference/sessions/{id}/end — idempotent. */
export function endInferenceSession(accessToken, sessionId) {
  return apiRequest(`/inference/sessions/${encodeURIComponent(sessionId)}/end`, 'POST', accessToken);
}

/** GET /inference/usage — {daily_cap_s, session_cap_s, used_today_s, remaining_today_s, live_session_id}. */
export function getInferenceUsage(accessToken) {
  return apiRequest('/inference/usage', 'GET', accessToken);
}

/**
 * The same end, as a request that survives the page going away (pagehide, a
 * release with nothing left to wait for, sign-out): `keepalive`, the bearer in
 * the Authorization header, NO body. Fire and forget; never throws.
 */
export function endInferenceSessionKeepalive(accessToken, sessionId) {
  if (!CLOUD_API_URL || !accessToken || !sessionId) return;
  try {
    const pending = fetch(`${CLOUD_API_URL}/inference/sessions/${encodeURIComponent(sessionId)}/end`, {
      method: 'POST',
      keepalive: true,
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
  } catch {
    /* the page is going away; the 45 s rule ends the session anyway */
  }
}
