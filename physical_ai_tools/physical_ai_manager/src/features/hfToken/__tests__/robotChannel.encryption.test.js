// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The token travels ENCRYPTED for the robot's key (2026-11-13; the audited
// libraries since 2026-11-14). Pinned: a robot
// that advertises a key on /edubotics/hf_token_state gets the `enc1:` value
// and never the token; an Orange Pi page sends NOTHING to a robot without a
// key; a Windows page (loopback) still sends the plain token to such a robot;
// clearing stays the empty string.
//
// VB-03 (2026-11-15): on a Pi the key the token is encrypted for comes from
// the pi-agent (`GET /api/system/robot-key`), never from the topic alone: a
// forged topic gets nothing, and a forged /register_hf_user receives only a
// ciphertext for the agent's (the node's) key.

import ROSLIB from 'roslib';
import {
  createDecipheriv, createECDH, createHash, hkdfSync, webcrypto,
} from 'crypto';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import {
  AGENT_KEY_URL, getRobotKey, KEY_CHECKS, readAgentRobotKey, setAgentKeySourceForTests,
  setRobotKeyForTests, setRobotToken, subscribeTokenState,
} from '../robotChannel';
import { CONTEXT, WIRE_PREFIX } from '../hfTokenCrypto';
import { hfCopy } from '../hfTokenCopy';
import fixture from './fixtures/hf-token-crypto-cases.json';

if (!globalThis.crypto || !globalThis.crypto.subtle) globalThis.crypto = webcrypto;

const TOKEN = `hf_${'0123456789'.repeat(3)}abcd`;
const KEY = { kid: fixture.robot.kid, pub: fixture.robot.pub };

const mockPi = vi.hoisted(() => ({ on: false }));
vi.mock('../../../utils/piMode', () => ({
  __esModule: true,
  getPiModeSync: () => mockPi.on,
}));

const mockAuth = vi.hoisted(() => ({ unauthorized: 0 }));
vi.mock('../../piAuth/piAuth', () => ({
  __esModule: true,
  noteUnauthorized: () => { mockAuth.unauthorized += 1; },
}));

// The pi-agent's GET /robot-key, as agent.py answers it.
const agentReads = [];
function agentServes(answer) {
  return async (url, init) => {
    agentReads.push({ url, init });
    const value = typeof answer === 'function' ? answer(agentReads.length) : answer;
    if (value instanceof Error) throw value;
    if (typeof value === 'number') return { ok: false, status: value, json: async () => ({ ok: false }) };
    return { ok: true, status: 200, json: async () => ({ ok: true, key: value }) };
  };
}
const served = (key) => (key ? { enc: 'enc1', ...key } : null);

/** A fresh P-256 key, as an attacker would make one (node:crypto). */
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

/** The robot's half in node:crypto (OpenSSL): what hf_token_crypto.RobotKey.decrypt does. */
function decryptWith(wire, privateKey) {
  const [kid, point, iv, ct] = wire.slice(WIRE_PREFIX.length).split('.');
  const ecdh = createECDH('prime256v1');
  ecdh.setPrivateKey(privateKey);
  const shared = ecdh.computeSecret(Buffer.from(point, 'base64url'));
  const key = Buffer.from(hkdfSync('sha256', shared, CONTEXT, `aes-256-gcm|${kid}`, 32));
  const raw = Buffer.from(ct, 'base64url');
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
  decipher.setAAD(Buffer.from(`${CONTEXT}|${kid}`, 'utf8'));
  decipher.setAuthTag(raw.subarray(raw.length - 16));
  return Buffer.concat([decipher.update(raw.subarray(0, raw.length - 16)), decipher.final()]).toString('utf8');
}

const ROBOT_D = Buffer.from(fixture.robot.d, 'base64url');

const mockTopics = [];
const mockCalls = [];
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
        mockCalls.push({ request });
        ok({ success: true, message: '' });
      };
    },
  },
}));

beforeEach(() => {
  mockTopics.length = 0;
  mockCalls.length = 0;
  agentReads.length = 0;
  mockAuth.unauthorized = 0;
  mockPi.on = false;
  setAgentKeySourceForTests({ fetch: agentServes(null), recheckMs: 0 });
  setRobotKeyForTests(null);
  rosConnectionManager.url = 'ws://localhost/rosbridge';
  vi.spyOn(rosConnectionManager, 'getCurrentConnection').mockReturnValue({ isConnected: true });
});

afterEach(() => {
  vi.restoreAllMocks();
  setAgentKeySourceForTests();
  setRobotKeyForTests(null);
  rosConnectionManager.url = '';
});

