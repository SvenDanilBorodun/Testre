// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The student's Hugging Face token, encrypted for the robot (2026-11-13).
//
// On an Orange Pi the token travels to `/register_hf_user` over a plain
// websocket on the school network. The robot advertises an EPHEMERAL P-256
// public key on /edubotics/hf_token_state (`enc`, `kid`, `pub`), and this module
// encrypts the token for it with WebCrypto, so a passive listener on the LAN
// reads nothing. The wire form and every constant are the robot's
// (physical_ai_server/data_processing/hf_token_crypto.py):
//
//   enc1:<kid>.<client public key>.<iv>.<ciphertext>   (each base64url, no padding)
//   ECDH P-256 (the 32-byte x coordinate) → HKDF-SHA256(salt = CONTEXT,
//   info = "aes-256-gcm|" + kid) → AES-256-GCM (12-byte iv, AAD = CONTEXT + "|" + kid)
//
// physical_ai_server/test/test_hf_token_crypto.py decrypts, in Python, the
// cases __tests__/hfTokenCrypto.test.js writes (hf-token-crypto-cases.json).
// Nothing here logs, stores or formats a token.
//
// TWO IMPLEMENTATIONS OF ONE SCHEME, chosen by what the page may use:
// `crypto.subtle` is a [SecureContext] API, present on a Windows page
// (http://localhost is a secure context) and ABSENT on a Pi's page (plain HTTP
// at a LAN address is not) — exactly where the encryption matters. There the
// same computation runs in hfTokenCryptoPure.js (BigInt P-256, SHA-256, HKDF,
// AES-256-GCM; `crypto.getRandomValues` is available in every context).

import { aes256GcmEncrypt, ecdhP256, hkdfSha256 } from './hfTokenCryptoPure';

export const WIRE_PREFIX = 'enc1:';
export const ENC_NAME = 'enc1';
export const CONTEXT = 'edubotics-hf-token:v1';

const KID_SHAPE = /^[0-9a-f]{16}$/;
// A 65-byte uncompressed P-256 point is 87 base64url characters.
const PUB_SHAPE = /^[A-Za-z0-9_-]{87}$/;
const IV_LEN = 12;

const utf8 = (text) => new TextEncoder().encode(text);

export function b64url(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = '';
  for (let i = 0; i < view.length; i += 1) binary += String.fromCharCode(view[i]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(text) {
  const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((text.length + 3) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

/**
 * The robot's key from one raw /edubotics/hf_token_state payload, or null when
 * the robot advertises none (an older image, an image without the token slot,
 * one without the crypto package) or the advertisement is malformed.
 * @param {string} data raw std_msgs/String payload
 * @returns {null|{kid: string, pub: string}}
 */
export function parseRobotKey(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || raw.accepts !== true) return null;
  if (raw.enc !== ENC_NAME) return null;
  if (typeof raw.kid !== 'string' || !KID_SHAPE.test(raw.kid)) return null;
  if (typeof raw.pub !== 'string' || !PUB_SHAPE.test(raw.pub)) return null;
  return { kid: raw.kid, pub: raw.pub };
}

function randomBytes(n) {
  if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== 'function') {
    throw new Error('random_unavailable');
  }
  return globalThis.crypto.getRandomValues(new Uint8Array(n));
}

/** The pure path (no WebCrypto): the same wire value, computed in JS. */
function encryptPure(token, key) {
  const { publicPoint, shared } = ecdhP256(b64urlDecode(key.pub), randomBytes);
  const aesKey = hkdfSha256(shared, utf8(CONTEXT), utf8(`aes-256-gcm|${key.kid}`), 32);
  const iv = randomBytes(IV_LEN);
  const ciphertext = aes256GcmEncrypt(aesKey, iv, utf8(token), utf8(`${CONTEXT}|${key.kid}`));
  return `${WIRE_PREFIX}${key.kid}.${b64url(publicPoint)}.${b64url(iv)}.${b64url(ciphertext)}`;
}

/**
 * Encrypt `token` for the robot's key. Rejects when the key is malformed or no
 * randomness is available (the caller turns that into a German sentence).
 * WebCrypto when the page has it, else the pure path (a Pi's plain-HTTP page).
 * @param {string} token
 * @param {{kid: string, pub: string}} key from parseRobotKey
 * @param {SubtleCrypto|null} [subtle] injected (tests); null forces the pure path
 * @returns {Promise<string>} the `enc1:` wire value
 */
export async function encryptForRobot(token, key, subtle) {
  if (!key || !KID_SHAPE.test(key.kid) || !PUB_SHAPE.test(key.pub)) throw new Error('bad_key');
  const s = subtle === null ? null
    : subtle || (globalThis.crypto && globalThis.crypto.subtle) || null;
  if (!s) return encryptPure(token, key);
  const robotPublic = await s.importKey(
    'raw', b64urlDecode(key.pub), { name: 'ECDH', namedCurve: 'P-256' }, false, [],
  );
  const mine = await s.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const shared = await s.deriveBits({ name: 'ECDH', public: robotPublic }, mine.privateKey, 256);
  const hkdf = await s.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
  const aesKey = await s.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt: utf8(CONTEXT), info: utf8(`aes-256-gcm|${key.kid}`) },
    hkdf,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt'],
  );
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(IV_LEN));
  const ciphertext = await s.encrypt(
    { name: 'AES-GCM', iv, additionalData: utf8(`${CONTEXT}|${key.kid}`) },
    aesKey,
    utf8(token),
  );
  const point = await s.exportKey('raw', mine.publicKey);
  return `${WIRE_PREFIX}${key.kid}.${b64url(point)}.${b64url(iv)}.${b64url(ciphertext)}`;
}
