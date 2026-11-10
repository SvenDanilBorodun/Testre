// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// VC-06 (verification round C, 2026-11-15): the Cloud-Inferenz start carried
// its connect token and Modal edge token in the clear over rosbridge, and on a
// Pi any robot session holder can advertise /edubotics/cloud_inference/start
// and take them (VB-03's root cause). On a REMOTE page (utils/robotLocality)
// both now travel in the `enc1:` envelope, encrypted for the key the pi-agent
// serves behind the robot login, each under its OWN context, and nothing is
// sent when that key cannot be had. The Windows GUI's loopback page is
// unchanged (robotCalls.test.js).

import { createDecipheriv, createECDH, createHash, hkdfSync, webcrypto } from 'crypto';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import { setPageHostnameForTests } from '../../../utils/robotLocality';
import { startCloudInference } from '../robotCalls';
import { START_REQUEST_KEYS, START_SERVICE } from '../robotContract';
import { setAgentKeySourceForTests } from '../../hfToken/robotChannel';
import {
  CLOUD_EDGE_CONTEXT, CLOUD_SESSION_CONTEXT, CONTEXT, WIRE_PREFIX,
} from '../../hfToken/hfTokenCrypto';
import { cloudCopy } from '../../../components/CloudInference/cloudCopy';
import fixture from '../../hfToken/__tests__/fixtures/hf-token-crypto-cases.json';

if (!globalThis.crypto || !globalThis.crypto.subtle) globalThis.crypto = webcrypto;

const CONNECT = 'C'.repeat(43);
const EDGE = `eyJ${'e'.repeat(700)}.sig`;
const KEY = { kid: fixture.robot.kid, pub: fixture.robot.pub };
const ROBOT_D = Buffer.from(fixture.robot.d, 'base64url');

const mockPi = vi.hoisted(() => ({ on: false }));
vi.mock('../../../utils/piMode', () => ({ __esModule: true, getPiModeSync: () => mockPi.on }));
vi.mock('../../piAuth/piAuth', () => ({ __esModule: true, noteUnauthorized: () => {} }));

