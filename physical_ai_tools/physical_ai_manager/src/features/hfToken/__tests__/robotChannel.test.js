// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The browser's wire to the robot's token slot. Pinned here: the parse is the
// contract (a malformed message is UNKNOWN, never "no token"), the write side
// never connects and never reaches the Jetson proxy, every write goes through
// one serial queue (a clear follows a pending push), the token is only ever the
// service request's field, and the clear used by sign-out cannot hang.

import ROSLIB from 'roslib';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import {
  CALL_TIMEOUT_MS,
  CLEAR_BOUND_MS,
  SERVICE_TYPE,
  SET_SERVICE,
  STATE_KEYS,
  STATE_SCHEMA_VERSION,
  STATE_STALE_MS,
  STATE_TOPIC,
  clearRobotHfToken,
  parseTokenState,
  setRobotToken,
  subscribeTokenState,
} from '../robotChannel';

// Low-entropy fixture on purpose (the repository's secret scan covers history).
const TOKEN = `hf_${'a'.repeat(34)}`;
const FP = 'c1770a7966b0771e';

const mockTopics = [];
const mockCalls = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
    ServiceRequest: function RequestMock(values) { Object.assign(this, values); },
    Service: function ServiceMock(opts) {
      this.opts = opts;
      this.callService = (request, ok, fail) => {
        const call = { service: this, request, ok, fail };
        mockCalls.push(call);
      };
    },
  },
}));

const payload = (o = {}) => JSON.stringify({
  v: 1, seq: 3, accepts: true, present: true, fp: FP, busy: false, ...o,
});

