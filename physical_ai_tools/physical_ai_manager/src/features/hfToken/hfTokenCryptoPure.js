// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The token's encryption for the robot WITHOUT WebCrypto (2026-11-13).
//
// WHY THIS EXISTS: `crypto.subtle` is a [SecureContext] API. A Pi's page is
// served over plain HTTP at a LAN address (http://192.168.x.y,
// http://edubotics.local), which is NOT a secure context, so every browser
// leaves `crypto.subtle` undefined there — exactly where the encryption matters
// (a Windows page is http://localhost, which IS secure). `crypto.getRandomValues`
// stays available everywhere, so this module computes the very same scheme as
// hfTokenCrypto.js's WebCrypto path with BigInt and typed arrays:
//
//   ECDH P-256 (the 32-byte x coordinate) → HKDF-SHA256 → AES-256-GCM.
//
// Scope: ONE encryption per token push, with a fresh ephemeral key pair, so
// there is no long-lived secret here, no decryption, no oracle and no nonce
// reuse (a new key per message). Timing side channels do not apply (nothing an
// attacker can query runs here). Correctness is held, primitive by primitive,
// against node:crypto over random vectors and end to end against WebCrypto and
// the robot's Python `cryptography` (__tests__/hfTokenCryptoPure.test.js,
// hf-token-crypto-cases.json, physical_ai_server/test/test_hf_token_crypto.py).

// ── SHA-256 (FIPS 180-4) ─────────────────────────────────────────────────────

const K256 = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotr = (x, n) => (x >>> n) | (x << (32 - n));

export function sha256(data) {
  const msg = data instanceof Uint8Array ? data : new Uint8Array(data);
  const bitLen = msg.length * 8;
  const padded = new Uint8Array(((msg.length + 9 + 63) >> 6) << 6);
  padded.set(msg);
  padded[msg.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000));
  view.setUint32(padded.length - 4, bitLen >>> 0);
  const h = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const w = new Uint32Array(64);
  for (let off = 0; off < padded.length; off += 64) {
    for (let i = 0; i < 16; i += 1) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i += 1) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0;
    h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0; h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
  }
  const out = new Uint8Array(32);
  const ov = new DataView(out.buffer);
  for (let i = 0; i < 8; i += 1) ov.setUint32(i * 4, h[i]);
  return out;
}

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

export function hmacSha256(key, data) {
  let k = key.length > 64 ? sha256(key) : key;
  const block = new Uint8Array(64);
  block.set(k);
  const ipad = block.map((x) => x ^ 0x36);
  const opad = block.map((x) => x ^ 0x5c);
  k = null;
  return sha256(concat(opad, sha256(concat(ipad, data))));
}

/** HKDF-SHA256 (RFC 5869) for an output of at most 32 bytes. */
export function hkdfSha256(ikm, salt, info, length = 32) {
  if (length > 32) throw new Error('hkdf_length');
  const prk = hmacSha256(salt, ikm);
  return hmacSha256(prk, concat(info, new Uint8Array([1]))).slice(0, length);
}

// ── AES-256 (FIPS 197), encryption direction only ────────────────────────────

const SBOX = (() => {
  const sbox = new Uint8Array(256);
  let p = 1;
  let q = 1;
  do {
    p = p ^ ((p << 1) & 0xff) ^ (p & 0x80 ? 0x1b : 0);
    q ^= q << 1; q ^= q << 2; q ^= q << 4; q &= 0xff;
    if (q & 0x80) q ^= 0x09;
    const x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4));
    sbox[p] = (x ^ 0x63) & 0xff;
  } while (p !== 1);
  sbox[0] = 0x63;
  return sbox;
})();

const xtime = (b) => ((b << 1) ^ (b & 0x80 ? 0x1b : 0)) & 0xff;

function expandKey256(key) {
  const w = new Uint8Array(240);
  w.set(key);
  let rcon = 1;
  for (let i = 32; i < 240; i += 4) {
    let t0 = w[i - 4]; let t1 = w[i - 3]; let t2 = w[i - 2]; let t3 = w[i - 1];
    if (i % 32 === 0) {
      [t0, t1, t2, t3] = [SBOX[t1] ^ rcon, SBOX[t2], SBOX[t3], SBOX[t0]];
      rcon = xtime(rcon);
    } else if (i % 32 === 16) {
      [t0, t1, t2, t3] = [SBOX[t0], SBOX[t1], SBOX[t2], SBOX[t3]];
    }
    w[i] = w[i - 32] ^ t0; w[i + 1] = w[i - 31] ^ t1; w[i + 2] = w[i - 30] ^ t2; w[i + 3] = w[i - 29] ^ t3;
  }
  return w;
}