const mockCalls = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock() { this.subscribe = () => {}; this.unsubscribe = () => {}; },
    ServiceRequest: function RequestMock(values) { Object.assign(this, values); },
    Service: function ServiceMock(opts) {
      this.opts = opts;
      this.callService = (request, ok, fail) => { mockCalls.push({ service: this, request, ok, fail }); };
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

/** The robot's half (hf_token_crypto.RobotKey.decrypt) in node:crypto. */
function open(wire, privateKey, context) {
  const [kid, point, iv, ct] = wire.slice(WIRE_PREFIX.length).split('.');
  const ecdh = createECDH('prime256v1');
  ecdh.setPrivateKey(privateKey);
  const shared = ecdh.computeSecret(Buffer.from(point, 'base64url'));
  const key = Buffer.from(hkdfSync('sha256', shared, context, `aes-256-gcm|${kid}`, 32));
  const raw = Buffer.from(ct, 'base64url');
  const d = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
  d.setAAD(Buffer.from(`${context}|${kid}`, 'utf8'));
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

const request = () => ({ task_info: { task_type: 'inference', fps: 30 }, session_token: CONNECT, edge_token: EDGE });

beforeEach(() => {
  mockCalls.length = 0;
  agentReads.length = 0;
  mockPi.on = false;
  setPageHostnameForTests('192.168.1.50'); // a Pi page; its marker may or may not have answered
  setAgentKeySourceForTests({ fetch: agentServes(KEY), recheckMs: 0 });
  rosConnectionManager.url = 'ws://192.168.1.50/rosbridge';
  vi.spyOn(rosConnectionManager, 'getCurrentConnection').mockReturnValue({ isConnected: true });
});

afterEach(() => {
  vi.restoreAllMocks();
  setPageHostnameForTests(null);
  setAgentKeySourceForTests();
  rosConnectionManager.url = '';
});

describe('VC-06: the cloud start from a remote page', () => {
  it('sends both tokens in the envelope, for the agent\'s key, each under its own context', async () => {
    const pending = startCloudInference(request());
    await vi.waitFor(() => expect(mockCalls).toHaveLength(1));
    const { service, request: req } = mockCalls[0];
    expect(service.opts.name).toBe(START_SERVICE);
    expect(Object.keys(req)).toEqual([...START_REQUEST_KEYS]);
    expect(req.session_token.startsWith(`${WIRE_PREFIX}${KEY.kid}.`)).toBe(true);
    expect(req.edge_token.startsWith(`${WIRE_PREFIX}${KEY.kid}.`)).toBe(true);
    expect(JSON.stringify(req)).not.toContain(CONNECT);
    expect(JSON.stringify(req)).not.toContain(EDGE.slice(0, 40));
    expect(open(req.session_token, ROBOT_D, CLOUD_SESSION_CONTEXT)).toBe(CONNECT);
    expect(open(req.edge_token, ROBOT_D, CLOUD_EDGE_CONTEXT)).toBe(EDGE);
    // No envelope opens as another kind (the HF token's included).
    expect(() => open(req.session_token, ROBOT_D, CLOUD_EDGE_CONTEXT)).toThrow();
    expect(() => open(req.edge_token, ROBOT_D, CLOUD_SESSION_CONTEXT)).toThrow();
    expect(() => open(req.session_token, ROBOT_D, CONTEXT)).toThrow();
    mockCalls[0].ok({ success: true, message: 'Cloud-Inferenz gestartet.' });
    await expect(pending).resolves.toEqual({ success: true, message: '', oldImage: false, timedOut: false });
  });

  it('a classmate who advertised the start service cannot open what it receives', async () => {
    const attacker = newKey();
    const pending = startCloudInference(request());
    await vi.waitFor(() => expect(mockCalls).toHaveLength(1));
    const { request: req } = mockCalls[0];
    expect(() => open(req.session_token, attacker.d, CLOUD_SESSION_CONTEXT)).toThrow();
    expect(() => open(req.edge_token, attacker.d, CLOUD_EDGE_CONTEXT)).toThrow();
    mockCalls[0].ok({ success: true, message: '' });
    await pending;
  });

  it('every start is a fresh envelope (the robot opens each value once)', async () => {
    const first = startCloudInference(request());
    await vi.waitFor(() => expect(mockCalls).toHaveLength(1));
    mockCalls[0].ok({ success: true, message: '' });
    await first;
    const second = startCloudInference(request());
    await vi.waitFor(() => expect(mockCalls).toHaveLength(2));
    mockCalls[1].ok({ success: true, message: '' });
    await second;
    expect(mockCalls[0].request.session_token).not.toBe(mockCalls[1].request.session_token);
    expect(mockCalls[0].request.edge_token).not.toBe(mockCalls[1].request.edge_token);
  });

  const refusals = [
    ['an agent that cannot be asked', new Error('offline'), cloudCopy.errors.robotKeyUnreachable],
    ['a lost robot session (401)', 401, cloudCopy.errors.robotKeyUnreachable],
    ['an agent without the key route', 404, cloudCopy.errors.robotOld],
    ['no key yet', null, cloudCopy.errors.robotKeyMissing],
  ];
  for (const [label, answer, sentence] of refusals) {
    it(`${label}: nothing is sent, and the page says why in German`, async () => {
      setAgentKeySourceForTests({ fetch: agentServes(answer), recheckMs: 0 });
      const result = await startCloudInference(request());
      expect(result).toEqual({ success: false, message: sentence, oldImage: false, timedOut: false });
      expect(mockCalls).toHaveLength(0);
      expect(sentence).toMatch(/[äöüÄÖÜß]|Roboter/);
    });
  }

  it('an agent that never answers is bounded: no hang, nothing sent', async () => {
    setAgentKeySourceForTests({ fetch: () => new Promise(() => {}), recheckMs: 0, timeoutMs: 30 });
    const result = await startCloudInference(request());
    expect(result.success).toBe(false);
    expect(result.message).toBe(cloudCopy.errors.robotKeyUnreachable);
    expect(mockCalls).toHaveLength(0);
  });

  it('a page whose marker says Pi, at localhost (a kiosk browser), encrypts too', async () => {
    mockPi.on = true;
    setPageHostnameForTests('localhost');
    rosConnectionManager.url = 'ws://localhost/rosbridge';
    const pending = startCloudInference(request());
    await vi.waitFor(() => expect(mockCalls).toHaveLength(1));
    expect(mockCalls[0].request.session_token.startsWith(WIRE_PREFIX)).toBe(true);
    mockCalls[0].ok({ success: true, message: '' });
    await pending;
  });

  it('the Windows GUI page (localhost, no marker) still sends the tokens as they are', async () => {
    setPageHostnameForTests('localhost');
    rosConnectionManager.url = 'ws://localhost/rosbridge';
    const pending = startCloudInference(request());
    expect(mockCalls).toHaveLength(1); // synchronous, as before
    expect(mockCalls[0].request.session_token).toBe(CONNECT);
    expect(agentReads).toHaveLength(0);
    mockCalls[0].ok({ success: true, message: '' });
    await pending;
  });

  it('the copy is German', () => {
    expect(cloudCopy.errors.robotKeyUnreachable).toMatch(/Schlüssel/);
    expect(cloudCopy.errors.robotKeyMissing).toMatch(/Schlüssel/);
    expect(cloudCopy.errors.encryptFailed).toMatch(/verschlüsselt/);
    expect(cloudCopy.errors.sealUnavailable).toMatch(/Seite neu laden/);
  });
});
