// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The student's Hugging Face token, encrypted for the robot (2026-11-13; the
// audited libraries since 2026-11-14).
//
// On an Orange Pi the token travels to `/register_hf_user` over a plain
// websocket on the school network. The robot advertises an EPHEMERAL P-256
// public key on /edubotics/hf_token_state (`enc`, `kid`, `pub`), and the token
// is encrypted for it, so a passive listener on the LAN reads nothing. The wire
// form and every constant are the robot's
// (physical_ai_server/data_processing/hf_token_crypto.py):
//
//   enc1:<kid>.<client public key>.<iv>.<ciphertext>   (each base64url, no padding)
//   ECDH P-256 (the 32-byte x coordinate) → HKDF-SHA256(salt = CONTEXT,
//   info = "aes-256-gcm|" + kid) → AES-256-GCM (12-byte iv, AAD = CONTEXT + "|" + kid)
//
// THE ENCRYPTION ITSELF is hfTokenSeal.js: @noble/curves, @noble/hashes and
// @noble/ciphers (audited, dependency-free, pinned exactly), loaded on demand
// (`loadTokenSeal`) so they stay out of the entry bundle this module rides in
// (robotChannel.js → utils/signOut.js). The same code runs on every page:
// `crypto.subtle` is a [SecureContext] API, absent on a Pi's plain-HTTP page,
// and one implementation everywhere means the Pi's path is the path every
// Windows page exercises too.
//
// physical_ai_server/test/test_hf_token_crypto.py decrypts, in Python, the
// cases __tests__/hfTokenCrypto.test.js writes (hf-token-crypto-cases.json),
// and recomputes its known answers byte for byte.
// Nothing here logs, stores or formats a token.

export const WIRE_PREFIX = 'enc1:';
export const ENC_NAME = 'enc1';
export const CONTEXT = 'edubotics-hf-token:v1';

const KID_SHAPE = /^[0-9a-f]{16}$/;
// A 65-byte uncompressed P-256 point is 87 base64url characters.
const PUB_SHAPE = /^[A-Za-z0-9_-]{87}$/;

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
  return keyFields(raw);
}

function keyFields(raw) {
  if (raw.enc !== ENC_NAME) return null;
  if (typeof raw.kid !== 'string' || !KID_SHAPE.test(raw.kid)) return null;
  if (typeof raw.pub !== 'string' || !PUB_SHAPE.test(raw.pub)) return null;
  return { kid: raw.kid, pub: raw.pub };
}

/**
 * The robot's key as the pi-agent serves it (`GET /api/system/robot-key`'s
 * `key`, VB-03): the same three fields and shapes as the topic's
 * advertisement, or null.
 * @param {unknown} value
 * @returns {null|{kid: string, pub: string}}
 */
export function parseServedKey(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return keyFields(value);
}

let sealModule = null;

/**
 * The lazy chunk with the audited libraries (hfTokenSeal.js). A failed load is
 * not cached: the next push loads again.
 * @returns {Promise<typeof import('./hfTokenSeal')>}
 */
export function loadTokenSeal() {
  if (!sealModule) {
    sealModule = import('./hfTokenSeal').catch((err) => {
      sealModule = null;
      throw err;
    });
  }
  return sealModule;
}

/** The rejection of `encryptForRobot` when its chunk could not be loaded. */
export const SEAL_UNAVAILABLE = 'seal_unavailable';

/**
 * Encrypt `token` for the robot's key. Rejects when the key is malformed, the
 * chunk cannot be loaded (`Error(SEAL_UNAVAILABLE)`), no randomness is
 * available or the robot's point is not on P-256 (the caller turns that into a
 * German sentence, never into a plain token).
 * @param {string} token
 * @param {{kid: string, pub: string}} key from parseRobotKey
 * @returns {Promise<string>} the `enc1:` wire value
 */
export async function encryptForRobot(token, key) {
  if (typeof token !== 'string' || !token) throw new Error('bad_token');
  if (!key || !KID_SHAPE.test(key.kid) || !PUB_SHAPE.test(key.pub)) throw new Error('bad_key');
  let seal;
  try {
    seal = await loadTokenSeal();
  } catch {
    throw new Error(SEAL_UNAVAILABLE);
  }
  return seal.sealToken(token, key);
}
