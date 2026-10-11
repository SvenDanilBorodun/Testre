/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import {
  CODE_SIDEBAR_WIDTH, DOCK_SPLIT, DOCK_WIDTH, METHODS_WIDTH, SIM_SPLIT, SIM_WIDTH,
  readCodeSidebarWidth, readDockSplit, readDockWidth, readMethodsOpen, readMethodsWidth,
  readSimHintDismissed, readSimSplit, readSimWidth,
  writeCodeSidebarWidth, writeDockSplit, writeDockWidth, writeMethodsOpen, writeMethodsWidth,
  writeSimHintDismissed, writeSimSplit, writeSimWidth,
} from '../layoutPrefs';
import { MACHINE_SCOPED_KEYS } from '../../../../utils/sessionScope';

const NUMERIC = [
  ['edubotics_workshop_dock_width', DOCK_WIDTH, readDockWidth, writeDockWidth],
  ['edubotics_workshop_dock_split', DOCK_SPLIT, readDockSplit, writeDockSplit],
  ['edubotics_workshop_sim_width', SIM_WIDTH, readSimWidth, writeSimWidth],
  ['edubotics_workshop_sim_split', SIM_SPLIT, readSimSplit, writeSimSplit],
  ['edubotics_code_sidebar_width', CODE_SIDEBAR_WIDTH, readCodeSidebarWidth, writeCodeSidebarWidth],
  ['edubotics_code_methods_width', METHODS_WIDTH, readMethodsWidth, writeMethodsWidth],
];

beforeEach(() => {
  window.localStorage.clear();
});

describe('layoutPrefs — the specs', () => {
  test('the defaults and bounds of §5.2', () => {
    expect(DOCK_WIDTH).toEqual({ default: 380, min: 280, max: 760 });
    expect(DOCK_SPLIT).toEqual({ default: 0.5, min: 0.18, max: 0.82 });
    expect(SIM_WIDTH).toEqual({ default: 0.55, min: 0.30, max: 0.75 });
    expect(SIM_SPLIT).toEqual({ default: 0.40, min: 0.25, max: 0.80 });
    expect(CODE_SIDEBAR_WIDTH).toEqual({ default: 176, min: 140, max: 360 });
    expect(METHODS_WIDTH).toEqual({ default: 300, min: 220, max: 520 });
    [DOCK_WIDTH, DOCK_SPLIT, SIM_WIDTH, SIM_SPLIT, CODE_SIDEBAR_WIDTH, METHODS_WIDTH]
      .forEach((spec) => expect(Object.isFrozen(spec)).toBe(true));
  });

  test('every key is a MACHINE key (window furniture of the shared PC)', () => {
    [...NUMERIC.map(([key]) => key), 'edubotics_workshop_sim_hint_dismissed', 'edubotics_code_methods_open']
      .forEach((key) => expect(MACHINE_SCOPED_KEYS).toContain(key));
  });
});

describe.each(NUMERIC)('layoutPrefs — %s', (key, spec, read, write) => {
  test('absent → the default', () => {
    expect(read()).toBe(spec.default);
  });

  test('a stored value inside the bounds is read back', () => {
    const mid = (spec.min + spec.max) / 2;
    window.localStorage.setItem(key, String(mid));
    expect(read()).toBe(mid);
  });

  test('a stored value outside the bounds is clamped', () => {
    window.localStorage.setItem(key, String(spec.max * 10));
    expect(read()).toBe(spec.max);
    window.localStorage.setItem(key, String(-5));
    expect(read()).toBe(spec.min);
  });

  test('garbage reads as the default', () => {
    ['', '   ', 'abc', 'NaN', 'Infinity', '{}'].forEach((raw) => {
      window.localStorage.setItem(key, raw);
      expect(read()).toBe(spec.default);
    });
  });

  test('a write stores the value under its key, clamped', () => {
    const mid = (spec.min + spec.max) / 2;
    write(mid);
    expect(read()).toBeCloseTo(mid, 3);
    write(spec.max * 10);
    expect(Number(window.localStorage.getItem(key))).toBe(spec.max);
  });
});

describe('layoutPrefs — the two flags', () => {
  test('the simulator notice: absent = shown, „1" = dismissed', () => {
    expect(readSimHintDismissed()).toBe(false);
    writeSimHintDismissed();
    expect(window.localStorage.getItem('edubotics_workshop_sim_hint_dismissed')).toBe('1');
    expect(readSimHintDismissed()).toBe(true);
    window.localStorage.setItem('edubotics_workshop_sim_hint_dismissed', '0');
    expect(readSimHintDismissed()).toBe(false);
  });

  test('the Methoden column: absent = open, „0" = closed, „1" = open', () => {
    expect(readMethodsOpen()).toBe(true);
    writeMethodsOpen(false);
    expect(window.localStorage.getItem('edubotics_code_methods_open')).toBe('0');
    expect(readMethodsOpen()).toBe(false);
    writeMethodsOpen(true);
    expect(window.localStorage.getItem('edubotics_code_methods_open')).toBe('1');
    expect(readMethodsOpen()).toBe(true);
  });
});

describe('layoutPrefs — storage that throws', () => {
  let original;
  beforeEach(() => {
    original = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() { throw new Error('storage disabled'); },
    });
  });
  afterEach(() => {
    if (original) Object.defineProperty(window, 'localStorage', original);
  });

  test('every reader answers its default and every writer is silent', () => {
    NUMERIC.forEach(([, spec, read, write]) => {
      expect(read()).toBe(spec.default);
      expect(() => write(spec.min)).not.toThrow();
    });
    expect(readSimHintDismissed()).toBe(false);
    expect(() => writeSimHintDismissed()).not.toThrow();
    expect(readMethodsOpen()).toBe(true);
    expect(() => writeMethodsOpen(false)).not.toThrow();
  });
});
