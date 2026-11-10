// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The token's encryption for the robot (2026-11-13; the audited libraries since
// 2026-11-14). The robot's half is
// physical_ai_server/data_processing/hf_token_crypto.py; the two meet in
// fixtures/hf-token-crypto-cases.json, which THIS file writes (with
// EDUBOTICS_REGEN_HF_CRYPTO_CASES=1) and the robot's test
// (physical_ai_server/test/test_hf_token_crypto.py) checks in Python:
//
//   cases  random encryptions by the product path (hfTokenSeal.sealToken);
//          here decrypted with WebCrypto, there with `cryptography`.
//   kats   known answers: a fixed ephemeral secret and iv. The wire value is
//          what noble produces; here it is recomputed byte for byte with
//          node:crypto, there with `cryptography` — two implementations that
//          share no code with noble or with each other.
//
// The robot key in the fixture is a TEST key that never left this repository.

import fs from 'fs';
import path from 'path';
import crypto, { webcrypto } from 'crypto';
import {
  b64url, b64urlDecode, CONTEXT, encryptForRobot, ENC_NAME, parseRobotKey, WIRE_PREFIX,
} from '../hfTokenCrypto';
import { sealTokenWith } from '../hfTokenSeal';

const FIXTURE = path.join(__dirname, 'fixtures', 'hf-token-crypto-cases.json');
const REGEN = process.env.EDUBOTICS_REGEN_HF_CRYPTO_CASES === '1';
const subtle = webcrypto.subtle;
if (!globalThis.crypto || !globalThis.crypto.getRandomValues) {
  globalThis.crypto = webcrypto;
}

// Low-entropy, digit-bearing test tokens on purpose (the secret scan's
// Hugging Face rule is hf_ followed by 34 letters).
const PLAINS = [
  'hf_test0token0for0the0interop00',
  `hf_${'0123456789'.repeat(25)}abcdef`, // the shape's longest: 256 after hf_
  'hf_Ab-_0123456789xyz',
];

// Fixed ephemeral secrets (hex) and ivs for the known answers. The first is
// RFC 5903 §8.1's `i`; the others are arbitrary scalars below the group order.
const KAT_INPUTS = [
  {
    plain: 'hf_test0token0for0the0interop00',
    d: 'c88f01f510d9ac3f70a292daa2316de544e9aab8afe84049c62a9c57862d1433',
    iv: '000102030405060708090a0b',
  },
  {
    plain: `hf_${'0123456789'.repeat(25)}abcdef`,
    d: '7d7dc5f71eb29ddaf80d6214632eeae03d9058af1fb6d22ed80badb62bc1a534',
    iv: 'cafebabefacedbaddecaf888',
  },
  {
    plain: 'hf_Ab-_0123456789xyz',
    d: '0000000000000000000000000000000000000000000000000000000000000001',
    iv: 'ffffffffffffffffffffffff',
  },
];

const utf8 = (text) => new TextEncoder().encode(text);
const hexBytes = (hex) => Uint8Array.from(Buffer.from(hex, 'hex'));

async function kidOf(point) {
  const digest = new Uint8Array(await subtle.digest('SHA-256', point));
  return Array.from(digest.slice(0, 8), (b) => b.toString(16).padStart(2, '0')).join('');
}

async function robotFromJwk(robot) {
  return subtle.importKey(
    'jwk',
    { kty: 'EC', crv: 'P-256', d: robot.d, x: robot.x, y: robot.y, ext: true },
    { name: 'ECDH', namedCurve: 'P-256' },
    false,
    ['deriveBits'],
  );
}

