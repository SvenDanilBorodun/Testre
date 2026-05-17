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

// Stub the cloudConfig module BEFORE importing jetsonClient so its
// top-level imports resolve cleanly without REACT_APP_CLOUD_API_URL
// being set in the test env.
jest.mock('../cloudConfig', () => ({
  CLOUD_API_URL: 'https://api.test.example',
  assertCloudApiConfigured: jest.fn(),
}));

import {
  claimJetson,
  forceReleaseJetson,
  getClassroomJetson,
  heartbeatJetson,
  pairJetson,
  regeneratePairingCode,
  releaseJetson,
  releaseJetsonBeacon,
  unpairJetson,
} from '../jetsonClient';

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

  test('pairJetson sends pairing_code in body and optionally mdns_name', async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { jetson_id: 'j-1', mdns_name: 'm' }));
    await pairJetson('jwt-token', 'classroom-1', '123456');
    const opts = global.fetch.mock.calls[0][1];
    expect(JSON.parse(opts.body)).toEqual({ pairing_code: '123456' });

    global.fetch.mockClear();
    global.fetch.mockResolvedValue(jsonResponse(200, { jetson_id: 'j-1', mdns_name: 'custom' }));
    await pairJetson('jwt-token', 'classroom-1', '654321', 'custom.local');
    const opts2 = global.fetch.mock.calls[0][1];
    expect(JSON.parse(opts2.body)).toEqual({ pairing_code: '654321', mdns_name: 'custom.local' });
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
  let originalNavigator;
  beforeEach(() => {
    originalNavigator = global.navigator;
    global.navigator = {
      sendBeacon: jest.fn(() => true),
    };
  });
  afterEach(() => {
    global.navigator = originalNavigator;
  });

  test('hits the dedicated release-beacon endpoint (NOT /release)', () => {
    releaseJetsonBeacon('jwt-token', 'j-1');
    expect(global.navigator.sendBeacon).toHaveBeenCalledTimes(1);
    const url = global.navigator.sendBeacon.mock.calls[0][0];
    expect(url).toBe('https://api.test.example/jetson/j-1/release-beacon');
  });

  test('sends body as application/json Blob with access_token field', () => {
    releaseJetsonBeacon('jwt-token-xyz', 'j-1');
    const body = global.navigator.sendBeacon.mock.calls[0][1];
    expect(body).toBeInstanceOf(Blob);
    expect(body.type).toBe('application/json');
    // Blob → text → JSON parse to confirm the payload shape.
    return body.text().then((text) => {
      expect(JSON.parse(text)).toEqual({ access_token: 'jwt-token-xyz' });
    });
  });

  test('no-op when navigator.sendBeacon is absent (Node SSR, old browsers)', () => {
    global.navigator = {};
    expect(() => releaseJetsonBeacon('jwt-token', 'j-1')).not.toThrow();
  });

  test('swallows sendBeacon throws (quota / payload-size)', () => {
    global.navigator.sendBeacon = jest.fn(() => {
      throw new Error('quota');
    });
    expect(() => releaseJetsonBeacon('jwt-token', 'j-1')).not.toThrow();
  });
});
