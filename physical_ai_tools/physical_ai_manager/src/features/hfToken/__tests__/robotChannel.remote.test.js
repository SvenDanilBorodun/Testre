// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// Verification round C (2026-11-15).
//
// VC-01: VB-03's protection hung on the Pi marker, and the marker fails OPEN
// (`fetchPiMarker` answers „not a Pi" on a network error or after 4 s). A Pi
// page in that state still reached the Pi's rosbridge, and the token went out
// the Windows way: encrypted for the TOPIC's key (which any session holder can
// forge) or, after a forged state without a key, in the clear. The robot is now
// remote whenever the page's own host is not exactly a loopback host, whatever
// the marker says (utils/robotLocality.js).
//
// VC-03: the agent's key read had no timeout and ran inside the write queue, so
// an agent answer that never came held the sign-out's clear for ever. A read is
// now bounded, and a clear never waits for a push that has not reached the wire
// yet: that push is dropped instead.

import { createDecipheriv, createECDH, createHash, hkdfSync, webcrypto } from 'crypto';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import { setPageHostnameForTests } from '../../../utils/robotLocality';
import {
  AGENT_KEY_TIMEOUT_MS, clearRobotHfToken, getRobotKey, readAgentRobotKey,
  setAgentKeySourceForTests, setRobotKeyForTests, setRobotToken, subscribeTokenState,
} from '../robotChannel';
import { CONTEXT, WIRE_PREFIX } from '../hfTokenCrypto';
import { hfCopy } from '../hfTokenCopy';
import fixture from './fixtures/hf-token-crypto-cases.json';

if (!globalThis.crypto || !globalThis.crypto.subtle) globalThis.crypto = webcrypto;

const TOKEN = `hf_${'0123456789'.repeat(3)}wxyz`;
const KEY = { kid: fixture.robot.kid, pub: fixture.robot.pub };
const ROBOT_D = Buffer.from(fixture.robot.d, 'base64url');

const mockPi = vi.hoisted(() => ({ on: false }));
vi.mock('../../../utils/piMode', () => ({ __esModule: true, getPiModeSync: () => mockPi.on }));
vi.mock('../../piAuth/piAuth', () => ({ __esModule: true, noteUnauthorized: () => {} }));

const mockTopics = [];
const mockCalls = [];
const mockAnswer = vi.hoisted(() => ({ hold: false, pending: [] }));
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => {};
      mockTopics.push(this);
    },
    ServiceRequest: function RequestMock(values) { Object.assign(this, values); },
    Service: function ServiceMock(opts) {
      this.opts = opts;
      this.callService = (request, ok) => {
        mockCalls.push({ name: opts.name, request });
        if (mockAnswer.hold) mockAnswer.pending.push(ok);
        else ok({ success: true, message: '' });
      };
    },
  },
}));

function newKey() {
  const ecdh = createECDH('prime256v1');
  ecdh.generateKeys();
  const point = ecdh.getPublicKey(null, 'uncompressed');
  return {
    d: ecdh.getPrivateKey(),
    pub: point.toString('base64url'),
    kid: createHash('sha256').update(point).digest('hex').slice(0, 16),
  };
}

function decryptWith(wire, privateKey) {
  const [kid, point, iv, ct] = wire.slice(WIRE_PREFIX.length).split('.');
  const ecdh = createECDH('prime256v1');
  ecdh.setPrivateKey(privateKey);
  const shared = ecdh.computeSecret(Buffer.from(point, 'base64url'));
  const key = Buffer.from(hkdfSync('sha256', shared, CONTEXT, `aes-256-gcm|${kid}`, 32));
  const raw = Buffer.from(ct, 'base64url');
  const d = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
  d.setAAD(Buffer.from(`${CONTEXT}|${kid}`, 'utf8'));
  d.setAuthTag(raw.subarray(raw.length - 16));
  return Buffer.concat([d.update(raw.subarray(0, raw.length - 16)), d.final()]).toString('utf8');
}

const agentReads = [];
function agentServes(answer) {
  return async (url, init) => {
    agentReads.push({ url, init });
    if (answer instanceof Error) throw answer;
    if (typeof answer === 'number') return { ok: false, status: answer, json: async () => ({ ok: false }) };
    return { ok: true, status: 200, json: async () => ({ ok: true, key: answer ? { enc: 'enc1', ...answer } : null }) };
  };
}

