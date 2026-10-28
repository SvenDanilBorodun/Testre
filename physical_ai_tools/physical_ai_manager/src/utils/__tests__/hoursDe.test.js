// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The hour count's one German noun phrase (credit system v2, spec §8.1/§10,
// review 2 m7) over the SHARED case table: the cloud API's Python twin
// (`credit_texts_de.hours_de`, `app/tests/test_hours_de.py`) reads the same
// file, so both languages write „1 Stunde", never „1 Stunden".

import fs from 'fs';
import path from 'path';
import { describe, expect, test } from 'vitest';
import { hoursDe, lostMinutes, numberDe } from '../hoursDe';

const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, 'hoursDe.cases.json'), 'utf8'));

describe('hoursDe.cases.json', () => {
  test('is an array of [value, text] pairs', () => {
    expect(Array.isArray(CASES)).toBe(true);
    expect(CASES.length).toBeGreaterThan(20);
    for (const row of CASES) {
      expect(Array.isArray(row)).toBe(true);
      expect(row).toHaveLength(2);
      expect(typeof row[0]).toBe('number');
      expect(typeof row[1]).toBe('string');
    }
  });

  test('covers 0, 1, 2, the 3-hour maximum, larger values and the non-integer forms', () => {
    const values = CASES.map(([v]) => v);
    for (const v of [0, 1, 2, 3, 3.5, 0.5, 1000, 10000]) expect(values).toContain(v);
    expect(CASES.some(([v]) => !Number.isInteger(v))).toBe(true);
  });

  test('„Stunde" only for exactly one, never „1 Stunden"', () => {
    for (const [, text] of CASES) {
      expect(text).not.toMatch(/(^|[^\d,.])1 Stunden/);
      if (/ Stunde$/.test(text)) expect(text).toBe('1 Stunde');
    }
  });
});

describe('hoursDe', () => {
  test.each(CASES)('%s → %s', (value, text) => {
    expect(hoursDe(value)).toBe(text);
  });

  test('an unknown value is „—", never „0 Stunden"', () => {
    for (const v of [null, undefined, '', NaN, Infinity, 'x', true]) expect(hoursDe(v)).toBe('—');
  });

  test('a numeric string reads like its number', () => {
    expect(hoursDe('2')).toBe('2 Stunden');
    expect(hoursDe('1')).toBe('1 Stunde');
  });

  test('-0 is „0 Stunden"', () => {
    expect(hoursDe(-0)).toBe('0 Stunden');
  });
});

describe('numberDe and lostMinutes', () => {
  test('numberDe groups thousands with a dot and never invents 0', () => {
    expect(numberDe(7500)).toBe('7.500');
    expect(numberDe(42)).toBe('42');
    expect(numberDe(1234567)).toBe('1.234.567');
    expect(numberDe(null)).toBe('—');
    expect(numberDe(undefined)).toBe('—');
  });

  test('lostMinutes rounds seconds to whole minutes', () => {
    expect(lostMinutes(60)).toBe(1);
    expect(lostMinutes(89)).toBe(1);
    expect(lostMinutes(90)).toBe(2);
    expect(lostMinutes(2100)).toBe(35);
    expect(lostMinutes(0)).toBe(0);
  });
});
