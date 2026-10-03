// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The cloud half of the per-student Hugging-Face token (`/me/hf-token`, cloud
// API migration 042). Every call is self-only: the server keys it to the JWT,
// so there is no user-id parameter anywhere (no IDOR by construction).
//
// THE TOKEN'S ONLY HOMES. `putHfToken` is the one place a token goes OUT (the
// student pasted it) and `revealHfToken` the one place it comes BACK (to be
// relayed to the robot). Callers hold it in a local variable for the length of
// one await and never in Redux, an action payload, storage, a URL or a log; the
// features/hfToken thunks are plain thunks for exactly that reason.
//
// Errors are `apiClient`'s: an Error with `.status` and `.detail`. A token
// problem is a 422 and never a 401/403 (the SPA signs out on those), so a bad
// token cannot bounce the student off the page.

import { apiRequest } from './apiClient';

const ENDPOINT = '/me/hf-token';

/** GET /me/hf-token: `{stored, usable, hf_username, hint, fp, role, validated_at}`. */
export async function getHfToken(accessToken) {
  return apiRequest(ENDPOINT, 'GET', accessToken);
}

/** PUT /me/hf-token `{token}`: validated against Hugging Face, then stored encrypted. */
export async function putHfToken(accessToken, token) {
  return apiRequest(ENDPOINT, 'PUT', accessToken, { token });
}

/** DELETE /me/hf-token: idempotent; keeps the proven `users.hf_username`. */
export async function deleteHfToken(accessToken) {
  return apiRequest(ENDPOINT, 'DELETE', accessToken);
}

/** POST /me/hf-token/reveal: `{token, fp}`, for relaying to the robot ONLY. */
export async function revealHfToken(accessToken) {
  return apiRequest(`${ENDPOINT}/reveal`, 'POST', accessToken);
}

/** POST /me/hf-token/verify: asks Hugging Face again and refreshes `validated_at`. */
export async function verifyHfToken(accessToken) {
  return apiRequest(`${ENDPOINT}/verify`, 'POST', accessToken);
}
