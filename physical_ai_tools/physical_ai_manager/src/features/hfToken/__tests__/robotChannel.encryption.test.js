// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The token travels ENCRYPTED for the robot's key (2026-11-13). Pinned: a robot
// that advertises a key on /edubotics/hf_token_state gets the `enc1:` value
// and never the token; an Orange Pi page sends NOTHING to a robot without a
// key; a Windows page (loopback) still sends the plain token to such a robot;
// clearing stays the empty string.

import ROSLIB from 'roslib';
import { webcrypto } from 'crypto';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import {
  getRobotKey, setRobotKeyForTests, setRobotToken, subscribeTokenState,
} from '../robotChannel';
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
  mockPi.on = false;
  setRobotKeyForTests(null);
  rosConnectionManager.url = 'ws://localhost/rosbridge';
  vi.spyOn(rosConnectionManager, 'getCurrentConnection').mockReturnValue({ isConnected: true });
});

afterEach(() => {
  vi.restoreAllMocks();
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
    const result = await setRobotToken(TOKEN);
    expect(result).toEqual({ success: false, message: hfCopy('robot.needsUpdate') });
    expect(mockCalls).toHaveLength(0);
  });

  it('on a Pi sends the enc1 value when a key is advertised', async () => {
    mockPi.on = true;
    setRobotKeyForTests(KEY);
    await setRobotToken(TOKEN);
    expect(mockCalls[0].request.token.startsWith('enc1:')).toBe(true);
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

  it('the copy is German', () => {
    expect(hfCopy('robot.needsUpdate')).toMatch(/Roboter-Software/);
    expect(hfCopy('robot.encryptFailed')).toMatch(/verschlüsselt/);
    expect(ROSLIB).toBeTruthy();
  });
});