/** The robot's half, in WebCrypto: what hf_token_crypto.RobotKey.decrypt does. */
async function decrypt(wire, robotPrivate, kid) {
  expect(wire.startsWith(WIRE_PREFIX)).toBe(true);
  const [wireKid, point, iv, ciphertext] = wire.slice(WIRE_PREFIX.length).split('.');
  expect(wireKid).toBe(kid);
  const peer = await subtle.importKey('raw', b64urlDecode(point), { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = await subtle.deriveBits({ name: 'ECDH', public: peer }, robotPrivate, 256);
  const hkdf = await subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
  const key = await subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt: utf8(CONTEXT), info: utf8(`aes-256-gcm|${kid}`) },
    hkdf, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  const plain = await subtle.decrypt(
    { name: 'AES-GCM', iv: b64urlDecode(iv), additionalData: utf8(`${CONTEXT}|${kid}`) },
    key, b64urlDecode(ciphertext),
  );
  return new TextDecoder().decode(plain);
}

/** The scheme computed by node:crypto (OpenSSL) alone: no noble code involved. */
function wireByNodeCrypto(plain, robot, dHex, ivHex) {
  const ecdh = crypto.createECDH('prime256v1');
  ecdh.setPrivateKey(Buffer.from(dHex, 'hex'));
  const point = ecdh.getPublicKey(null, 'uncompressed');
  const shared = ecdh.computeSecret(Buffer.from(b64urlDecode(robot.pub)));
  const key = Buffer.from(crypto.hkdfSync('sha256', shared, CONTEXT, `aes-256-gcm|${robot.kid}`, 32));
  const cipher = crypto.createCipheriv('aes-256-gcm', key, Buffer.from(ivHex, 'hex'));
  cipher.setAAD(Buffer.from(`${CONTEXT}|${robot.kid}`, 'utf8'));
  const ct = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final(), cipher.getAuthTag()]);
  return `${WIRE_PREFIX}${robot.kid}.${b64url(point)}.${b64url(Buffer.from(ivHex, 'hex'))}.${b64url(ct)}`;
}

async function newRobot() {
  const pair = await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const jwk = await subtle.exportKey('jwk', pair.privateKey);
  const point = new Uint8Array(await subtle.exportKey('raw', pair.publicKey));
  return { d: jwk.d, x: jwk.x, y: jwk.y, pub: b64url(point), kid: await kidOf(point) };
}

async function regenerate() {
  // The robot key is kept across regenerations (it is a test key; only the
  // cases change), and made fresh only when there is no fixture yet.
  const robot = fs.existsSync(FIXTURE)
    ? JSON.parse(fs.readFileSync(FIXTURE, 'utf8')).robot
    : await newRobot();
  const key = { kid: robot.kid, pub: robot.pub };
  const cases = [];
  for (const plain of PLAINS) {
    // eslint-disable-next-line no-await-in-loop
    cases.push({ impl: 'noble', plain, wire: await encryptForRobot(plain, key) });
  }
  const kats = KAT_INPUTS.map(({ plain, d, iv }) => ({
    plain,
    ephemeral_d: d,
    iv,
    wire: sealTokenWith(plain, key, hexBytes(d), hexBytes(iv)),
  }));
  return {
    _generated_by: 'physical_ai_manager/src/features/hfToken/__tests__/hfTokenCrypto.test.js '
      + '(EDUBOTICS_REGEN_HF_CRYPTO_CASES=1 to regenerate)',
    _checked_by: 'physical_ai_server/test/test_hf_token_crypto.py (the robot decrypts every case '
      + 'and recomputes every known answer)',
    _note: 'robot is a TEST key pair made for this file; it never belonged to a robot. '
      + 'ephemeral_d and iv are hex; every other binary value is base64url.',
    robot,
    cases,
    kats,
  };
}

describe('parseRobotKey', () => {
  const state = (o = {}) => JSON.stringify({
    v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false,
    enc: ENC_NAME, kid: '0123456789abcdef', pub: 'A'.repeat(87), ...o,
  });

  it('reads the additive key of a robot that advertises one', () => {
    expect(parseRobotKey(state())).toEqual({ kid: '0123456789abcdef', pub: 'A'.repeat(87) });
  });

  it('is null for an older robot, a malformed advertisement or no slot', () => {
    for (const data of [
      JSON.stringify({ v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false }),
      state({ enc: 'enc2' }), state({ kid: 'XYZ' }), state({ pub: 'short' }),
      state({ accepts: false }), 'not json', '', null, undefined,
    ]) {
      expect(parseRobotKey(data)).toBeNull();
    }
  });
});