describe('the robot key from the state topic', () => {
  it('is taken from every valid state message and dropped when a robot advertises none', async () => {
    vi.spyOn(rosConnectionManager, 'getConnection').mockResolvedValue({});
    const states = [];
    await subscribeTokenState('ws://localhost/rosbridge', (s) => states.push(s));
    const topic = mockTopics[0];
    const base = { v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false };
    topic.cb({ data: JSON.stringify({ ...base, enc: 'enc1', ...KEY }) });
    expect(getRobotKey()).toEqual(KEY);
    expect(states[0]).toEqual(base); // the parsed state keeps its six keys
    topic.cb({ data: JSON.stringify(base) }); // an older robot after a reconnect
    expect(getRobotKey()).toBeNull();
  });
});

describe('setRobotToken', () => {
  it('sends the enc1 value, never the token, when the robot advertises a key', async () => {
    setRobotKeyForTests(KEY);
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    expect(mockCalls).toHaveLength(1);
    const sent = mockCalls[0].request.token;
    expect(sent.startsWith(`enc1:${KEY.kid}.`)).toBe(true);
    expect(sent).not.toContain(TOKEN);
    expect(sent).not.toContain('hf_');
  });

  it('on a Pi sends nothing to a robot without a key and says why', async () => {
    mockPi.on = true;
    // an agent and a node with no key at all: an image from before the key
    let result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.needsUpdate') });
    // an agent from before GET /robot-key (404), even with a key on the topic
    setAgentKeySourceForTests({ fetch: agentServes(404), recheckMs: 0 });
    setRobotKeyForTests(KEY);
    result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.needsUpdate') });
    expect(mockCalls).toHaveLength(0);
  });

  it('on a Pi sends the enc1 value when the agent and the topic name the same key', async () => {
    mockPi.on = true;
    setAgentKeySourceForTests({ fetch: agentServes(served(KEY)), recheckMs: 0 });
    setRobotKeyForTests(KEY);
    await setRobotToken(TOKEN);
    expect(mockCalls[0].request.token.startsWith(`enc1:${KEY.kid}.`)).toBe(true);
    expect(decryptWith(mockCalls[0].request.token, ROBOT_D)).toBe(TOKEN);
  });

  it('on Windows a robot without a key still gets the plain token (loopback)', async () => {
    await setRobotToken(TOKEN);
    expect(mockCalls[0].request.token).toBe(TOKEN);
  });

  it('clearing is the empty string, on a Pi and with a key too', async () => {
    mockPi.on = true;
    setRobotKeyForTests(KEY);
    const result = await setRobotToken('');
    expect(result.success).toBe(true);
    expect(mockCalls[0].request.token).toBe('');
  });

  it('a Pi page without crypto.subtle (plain HTTP is no secure context) still encrypts', async () => {
    mockPi.on = true;
    setAgentKeySourceForTests({ fetch: agentServes(served(KEY)), recheckMs: 0 });
    setRobotKeyForTests(KEY);
    const saved = globalThis.crypto;
    const noSubtle = { getRandomValues: (a) => webcrypto.getRandomValues(a) };
    Object.defineProperty(globalThis, 'crypto', { value: noSubtle, configurable: true });
    try {
      const result = await setRobotToken(TOKEN);
      expect(result.success).toBe(true);
      expect(mockCalls[0].request.token.startsWith(`enc1:${KEY.kid}.`)).toBe(true);
      expect(mockCalls[0].request.token).not.toContain('hf_');
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: saved, configurable: true });
    }
  });

  it('a browser without any crypto gets a German sentence, never the plain token', async () => {
    setRobotKeyForTests(KEY);
    const saved = globalThis.crypto;
    Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
    try {
      const result = await setRobotToken(TOKEN);
      expect(result).toEqual({ success: false, message: hfCopy('robot.encryptFailed') });
      expect(mockCalls).toHaveLength(0);
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: saved, configurable: true });
    }
  });

  it('a robot point off the curve gets a German sentence, never the plain token', async () => {
    const bytes = Buffer.from(KEY.pub, 'base64url');
    bytes[64] ^= 1; // no longer on P-256: noble refuses it
    setRobotKeyForTests({ kid: KEY.kid, pub: bytes.toString('base64url') });
    const result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.encryptFailed') });
    expect(mockCalls).toHaveLength(0);
  });

  it('a chunk that did not load says to reload the page, and sends nothing', async () => {
    vi.resetModules();
    vi.doMock('../hfTokenSeal', () => {
      throw new Error('Chunk nicht geladen');
    });
    try {
      const fresh = await import('../robotChannel');
      const copy = await import('../hfTokenCopy');
      fresh.setRobotKeyForTests(KEY);
      const result = await fresh.setRobotToken(TOKEN);
      expect(result).toEqual({ success: false, message: copy.hfCopy('robot.sealUnavailable') });
      expect(mockCalls).toHaveLength(0);
    } finally {
      vi.doUnmock('../hfTokenSeal');
      vi.resetModules();
    }
  });

  it('the copy is German', () => {
    expect(hfCopy('robot.needsUpdate')).toMatch(/Roboter-Software/);
    expect(hfCopy('robot.encryptFailed')).toMatch(/verschlüsselt/);
    expect(hfCopy('robot.sealUnavailable')).toMatch(/Seite neu laden/);
    expect(ROSLIB).toBeTruthy();
  });
});

