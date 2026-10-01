// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Every string of the Aufnahme page is German (Rule §1), carries no icon glyph
// (owner decision D7; noIconGlyphs.test.js fences the source as well), and
// uses the page's vocabulary (F6a): „Leader-Arm" / „Follower-Arm", never
// Leitarm/Folgearm; key labels as words, never ⇧/⌘ (H9).

import { OUTCOMES } from '../../../../features/tasks/recordSession';
import RECORD_COPY, {
  KEEP_DROPPED_AFTER_REDO,
  armNameDe,
  cameraNameDe,
  clockDe,
  gbDe,
  mmss,
  numberDe,
} from '../recordCopy';

// The vocabulary of components/__tests__/germanUi.test.js (kept in step with
// it): English words that are not also German UI words.
const ENGLISH = /\b(the|and|please|select|selected|selectable|failed|failed to|loading|load|cancel|browse|directory|folder|file|merge|output|add|remove|refresh|empty|found|detected|usage|used|free|total|user|dataset|datasets|model|instruction|parent|home|current|path|already|exists|choose|different|existing|more|only|navigation|disabled|repository name|cannot|must|characters|letters|numbers|configuration|type|local|full|will|be|saved|switch|while|in progress|memory|storage|almost|high|very|consider|closing|applications|cleaning)\b/i;

// The glyph ranges of components/icons/__tests__/noIconGlyphs.test.js.
const BANNED = [
  [0x1F000, 0x1FAFF], [0x2300, 0x23FF], [0x25A0, 0x25FF], [0x2600, 0x27BF], [0x2195, 0x21FF],
  [0x2900, 0x297F], [0x2B00, 0x2BFF], [0x2139, 0x2139], [0x22EF, 0x22EF], [0xFE0F, 0xFE0F],
  [0x20E3, 0x20E3], [0x2460, 0x24FF], [0x27C0, 0x27FF], [0x2800, 0x28FF],
];
const hasBannedGlyph = (text) => [...text].some((ch) => {
  const cp = ch.codePointAt(0);
  return BANNED.some(([lo, hi]) => cp >= lo && cp <= hi);
});

const SAMPLE_ARGS = [3, 5, 20];

// Every string reachable from the copy object — functions are called with
// sample arguments (numbers, and a camera role for the camera sentences).
function allStrings(node, path = 'RECORD_COPY', out = []) {
  if (typeof node === 'string') {
    out.push({ path, text: node });
  } else if (typeof node === 'function') {
    const argSets = [SAMPLE_ARGS, ['scene', 11.2, 30], ['gripper', 12, 30], ['leader', 40, 30]];
    for (const args of argSets) {
      let value;
      try { value = node(...args); } catch { value = null; }
      allStrings(value, `${path}(${args.join(',')})`, out);
    }
  } else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) allStrings(v, `${path}.${k}`, out);
  }
  return out;
}

const STRINGS = allStrings(RECORD_COPY);

