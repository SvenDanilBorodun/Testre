/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { describe, it, expect } from 'vitest';
import {
  formatMmDe,
  formatSecondsDe,
  formatRecordedAtDe,
  formatVersionDateDe,
  LEGACY_ROBOT_TYPES,
  robotLongLabelDe,
  robotShortLabelDe,
} from '../format';

describe('Sammlung German formatting', () => {
  it('dates use local parts and a four-digit year', () => {
    const iso = new Date(2026, 8, 13, 10, 42).toISOString();
    expect(formatRecordedAtDe(iso)).toBe('13.09.2026, 10:42');
    expect(formatVersionDateDe(iso)).toBe('13.09. 10:42');
    const early = new Date(2026, 0, 2, 3, 4).toISOString();
    expect(formatRecordedAtDe(early)).toBe('02.01.2026, 03:04');
  });

  it('garbage dates read „—", never 1970', () => {
    for (const bad of [null, undefined, '', 'gestern', 42, {}]) {
      expect(formatRecordedAtDe(bad)).toBe('—');
      expect(formatVersionDateDe(bad)).toBe('—');
    }
  });

  it('millimetres use a real minus sign; seconds a German comma', () => {
    expect(formatMmDe(-0.064)).toBe('−64');
    expect(formatMmDe(0.182)).toBe('182');
    expect(formatSecondsDe(4.2)).toBe('4,2');
    expect(formatSecondsDe(12)).toBe('12,0');
  });

  it('robot labels map the two arm ids, the legacy ids, and nothing else', () => {
    expect(robotLongLabelDe('omx_f')).toBe('OpenMANIPULATOR-X');
    expect(robotLongLabelDe('v1_studio')).toBe('V1');
    expect(robotShortLabelDe('omx_f')).toBe('OMX');
    expect(robotShortLabelDe('v1_studio')).toBe('V1');
    // The two retired arms keep their stored tag and share ONE label, never
    // their raw id or their old product name.
    expect(LEGACY_ROBOT_TYPES).toEqual(['edu6_studio', 'edu1_studio']);
    expect(robotLongLabelDe('edu6_studio')).toBe('Früherer EduBotics-Arm');
    expect(robotLongLabelDe('edu1_studio')).toBe('Früherer EduBotics-Arm');
    expect(robotShortLabelDe('edu6_studio')).toBe('früherer Arm');
    expect(robotShortLabelDe('edu1_studio')).toBe('früherer Arm');
    for (const unknown of ['', 'omx_full', 'v1', 'constructor', null, undefined]) {
      expect(robotLongLabelDe(unknown)).toBeNull();
      expect(robotShortLabelDe(unknown)).toBeNull();
    }
  });
});