function aesEncryptBlock(w, input) {
  const s = Uint8Array.from(input);
  for (let i = 0; i < 16; i += 1) s[i] ^= w[i];
  for (let round = 1; round <= 14; round += 1) {
    for (let i = 0; i < 16; i += 1) s[i] = SBOX[s[i]];
    // ShiftRows (state is column-major: s[r + 4c])
    let t = s[1]; s[1] = s[5]; s[5] = s[9]; s[9] = s[13]; s[13] = t;
    t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
    t = s[15]; s[15] = s[11]; s[11] = s[7]; s[7] = s[3]; s[3] = t;
    if (round !== 14) {
      for (let c = 0; c < 16; c += 4) {
        const a0 = s[c]; const a1 = s[c + 1]; const a2 = s[c + 2]; const a3 = s[c + 3];
        const all = a0 ^ a1 ^ a2 ^ a3;
        s[c] ^= all ^ xtime(a0 ^ a1);
        s[c + 1] ^= all ^ xtime(a1 ^ a2);
        s[c + 2] ^= all ^ xtime(a2 ^ a3);
        s[c + 3] ^= all ^ xtime(a3 ^ a0);
      }
    }
    for (let i = 0; i < 16; i += 1) s[i] ^= w[round * 16 + i];
  }
  return s;
}

export function aes256EncryptBlock(key, block) {
  return aesEncryptBlock(expandKey256(key), block);
}

// ── GCM (NIST SP 800-38D) with a 96-bit IV ───────────────────────────────────

const toBig = (bytes) => bytes.reduce((acc, b) => (acc << 8n) | BigInt(b), 0n);
function fromBig(value, len) {
  const out = new Uint8Array(len);
  let v = value;
  for (let i = len - 1; i >= 0; i -= 1) { out[i] = Number(v & 0xffn); v >>= 8n; }
  return out;
}

const R_GCM = 0xe1n << 120n;
function gfMul(x, y) {
  let z = 0n;
  let v = y;
  for (let i = 127n; i >= 0n; i -= 1n) {
    if ((x >> i) & 1n) z ^= v;
    v = v & 1n ? (v >> 1n) ^ R_GCM : v >> 1n;
  }
  return z;
}

function ghash(h, data) {
  let y = 0n;
  for (let off = 0; off < data.length; off += 16) {
    const block = new Uint8Array(16);
    block.set(data.subarray(off, off + 16));
    y = gfMul(y ^ toBig(block), h);
  }
  return y;
}

const padTo16 = (bytes) => concat(bytes, new Uint8Array((16 - (bytes.length % 16)) % 16));

/** AES-256-GCM encryption with a 12-byte IV; returns ciphertext || 16-byte tag. */
export function aes256GcmEncrypt(key, iv, plaintext, aad) {
  if (key.length !== 32 || iv.length !== 12) throw new Error('gcm_params');
  const w = expandKey256(key);
  const h = toBig(aesEncryptBlock(w, new Uint8Array(16)));
  const j0 = concat(iv, new Uint8Array([0, 0, 0, 1]));
  const ct = new Uint8Array(plaintext.length);
  const counter = Uint8Array.from(j0);
  for (let off = 0; off < plaintext.length; off += 16) {
    const view = new DataView(counter.buffer);
    view.setUint32(12, (view.getUint32(12) + 1) >>> 0);
    const ks = aesEncryptBlock(w, counter);
    for (let i = 0; i < 16 && off + i < plaintext.length; i += 1) ct[off + i] = plaintext[off + i] ^ ks[i];
  }
  const lengths = new Uint8Array(16);
  const lv = new DataView(lengths.buffer);
  lv.setUint32(4, (aad.length * 8) >>> 0);
  lv.setUint32(12, (ct.length * 8) >>> 0);
  const s = ghash(h, concat(padTo16(aad), padTo16(ct), lengths));
  const tag = fromBig(toBig(aesEncryptBlock(w, j0)) ^ s, 16);
  return concat(ct, tag);
}

// ── P-256 (SEC 2 / FIPS 186-4) ───────────────────────────────────────────────

