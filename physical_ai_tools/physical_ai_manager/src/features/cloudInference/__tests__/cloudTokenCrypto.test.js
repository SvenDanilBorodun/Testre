// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// VC-06 (verification round C, 2026-11-15): Cloud-Inferenz's connect token and
// Modal edge token ride the HF token's `enc1:` envelope to a remote robot, each
// under its own context. The robot's half is
// physical_ai_server/data_processing/hf_token_crypto.py; the two meet in
// fixtures/cloud-token-crypto-cases.json, which THIS file writes (with
// EDUBOTICS_REGEN_CLOUD_CRYPTO_CASES=1, or when it is missing) and the robot's
// test (physical_ai_server/test/test_cloud_token_crypto.py) checks in Python,
// exactly like the HF token's hf-token-crypto-cases.json:
//
//   cases  random encryptions by the product path (encryptForRobot), per context;
//   kats   known answers from a fixed ephemeral secret and iv, recomputed here
//          byte for byte with node:crypto and there with `cryptography`.
//
// The robot key is the HF fixture's TEST key (it never left this repository).

import fs from 'fs';
import path from 'path';
import crypto, { webcrypto } from 'crypto';
import {
  b64url, b64urlDecode, CLOUD_EDGE_CONTEXT, CLOUD_SESSION_CONTEXT, CONTEXT, encryptForRobot,
  ENVELOPE_CONTEXTS, WIRE_PREFIX,
} from '../../hfToken/hfTokenCrypto';
import { sealTokenWith } from '../../hfToken/hfTokenSeal';
import hfFixture from '../../hfToken/__tests__/fixtures/hf-token-crypto-cases.json';

if (!globalThis.crypto || !globalThis.crypto.getRandomValues) globalThis.crypto = webcrypto;

const FIXTURE = path.join(__dirname, 'fixtures', 'cloud-token-crypto-cases.json');
const REGEN = process.env.EDUBOTICS_REGEN_CLOUD_CRYPTO_CASES === '1';
const ROBOT = { kid: hfFixture.robot.kid, pub: hfFixture.robot.pub };
const ROBOT_D = Buffer.from(hfFixture.robot.d, 'base64url');

const CONTEXTS = { session: CLOUD_SESSION_CONTEXT, edge: CLOUD_EDGE_CONTEXT };
// Low-entropy test values of the real shapes: a connect token is
// secrets.token_urlsafe(32) (43 characters), an edge token a JWT-like printable
// string of up to 4096 (cloud_protocol.TOKEN_RE / EDGE_TOKEN_RE).
const PLAINS = [
  { context: 'session', plain: `${'0123456789'.repeat(4)}abc` },
  { context: 'edge', plain: `eyJ0ZXN0${'0'.repeat(32)}.${'a'.repeat(700)}.sig` },
  { context: 'edge', plain: `!${'~'.repeat(4095)}` },
];
const KAT_INPUTS = [
  {
    context: 'session',
    plain: `${'0123456789'.repeat(4)}abc`,
    d: 'c88f01f510d9ac3f70a292daa2316de544e9aab8afe84049c62a9c57862d1433',
    iv: '000102030405060708090a0b',
  },
  {
    context: 'edge',
    plain: `eyJ0ZXN0${'0'.repeat(32)}.${'a'.repeat(700)}.sig`,
    d: '7d7dc5f71eb29ddaf80d6214632eeae03d9058af1fb6d22ed80badb62bc1a534',
    iv: 'cafebabefacedbaddecaf888',
  },
];

const hexBytes = (hex) => Uint8Array.from(Buffer.from(hex, 'hex'));

/** The robot's half in node:crypto: what RobotKey.decrypt(wire, context) does. */
function open(wire, context) {
  const [kid, point, iv, ct] = wire.slice(WIRE_PREFIX.length).split('.');
  const ecdh = crypto.createECDH('prime256v1');
  ecdh.setPrivateKey(ROBOT_D);
  const shared = ecdh.computeSecret(Buffer.from(point, 'base64url'));
  const key = Buffer.from(crypto.hkdfSync('sha256', shared, context, `aes-256-gcm|${kid}`, 32));
  const raw = Buffer.from(ct, 'base64url');
  const d = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
  d.setAAD(Buffer.from(`${context}|${kid}`, 'utf8'));
  d.setAuthTag(raw.subarray(raw.length - 16));
  return Buffer.concat([d.update(raw.subarray(0, raw.length - 16)), d.final()]).toString('utf8');
}

