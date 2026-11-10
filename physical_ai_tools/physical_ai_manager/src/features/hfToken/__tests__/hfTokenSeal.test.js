// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The scheme's steps as wired from the audited libraries (2026-11-14). The
// primitives themselves are noble's, audited and tested upstream; what is ours
// is the wiring, and that is what these known answers hold: the ECDH step
// against RFC 5903 §8.1's published P-256 vector, the whole scheme against
// node:crypto (OpenSSL), and the robot point's validation (noble's on-curve
// check plus our uncompressed-only shape). hfTokenCrypto.test.js holds the
// fixture the robot's Python test reads.

import crypto from 'node:crypto';
import { b64url } from '../hfTokenCrypto';
import {
  clientPointOf, IV_LEN, robotPointOf, sealToken, sealTokenWith, sharedX,
} from '../hfTokenSeal';

const hex = (b) => Buffer.from(b).toString('hex');
const hexBytes = (h) => Uint8Array.from(Buffer.from(h, 'hex'));

// RFC 5903 §8.1 (256-bit Random ECP Group).
const RFC5903 = {
  i: 'c88f01f510d9ac3f70a292daa2316de544e9aab8afe84049c62a9c57862d1433',
  gix: 'dad0b65394221cf9b051e1feca5787d098dfe637fc90b9ef945d0c3772581180',
  giy: '5271a0461cdb8252d61f1c456fa3e59ab1f45b33accf5f58389e0577b8990bb3',
  r: 'c6ef9c5d78ae012a011164acb397ce2088685d8f06bf9be0b283ab46476bee53',
  grx: 'd12dfb5289c8d4f81208b70270398c342296970a0bccb74c736fc7554494bf63',
  gry: '56fbf3ca366cc23e8157854c13c58d6aac23f046ada30f8353e74f33039872ab',
  girx: 'd6840f6b42f6edafd13116e0e12565202fef8e9ece7dce03812464d04b9442de',
};

function nodeRobot() {
  const ecdh = crypto.createECDH('prime256v1');
  ecdh.generateKeys();
  const point = new Uint8Array(ecdh.getPublicKey(null, 'uncompressed'));
  const kid = crypto.createHash('sha256').update(point).digest('hex').slice(0, 16);
  return { ecdh, key: { kid, pub: b64url(point) }, point };
}

/** The robot's decryption, in node:crypto. */
function openWithNode(wire, robot) {
  const [kid, point, iv, ct] = wire.slice('enc1:'.length).split('.');
  const shared = robot.ecdh.computeSecret(Buffer.from(point, 'base64url'));
  const key = Buffer.from(crypto.hkdfSync('sha256', shared, 'edubotics-hf-token:v1', `aes-256-gcm|${kid}`, 32));
  const body = Buffer.from(ct, 'base64url');
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
  decipher.setAAD(Buffer.from(`edubotics-hf-token:v1|${kid}`));
  decipher.setAuthTag(body.subarray(body.length - 16));
  return Buffer.concat([decipher.update(body.subarray(0, body.length - 16)), decipher.final()]).toString('utf8');
}

describe('the ECDH step: RFC 5903 §8.1', () => {
  it('the public point of i and of r', () => {
    expect(hex(clientPointOf(hexBytes(RFC5903.i)))).toBe(`04${RFC5903.gix}${RFC5903.giy}`);
    expect(hex(clientPointOf(hexBytes(RFC5903.r)))).toBe(`04${RFC5903.grx}${RFC5903.gry}`);
  });

  it('the shared x coordinate, both ways', () => {
    const gr = hexBytes(`04${RFC5903.grx}${RFC5903.gry}`);
    const gi = hexBytes(`04${RFC5903.gix}${RFC5903.giy}`);
    expect(hex(sharedX(hexBytes(RFC5903.i), gr))).toBe(RFC5903.girx);
    expect(hex(sharedX(hexBytes(RFC5903.r), gi))).toBe(RFC5903.girx);
  });
});

describe('the whole scheme', () => {
  it('opens with node:crypto for every token length of the shape', () => {
    const robot = nodeRobot();
    for (const n of [1, 17, 64, 255, 256]) {
      const token = `hf_${'0123456789abcdef'.repeat(17).slice(0, n)}`;
      const wire = sealToken(token, robot.key);
      expect(wire).not.toContain(token);
      expect(openWithNode(wire, robot)).toBe(token);
    }
  });

  it('every push gets a fresh ephemeral key and iv', () => {
    const robot = nodeRobot();
    const points = new Set();
    const ivs = new Set();
    for (let k = 0; k < 20; k += 1) {
      const [, point, iv] = sealToken('hf_test0token0fresh00', robot.key).slice(5).split('.');
      points.add(point);
      ivs.add(iv);
    }
    expect(points.size).toBe(20);
    expect(ivs.size).toBe(20);
  });

  it('a tampered ciphertext does not open (the tag is noble GCM’s, checked by OpenSSL)', () => {
    const robot = nodeRobot();
    const wire = sealToken('hf_test0token0tamper0', robot.key);
    const parts = wire.split('.');
    const body = Buffer.from(parts[3], 'base64url');
    body[0] ^= 1;
    parts[3] = b64url(body);
    expect(() => openWithNode(parts.join('.'), robot)).toThrow();
  });

  it('refuses a wrong iv length, an empty token and a missing key', () => {
    const robot = nodeRobot();
    const d = hexBytes(RFC5903.i);
    expect(() => sealTokenWith('hf_x', robot.key, d, new Uint8Array(IV_LEN - 1))).toThrow();
    expect(() => sealTokenWith('', robot.key, d, new Uint8Array(IV_LEN))).toThrow();
    expect(() => sealToken('hf_x', null)).toThrow();
  });

  it('refuses without randomness instead of falling back to anything', () => {
    const robot = nodeRobot();
    const before = globalThis.crypto;
    Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
    try {
      expect(() => sealToken('hf_test0token0norandom', robot.key)).toThrow();
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: before, configurable: true });
    }
  });
});

describe('the robot point is validated before it is used', () => {
  const robot = nodeRobot();
  const good = robot.point;
  const d = hexBytes(RFC5903.i);
  const iv = new Uint8Array(IV_LEN);
  const sealFor = (point) => sealTokenWith('hf_test0token0point00', { kid: robot.key.kid, pub: b64url(point) }, d, iv);

  it('accepts the robot’s uncompressed point', () => {
    expect(robotPointOf(b64url(good))).toEqual(good);
    expect(() => sealFor(good)).not.toThrow();
  });

  it('refuses a point off the curve (noble’s check: an invalid-curve key never meets our secret)', () => {
    const off = Uint8Array.from(good);
    off[64] ^= 1;
    expect(() => sealFor(off)).toThrow();
  });

  it('refuses a coordinate at or above the field prime', () => {
    const big = Uint8Array.from(good);
    big.fill(0xff, 1, 33);
    expect(() => sealFor(big)).toThrow();
  });

  it('refuses every encoding but uncompressed, and every other length', () => {
    for (const prefix of [0x00, 0x02, 0x03, 0x06, 0x07]) {
      const other = Uint8Array.from(good);
      other[0] = prefix;
      expect(() => sealFor(other)).toThrow();
    }
    expect(() => sealFor(good.slice(0, 33))).toThrow();
    expect(() => sealFor(new Uint8Array(65))).toThrow();
  });
});