describe('recordCopy', () => {
  it('has strings to check (the walker is not vacuous)', () => {
    expect(STRINGS.length).toBeGreaterThan(150);
  });

  it('carries no English UI word', () => {
    const hits = STRINGS.filter(({ text }) => ENGLISH.test(text)).map(({ path, text }) => `${path}: ${text}`);
    expect(hits).toEqual([]);
  });

  it('carries no icon glyph', () => {
    const hits = STRINGS.filter(({ text }) => hasBannedGlyph(text)).map(({ path, text }) => `${path}: ${text}`);
    expect(hits).toEqual([]);
  });

  it('uses the page vocabulary: Leader-Arm / Follower-Arm, never Leitarm / Folgearm', () => {
    const hits = STRINGS.filter(({ text }) => /leitarm|folgearm/i.test(text)).map(({ path }) => path);
    expect(hits).toEqual([]);
    expect(RECORD_COPY.overlay.warmLine).toContain('Leader-Arm');
    expect(RECORD_COPY.problem.followerStalled).toContain('Follower-Arm');
  });

  it('writes umlauts, never their transliterations', () => {
    const TRANSLITERATED = /(moeglich|moechte|\bfuer\b|aufloesung|pruef|enthaelt|schueler|zurueck|naechst|oeffentlich|loesch|groesse|hoechstens|gueltig|aender)/i;
    const hits = STRINGS.filter(({ text }) => TRANSLITERATED.test(text)).map(({ path }) => path);
    expect(hits).toEqual([]);
  });

  it('names the keys as words (H9)', () => {
    expect(RECORD_COPY.kbd).toEqual({
      space: 'Leertaste', right: '→', left: '←', end: 'Strg+Umschalt+X', esc: 'Esc',
    });
  });

  it('pins the sentences the owner decided', () => {
    expect(KEEP_DROPPED_AFTER_REDO)
      .toBe('Diese Episode war zu kurz nach dem Wiederholen und wird nicht gespeichert.');
    expect(RECORD_COPY.problem.leaderNotActivated)
      .toBe('Der Roboter ist nicht aktiviert, der Leader-Arm sendet keine Daten. Aktiviere ihn auf der Startseite.');
    expect(RECORD_COPY.problem.leaderNotActivated.endsWith(`${RECORD_COPY.problem.homeLabel}.`)).toBe(true);
    expect(RECORD_COPY.estimate(125, 5))
      .toBe('≈ 2:05 min für 5 Episoden. Aufwärmen nur einmal am Anfang, nach der letzten Episode kein Zurücksetzen.');
    expect(RECORD_COPY.estimate(20, 1)).toContain('für 1 Episode.');
    expect(RECORD_COPY.overlay.next(2, 5, 20)).toEqual({
      pre: 'Als Nächstes: ', bold: 'Episode 2 von 5', post: ' · 20 s Aufnahme',
    });
    expect(RECORD_COPY.overlay.resetKicker(1)).toBe('Nach Episode 1');
    expect(RECORD_COPY.question.endTitle(3)).toBe('Episode 3 ist noch nicht fertig.');
  });

  it('formats numbers, sizes and times the German way', () => {
    expect(numberDe(11.2)).toBe('11,2');
    expect(numberDe(30)).toBe('30');
    expect(numberDe(null)).toBe('–');
    expect(gbDe(2_400_000_000)).toBe('2,4 GB');
    expect(mmss(75)).toBe('01:15');
    expect(clockDe(new Date(2026, 8, 29, 9, 5).getTime())).toBe('09:05');
    expect(clockDe(undefined)).toBe('');
    expect(cameraNameDe('gripper')).toBe('Greifer-Kamera');
    expect(cameraNameDe('scene')).toBe('Szenen-Kamera');
    expect(cameraNameDe('wrist')).toBe('Kamera „wrist“');
    expect(armNameDe('leader')).toBe('Leader-Arm');
    expect(armNameDe('follower')).toBe('Follower-Arm');
  });

  it('the camera and arm sentences read with the numbers in German', () => {
    expect(RECORD_COPY.problem.cameraSlow('scene', 11.2, 30))
      .toBe('Die Szenen-Kamera liefert nur 11,2 statt 30 Bilder pro Sekunde. Aufnehmen geht, die Videos '
        + 'ruckeln aber. Steck die Kamera direkt am PC ein, nicht am USB-Hub.');
    expect(RECORD_COPY.problem.cameraStalled('gripper'))
      .toBe('Die Greifer-Kamera sendet keine Bilder. Prüfe das Kabel. Hilft das nicht, starte die Umgebung neu.');
    expect(RECORD_COPY.problem.armSlow('leader', 40, 30))
      .toBe('Der Leader-Arm meldet nur 40 statt mindestens 30 Messungen pro Sekunde. Die Aufnahme kann ruckeln.');
  });
});

describe('round 5 rows and notes', () => {
  it('every discarded outcome has its own label', () => {
    for (const outcome of OUTCOMES.filter((o) => o !== 'saved')) {
      expect(RECORD_COPY.session.outcome[outcome]).toEqual(expect.any(String));
    }
    expect(RECORD_COPY.session.outcome.source).toBe('Abgebrochen, verworfen');
    expect(RECORD_COPY.session.outcome.gap).toBe('Signalaussetzer, wiederholt');
  });

  it('the Q8 reason and „nothing saved" in one sentence', () => {
    expect(RECORD_COPY.note.nothingSavedAfterRedo).toBe(
      'Beendet. Diese Episode war zu kurz nach dem Wiederholen und wird nicht gespeichert. Es wurde '
      + 'keine Episode gespeichert, also wird nichts hochgeladen.',
    );
  });
});
