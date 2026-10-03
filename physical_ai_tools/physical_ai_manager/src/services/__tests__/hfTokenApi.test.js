// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the /me/hf-token wire shape: five self-only calls, the bearer JWT on
// each, a body only on PUT, and the status-carrying error contract the card
// relies on (a token problem is a 422, never a 401/403).

import {
  deleteHfToken,
  getHfToken,
  putHfToken,
  revealHfToken,
  verifyHfToken,
} from '../hfTokenApi';

vi.mock('../cloudConfig', () => ({
  CLOUD_API_URL: 'https://api.test.example',
  assertCloudApiConfigured: vi.fn(),
}));

// Low-entropy fixture on purpose (the repository's secret scan covers history).
const TOKEN = `hf_${'a'.repeat(34)}`;

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  };
}

describe('hfTokenApi', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = vi.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  const lastCall = () => global.fetch.mock.calls[0];

  test('getHfToken GETs /me/hf-token with the bearer token and no body', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { stored: false }));
    await expect(getHfToken('jwt-1')).resolves.toEqual({ stored: false });
    const [url, opts] = lastCall();
    expect(url).toBe('https://api.test.example/me/hf-token');
    expect(opts.method).toBe('GET');
    expect(opts.headers.Authorization).toBe('Bearer jwt-1');
    expect(opts.body).toBeUndefined();
  });

  test('putHfToken PUTs the token as {token} and nothing else', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { stored: true }));
    await putHfToken('jwt-1', TOKEN);
    const [url, opts] = lastCall();
    expect(url).toBe('https://api.test.example/me/hf-token');
    expect(opts.method).toBe('PUT');
    expect(JSON.parse(opts.body)).toEqual({ token: TOKEN });
  });

  test('the token appears in the request body only, never in the URL or a header', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { stored: true }));
    await putHfToken('jwt-1', TOKEN);
    const [url, opts] = lastCall();
    expect(url).not.toContain(TOKEN);
    expect(JSON.stringify(opts.headers)).not.toContain(TOKEN);
  });

  test('deleteHfToken DELETEs /me/hf-token', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { stored: false }));
    await deleteHfToken('jwt-1');
    const [url, opts] = lastCall();
    expect(url).toBe('https://api.test.example/me/hf-token');
    expect(opts.method).toBe('DELETE');
    expect(opts.body).toBeUndefined();
  });

  test('revealHfToken POSTs /me/hf-token/reveal with no body and no id', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { token: TOKEN, fp: 'c1770a7966b0771e' }));
    await expect(revealHfToken('jwt-1')).resolves.toEqual({ token: TOKEN, fp: 'c1770a7966b0771e' });
    const [url, opts] = lastCall();
    expect(url).toBe('https://api.test.example/me/hf-token/reveal');
    expect(opts.method).toBe('POST');
    expect(opts.body).toBeUndefined();
  });

  test('verifyHfToken POSTs /me/hf-token/verify', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { stored: true }));
    await verifyHfToken('jwt-1');
    const [url, opts] = lastCall();
    expect(url).toBe('https://api.test.example/me/hf-token/verify');
    expect(opts.method).toBe('POST');
  });

  test('a refused token is an Error carrying .status 422 and the German .detail', async () => {
    const detail = 'Dieses Token hat nur Leserechte.';
    global.fetch.mockResolvedValue(jsonResponse(422, { detail }));
    await expect(putHfToken('jwt-1', TOKEN)).rejects.toMatchObject({ status: 422, detail });
  });

  test('a missing route (an older server) is a 404', async () => {
    global.fetch.mockResolvedValue(jsonResponse(404, { detail: 'Not Found' }));
    await expect(getHfToken('jwt-1')).rejects.toMatchObject({ status: 404 });
  });
});
