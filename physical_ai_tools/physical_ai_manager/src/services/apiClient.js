import { CLOUD_API_URL, assertCloudApiConfigured } from './cloudConfig';
import { MFA_REQUIRED_CODE, notifyMfaRequired } from '../features/auth/mfaRequired';

export async function apiRequest(endpoint, method, accessToken, body = null) {
  assertCloudApiConfigured();
  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${accessToken}`,
  };

  const options = { method, headers };
  if (body !== null && body !== undefined) {
    options.body = JSON.stringify(body);
  }

  const response = await fetch(`${CLOUD_API_URL}${endpoint}`, options);

  if (!response.ok) {
    // Carry BOTH the HTTP status and the German detail on the thrown Error.
    // This is load-bearing: the /me error branches (StudentApp/WebApp) and
    // the dataset-sync handler decide on `err.status` (401/403 → re-login,
    // 400 → "HF noch nicht verknüpft", 404/502/5xx → their own copy). A
    // status-less Error collapsed all of these into one opaque string and
    // the re-login logic silently never fired.
    // `err.code` is the API's machine-readable reason where it sends one
    // (2026-11-10: `mfa_required` on an admin route without the second
    // factor, `auth_unavailable` when the login cannot be checked).
    let detail;
    let code;
    try {
      const parsed = await response.json();
      detail = parsed?.detail;
      code = typeof parsed?.code === 'string' ? parsed.code : undefined;
    } catch {
      detail = response.statusText;
    }
    const err = new Error(detail || `HTTP ${response.status}`);
    err.status = response.status;
    err.detail = detail;
    if (code) err.code = code;
    if (response.status === 403 && code === MFA_REQUIRED_CODE) notifyMfaRequired();
    throw err;
  }

  if (response.status === 204) return null;
  return response.json();
}