const P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffffn;
const N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n;
const B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604bn;
const GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296n;
const GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5n;

const mod = (a, m = P) => { const r = a % m; return r >= 0n ? r : r + m; };
function modPow(base, exp, m) {
  let result = 1n;
  let b = mod(base, m);
  let e = exp;
  while (e > 0n) {
    if (e & 1n) result = (result * b) % m;
    b = (b * b) % m;
    e >>= 1n;
  }
  return result;
}
const inv = (a) => modPow(a, P - 2n, P);

// Jacobian coordinates; the point at infinity is z = 0.
function jDouble([x, y, z]) {
  if (z === 0n || y === 0n) return [0n, 1n, 0n];
  const ysq = mod(y * y);
  const s = mod(4n * x * ysq);
  const zsq = mod(z * z);
  const m = mod(3n * (x - zsq) * (x + zsq)); // a = -3
  const nx = mod(m * m - 2n * s);
  const ny = mod(m * (s - nx) - 8n * ysq * ysq);
  const nz = mod(2n * y * z);
  return [nx, ny, nz];
}

function jAdd(p1, p2) {
  if (p1[2] === 0n) return p2;
  if (p2[2] === 0n) return p1;
  const [x1, y1, z1] = p1;
  const [x2, y2, z2] = p2;
  const z1z1 = mod(z1 * z1);
  const z2z2 = mod(z2 * z2);
  const u1 = mod(x1 * z2z2);
  const u2 = mod(x2 * z1z1);
  const s1 = mod(y1 * z2 * z2z2);
  const s2 = mod(y2 * z1 * z1z1);
  if (u1 === u2) return s1 === s2 ? jDouble(p1) : [0n, 1n, 0n];
  const hh = mod(u2 - u1);
  const r = mod(s2 - s1);
  const h2 = mod(hh * hh);
  const h3 = mod(h2 * hh);
  const nx = mod(r * r - h3 - 2n * u1 * h2);
  const ny = mod(r * (u1 * h2 - nx) - s1 * h3);
  const nz = mod(hh * z1 * z2);
  return [nx, ny, nz];
}

function scalarMul(k, [x, y]) {
  let result = [0n, 1n, 0n];
  let addend = [x, y, 1n];
  let e = k;
  while (e > 0n) {
    if (e & 1n) result = jAdd(result, addend);
    addend = jDouble(addend);
    e >>= 1n;
  }
  if (result[2] === 0n) throw new Error('point_at_infinity');
  const zi = inv(result[2]);
  const zi2 = mod(zi * zi);
  return [mod(result[0] * zi2), mod(result[1] * zi2 * zi)];
}

/** The affine point of a 65-byte uncompressed SEC1 encoding, validated on P-256. */
export function decodePoint(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length !== 65 || bytes[0] !== 0x04) throw new Error('bad_point');
  const x = toBig(bytes.subarray(1, 33));
  const y = toBig(bytes.subarray(33, 65));
  if (x >= P || y >= P || mod(y * y) !== mod(x * x * x - 3n * x + B)) throw new Error('bad_point');
  return [x, y];
}

const encodePoint = ([x, y]) => concat(new Uint8Array([4]), fromBig(x, 32), fromBig(y, 32));

/** A fresh private scalar in [1, n-1] from crypto.getRandomValues. */
function randomScalar(random) {
  for (;;) {
    const d = toBig(random(32));
    if (d > 0n && d < N) return d;
  }
}

/**
 * The ephemeral half of an ECDH P-256 exchange with `peerPoint` (65 bytes):
 * `{ publicPoint: Uint8Array(65), shared: Uint8Array(32) }`, `shared` being
 * the x coordinate (what WebCrypto's deriveBits(256) and the robot's
 * `exchange(ECDH())` return).
 */
export function ecdhP256(peerPoint, random) {
  const peer = decodePoint(peerPoint);
  const d = randomScalar(random);
  const publicPoint = encodePoint(scalarMul(d, [GX, GY]));
  const shared = fromBig(scalarMul(d, peer)[0], 32);
  return { publicPoint, shared };
}

/** Tests only: the public point of a private scalar. */
export function publicPointOf(d) {
  return encodePoint(scalarMul(d, [GX, GY]));
}

/** Tests only: the shared x coordinate of a private scalar and a peer point. */
export function sharedOf(d, peerPoint) {
  return fromBig(scalarMul(d, decodePoint(peerPoint))[0], 32);
}
