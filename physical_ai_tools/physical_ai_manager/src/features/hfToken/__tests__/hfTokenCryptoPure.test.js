// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The pure-JS path of the token's encryption (a Pi's plain-HTTP page has no
// crypto.subtle). Every primitive is held against node:crypto, an independent
// implementation, over fixed and random vectors; the whole scheme against
// WebCrypto (the robot's decryption done here) — the Python side decrypts the
// pure path's cases in the shared fixture.

/* global BigInt */
import crypto from 'node:crypto';
import {
  aes256EncryptBlock, aes256GcmEncrypt, decodePoint, ecdhP256, hkdfSha256, hmacSha256,
  publicPointOf, sha256, sharedOf,
} from '../hfTokenCryptoPure';
import { b64urlDecode, CONTEXT, encryptForRobot } from '../hfTokenCrypto';

const rnd = (n) => new Uint8Array(crypto.randomBytes(n));
const hex = (b) => Buffer.from(b).toString('hex');
const subtle = crypto.webcrypto.subtle;

describe('SHA-256 / HMAC / HKDF', () => {
  it('matches the FIPS 180-4 test vectors', () => {
    expect(hex(sha256(new TextEncoder().encode('abc'))))
      .toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(hex(sha256(new Uint8Array(0))))
      .toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });

  it('matches node:crypto on every length around the block boundaries', () => {
    for (let len = 0; len < 200; len += 1) {
      const data = rnd(len);
      expect(hex(sha256(data))).toBe(crypto.createHash('sha256').update(data).digest('hex'));
    }
  });

  it('HMAC and HKDF match node:crypto, a long key included', () => {
    for (const keyLen of [0, 16, 32, 64, 65, 100]) {
      const key = rnd(keyLen);
      const data = rnd(77);
      expect(hex(hmacSha256(key, data))).toBe(crypto.createHmac('sha256', key).update(data).digest('hex'));
    }
    for (let i = 0; i < 20; i += 1) {
      const ikm = rnd(32);
      const salt = rnd(21);
      const info = rnd(i);
      const mine = hkdfSha256(ikm, salt, info, 32);
      expect(hex(mine)).toBe(Buffer.from(crypto.hkdfSync('sha256', ikm, salt, info, 32)).toString('hex'));
    }
  });
});

describe('AES-256 and GCM', () => {
  it('matches the FIPS 197 AES-256 vector', () => {
    const key = Uint8Array.from(Buffer.from('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'hex'));
    const block = Uint8Array.from(Buffer.from('00112233445566778899aabbccddeeff', 'hex'));
    expect(hex(aes256EncryptBlock(key, block))).toBe('8ea2b7ca516745bfeafc49904b496089');
  });

  it('GCM matches node:crypto for every plaintext length up to 70 bytes and any AAD', () => {
    for (let len = 0; len <= 70; len += 1) {
      const key = rnd(32);
      const iv = rnd(12);
      const plain = rnd(len);
      const aad = rnd(len % 23);
      const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
      cipher.setAAD(aad);
      const expected = Buffer.concat([cipher.update(plain), cipher.final(), cipher.getAuthTag()]);
      expect(hex(aes256GcmEncrypt(key, iv, plain, aad))).toBe(expected.toString('hex'));
    }
  });
});

describe('P-256', () => {
  it('the public point of a scalar matches node:crypto', () => {
    for (let i = 0; i < 5; i += 1) {
      const ecdh = crypto.createECDH('prime256v1');
      ecdh.generateKeys();
      const d = BigInt(`0x${ecdh.getPrivateKey('hex')}`);
      expect(hex(publicPointOf(d))).toBe(ecdh.getPublicKey('hex', 'uncompressed'));
    }
  });

  it('ECDH agrees with node:crypto in both directions', () => {
    for (let i = 0; i < 5; i += 1) {
      const robot = crypto.createECDH('prime256v1');
      robot.generateKeys();
      const robotPoint = new Uint8Array(robot.getPublicKey(null, 'uncompressed'));
      const { publicPoint, shared } = ecdhP256(robotPoint, rnd);
      expect(hex(shared)).toBe(robot.computeSecret(Buffer.from(publicPoint)).toString('hex'));
      const d = BigInt(`0x${robot.getPrivateKey('hex')}`);
      expect(hex(sharedOf(d, publicPoint))).toBe(hex(shared));
    }
  });

  it('refuses a point that is not on the curve or not uncompressed', () => {
    const ecdh = crypto.createECDH('prime256v1');
    ecdh.generateKeys();
    const good = new Uint8Array(ecdh.getPublicKey(null, 'uncompressed'));
    expect(() => decodePoint(good)).not.toThrow();
    const off = Uint8Array.from(good);
    off[64] ^= 1;
    expect(() => decodePoint(off)).toThrow();
    expect(() => decodePoint(good.slice(0, 33))).toThrow();
    const compressed = Uint8Array.from(good);
    compressed[0] = 0x02;
    expect(() => decodePoint(compressed)).toThrow();
  });
});

describe('the whole scheme: the pure path decrypts like the WebCrypto one', () => {
  async function robot() {
    const pair = await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
    const point = new Uint8Array(await subtle.exportKey('raw', pair.publicKey));
    const digest = new Uint8Array(await subtle.digest('SHA-256', point));
    const kid = hex(digest.slice(0, 8));
    return { pair, kid, pub: Buffer.from(point).toString('base64url') };
  }

  async function decrypt(wire, r) {
    const [kid, point, iv, ciphertext] = wire.slice('enc1:'.length).split('.');
    expect(kid).toBe(r.kid);
    const peer = await subtle.importKey('raw', b64urlDecode(point), { name: 'ECDH', namedCurve: 'P-256' }, false, []);
    const shared = await subtle.deriveBits({ name: 'ECDH', public: peer }, r.pair.privateKey, 256);
    const hkdf = await subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
    const enc = new TextEncoder();
    const key = await subtle.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt: enc.encode(CONTEXT), info: enc.encode(`aes-256-gcm|${kid}`) },
      hkdf, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
    );
    const plain = await subtle.decrypt(
      { name: 'AES-GCM', iv: b64urlDecode(iv), additionalData: enc.encode(`${CONTEXT}|${kid}`) },
      key, b64urlDecode(ciphertext),
    );
    return new TextDecoder().decode(plain);
  }

  it('a token encrypted without WebCrypto opens with the robot key', async () => {
    const r = await robot();
    for (const token of ['hf_test0token0for0pure0path00', `hf_${'0123456789'.repeat(25)}abcdef`]) {
      // eslint-disable-next-line no-await-in-loop
      const wire = await encryptForRobot(token, { kid: r.kid, pub: r.pub }, null);
      expect(wire).not.toContain('hf_');
      // eslint-disable-next-line no-await-in-loop
      expect(await decrypt(wire, r)).toBe(token);
    }
  });
});