/** The scheme computed by node:crypto alone: no noble code involved. */
function wireByNodeCrypto(plain, context, dHex, ivHex) {
  const ecdh = crypto.createECDH('prime256v1');
  ecdh.setPrivateKey(Buffer.from(dHex, 'hex'));
  const point = ecdh.getPublicKey(null, 'uncompressed');
  const shared = ecdh.computeSecret(Buffer.from(b64urlDecode(ROBOT.pub)));
  const key = Buffer.from(crypto.hkdfSync('sha256', shared, context, `aes-256-gcm|${ROBOT.kid}`, 32));
  const cipher = crypto.createCipheriv('aes-256-gcm', key, Buffer.from(ivHex, 'hex'));
  cipher.setAAD(Buffer.from(`${context}|${ROBOT.kid}`, 'utf8'));
  const ct = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final(), cipher.getAuthTag()]);
  return `${WIRE_PREFIX}${ROBOT.kid}.${b64url(point)}.${b64url(Buffer.from(ivHex, 'hex'))}.${b64url(ct)}`;
}

async function regenerate() {
  const cases = [];
  for (const { context, plain } of PLAINS) {
    // eslint-disable-next-line no-await-in-loop
    cases.push({ context, plain, wire: await encryptForRobot(plain, ROBOT, { context: CONTEXTS[context] }) });
  }
  const kats = KAT_INPUTS.map(({ context, plain, d, iv }) => ({
    context,
    plain,
    ephemeral_d: d,
    iv,
    wire: sealTokenWith(plain, ROBOT, hexBytes(d), hexBytes(iv), CONTEXTS[context]),
  }));
  return {
    _generated_by: 'physical_ai_manager/src/features/cloudInference/__tests__/cloudTokenCrypto.test.js '
      + '(EDUBOTICS_REGEN_CLOUD_CRYPTO_CASES=1 to regenerate)',
    _checked_by: 'physical_ai_server/test/test_cloud_token_crypto.py (the robot opens every case '
      + 'under its context and recomputes every known answer)',
    _note: 'robot is the TEST key of hfToken/__tests__/fixtures/hf-token-crypto-cases.json; '
      + 'ephemeral_d and iv are hex; every other binary value is base64url.',
    contexts: { session: CLOUD_SESSION_CONTEXT, edge: CLOUD_EDGE_CONTEXT, hf_token: CONTEXT },
    robot: ROBOT,
    cases,
    kats,
  };
}

describe('the cloud tokens\' envelope ↔ the robot (fixture shared with Python)', () => {
  it('three distinct contexts, the HF token\'s first', () => {
    expect(ENVELOPE_CONTEXTS).toEqual([CONTEXT, CLOUD_SESSION_CONTEXT, CLOUD_EDGE_CONTEXT]);
    expect(new Set(ENVELOPE_CONTEXTS).size).toBe(3);
  });

  it('the checked-in cases open with the fixture robot key, each only under its own context', async () => {
    if (REGEN || !fs.existsSync(FIXTURE)) {
      const fresh = await regenerate();
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(fresh, null, 1)}\n`);
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(saved.robot).toEqual(ROBOT);
    expect(saved.contexts).toEqual({ session: CLOUD_SESSION_CONTEXT, edge: CLOUD_EDGE_CONTEXT, hf_token: CONTEXT });
    expect(saved.cases.map(({ context, plain }) => ({ context, plain }))).toEqual(PLAINS);
    for (const c of saved.cases) {
      expect(open(c.wire, CONTEXTS[c.context])).toBe(c.plain);
      for (const other of ENVELOPE_CONTEXTS.filter((x) => x !== CONTEXTS[c.context])) {
        expect(() => open(c.wire, other)).toThrow();
      }
    }
  });

  it('every known answer is what noble produces AND what node:crypto computes independently', () => {
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(saved.kats.map(({ context, plain, ephemeral_d: d, iv }) => ({ context, plain, d, iv })))
      .toEqual(KAT_INPUTS);
    for (const kat of saved.kats) {
      const context = CONTEXTS[kat.context];
      expect(sealTokenWith(kat.plain, ROBOT, hexBytes(kat.ephemeral_d), hexBytes(kat.iv), context)).toBe(kat.wire);
      expect(wireByNodeCrypto(kat.plain, context, kat.ephemeral_d, kat.iv)).toBe(kat.wire);
      expect(open(kat.wire, context)).toBe(kat.plain);
    }
  });

  it('refuses a context it does not know before any crypto', async () => {
    await expect(encryptForRobot('x'.repeat(43), ROBOT, { context: 'edubotics-other:v1' })).rejects.toThrow('bad_context');
  });
});
