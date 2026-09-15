// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the renameTrajectory request shape and its error contract: the cloud
// route answers a name clash with a German 409 detail, which the Sammlung
// drawer shows verbatim, while an English detail falls back to the module's
// generic German 409 message.

import { renameTrajectory } from '../workflowApi';

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

describe('workflowApi.renameTrajectory', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = vi.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('PATCHes the trajectory with the new name and the bearer token', async () => {
    const row = { id: 'tr-1', workflow_id: 'wf-1', name: 'Neu', samples: null };
    global.fetch.mockResolvedValue(jsonResponse(200, row));

    const result = await renameTrajectory('jwt', 'wf-1', 'tr-1', 'Neu');

    expect(result).toEqual(row);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, options] = global.fetch.mock.calls[0];
    expect(url).toBe('https://api.test.example/workflows/wf-1/trajectories/tr-1');
    expect(options.method).toBe('PATCH');
    expect(JSON.parse(options.body)).toEqual({ name: 'Neu' });
    expect(options.headers.Authorization).toBe('Bearer jwt');
  });

  test('a 409 with a German detail rejects with that exact message', async () => {
    const detail =
      'Eine Bewegung mit dem Namen „Tanz" gibt es schon — bitte einen anderen Namen wählen.';
    global.fetch.mockResolvedValue(jsonResponse(409, { detail }));

    await expect(renameTrajectory('jwt', 'wf-1', 'tr-1', 'Tanz')).rejects.toMatchObject({
      message: detail,
      status: 409,
    });
  });

  test('a 409 with an English detail rejects with the generic German message', async () => {
    global.fetch.mockResolvedValue(jsonResponse(409, { detail: 'Conflict' }));

    await expect(renameTrajectory('jwt', 'wf-1', 'tr-1', 'Tanz')).rejects.toMatchObject({
      message: 'Konflikt — bitte Seite neu laden und nochmals versuchen.',
      status: 409,
    });
  });
});
