// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The per-student token feature is one of the few places a student reads the
// words „Token" and „Hugging Face" every day, so the table is held to the
// repository's German-UI rules directly: real umlauts, no transliteration, no
// en-dash, no icon glyph, nothing shaped like a token, and a sentence for every
// key the card and the channel ask for.

import HF_TOKEN_COPY, { hfCopy } from '../hfTokenCopy';

const ENTRIES = Object.entries(HF_TOKEN_COPY);

// ae/oe/ue and „ss" standing in for an umlaut or ß in words that have one.
const TRANSLITERATION = /\b\w*(?:Ueber|ueber|Aender|aender|Loesch|loesch|Pruef|pruef|Schliess|schliess|Waehl|waehl|Fuer|fuer|Koenn|koenn|Muess|muess|Spaet|spaet|Zurueck|zurueck|Gueltig|gueltig|Uebertrag|uebertrag)\w*/;
// Icon ranges the repository bans in UI text (components/icons/__tests__/noIconGlyphs).
const ICON_GLYPHS = /[①-⓿☀-➿⠀-⣿\u{1F300}-\u{1FAFF}]/u;

describe('hfTokenCopy', () => {
  it('has a non-empty German sentence for every key', () => {
    // 41 card sentences (spec B7) + 3 for the channel to the robot.
    expect(ENTRIES.length).toBeGreaterThanOrEqual(44);
    for (const [key, text] of ENTRIES) {
      expect(typeof text).toBe('string');
      expect(text.trim().length).toBeGreaterThan(0);
      expect(key).toMatch(/^[a-z]+(?:\.[A-Za-z0-9]+)+$/);
    }
  });

  it('carries the keys the card, the channel and the start block read', () => {
    for (const key of [
      'card.title', 'card.none.body', 'card.none.step1', 'card.none.step4', 'card.input.placeholder',
      'card.input.aria', 'card.save', 'card.saving', 'card.stored.as', 'card.stored.hint', 'card.stored.checked',
      'card.replace', 'card.remove', 'card.removeConfirm', 'card.removeAbort', 'card.verify', 'card.retry',
      'card.reload', 'card.unusable', 'card.unsupported', 'card.unavailable', 'card.loadError', 'card.unknown',
      'card.accountChanged', 'card.pill.active', 'card.pill.working', 'card.pill.waiting', 'card.pill.failed',
      'card.pill.noLink', 'card.pill.takenOver', 'card.pill.notAccepted', 'card.pill.none', 'card.pill.stored',
      'card.waitingNote', 'card.takenOverNote', 'card.failedNote', 'card.jetsonNote', 'card.errorGeneric',
      'card.removeError', 'robot.noLink', 'robot.timeout', 'robot.failed',
    ]) {
      expect(HF_TOKEN_COPY).toHaveProperty([key]);
    }
  });

  it('uses real umlauts, never ae/oe/ue/ss in their place', () => {
    for (const [key, text] of ENTRIES) {
      expect(`${key}: ${TRANSLITERATION.exec(text)?.[0] ?? ''}`).toBe(`${key}: `);
    }
    expect(HF_TOKEN_COPY['card.none.step1']).toContain('Öffne');
    expect(HF_TOKEN_COPY['card.stored.checked']).toBe('Zuletzt geprüft');
  });

  it('uses no en-dash, no icon glyph and no token-shaped string', () => {
    for (const [key, text] of ENTRIES) {
      expect(`${key}: ${text.includes('–')}`).toBe(`${key}: false`);
      expect(`${key}: ${ICON_GLYPHS.test(text)}`).toBe(`${key}: false`);
      expect(`${key}: ${/hf_[A-Za-z0-9_-]{8,}/.test(text)}`).toBe(`${key}: false`);
    }
  });

  it('writes quotes the German way', () => {
    expect(HF_TOKEN_COPY['card.none.step2']).toBe('Gehe zu „Settings“ und dann zu „Access Tokens“.');
    expect(HF_TOKEN_COPY['card.none.step3']).toContain('„Write“');
    // no straight double quote anywhere
    for (const [key, text] of ENTRIES) {
      expect(`${key}: ${text.includes('"')}`).toBe(`${key}: false`);
    }
  });

  it('hfCopy answers a known key and refuses an unknown one', () => {
    expect(hfCopy('card.save')).toBe('Token speichern');
    expect(() => hfCopy('card.nope')).toThrow(/Unknown hf-token copy key/);
  });

  it('is frozen', () => {
    expect(Object.isFrozen(HF_TOKEN_COPY)).toBe(true);
  });
});

describe('hfTokenCopy — what the card says (review c, e)', () => {
  it('names every cause of a busy robot, downloads and list fetches included', () => {
    expect(HF_TOKEN_COPY['card.waitingNote']).toBe(
      'Der Roboter ist gerade beschäftigt: Er nimmt auf, lädt etwas hoch oder herunter oder fragt eine Liste '
      + 'bei Hugging Face ab. Das Token wird danach übertragen.',
    );
  });
});
