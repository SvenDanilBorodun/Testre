/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-50 — the „Zähler" half of the Debug-Panel, as a pure contract.
//
// The counter blocks („setze Zähler … auf 0", „erhöhe Zähler … um 1") wrote to
// `ctx.counters` and emitted NOTHING, so the canonical points lesson — set
// „Punkte" to 0, increase it inside „Solange sichtbar", test „wenn Punkte > 3"
// — showed „Noch keine Variablen." in the panel while it was visibly counting.
//
// This file pins the NAME contract and nothing else. It deliberately cannot see
// whether anyone CALLS the predicate — that blindness is how RS-49 shipped its
// own fix — so the wire, the store and the screen each have their own suite.
//
// The one rule this file exists to enforce: counter names go through the SAME
// `isDisplayableVariableName` the variables path uses. A counter-flavoured copy
// would be a second opinion about one question, which is precisely the defect
// RS-49 turned out to be.

import fs from 'node:fs';
import path from 'node:path';

import { isDisplayableVariableName, VAR_NAME_MAX_LEN } from '../variableName';
import { counterNameValidator } from '../../components/Workshop/blocks/counters';
import {
  COUNTER_NAMES_SHOWN,
  COUNTER_NAMES_HIDDEN,
  NAME_MAX_LEN,
  OVERLONG_TYPED_NAME,
  OVERLONG_FIELD_NAME,
} from './counterNames.fixture';

describe('the fixture is reachable — every name really comes out of the field', () => {
  // Without this the two lists below would be a programmer's imagination. The
  // block's own validator is the only thing between a keystroke and the name
  // the server later echoes back in the sentinel.
  test.each([...COUNTER_NAMES_SHOWN, ...COUNTER_NAMES_HIDDEN])(
    '%j survives counterNameValidator unchanged',
    (name) => {
      expect(counterNameValidator(name)).toBe(name);
    }
  );

  test('„Punkte" is the name the blocks ship pre-filled', () => {
    expect(COUNTER_NAMES_SHOWN[0]).toBe('Punkte');
  });
});

describe('names the panel must show', () => {
  test.each(COUNTER_NAMES_SHOWN)('%j is displayable', (name) => {
    expect(isDisplayableVariableName(name)).toBe(true);
  });
});

describe('names the field allows but the panel hides — the documented limit', () => {
  // NOT a bug to be fixed by tightening `counterNameValidator`: Blockly field
  // validators also run during DESERIALIZATION, so a stricter one would rename
  // counters inside workflows that are already saved (the load-time trap that
  // forced this round's `check: 'String'` revert). The fix, if it is ever
  // wanted, is a load-time name migration first.
  test.each(COUNTER_NAMES_HIDDEN)('%j is refused', (name) => {
    expect(isDisplayableVariableName(name)).toBe(false);
  });

  test('the `=` case is the interesting one — it is a NAME, not a frame bug', () => {
    // The [CNT:] frame splits on the LAST `=` with a digits-only value, so
    // `[CNT:Punkte=2=5]` parses as ("Punkte=2", 5) and is refused by the name
    // gate — CONSUMED, not printed into the student's Protokoll. See the wire
    // suite; here we only pin that the field really permits the name.
    expect(counterNameValidator('Punkte=2')).toBe('Punkte=2');
    expect(isDisplayableVariableName('Punkte=2')).toBe(false);
  });
});

describe('the length arithmetic — the cap can never be what hides a counter', () => {
  test('the field caps at 40, the sentinel gate at 64', () => {
    expect(NAME_MAX_LEN).toBe(40);
    expect(VAR_NAME_MAX_LEN).toBe(64);
    expect(NAME_MAX_LEN).toBeLessThan(VAR_NAME_MAX_LEN);
  });

  test('a 60-character typed name comes back 40 and is still displayable', () => {
    expect(OVERLONG_TYPED_NAME).toHaveLength(60);
    expect(OVERLONG_FIELD_NAME).toHaveLength(NAME_MAX_LEN);
    expect(isDisplayableVariableName(OVERLONG_FIELD_NAME)).toBe(true);
  });

  test('anything above 64 is still refused (the wire is not the field)', () => {
    // 41..64 is unreachable from the editor but perfectly reachable from a
    // hand-built /workflow/start payload or a direct rosbridge publish.
    expect(isDisplayableVariableName('K'.repeat(64))).toBe(true);
    expect(isDisplayableVariableName('K'.repeat(65))).toBe(false);
  });
});

describe('ONE predicate — no counter-flavoured second copy', () => {
  test('the counter path imports the variables predicate, not a local regex', () => {
    // A source fence, in the shape `utils/__tests__/authGate.test.js` uses for
    // its call site. It is here because the DEFECT this whole round is about
    // was a second, stricter copy of a predicate — a behaviour test cannot see
    // a copy that happens to agree today and drifts tomorrow.
    const src = (rel) => fs.readFileSync(path.resolve(__dirname, '..', '..', rel), 'utf8');
    const slice = src('features/workshop/workshopSlice.js');
    const hook = src('hooks/useRosTopicSubscription.js');
    const counterReducer = slice.slice(slice.indexOf('setCounter:'), slice.indexOf('clearCounters:'));
    // lastIndexOf: the token also appears in the header comment listing every
    // sentinel, ~100 lines above the branch itself.
    const cntBranch = hook.slice(
      hook.lastIndexOf('[CNT:name=int]'),
      hook.indexOf("console.warn('CNT token parse failed'")
    );

    expect(counterReducer).toContain('isDisplayableVariableName(name)');
    expect(cntBranch).toContain('isDisplayableVariableName(rawName)');
    // No name-shaped regex literal of its own in either counter path.
    expect(counterReducer).not.toMatch(/\/\^\[/);
    expect(cntBranch).not.toMatch(/A-Za-z/);
  });
});