describe('encryptForRobot ↔ the robot (fixture shared with Python)', () => {
  it('the checked-in cases decrypt with the fixture robot key, here as in the robot', async () => {
    if (REGEN || !fs.existsSync(FIXTURE)) {
      const fresh = await regenerate();
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(fresh, null, 1)}\n`);
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(saved.cases.map((c) => c.plain)).toEqual(PLAINS);
    expect(saved.cases.map((c) => c.impl)).toEqual(PLAINS.map(() => 'noble'));
    expect(await kidOf(b64urlDecode(saved.robot.pub))).toBe(saved.robot.kid);
    const robotPrivate = await robotFromJwk(saved.robot);
    for (const c of saved.cases) {
      // eslint-disable-next-line no-await-in-loop
      expect(await decrypt(c.wire, robotPrivate, saved.robot.kid)).toBe(c.plain);
    }
  });

  it('every known answer is what noble produces AND what node:crypto computes independently', async () => {
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    const key = { kid: saved.robot.kid, pub: saved.robot.pub };
    expect(saved.kats.map(({ plain, ephemeral_d: d, iv }) => ({ plain, d, iv }))).toEqual(KAT_INPUTS);
    const robotPrivate = await robotFromJwk(saved.robot);
    for (const kat of saved.kats) {
      expect(sealTokenWith(kat.plain, key, hexBytes(kat.ephemeral_d), hexBytes(kat.iv))).toBe(kat.wire);
      expect(wireByNodeCrypto(kat.plain, saved.robot, kat.ephemeral_d, kat.iv)).toBe(kat.wire);
      // eslint-disable-next-line no-await-in-loop
      expect(await decrypt(kat.wire, robotPrivate, saved.robot.kid)).toBe(kat.plain);
    }
  });

  it('a fresh encryption round-trips and never carries the token', async () => {
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    const key = { kid: saved.robot.kid, pub: saved.robot.pub };
    const a = await encryptForRobot(PLAINS[0], key);
    const b = await encryptForRobot(PLAINS[0], key);
    expect(a).not.toBe(b); // a new ephemeral key and iv every time
    expect(a.split('.')[1]).not.toBe(b.split('.')[1]);
    expect(a.split('.')[2]).not.toBe(b.split('.')[2]);
    expect(a).not.toContain('hf_');
    expect(a).toMatch(/^enc1:[0-9a-f]{16}\.[A-Za-z0-9_-]{87}\.[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]+$/);
    expect(await decrypt(a, await robotFromJwk(saved.robot), saved.robot.kid)).toBe(PLAINS[0]);
  });

  it('a page without crypto.subtle (a Pi on plain HTTP) encrypts with the same code', async () => {
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    const key = { kid: saved.robot.kid, pub: saved.robot.pub };
    const before = globalThis.crypto;
    Object.defineProperty(globalThis, 'crypto', {
      value: { getRandomValues: (a) => webcrypto.getRandomValues(a) }, configurable: true,
    });
    let wire;
    try {
      wire = await encryptForRobot(PLAINS[2], key);
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: before, configurable: true });
    }
    expect(await decrypt(wire, await robotFromJwk(saved.robot), saved.robot.kid)).toBe(PLAINS[2]);
  });

  it('refuses a malformed key or an empty token before any crypto', async () => {
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    await expect(encryptForRobot('hf_x', { kid: 'nope', pub: 'A'.repeat(87) })).rejects.toThrow();
    await expect(encryptForRobot('hf_x', null)).rejects.toThrow();
    await expect(encryptForRobot('', { kid: saved.robot.kid, pub: saved.robot.pub })).rejects.toThrow();
  });
});
