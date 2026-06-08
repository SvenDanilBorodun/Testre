// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the wire shape of the dataset registry calls — chiefly syncDatasets,
// the manual "Datensätze synchronisieren" escape hatch. A future refactor
// that drops the bearer header or changes the method/path would break the
// student's ability to recover a missed auto-register, and only surface in a
// classroom.

import { syncDatasets, registerDataset, listDatasets } from '../datasetsApi';

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

describe('datasetsApi', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = vi.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('syncDatasets POSTs /datasets/sync with the bearer token and returns the summary', async () => {
    const summary = {
      registered: 2,
      already_known: 5,
      total_found: 7,
      hf_username: 'student42',
    };
    global.fetch.mockResolvedValue(jsonResponse(200, summary));

    const result = await syncDatasets('jwt-token');

    expect(result).toEqual(summary);
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.test.example/datasets/sync',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer jwt-token',
        }),
      })
    );
  });

  test('syncDatasets surfaces a 400 (hf not linked) with status on the Error', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(400, { detail: 'HuggingFace-ID nicht verknüpft' })
    );
    await expect(syncDatasets('jwt-token')).rejects.toMatchObject({
      status: 400,
    });
  });

  test('listDatasets GETs /datasets with the bearer token', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, []));
    await listDatasets('jwt-token');
    expect(global.fetch).toHaveBeenLastCalledWith(
      'https://api.test.example/datasets',
      expect.objectContaining({
        method: 'GET',
        headers: expect.objectContaining({ Authorization: 'Bearer jwt-token' }),
      })
    );
  });

  test('registerDataset POSTs the payload to /datasets', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { id: 'd-1' }));
    await registerDataset('jwt-token', { hf_repo_id: 'student42/pick' });
    const opts = global.fetch.mock.calls[0][1];
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ hf_repo_id: 'student42/pick' });
  });
});
