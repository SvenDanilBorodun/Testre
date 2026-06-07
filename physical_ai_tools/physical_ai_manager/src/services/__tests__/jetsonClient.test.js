// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Verifies the wire shape of the jetsonClient functions. The sendBeacon
// fix is the v2.3.0 regression-critical path — without these tests, a
// future refactor could quietly revert to query-param auth (which the
// Cloud API doesn't accept) and the lock leak would only surface in a
// classroom.

// The cloudConfig stub below is hoisted by Vitest ABOVE this import (vi.mock
// is statically hoisted, like babel-jest's jest.mock), so jetsonClient's
// top-level imports resolve the mocked module cleanly without
// REACT_APP_CLOUD_API_URL being set in the test env — source order of import
// vs vi.mock() is irrelevant.
import {
  claimJetson,
  forceReleaseJetson,
  getClassroomJetson,
  heartbeatJetson,
  pairJetson,
  pairJetsonIntent,
  regeneratePairingCode,
  releaseJetson,
  releaseJetsonBeacon,
  unpairJetson,
} from '../jetsonClient';

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

describe('jetsonClient request shape', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = global.fetch;
    global.fetch = jest.fn();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test('getClassroomJetson returns null on 404 (no Jetson paired)', async () => {
    global.fetch.mockResolvedValue(jsonResponse(404, { detail: 'Kein Klassen-Jetson in diesem Raum' }));
    const result = await getClassroomJetson('jwt-token', 'classroom-1');
    expect(result).toBeNull();
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.test.example/classrooms/classroom-1/jetson',
      expect.objectContaining({
        method: 'GET',
        headers: expect.objectContaining({
          Authorization: 'Bearer jwt-token',
        }),
      })
    );
  });

  test('getClassroomJetson returns parsed body on 200', async () => {
    const info = { jetson_id: 'j-1', mdns_name: 'm', lan_ip: '1.2.3.4', online: true };
    global.fetch.mockResolvedValue(jsonResponse(200, info));
    const result = await getClassroomJetson('jwt-token', 'classroom-1');
    expect(result).toEqual(info);
  });

  test('claimJetson throws with status 409 on Jetson belegt', async () => {
    global.fetch.mockResolvedValue(jsonResponse(409, { detail: 'Jetson ist bereits belegt' }));
    await expect(claimJetson('jwt-token', 'j-1')).rejects.toMatchObject({
      status: 409,
      message: expect.stringContaining('belegt'),
    });
  });

  test('heartbeatJetson throws with status 410 on Lock verloren', async () => {
    global.fetch.mockResolvedValue(jsonResponse(410, { detail: 'Lock verloren — bitte erneut verbinden' }));
    await expect(heartbeatJetson('jwt-token', 'j-1')).rejects.toMatchObject({
      status: 410,
      message: expect.stringContaining('Lock'),
    });
  });

  test('releaseJetson hits POST /jetson/{id}/release with Bearer header', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { ok: true }));
    await releaseJetson('jwt-token', 'j-1');
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.test.example/jetson/j-1/release',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer jwt-token',
        }),
      })
    );
  });

  test('pairJetson sends pairing_code + intent_token in body and optionally mdns_name', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { jetson_id: 'j-1', mdns_name: 'm' }));
    await pairJetson('jwt-token', 'classroom-1', '123456', 'intent-uuid-1');
    const opts = global.fetch.mock.calls[0][1];
    expect(JSON.parse(opts.body)).toEqual({
      pairing_code: '123456',
      intent_token: 'intent-uuid-1',
    });
    expect(global.fetch).toHaveBeenLastCalledWith(
      'https://api.test.example/teacher/classrooms/classroom-1/jetson/pair',
      expect.objectContaining({ method: 'POST' })
    );

    global.fetch.mockClear();
    global.fetch.mockResolvedValue(jsonResponse(200, { jetson_id: 'j-1', mdns_name: 'custom' }));
    await pairJetson('jwt-token', 'classroom-1', '654321', 'intent-uuid-2', 'custom.local');
    const opts2 = global.fetch.mock.calls[0][1];
    expect(JSON.parse(opts2.body)).toEqual({
      pairing_code: '654321',
      intent_token: 'intent-uuid-2',
      mdns_name: 'custom.local',
    });
  });

  test('pairJetson refuses to call the API when intentToken is missing', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { jetson_id: 'j-1' }));
    await expect(
      pairJetson('jwt-token', 'classroom-1', '123456')
    ).rejects.toMatchObject({
      message: expect.stringContaining('Pairing-Intent fehlt'),
    });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test('pairJetson maps 403 to the German "intent abgelaufen" message and tags step="pair"', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(403, { detail: 'intent_token mismatch' })
    );
    await expect(
      pairJetson('jwt-token', 'classroom-1', '123456', 'intent-uuid-bad')
    ).rejects.toMatchObject({
      status: 403,
      step: 'pair',
      message:
        'Pairing-Intent abgelaufen oder ungültig. Bitte erneut versuchen.',
    });
  });

  test('pairJetsonIntent hits the pair-intent endpoint and returns intent_token', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(200, { intent_token: 'uuid-intent-abc' })
    );
    const result = await pairJetsonIntent('jwt-token', 'classroom-1', '123456');
    expect(result).toEqual({ intent_token: 'uuid-intent-abc' });
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.test.example/teacher/classrooms/classroom-1/jetson/pair-intent',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer jwt-token',
        }),
      })
    );
    const opts = global.fetch.mock.calls[0][1];
    expect(JSON.parse(opts.body)).toEqual({ pairing_code: '123456' });
  });

  test('pairJetsonIntent maps 409 to the German "bereits beansprucht" message and tags step="intent"', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(409, { detail: 'pairing code already claimed' })
    );
    await expect(
      pairJetsonIntent('jwt-token', 'classroom-1', '123456')
    ).rejects.toMatchObject({
      status: 409,
      step: 'intent',
      message:
        'Dieser Pairing-Code wurde bereits von einem anderen Lehrer beansprucht. Bitte den Schüler-Agenten neu starten.',
    });
  });

  test('pairJetsonIntent passes 404 through (code invalid/expired)', async () => {
    global.fetch.mockResolvedValue(
      jsonResponse(404, { detail: 'pairing_code unknown' })
    );
    await expect(
      pairJetsonIntent('jwt-token', 'classroom-1', '000000')
    ).rejects.toMatchObject({
      status: 404,
    });
  });

  test('regeneratePairingCode hits the teacher endpoint and returns the new code payload', async () => {
    const expected = {
      jetson_id: 'j-1',
      pairing_code: '999000',
      pairing_code_expires_at: '2026-05-17T12:00:00+00:00',
    };
    global.fetch.mockResolvedValue(jsonResponse(200, expected));
    const result = await regeneratePairingCode('jwt-token', 'classroom-1');
    expect(result).toEqual(expected);
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.test.example/teacher/classrooms/classroom-1/jetson/regenerate-code',
      expect.objectContaining({ method: 'POST' })
    );
  });

  test('forceReleaseJetson and unpairJetson hit their respective teacher endpoints', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { ok: true }));
    await forceReleaseJetson('jwt-token', 'classroom-1');
    expect(global.fetch).toHaveBeenLastCalledWith(
      'https://api.test.example/teacher/classrooms/classroom-1/jetson/force-release',
      expect.objectContaining({ method: 'POST' })
    );

    await unpairJetson('jwt-token', 'classroom-1');
    expect(global.fetch).toHaveBeenLastCalledWith(
      'https://api.test.example/teacher/classrooms/classroom-1/jetson/unpair',
      expect.objectContaining({ method: 'POST' })
    );
  });
});

