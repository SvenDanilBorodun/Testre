// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The token's encryption for the robot (2026-11-13). The robot's half is
// physical_ai_server/data_processing/hf_token_crypto.py; the two meet in
// fixtures/hf-token-crypto-cases.json, which THIS file writes (with
// EDUBOTICS_REGEN_HF_CRYPTO_CASES=1) and the robot's test
// (physical_ai_server/test/test_hf_token_crypto.py) decrypts in Python. Here
// the same cases are decrypted with WebCrypto, so the fixture is proven to be
// what this module produces. The robot key in the fixture is a TEST key that
// never left this repository.

import fs from 'fs';
import path from 'path';
import { webcrypto } from 'crypto';
import {
  b64url, b64urlDecode, CONTEXT, encryptForRobot, ENC_NAME, parseRobotKey, WIRE_PREFIX,
} from '../hfTokenCrypto';

const FIXTURE = path.join(__dirname, 'fixtures', 'hf-token-crypto-cases.json');
const REGEN = process.env.EDUBOTICS_REGEN_HF_CRYPTO_CASES === '1';
const subtle = (globalThis.crypto && globalThis.crypto.subtle) || webcrypto.subtle;
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

const utf8 = (text) => new TextEncoder().encode(text);

async function kidOf(point) {
  const digest = new Uint8Array(await subtle.digest('SHA-256', point));
  return Array.from(digest.slice(0, 8), (b) => b.toString(16).padStart(2, '0')).join('');
}

async function robotFromJwk(robot) {
  const priv = await subtle.importKey(
    'jwk',
    { kty: 'EC', crv: 'P-256', d: robot.d, x: robot.x, y: robot.y, ext: true },
    { name: 'ECDH', namedCurve: 'P-256' },
    false,
    ['deriveBits'],
  );
  return priv;
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

async function regenerate() {
  const pair = await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const jwk = await subtle.exportKey('jwk', pair.privateKey);
  const point = new Uint8Array(await subtle.exportKey('raw', pair.publicKey));
  const robot = { d: jwk.d, x: jwk.x, y: jwk.y, pub: b64url(point), kid: await kidOf(point) };
  const cases = [];
  for (const plain of PLAINS) {
    // eslint-disable-next-line no-await-in-loop
    cases.push({ plain, wire: await encryptForRobot(plain, { kid: robot.kid, pub: robot.pub }, subtle) });
  }
  return {
    _generated_by: 'physical_ai_manager/src/features/hfToken/__tests__/hfTokenCrypto.test.js '
      + '(EDUBOTICS_REGEN_HF_CRYPTO_CASES=1 to regenerate)',
    _checked_by: 'physical_ai_server/test/test_hf_token_crypto.py (the robot decrypts every case)',
    _note: 'robot is a TEST key pair made for this file; it never belonged to a robot',
    robot,
    cases,
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
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(await regenerate(), null, 1)}\n`);
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(saved.cases.map((c) => c.plain)).toEqual(PLAINS);
    expect(await kidOf(b64urlDecode(saved.robot.pub))).toBe(saved.robot.kid);
    const robotPrivate = await robotFromJwk(saved.robot);
    for (const c of saved.cases) {
      // eslint-disable-next-line no-await-in-loop
      expect(await decrypt(c.wire, robotPrivate, saved.robot.kid)).toBe(c.plain);
    }
  });

  it('a fresh encryption round-trips and never carries the token', async () => {
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    const key = { kid: saved.robot.kid, pub: saved.robot.pub };
    const a = await encryptForRobot(PLAINS[0], key, subtle);
    const b = await encryptForRobot(PLAINS[0], key, subtle);
    expect(a).not.toBe(b); // a new ephemeral key and iv every time
    expect(a).not.toContain('hf_');
    expect(a).toMatch(/^enc1:[0-9a-f]{16}\.[A-Za-z0-9_-]{87}\.[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]+$/);
    expect(await decrypt(a, await robotFromJwk(saved.robot), saved.robot.kid)).toBe(PLAINS[0]);
  });

  it('refuses a malformed key before any crypto', async () => {
    await expect(encryptForRobot('hf_x', { kid: 'nope', pub: 'A'.repeat(87) }, subtle)).rejects.toThrow();
    await expect(encryptForRobot('hf_x', null, subtle)).rejects.toThrow();
  });
});
