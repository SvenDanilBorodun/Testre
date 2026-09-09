/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-41 + RS-56(tooltips) — the editor-side half of two defects:
//
//  * RS-41: blocks/destinations.js::nameValidator had no CHARACTER CLASS while
//    the server (handlers/destinations.py::_DESTINATION_NAME_RE) enforces
//    ^[A-Za-zÄÖÜäöüß0-9 _\-]{1,40}$ and raises the German „Ungültiger
//    Ziel-Name." which ABORTS the run. So a name the editor happily accepted
//    only failed at RUN time, far from the block that caused it.
//    The 24-char cap is DELIBERATE (CLAUDE.md: "Destination-pin names cap at 24
//    chars in React (the Blockly field's NAME_MAX_LEN); trajectory names use the
//    full 1-40 backend range — two frontend validators over one backend regex")
//    and is pinned here so nobody "fixes" it up to 40.
//
//  * Tooltips: several student-facing blocks shipped with none at all.
//
// The existing components/Workshop/__tests__/destinations.driveTo.test.js covers
// a DIFFERENT concern on the same module (the „fahre dorthin" button bridge).

import * as Blockly from 'blockly/core';
import 'blockly/blocks';

import { registerDestinationBlocks, nameValidator } from '../destinations';
import { registerPerceptionBlocks } from '../perception';
import { registerOutputBlocks } from '../output';

beforeAll(() => {
  registerDestinationBlocks();
  registerPerceptionBlocks();
  registerOutputBlocks();
});

// The server's alphabet, restated here so the test fails if either side drifts.
const SERVER_NAME_RE = /^[A-Za-zÄÖÜäöüß0-9 _-]{1,40}$/;

describe('RS-41 nameValidator mirrors the server alphabet', () => {
  // Every one of these was silently ACCEPTED by the editor before the fix and
  // then refused by the server mid-run with „Ungültiger Ziel-Name.".
  const AUDIT_EXAMPLES = ['A!', 'A/B', '日本', '😀', 'A\nB', 'A]B'];

  test.each(AUDIT_EXAMPLES)(
    'the server would refuse %j, so the editor must not hand it back unchanged',
    (raw) => {
      expect(SERVER_NAME_RE.test(raw)).toBe(false);
      const out = nameValidator(raw);
      // Either the edit is rejected (null) or it is sanitised into something the
      // server accepts. What must NEVER happen is passing the raw value through.
      expect(out).not.toBe(raw);
      expect(out === null || SERVER_NAME_RE.test(out)).toBe(true);
    },
  );

  test('strips the offending characters but keeps the valid remainder', () => {
    expect(nameValidator('A!')).toBe('A');
    expect(nameValidator('A/B')).toBe('AB');
    expect(nameValidator('A]B')).toBe('AB');
    // A newline is the log-injection vector the server comment calls out
    // ("prevents log-message-spoofing tricks (a `\n[FEHLER] …` injection)").
    expect(nameValidator('A\nB')).toBe('AB');
    expect(nameValidator('Ablage [FEHLER]')).toBe('Ablage FEHLER');
  });

  test('rejects the edit when nothing valid survives', () => {
    // Sanitising to '' must NOT set an empty name — the server refuses that too
    // with „Ziel-Name fehlt.". Returning null makes Blockly revert the edit.
    expect(nameValidator('日本')).toBeNull();
    expect(nameValidator('😀')).toBeNull();
    expect(nameValidator('!!!')).toBeNull();
    expect(nameValidator('///')).toBeNull();
  });

  test('keeps German umlauts and ß — they are IN the server alphabet', () => {
    expect(nameValidator('Ablage Grün')).toBe('Ablage Grün');
    expect(nameValidator('Fuß')).toBe('Fuß');
    expect(nameValidator('ÄÖÜäöüß')).toBe('ÄÖÜäöüß');
  });

  test('keeps the rest of the alphabet: digits, space, underscore, hyphen', () => {
    expect(nameValidator('Ziel_1')).toBe('Ziel_1');
    expect(nameValidator('Ziel-2')).toBe('Ziel-2');
    expect(nameValidator('Ablage 3')).toBe('Ablage 3');
    expect(nameValidator('AbC123 _-')).toBe('AbC123 _-');
  });

  test('pre-existing behaviour is unchanged: trim, sentinel, 24-char cap', () => {
    expect(nameValidator('  A  ')).toBe('A');
    expect(nameValidator('')).toBeNull();
    expect(nameValidator('   ')).toBeNull();
    expect(nameValidator('—')).toBeNull();        // the UNPINNED sentinel
    expect(nameValidator(42)).toBeNull();          // non-string
    // 24, NOT the server's 40 — the cap is deliberate (see file header).
    const long = 'A'.repeat(60);
    expect(nameValidator(long)).toHaveLength(24);
    expect(SERVER_NAME_RE.test(nameValidator(long))).toBe(true);
  });

  test('truncation happens AFTER stripping, so the cap is 24 VALID chars', () => {
    // 'A!' repeated 30x is 60 chars raw but only 30 valid ones.
    expect(nameValidator('A!'.repeat(30))).toBe('A'.repeat(24));
  });

  test('every accepted output satisfies the server regex (fuzz)', () => {
    const alphabet = 'AzÄß9 _-!/[]\n\t@#$%^&*(){}<>?|\\"\'`~;:,.日😀';
    const offenders = [];
    let accepted = 0;
    for (let i = 0; i < 800; i += 1) {
      let s = '';
      const len = 1 + (i % 30);
      for (let j = 0; j < len; j += 1) {
        s += alphabet[(i * 7 + j * 13) % alphabet.length];
      }
      const out = nameValidator(s);
      if (out !== null) {
        accepted += 1;
        if (!SERVER_NAME_RE.test(out)) offenders.push([s, out]);
      }
    }
    expect(offenders).toEqual([]);
    expect(accepted).toBeGreaterThan(0);
  });
});