describe('releaseJetsonBeacon (v2.3.0 sendBeacon fix)', () => {
  // jsdom (Node >=22 vintage) exposes `global.navigator` as a getter-only
  // accessor: the old `global.navigator = {...}` assignment SILENTLY
  // no-op'd, sendBeacon stayed undefined, and the two assertion tests
  // below failed (while the "absent" test passed vacuously). Stub the
  // method on the existing navigator object instead — defineProperty with
  // configurable:true so afterEach can remove it cleanly.
  const setSendBeacon = (impl) =>
    Object.defineProperty(global.navigator, 'sendBeacon', {
      value: impl,
      writable: true,
      configurable: true,
    });
  beforeEach(() => {
    setSendBeacon(jest.fn(() => true));
  });
  afterEach(() => {
    delete global.navigator.sendBeacon;
  });

  test('hits the dedicated release-beacon endpoint (NOT /release)', () => {
    releaseJetsonBeacon('jwt-token', 'j-1');
    expect(global.navigator.sendBeacon).toHaveBeenCalledTimes(1);
    const url = global.navigator.sendBeacon.mock.calls[0][0];
    expect(url).toBe('https://api.test.example/jetson/j-1/release-beacon');
  });

  test('sends body as application/json Blob with access_token field', async () => {
    releaseJetsonBeacon('jwt-token-xyz', 'j-1');
    const body = global.navigator.sendBeacon.mock.calls[0][1];
    expect(body).toBeInstanceOf(Blob);
    expect(body.type).toBe('application/json');
    // Read via FileReader — jsdom's Blob lacks .text() on the jest
    // (jsdom ~20) environment CRA ships; FileReader works everywhere.
    const text = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsText(body);
    });
    expect(JSON.parse(text)).toEqual({ access_token: 'jwt-token-xyz' });
  });

  test('no-op when navigator.sendBeacon is absent (Node SSR, old browsers)', () => {
    delete global.navigator.sendBeacon;
    expect(() => releaseJetsonBeacon('jwt-token', 'j-1')).not.toThrow();
  });

  test('swallows sendBeacon throws (quota / payload-size)', () => {
    setSendBeacon(
      jest.fn(() => {
        throw new Error('quota');
      })
    );
    expect(() => releaseJetsonBeacon('jwt-token', 'j-1')).not.toThrow();
  });
});
