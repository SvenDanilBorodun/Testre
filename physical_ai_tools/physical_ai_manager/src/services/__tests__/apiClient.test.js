// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// Locks the status-carrying error contract. This is load-bearing: the /me
// error branches (StudentApp/WebApp 401/403 → re-login) and the dataset-sync
// handler (400 → "HF noch nicht verknüpft", 502 → "HF nicht erreichbar")
// all decide on `err.status`. Before the fix the thrown Error had no status
// and the re-login logic silently never fired.

import { apiRequest } from '../apiClient';
import { onMfaRequired } from '../../features/auth/mfaRequired';

vi.mock('../cloudConfig', () => ({
  CLOUD_API_URL: 'https://api.test.example',
  assertCloudApiConfigured: vi.fn(),
}));

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  };
}

describe('apiClient.apiRequest error contract', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = vi.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('throws an Error carrying .status AND .detail on a non-ok response', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(403, { detail: 'Nicht berechtigt' })
    );
    await expect(apiRequest('/me', 'GET', 'jwt')).rejects.toMatchObject({
      status: 403,
      detail: 'Nicht berechtigt',
      message: 'Nicht berechtigt',
    });
  });

  test('maps a 400 hf-not-linked error with its German detail', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(400, { detail: 'HuggingFace-ID nicht verknüpft' })
    );
    await expect(
      apiRequest('/datasets/sync', 'POST', 'jwt')
    ).rejects.toMatchObject({
      status: 400,
      detail: 'HuggingFace-ID nicht verknüpft',
    });
  });

  test('falls back to statusText when the body is not JSON, still carrying .status', async () => {
    global.fetch.mockResolvedValue({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => {
        throw new Error('not json');
      },
    });
    const err = await apiRequest('/datasets/sync', 'POST', 'jwt').catch((e) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(502);
    expect(err.message).toBe('Bad Gateway');
  });

  test('returns the parsed body on 200', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { role: 'student' }));
    await expect(apiRequest('/me', 'GET', 'jwt')).resolves.toEqual({
      role: 'student',
    });
  });

  test('returns null on 204 (no content)', async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      status: 204,
      statusText: 'No Content',
      json: async () => {
        throw new Error('no body');
      },
    });
    await expect(apiRequest('/x', 'DELETE', 'jwt')).resolves.toBeNull();
  });

  // 2026-11-10: the API's machine-readable reason rides on the error, and an
  // admin route's `mfa_required` sends the admin back to the TOTP screen.
  test('carries the code and announces mfa_required', async () => {
    const heard = vi.fn();
    const off = onMfaRequired(heard);
    try {
      global.fetch.mockResolvedValue(
        jsonResponse(403, { detail: 'Zwei-Faktor-Anmeldung erforderlich', code: 'mfa_required' })
      );
      await expect(apiRequest('/admin/teachers', 'GET', 'jwt')).rejects.toMatchObject({
        status: 403,
        code: 'mfa_required',
        detail: 'Zwei-Faktor-Anmeldung erforderlich',
      });
      expect(heard).toHaveBeenCalledTimes(1);

      global.fetch.mockResolvedValue(jsonResponse(403, { detail: 'Nur für Lehrkräfte erlaubt.' }));
      const err = await apiRequest('/teacher/classrooms', 'GET', 'jwt').catch((e) => e);
      expect(err.code).toBeUndefined();
      global.fetch.mockResolvedValue(
        jsonResponse(503, { detail: 'Die Anmeldung kann gerade nicht geprüft werden.', code: 'auth_unavailable' })
      );
      await expect(apiRequest('/me', 'GET', 'jwt')).rejects.toMatchObject({ status: 503, code: 'auth_unavailable' });
      expect(heard).toHaveBeenCalledTimes(1);
    } finally {
      off();
    }
  });
});