describe('RS-56 every student-facing block carries a German tooltip', () => {
  // Blocks the audit found with NO tooltip. grasp/see/count/find come from
  // perception.js::defineObjectTypeBlock, which set none for any of them.
  const NEEDS_TOOLTIP = [
    'edubotics_grasp_object',
    'edubotics_see_object',
    'edubotics_count_object',
    'edubotics_find_object',
    'edubotics_when_object_seen',
    'edubotics_wait_until_object_seen',
    'edubotics_destination_current',
    'edubotics_play_tone',
  ];

  test.each(NEEDS_TOOLTIP)('%s has a non-empty tooltip', (type) => {
    const ws = new Blockly.Workspace();
    try {
      const block = ws.newBlock(type);
      const tip = typeof block.getTooltip === 'function'
        ? block.getTooltip()
        : block.tooltip;
      const text = typeof tip === 'function' ? tip() : tip;
      expect(typeof text).toBe('string');
      expect(text.trim().length).toBeGreaterThan(0);
    } finally {
      ws.dispose();
    }
  });

  test('tooltips are German, not English placeholders', () => {
    // Rule §1: everything a student reads is German. A cheap but effective
    // check — these blocks describe robot actions, so an English tooltip would
    // almost certainly contain one of these words.
    const ENGLISH_TELLS = /\b(the|object|gripper|robot|returns?|move)\b/i;
    const ws = new Blockly.Workspace();
    try {
      NEEDS_TOOLTIP.forEach((type) => {
        const block = ws.newBlock(type);
        const tip = block.getTooltip();
        const text = typeof tip === 'function' ? tip() : tip;
        expect(text).not.toMatch(ENGLISH_TELLS);
      });
    } finally {
      ws.dispose();
    }
  });

  test('German tooltips use real umlauts, never ae/oe/ue transliterations', () => {
    // Rule §1: "Use literal ä ö ü ß". german-strings-lint cannot see JSX/JS
    // block defs (it is a Python AST walker), so this is the only automated
    // fence on these strings.
    const TRANSLITERATION = /\b(fuer|ueber|oeffn|schliess|groess|waehl|zurueck|naechst|hoehe|laenge)/i;
    const ws = new Blockly.Workspace();
    try {
      NEEDS_TOOLTIP.forEach((type) => {
        const block = ws.newBlock(type);
        const tip = block.getTooltip();
        const text = typeof tip === 'function' ? tip() : tip;
        expect(text).not.toMatch(TRANSLITERATION);
      });
    } finally {
      ws.dispose();
    }
  });
});
