// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The audited libraries ride a lazy chunk, and no hand-written cryptography is
// left in src/ (2026-11-14). hfTokenCrypto.js rides the entry bundle
// (robotChannel.js → utils/signOut.js), so @noble/* may be imported by
// hfTokenSeal.js alone, and hfTokenSeal.js only through hfTokenCrypto.js's
// dynamic import. A failed load is not cached.

import fs from 'fs';
import path from 'path';
import {
  describe, it, expect, vi, afterEach,
} from 'vitest';

const SRC = path.resolve(__dirname, '../../..');
const HF = path.join('features', 'hfToken');

function sources(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== '__tests__' && e.name !== 'node_modules') sources(full, out);
    } else if (/\.(js|jsx|mjs)$/.test(e.name)) {
      out.push(full);
    }
  }
  return out;
}

const FILES = sources(SRC).map((f) => [path.relative(SRC, f), fs.readFileSync(f, 'utf8')]);

describe('the audited libraries stay in their lazy chunk', () => {
  it('only hfTokenSeal.js imports @noble/*', () => {
    const importers = FILES
      .filter(([, text]) => /(?:from|import)\s*\(?\s*['"]@noble\//.test(text))
      .map(([rel]) => rel);
    expect(importers).toEqual([path.join(HF, 'hfTokenSeal.js')]);
  });

  it('nothing imports hfTokenSeal statically; hfTokenCrypto.js loads it with a dynamic import', () => {
    const statics = FILES
      .filter(([, text]) => /(?:from|import)\s+['"][^'"]*\/hfTokenSeal['"]/.test(text))
      .map(([rel]) => rel);
    expect(statics).toEqual([]);
    const dynamic = FILES
      .filter(([, text]) => /import\(\s*['"][^'"]*\/hfTokenSeal['"]\s*\)/.test(text))
      .map(([rel]) => rel);
    expect(dynamic).toEqual([path.join(HF, 'hfTokenCrypto.js')]);
  });

  it('the three libraries are pinned exactly', () => {
    const pkg = JSON.parse(fs.readFileSync(path.resolve(SRC, '..', 'package.json'), 'utf8'));
    for (const name of ['@noble/curves', '@noble/hashes', '@noble/ciphers']) {
      expect(pkg.dependencies[name]).toMatch(/^\d+\.\d+\.\d+$/);
    }
  });
});

describe('no hand-written cryptography in src/', () => {
  // Fingerprints of a hand-rolled SHA-256, P-256, AES or GHASH: their defining
  // constants. A library import carries none of them in our source.
  const FINGERPRINTS = [
    '6a09e667', // SHA-256 initial hash value
    '428a2f98', // SHA-256 round constant K[0]
    'ffffffff00000001000000000000000000000000ffffffffffffffffffffffff', // the P-256 field prime
    'bce6faada7179e84f3b9cac2fc632551', // the P-256 group order
    '5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b', // the P-256 b
    '6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296', // the P-256 base point x
    '0xe1n', // GHASH's reduction polynomial as a BigInt
  ];

  it('the hand-written module is gone', () => {
    expect(fs.existsSync(path.join(SRC, HF, 'hfTokenCryptoPure.js'))).toBe(false);
  });

  it('no source file carries a primitive’s constants', () => {
    for (const [rel, text] of FILES) {
      const flat = text.toLowerCase().replace(/[\s_,]/g, '');
      for (const mark of FINGERPRINTS) {
        expect([rel, flat.includes(mark)]).toEqual([rel, false]);
      }
    }
  });

  it('nothing in src/ calls WebCrypto: one implementation on every page', () => {
    for (const [rel, text] of FILES) {
      const calls = /\bsubtle\s*\.\s*(?:importKey|generateKey|deriveBits|deriveKey|encrypt|decrypt|digest|exportKey|wrapKey|unwrapKey|sign|verify)\b/;
      expect([rel, calls.test(text)]).toEqual([rel, false]);
    }
  });
});

describe('loadTokenSeal', () => {
  afterEach(() => {
    vi.doUnmock('../hfTokenSeal');
    vi.resetModules();
  });

  it('a failed load is not cached: the next push loads again', async () => {
    vi.resetModules();
    const state = { fail: true };
    vi.doMock('../hfTokenSeal', async (importOriginal) => {
      if (state.fail) throw new Error('Chunk nicht geladen');
      return importOriginal();
    });
    const { loadTokenSeal } = await import('../hfTokenCrypto');
    await expect(loadTokenSeal()).rejects.toThrow();
    state.fail = false;
    vi.resetModules();
    const mod = await loadTokenSeal();
    expect(typeof mod.sealToken).toBe('function');
    expect(await loadTokenSeal()).toBe(mod);
  });
});
