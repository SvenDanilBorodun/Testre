// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the /me wire shape, incl. the new PATCH that links the HF
// "Benutzer-ID" to the cloud profile (StudentApp auto-link). A regression
// here silently breaks the dataset-visibility reconciliation.

import { getMe, patchMyHfUsername } from '../meApi';

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

describe('meApi', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = vi.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('getMe GETs /me with the bearer token', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(200, { role: 'student', hf_username: null })
    );
    const me = await getMe('jwt-token');
    expect(me).toEqual({ role: 'student', hf_username: null });
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.test.example/me',
      expect.objectContaining({
        method: 'GET',
        headers: expect.objectContaining({ Authorization: 'Bearer jwt-token' }),
      })
    );
  });

  test('patchMyHfUsername PATCHes /me with { hf_username } and returns the updated profile', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(200, { role: 'student', hf_username: 'student42' })
    );
    const updated = await patchMyHfUsername('jwt-token', 'student42');
    expect(updated).toEqual({ role: 'student', hf_username: 'student42' });
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe('https://api.test.example/me');
    expect(opts.method).toBe('PATCH');
    expect(opts.headers.Authorization).toBe('Bearer jwt-token');
    expect(JSON.parse(opts.body)).toEqual({ hf_username: 'student42' });
  });
});
