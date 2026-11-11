// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The token's encryption for the robot, built from AUDITED libraries
// (2026-11-14): Paul Miller's dependency-free @noble/curves (P-256 ECDH, with
// its own point validation), @noble/hashes (SHA-256, HKDF) and @noble/ciphers
// (AES-256-GCM), each pinned exactly in package.json and recorded by integrity
// hash in package-lock.json. This module holds no cryptographic primitive of
// its own; it only wires the three libraries into the robot's scheme
// (physical_ai_server/data_processing/hf_token_crypto.py):
//
//   ECDH P-256 (the 32-byte x coordinate) → HKDF-SHA256(salt = CONTEXT,
//   info = "aes-256-gcm|" + kid) → AES-256-GCM (12-byte iv,
//   AAD = CONTEXT + "|" + kid, ciphertext || 16-byte tag)
//
// ONE implementation on every page: a Windows page (http://localhost, a secure
// context) runs the same code as a Pi's plain-HTTP page, where `crypto.subtle`
// does not exist. Only `crypto.getRandomValues` is needed, and every context
// has it (noble's randomBytes refuses to run without it).
//
// A LAZY CHUNK: only hfTokenCrypto.js::loadTokenSeal imports this module, and
// with a dynamic import, so the three libraries stay out of the entry bundle
// (package.json's no-restricted-imports keeps every other file off @noble/*;
// __tests__/hfTokenSeal.lazy.test.js fences both). Nothing here logs, stores
// or formats a token.

import { gcm } from '@noble/ciphers/aes.js';
import { p256 } from '@noble/curves/nist.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { randomBytes } from '@noble/hashes/utils.js';

import { b64url, b64urlDecode, CONTEXT, WIRE_PREFIX } from './hfTokenCrypto';

export const IV_LEN = 12;
const POINT_LEN = 65;
const KEY_LEN = 32;

const utf8 = (text) => new TextEncoder().encode(text);

/**
 * The robot's advertised point as bytes: exactly an UNCOMPRESSED SEC1 point
 * (65 bytes, 0x04 first) — what the robot publishes. Whether it lies on P-256
 * (and is not the identity) is noble's check, in getSharedSecret.
 */
export function robotPointOf(pub) {
  const point = b64urlDecode(pub);
  if (point.length !== POINT_LEN || point[0] !== 0x04) throw new Error('bad_key');
  return point;
}

/** Our ephemeral public point, uncompressed (65 bytes). */
export function clientPointOf(secretKey) {
  return p256.getPublicKey(secretKey, false);
}

/**
 * The ECDH shared secret: the 32-byte x coordinate of secretKey · peerPoint
 * (what WebCrypto's deriveBits(256) and the robot's `exchange(ECDH())`
 * return). noble validates `peerPoint` (on the curve, not the identity).
 */
export function sharedX(secretKey, peerPoint) {
  return p256.getSharedSecret(secretKey, peerPoint, true).slice(1);
}

/**
 * The `enc1:` wire value of `token` for the robot `key`, from a GIVEN
 * ephemeral secret and iv. Deterministic: the known-answer tests run it; the
 * product calls `sealToken`, which draws both fresh. `context` is the HKDF
 * salt and the head of the AAD (hfTokenCrypto's ENVELOPE_CONTEXTS; the HF
 * token's by default).
 */
export function sealTokenWith(token, key, ephemeralSecret, iv, context = CONTEXT) {
  if (typeof token !== 'string' || !token) throw new Error('bad_token');
  if (!(iv instanceof Uint8Array) || iv.length !== IV_LEN) throw new Error('bad_iv');
  if (typeof context !== 'string' || !context) throw new Error('bad_context');
  const robotPoint = robotPointOf(key.pub);
  const shared = sharedX(ephemeralSecret, robotPoint);
  const aesKey = hkdf(sha256, shared, utf8(context), utf8(`aes-256-gcm|${key.kid}`), KEY_LEN);
  shared.fill(0);
  const ciphertext = gcm(aesKey, iv, utf8(`${context}|${key.kid}`)).encrypt(utf8(token));
  aesKey.fill(0);
  const clientPoint = clientPointOf(ephemeralSecret);
  return `${WIRE_PREFIX}${key.kid}.${b64url(clientPoint)}.${b64url(iv)}.${b64url(ciphertext)}`;
}

/**
 * Encrypt `token` for the robot `key` with a FRESH ephemeral key pair and iv
 * (one per push: no nonce reuse, no long-lived secret on this side, and a
 * value the robot never sees twice: it refuses a repeat, VC-05).
 * Throws when no randomness is available or the key is not a valid point.
 */
export function sealToken(token, key, context = CONTEXT) {
  const secret = p256.utils.randomSecretKey();
  try {
    return sealTokenWith(token, key, secret, randomBytes(IV_LEN), context);
  } finally {
    secret.fill(0);
  }
}
