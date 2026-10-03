import { apiRequest } from './apiClient';

export async function getMe(accessToken) {
  return apiRequest('/me', 'GET', accessToken);
}

/**
 * Link the caller's HuggingFace "Benutzer-ID" to their cloud profile.
 *
 * PATCH /me { hf_username } — self-only (keyed to the JWT server-side).
 * Returns the full updated profile (same shape as GET /me), so callers
 * can feed the response straight into setProfile if they want, or just
 * read back the persisted hf_username.
 *
 * @deprecated Nothing in this app calls it any more. The account name is now
 * PROVEN by the cloud from the student's own stored token (PUT /me/hf-token,
 * services/hfTokenApi), and PATCH /me answers 409 once a token is stored. It
 * stays exported for the wire-shape test and for older clients.
 */
export async function patchMyHfUsername(accessToken, hfUsername) {
  return apiRequest('/me', 'PATCH', accessToken, { hf_username: hfUsername });
}