describe('VB-03: on a Pi the key comes from the agent, never from the topic alone', () => {
  beforeEach(() => {
    mockPi.on = true;
  });

  it('a forged topic naming an attacker key gets nothing, and the page says why', async () => {
    const attacker = newKey();
    setAgentKeySourceForTests({ fetch: agentServes(served(KEY)), recheckMs: 0 });
    setRobotKeyForTests({ kid: attacker.kid, pub: attacker.pub });
    const result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.keyMismatch') });
    expect(mockCalls).toHaveLength(0); // nothing reached /register_hf_user, forged or real
    expect(agentReads).toHaveLength(KEY_CHECKS); // re-read before giving up
  });

  it('a forged topic with the real key id but the attacker point: the ciphertext is for the agent key', async () => {
    const attacker = newKey();
    setAgentKeySourceForTests({ fetch: agentServes(served(KEY)), recheckMs: 0 });
    setRobotKeyForTests({ kid: KEY.kid, pub: attacker.pub });
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    const wire = mockCalls[0].request.token;
    // A forged /register_hf_user receives exactly this: only the node opens it.
    expect(decryptWith(wire, ROBOT_D)).toBe(TOKEN);
    expect(() => decryptWith(wire, attacker.d)).toThrow();
    // ...not relabelled with the attacker's own key id either.
    const relabelled = `${WIRE_PREFIX}${attacker.kid}${wire.slice(WIRE_PREFIX.length + 16)}`;
    expect(() => decryptWith(relabelled, attacker.d)).toThrow();
    expect(wire).not.toContain(TOKEN);
  });

  it('a restarted node: the file is new before the topic is, so the page waits and then sends', async () => {
    const before = newKey(); // the key the page saw before the restart
    setRobotKeyForTests({ kid: before.kid, pub: before.pub });
    setAgentKeySourceForTests({
      fetch: agentServes((n) => {
        if (n === 2) setRobotKeyForTests(KEY); // the topic catches up between two reads
        return served(KEY);
      }),
      recheckMs: 0,
    });
    const result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    expect(agentReads).toHaveLength(2);
    expect(decryptWith(mockCalls[0].request.token, ROBOT_D)).toBe(TOKEN);
  });

  it('no key from the agent, an agent that cannot be asked, or a lost session: nothing is sent', async () => {
    setRobotKeyForTests(KEY);
    const cases = [
      [null, 'robot.keyUnavailable'],
      [{ enc: 'enc1', kid: 'XYZ', pub: KEY.pub }, 'robot.keyUnavailable'],
      // VC-01: an agent that never answered is named as such.
      [new Error('offline'), 'robot.agentUnreachable'],
      [500, 'robot.agentUnreachable'],
    ];
    for (const [answer, copyKey] of cases) {
      mockCalls.length = 0;
      agentReads.length = 0;
      setAgentKeySourceForTests({ fetch: agentServes(answer), recheckMs: 0 });
      // eslint-disable-next-line no-await-in-loop
      const result = await setRobotToken(TOKEN);
      expect(result).toEqual({ success: false, message: hfCopy(copyKey) });
      expect(mockCalls).toHaveLength(0);
      expect(agentReads).toHaveLength(KEY_CHECKS);
    }
    setAgentKeySourceForTests({ fetch: agentServes(401), recheckMs: 0 });
    const result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.agentUnreachable') });
    expect(mockAuth.unauthorized).toBe(KEY_CHECKS); // the robot login re-checks its session
  });

  it('reads the agent same-origin, with the session cookie and no Authorization header', async () => {
    setAgentKeySourceForTests({ fetch: agentServes(served(KEY)), recheckMs: 0 });
    expect(await readAgentRobotKey()).toEqual({ status: 'ok', key: KEY });
    const [{ url, init }] = agentReads;
    expect(url.split('?')[0]).toBe(AGENT_KEY_URL);
    expect(AGENT_KEY_URL).toBe('/api/system/robot-key');
    expect(init.credentials).toBe('same-origin');
    expect(init.headers).toBeUndefined();
  });

  it('clearing needs no key; Windows never asks the agent', async () => {
    let result = await setRobotToken('');
    expect(result.success).toBe(true);
    expect(mockCalls[0].request.token).toBe('');
    mockPi.on = false;
    setRobotKeyForTests(KEY);
    result = await setRobotToken(TOKEN);
    expect(result.success).toBe(true);
    expect(agentReads).toHaveLength(0);
  });

  it('the copy is German', () => {
    expect(hfCopy('robot.keyMismatch')).toMatch(/verschiedene Schlüssel/);
    expect(hfCopy('robot.keyUnavailable')).toMatch(/Schlüssel/);
  });
});
