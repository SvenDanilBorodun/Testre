// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The sidecar's HTTP client (spec §B3, §B8, §B10): every failure becomes one
// code; a 503 waits out Retry-After; 502/504/no answer is „sidecar down";
// a /daten/command call is normalised and the old image is recognised by
// rosbridge's „does not exist" (P10).

import { describe, expect, it, vi } from 'vitest';
import {
  DatenHttpError, classifyMedia, clipUrl, dsUrl, getJson, isTokenError, libUrl, thumbUrl,
} from '../api/datenHttp';
import { runDatenCommand } from '../hooks/useDatenCommand';

const json = (status, body, headers = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (k) => headers[k] ?? null },
  json: () => Promise.resolve(body),
});

describe('URLs', () => {
  it('builds the routes under /daten-api/v1 with the token in the path', () => {
    expect(libUrl('v1.a.b', 'library', { ns: ['lena', 'max'], hub: 1, ids: ['lena/omx_f_a'] }))
      .toBe('/daten-api/v1/lib/v1.a.b/library?ns=lena,max&hub=1&ids=lena%2Fomx_f_a');
    expect(libUrl('T', 'library', { ns: ['lena'], hub: 0, ids: null })).toBe('/daten-api/v1/lib/T/library?ns=lena&hub=0');
    expect(dsUrl('T', 'summary')).toBe('/daten-api/v1/ds/T/summary');
    expect(clipUrl('T', 3, 1)).toBe('/daten-api/v1/ds/T/episode/3/video/1.mp4');
    expect(thumbUrl('T')).toBe('/daten-api/v1/ds/T/thumb.jpg');
  });
});

describe('getJson', () => {
  it('resolves the body', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(json(200, { v: 1 })));
    await expect(getJson('/x', { fetchImpl })).resolves.toEqual({ v: 1 });
  });

  it('maps the sidecar\'s JSON error to its code', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(json(403, { error: 'token_expired' })));
    const err = await getJson('/x', { fetchImpl }).catch((e) => e);
    expect(err).toBeInstanceOf(DatenHttpError);
    expect(err.code).toBe('token_expired');
    expect(isTokenError(err.code)).toBe(true);
    expect(isTokenError('in_session')).toBe(false);
  });

  it('502, 504 and no answer at all are „sidecar down"', async () => {
    for (const status of [502, 504]) {
      const fetchImpl = vi.fn(() => Promise.resolve(json(status, null)));
      // eslint-disable-next-line no-await-in-loop
      expect((await getJson('/x', { fetchImpl }).catch((e) => e)).code).toBe('sidecar_down');
    }
    const fetchImpl = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
    expect((await getJson('/x', { fetchImpl }).catch((e) => e)).code).toBe('sidecar_down');
  });

  it('a 503 waits out Retry-After and tries again (twice), then gives up as overloaded', async () => {
    const sleep = vi.fn(() => Promise.resolve());
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(json(503, { error: 'overloaded' }, { 'Retry-After': '2' }))
      .mockResolvedValueOnce(json(200, { v: 1 }));
    await expect(getJson('/x', { fetchImpl, sleep })).resolves.toEqual({ v: 1 });
    expect(sleep).toHaveBeenCalledWith(2000);

    const always = vi.fn(() => Promise.resolve(json(503, { error: 'overloaded' }, { 'Retry-After': '2' })));
    const err = await getJson('/x', { fetchImpl: always, sleep }).catch((e) => e);
    expect(err.code).toBe('overloaded');
    expect(always).toHaveBeenCalledTimes(3);
  });

  it('an unknown status without a code is „http"', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(json(418, {})));
    expect((await getJson('/x', { fetchImpl }).catch((e) => e)).code).toBe('http');
  });
});

describe('classifyMedia', () => {
  it('asks for one byte and reports the sidecar\'s code', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(json(409, { error: 'unplayable' })));
    await expect(classifyMedia('/clip', { fetchImpl })).resolves.toEqual({ status: 409, code: 'unplayable' });
    expect(fetchImpl.mock.calls[0][1].headers).toEqual({ Range: 'bytes=0-0' });
    const ok = vi.fn(() => Promise.resolve(json(206, null)));
    await expect(classifyMedia('/clip', { fetchImpl: ok })).resolves.toEqual({ status: 206, code: '' });
    const down = vi.fn(() => Promise.reject(new TypeError('x')));
    await expect(classifyMedia('/clip', { fetchImpl: down })).resolves.toEqual({ status: 0, code: 'sidecar_down' });
  });
});

describe('runDatenCommand', () => {
  it('normalises a success and parses result_json', async () => {
    const call = vi.fn(() => Promise.resolve({ success: true, code: '', message: '', result_json: '{"job_id":"j1"}' }));
    await expect(runDatenCommand(call, 'edit', { op: 'delete' })).resolves.toEqual({
      ok: true, code: '', message: '', result: { job_id: 'j1' }, oldImage: false, unreachable: false,
    });
    expect(call).toHaveBeenCalledWith('edit', { op: 'delete' });
  });

  it('a refusal keeps its code and German message', async () => {
    const call = () => Promise.resolve({ success: false, code: 'stale', message: 'Der Datensatz hat sich inzwischen geändert.', result_json: '{}' });
    await expect(runDatenCommand(call, 'edit', {})).resolves.toMatchObject({
      ok: false, code: 'stale', message: 'Der Datensatz hat sich inzwischen geändert.',
    });
  });

  it('an image without the service is the OLD IMAGE (rosbridge: „does not exist")', async () => {
    const call = () => Promise.reject(new Error('Service call failed for /daten/command: Service /daten/command does not exist'));
    await expect(runDatenCommand(call, 'link', {})).resolves.toMatchObject({ ok: false, code: 'old_image', oldImage: true, unreachable: false });
  });

  it('a timeout or a dead link is unreachable, never the old image', async () => {
    const call = () => Promise.reject(new Error('Service call timeout for /daten/command'));
    await expect(runDatenCommand(call, 'link', {})).resolves.toMatchObject({ ok: false, code: 'unreachable', oldImage: false, unreachable: true });
  });

  it('a garbled result_json is an empty object', async () => {
    const call = () => Promise.resolve({ success: true, result_json: 'nope' });
    expect((await runDatenCommand(call, 'state', {})).result).toEqual({});
  });
});