beforeEach(() => {
  mockTopics.length = 0;
  mockCalls.length = 0;
  rosConnectionManager.url = 'ws://localhost/rosbridge';
  vi.spyOn(rosConnectionManager, 'getCurrentConnection').mockReturnValue({ isConnected: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  rosConnectionManager.url = '';
});

// The write queue is module state, so every test must leave it idle. A call
// reaches the wire a few microtasks after setRobotToken(); wait for it.
const onTheWire = (n) => vi.waitFor(() => expect(mockCalls).toHaveLength(n));

describe('the contract constants', () => {
  it('are the names the robot publishes and serves', () => {
    expect(STATE_TOPIC).toBe('/edubotics/hf_token_state');
    expect(SET_SERVICE).toBe('/register_hf_user');
    expect(SERVICE_TYPE).toBe('physical_ai_interfaces/srv/SetHFUser');
    expect(STATE_KEYS).toEqual(['v', 'seq', 'accepts', 'present', 'fp', 'busy']);
    expect(STATE_SCHEMA_VERSION).toBe(1);
  });

  it('consider a state stale only after more than twice the robot\'s 1 s period', () => {
    expect(STATE_STALE_MS).toBeGreaterThan(2000);
    expect(CLEAR_BOUND_MS).toBe(2000);
    expect(CALL_TIMEOUT_MS).toBeGreaterThan(CLEAR_BOUND_MS);
  });
});

describe('parseTokenState', () => {
  it('reads a full payload', () => {
    expect(parseTokenState(payload())).toEqual({
      v: 1, seq: 3, accepts: true, present: true, fp: FP, busy: false,
    });
  });

  it('reads an empty slot and a robot that takes no personal token', () => {
    expect(parseTokenState(payload({ present: false, fp: null }))).toMatchObject({ present: false, fp: null });
    expect(parseTokenState(payload({ accepts: false, present: false, fp: null }))).toMatchObject({ accepts: false });
  });

  it('ignores keys it does not know (additive changes are safe)', () => {
    expect(parseTokenState(payload({ future: 'x' }))).toEqual({
      v: 1, seq: 3, accepts: true, present: true, fp: FP, busy: false,
    });
  });

  it('returns null — UNKNOWN, never "no token" — for anything untrustworthy', () => {
    expect(parseTokenState('')).toBeNull();
    expect(parseTokenState(undefined)).toBeNull();
    expect(parseTokenState(null)).toBeNull();
    expect(parseTokenState(42)).toBeNull();
    expect(parseTokenState({ v: 1 })).toBeNull();
    expect(parseTokenState('{kaputt')).toBeNull();
    expect(parseTokenState('[]')).toBeNull();
    expect(parseTokenState('null')).toBeNull();
    expect(parseTokenState('"text"')).toBeNull();
  });

  it('rejects another schema version', () => {
    expect(parseTokenState(payload({ v: 2 }))).toBeNull();
    expect(parseTokenState(payload({ v: '1' }))).toBeNull();
  });

  it('rejects a payload with a missing key', () => {
    for (const key of STATE_KEYS) {
      const raw = JSON.parse(payload());
      delete raw[key];
      expect(parseTokenState(JSON.stringify(raw))).toBeNull();
    }
  });

  it('rejects a key of the wrong type', () => {
    expect(parseTokenState(payload({ seq: 'x' }))).toBeNull();
    expect(parseTokenState(payload({ accepts: 'yes' }))).toBeNull();
    expect(parseTokenState(payload({ present: 1 }))).toBeNull();
    expect(parseTokenState(payload({ busy: null }))).toBeNull();
  });

  it('rejects a fingerprint that is not 16 lower-case hex characters', () => {
    expect(parseTokenState(payload({ fp: 'ABCDEF0123456789' }))).toBeNull();
    expect(parseTokenState(payload({ fp: 'abc' }))).toBeNull();
    expect(parseTokenState(payload({ fp: 12 }))).toBeNull();
    // a token-shaped string can never be mistaken for a fingerprint
    expect(parseTokenState(payload({ fp: TOKEN }))).toBeNull();
  });
});

describe('subscribeTokenState', () => {
  beforeEach(() => {
    vi.spyOn(rosConnectionManager, 'getConnection').mockResolvedValue({ isConnected: true });
  });

  it('subscribes throttled with a depth of one and forwards each valid state', async () => {
    const seen = [];
    const stop = await subscribeTokenState('ws://localhost/rosbridge', (s) => seen.push(s));
    expect(rosConnectionManager.getConnection).toHaveBeenCalledWith('ws://localhost/rosbridge');
    const topic = mockTopics[0];
    expect(topic.opts).toMatchObject({
      name: '/edubotics/hf_token_state',
      messageType: 'std_msgs/msg/String',
      throttle_rate: 500,
      queue_length: 1,
    });
    topic.cb({ data: payload() });
    expect(seen).toEqual([{ v: 1, seq: 3, accepts: true, present: true, fp: FP, busy: false }]);
    expect(typeof stop).toBe('function');
  });

  it('drops a malformed message instead of reporting it', async () => {
    const seen = [];
    await subscribeTokenState('ws://localhost/rosbridge', (s) => seen.push(s));
    mockTopics[0].cb({ data: '{kaputt' });
    mockTopics[0].cb({});
    mockTopics[0].cb(undefined);
    expect(seen).toEqual([]);
  });

  it('stops forwarding after unsubscribe', async () => {
    const seen = [];
    const stop = await subscribeTokenState('ws://localhost/rosbridge', (s) => seen.push(s));
    stop();
    expect(mockTopics[0].unsubscribed).toBe(true);
    mockTopics[0].cb({ data: payload() });
    expect(seen).toEqual([]);
  });

  it('rejects when the connection cannot be made', async () => {
    rosConnectionManager.getConnection.mockRejectedValueOnce(new Error('down'));
    await expect(subscribeTokenState('ws://localhost/rosbridge', () => {})).rejects.toThrow('down');
  });
});

describe('setRobotToken — what it refuses', () => {
  it('refuses when there is no live connection, without queueing anything', async () => {
    rosConnectionManager.getCurrentConnection.mockReturnValue(null);
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(false);
    expect(result.message).toMatch(/Roboter/);
    expect(mockCalls).toHaveLength(0);
  });

  it('refuses the classroom Jetson proxy: it keeps its own token', async () => {
    rosConnectionManager.url = 'ws://10.0.0.5:9091';
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(false);
    expect(mockCalls).toHaveLength(0);
  });

  it('refuses a bare host that is not the local /rosbridge path', async () => {
    rosConnectionManager.url = 'ws://localhost:9090';
    expect((await setRobotToken(TOKEN)).success).toBe(false);
    rosConnectionManager.url = '';
    expect((await setRobotToken(TOKEN)).success).toBe(false);
    expect(mockCalls).toHaveLength(0);
  });

  it('never connects: it asks only for the CURRENT connection', async () => {
    const getConnection = vi.spyOn(rosConnectionManager, 'getConnection');
    const pending = setRobotToken(TOKEN);
    await onTheWire(1);
    mockCalls[0].ok({ success: true, message: '' });
    await pending;
    expect(getConnection).not.toHaveBeenCalled();
  });
});

describe('setRobotToken — the call', () => {
  it('sends the token as the request\'s only field to /register_hf_user', async () => {
    const pending = setRobotToken(TOKEN);
    await onTheWire(1);
    const { service, request } = mockCalls[0];
    expect(service.opts).toMatchObject({ name: '/register_hf_user', serviceType: 'physical_ai_interfaces/srv/SetHFUser' });
    expect(request).toEqual({ token: TOKEN });
    mockCalls[0].ok({ success: true, message: 'Dein Hugging-Face-Token ist auf dem Roboter aktiv.', user_id_list: [] });
    await expect(pending).resolves.toEqual({
      success: true, message: 'Dein Hugging-Face-Token ist auf dem Roboter aktiv.',
    });
  });

  it('sends an empty token for a clear, and a non-string as an empty token', async () => {
    const a = setRobotToken('');
    await onTheWire(1);
    expect(mockCalls[0].request).toEqual({ token: '' });
    mockCalls[0].ok({ success: true, message: '' });
    await a;
    const b = setRobotToken(undefined);
    await onTheWire(2);
    expect(mockCalls[1].request).toEqual({ token: '' });
    mockCalls[1].ok({ success: true, message: '' });
    await b;
  });

  it('reports the robot\'s refusal with its own German sentence', async () => {
    const pending = setRobotToken(TOKEN);
    await onTheWire(1);
    mockCalls[0].ok({ success: false, message: 'Während einer Aufnahme kann das Token nicht geändert werden.' });
    await expect(pending).resolves.toEqual({
      success: false, message: 'Während einer Aufnahme kann das Token nicht geändert werden.',
    });
  });

  it('supplies a German sentence when the robot sent none', async () => {
    const pending = setRobotToken(TOKEN);
    await onTheWire(1);
    mockCalls[0].ok({ success: false });
    const result = await pending;
    expect(result.success).toBe(false);
    expect(result.message).not.toBe('');
  });

  it('never lets a token-shaped string reach a sentence', async () => {
    const pending = setRobotToken(TOKEN);
    await onTheWire(1);
    mockCalls[0].ok({ success: false, message: `Abgelehnt: ${TOKEN}` });
    const result = await pending;
    expect(result.message).not.toContain(TOKEN);
    expect(result.message).toContain('hf_***');
  });

  it('turns rosbridge\'s own failure into a German sentence and drops its text', async () => {
    const pending = setRobotToken(TOKEN);
    await onTheWire(1);
    mockCalls[0].fail(`Service /register_hf_user not found (${TOKEN})`);
    const result = await pending;
    expect(result.success).toBe(false);
    expect(result.message).not.toMatch(/not found|hf_/);
  });

  it('gives up after the call timeout instead of hanging', async () => {
    vi.useFakeTimers();
    const pending = setRobotToken(TOKEN);
    await vi.advanceTimersByTimeAsync(CALL_TIMEOUT_MS + 1);
    const result = await pending;
    expect(result.success).toBe(false);
    expect(result.message).toMatch(/rechtzeitig/);
    // a late answer after the timeout changes nothing
    mockCalls[0].ok({ success: true, message: 'zu spät' });
  });

  it('never rejects, even when ROSLIB throws', async () => {
    const original = ROSLIB.Service;
    ROSLIB.Service = function Boom() { throw new Error(`boom ${TOKEN}`); };
    try {
      const result = await setRobotToken(TOKEN);
      expect(result.success).toBe(false);
      expect(result.message).not.toContain(TOKEN);
    } finally {
      ROSLIB.Service = original;
    }
  });
});

describe('the serial queue', () => {
  it('runs a clear only after the push before it has answered', async () => {
    const push = setRobotToken(TOKEN);
    const clear = setRobotToken('');
    await onTheWire(1);
    // only the push is on the wire; the clear waits behind it
    await new Promise((resolve) => { setTimeout(resolve, 20); });
    expect(mockCalls).toHaveLength(1);
    expect(mockCalls[0].request).toEqual({ token: TOKEN });
    mockCalls[0].ok({ success: true, message: '' });
    await push;
    await onTheWire(2);
    expect(mockCalls[1].request).toEqual({ token: '' });
    mockCalls[1].ok({ success: true, message: '' });
    await clear;
  });

  it('keeps going after a call that failed', async () => {
    const first = setRobotToken(TOKEN);
    const second = setRobotToken('');
    await onTheWire(1);
    mockCalls[0].fail('x');
    await first;
    await onTheWire(2);
    mockCalls[1].ok({ success: true, message: '' });
    await expect(second).resolves.toMatchObject({ success: true });
  });
});

describe('clearRobotHfToken', () => {
  it('resolves true once the robot confirmed the empty slot', async () => {
    const pending = clearRobotHfToken();
    await onTheWire(1);
    expect(mockCalls[0].request).toEqual({ token: '' });
    mockCalls[0].ok({ success: true, message: 'Das Hugging-Face-Token wurde vom Roboter entfernt.' });
    await expect(pending).resolves.toBe(true);
  });

  it('resolves false when the robot refuses (busy)', async () => {
    const pending = clearRobotHfToken();
    await onTheWire(1);
    mockCalls[0].ok({ success: false, message: 'Während einer Aufnahme …' });
    await expect(pending).resolves.toBe(false);
  });

  it('resolves false — never rejects — with no connection', async () => {
    rosConnectionManager.getCurrentConnection.mockReturnValue(null);
    await expect(clearRobotHfToken()).resolves.toBe(false);
  });

  it('settles within the bound when the robot never answers', async () => {
    vi.useFakeTimers();
    let settled = null;
    clearRobotHfToken().then((v) => { settled = v; });
    await vi.advanceTimersByTimeAsync(CLEAR_BOUND_MS - 1);
    expect(settled).toBeNull();
    await vi.advanceTimersByTimeAsync(2);
    expect(settled).toBe(false);
  });

  it('honours a shorter bound', async () => {
    vi.useFakeTimers();
    let settled = null;
    clearRobotHfToken({ timeoutMs: 300 }).then((v) => { settled = v; });
    await vi.advanceTimersByTimeAsync(301);
    expect(settled).toBe(false);
  });

  it('counts the wait behind a pending push inside the bound', async () => {
    vi.useFakeTimers();
    setRobotToken(TOKEN); // never answered
    let settled = null;
    clearRobotHfToken().then((v) => { settled = v; });
    await vi.advanceTimersByTimeAsync(CLEAR_BOUND_MS + 1);
    expect(settled).toBe(false);
    // Let the stuck push, and the clear queued behind it, time out too: the
    // queue is module state and must be idle for whatever runs next.
    await vi.advanceTimersByTimeAsync(2 * CALL_TIMEOUT_MS + 10);
  });
});