const STATE = { v: 1, seq: 9, accepts: true, present: false, fp: null, busy: false };
const PI_URL = 'ws://192.168.1.50/rosbridge';

beforeEach(() => {
  mockTopics.length = 0;
  mockCalls.length = 0;
  agentReads.length = 0;
  mockAnswer.hold = false;
  mockAnswer.pending.length = 0;
  mockPi.on = false;
  setRobotKeyForTests(null);
  setAgentKeySourceForTests({ fetch: agentServes(null), recheckMs: 0 });
  rosConnectionManager.url = PI_URL;
  vi.spyOn(rosConnectionManager, 'getCurrentConnection').mockReturnValue({ isConnected: true });
  vi.spyOn(rosConnectionManager, 'getConnection').mockResolvedValue({});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  setAgentKeySourceForTests();
  setRobotKeyForTests(null);
  setPageHostnameForTests(null);
  rosConnectionManager.url = '';
});

describe('VC-01: a Pi page whose /pi-mode.json did not resolve (the marker failed to non-Pi)', () => {
  beforeEach(() => {
    mockPi.on = false; // fetchPiMarker answered false: a timeout or a network error
    setPageHostnameForTests('192.168.1.50'); // the Pi's own page, opened at its LAN address
  });

  it('a forged topic naming a classmate\'s key gets nothing', async () => {
    const attacker = newKey();
    await subscribeTokenState(PI_URL, () => {});
    mockTopics[0].cb({ data: JSON.stringify({ ...STATE, enc: 'enc1', kid: attacker.kid, pub: attacker.pub }) });
    expect(getRobotKey()).toEqual({ kid: attacker.kid, pub: attacker.pub });
    setAgentKeySourceForTests({ fetch: agentServes(KEY), recheckMs: 0 });
    const result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.keyMismatch') });
    expect(mockCalls).toHaveLength(0);
  });

  it('a forged state without a key never gets the plain token', async () => {
    await subscribeTokenState(PI_URL, () => {});
    mockTopics[0].cb({ data: JSON.stringify(STATE) });
    expect(getRobotKey()).toBeNull();
    for (const agent of [null, KEY, 404, new Error('offline')]) {
      mockCalls.length = 0;
      setAgentKeySourceForTests({ fetch: agentServes(agent), recheckMs: 0 });
      // eslint-disable-next-line no-await-in-loop
      const result = await setRobotToken(TOKEN);
      expect(result.success).toBe(false);
      expect(mockCalls).toHaveLength(0);
    }
  });

  it('encrypts for the key the AGENT serves, which only the node opens', async () => {
    setAgentKeySourceForTests({ fetch: agentServes(KEY), recheckMs: 0 });
    setRobotKeyForTests(KEY);
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    expect(agentReads.length).toBeGreaterThan(0);
    expect(decryptWith(mockCalls[0].request.token, ROBOT_D)).toBe(TOKEN);
  });

  it('an agent that cannot be asked: nothing is sent, and the page says so in German', async () => {
    setRobotKeyForTests(KEY);
    setAgentKeySourceForTests({ fetch: agentServes(new Error('offline')), recheckMs: 0 });
    const result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.agentUnreachable') });
    expect(hfCopy('robot.agentUnreachable')).toMatch(/nicht übertragen/);
    expect(mockCalls).toHaveLength(0);
  });

  for (const host of ['raspi.local', 'pi.schule.intern', '10.0.0.7', '[fe80::1]', 'localhost.', 'LOCALHOST.example', '']) {
    it(`a page at „${host}" is remote`, async () => {
      setPageHostnameForTests(host);
      setRobotKeyForTests(KEY); // a topic key alone is never enough
      const result = await setRobotToken(TOKEN);
      expect(result.success).toBe(false);
      expect(mockCalls).toHaveLength(0);
      expect(agentReads.length).toBeGreaterThan(0); // the agent was asked
    });
  }
});

