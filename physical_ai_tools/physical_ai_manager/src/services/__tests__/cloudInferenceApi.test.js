// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The reserve call keeps the API's machine-readable `code` (owner, round 2):
// the controller retries a reserve on `code === 'gpu_not_started'` alone and
// never on the German `detail`. apiRequest drops every body field but
// `detail`, so the reserve reads its own error body; the rest of its contract
// is apiRequest's (status + detail on the Error, the bearer in the header).

import { reserveInferenceSession } from '../cloudInferenceApi';

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

const GPU_DE = 'Die Cloud-GPU konnte gerade nicht gestartet werden. Bitte versuche es gleich noch einmal.';

describe('reserveInferenceSession', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = vi.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('POSTs {training_id} with the bearer and answers the JSON', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { session_id: 's', summary: {} }));
    await expect(reserveInferenceSession('jwt', 7)).resolves.toEqual({ session_id: 's', summary: {} });
    const [url, options] = global.fetch.mock.calls[0];
    expect(url).toBe('https://api.test.example/inference/sessions');
    expect(options.method).toBe('POST');
    expect(options.headers).toMatchObject({ Authorization: 'Bearer jwt', 'Content-Type': 'application/json' });
    expect(JSON.parse(options.body)).toEqual({ training_id: 7 });
  });

  test('a start failure carries status, the German detail AND the code', async () => {
    global.fetch.mockResolvedValue(jsonResponse(503, { detail: GPU_DE, code: 'gpu_not_started' }));
    await expect(reserveInferenceSession('jwt', 7)).rejects.toMatchObject({
      status: 503, detail: GPU_DE, message: GPU_DE, code: 'gpu_not_started',
    });
  });

  test('any other refusal carries no code', async () => {
    global.fetch.mockResolvedValue(jsonResponse(503, { detail: 'Die Cloud ist gerade voll ausgelastet.' }));
    const err = await reserveInferenceSession('jwt', 7).catch((e) => e);
    expect(err).toMatchObject({ status: 503, detail: 'Die Cloud ist gerade voll ausgelastet.' });
    expect(err.code).toBeUndefined();
  });

  test('a code that is not a string is ignored', async () => {
    global.fetch.mockResolvedValue(jsonResponse(503, { detail: GPU_DE, code: 1 }));
    const err = await reserveInferenceSession('jwt', 7).catch((e) => e);
    expect(err.code).toBeUndefined();
  });

  test('a body that is not JSON falls back to the status text, like apiRequest', async () => {
    global.fetch.mockResolvedValue({ ok: false, status: 502, statusText: 'Bad Gateway', json: async () => { throw new Error('html'); } });
    await expect(reserveInferenceSession('jwt', 7)).rejects.toMatchObject({ status: 502, detail: 'Bad Gateway' });
  });

  test('a network failure rejects as fetch does, with no status and no code', async () => {
    global.fetch.mockRejectedValue(new TypeError('Failed to fetch'));
    const err = await reserveInferenceSession('jwt', 7).catch((e) => e);
    expect(err).toBeInstanceOf(TypeError);
    expect(err.status).toBeUndefined();
    expect(err.code).toBeUndefined();
  });
});