describe('VC-01: the Windows GUI page (exactly a loopback host) keeps today\'s behaviour', () => {
  for (const host of ['localhost', '127.0.0.1', '[::1]']) {
    it(`at „${host}" the topic's key is used and the agent is never asked`, async () => {
      setPageHostnameForTests(host);
      rosConnectionManager.url = `ws://${host}/rosbridge`;
      setRobotKeyForTests(KEY);
      const result = await setRobotToken(TOKEN);
      expect(result.success).toBe(true);
      expect(decryptWith(mockCalls[0].request.token, ROBOT_D)).toBe(TOKEN);
      expect(agentReads).toHaveLength(0);
    });
  }

  it('a kiosk browser on the Pi itself (marker says Pi, page at localhost) still asks the agent', async () => {
    mockPi.on = true;
    setPageHostnameForTests('localhost');
    setRobotKeyForTests(KEY);
    setAgentKeySourceForTests({ fetch: agentServes(KEY), recheckMs: 0 });
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    expect(agentReads.length).toBeGreaterThan(0);
  });
});

describe('VC-03: nothing in the token queue can hang the sign-out\'s clear', () => {
  beforeEach(() => {
    setPageHostnameForTests('192.168.1.50');
  });

  it('a GET /robot-key that never answers does not hold the clear', async () => {
    setRobotKeyForTests(KEY);
    // Never answers and ignores the abort signal: only our own timer ends it.
    setAgentKeySourceForTests({ fetch: () => new Promise(() => {}), recheckMs: 0, timeoutMs: 60 });
    const push = setRobotToken(TOKEN); // e.g. the focus re-sync
    const cleared = await clearRobotHfToken({ timeoutMs: 200 }); // „Abmelden"
    expect(cleared).toBe(true);
    expect(mockCalls.map((c) => c.request.token)).toEqual(['']);
    // The push ends by itself (bounded reads) and sends NOTHING after the clear.
    const result = await push;
    expect(result.success).toBe(false);
    expect(mockCalls.map((c) => c.request.token)).toEqual(['']);
  });

  it('every agent read is bounded, even by a fetch that ignores the abort', async () => {
    vi.useFakeTimers();
    setAgentKeySourceForTests({ fetch: () => new Promise(() => {}), recheckMs: 0 });
    let settled = null;
    readAgentRobotKey().then((v) => { settled = v; });
    await vi.advanceTimersByTimeAsync(AGENT_KEY_TIMEOUT_MS - 1);
    expect(settled).toBeNull();
    await vi.advanceTimersByTimeAsync(2);
    expect(settled).toEqual({ status: 'unreachable' });
  });

  it('the read passes an abort signal, so a fetch that honours it stops too', async () => {
    let signal = null;
    setAgentKeySourceForTests({
      fetch: (url, init) => new Promise((resolve, reject) => {
        signal = init.signal;
        init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      }),
      recheckMs: 0,
      timeoutMs: 20,
    });
    expect(await readAgentRobotKey()).toEqual({ status: 'unreachable' });
    expect(signal && signal.aborted).toBe(true);
  });

  it('a push whose service call is already on the wire is still followed, never overtaken, by the clear', async () => {
    setAgentKeySourceForTests({ fetch: agentServes(KEY), recheckMs: 0 });
    setRobotKeyForTests(KEY);
    mockAnswer.hold = true;
    const push = setRobotToken(TOKEN);
    await vi.waitFor(() => expect(mockCalls).toHaveLength(1));
    const clear = setRobotToken('');
    await new Promise((resolve) => { setTimeout(resolve, 20); });
    expect(mockCalls).toHaveLength(1); // the clear waits behind the push on the wire
    mockAnswer.pending.shift()({ success: true, message: '' });
    await push;
    await vi.waitFor(() => expect(mockCalls).toHaveLength(2));
    expect(mockCalls[1].request.token).toBe('');
    mockAnswer.pending.shift()({ success: true, message: '' });
    await expect(clear).resolves.toMatchObject({ success: true });
  });

  it('a push asked for after the clear goes out normally', async () => {
    setAgentKeySourceForTests({ fetch: agentServes(KEY), recheckMs: 0 });
    setRobotKeyForTests(KEY);
    await setRobotToken('');
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    expect(mockCalls.map((c) => c.request.token.slice(0, 5))).toEqual(['', WIRE_PREFIX]);
  });
});
