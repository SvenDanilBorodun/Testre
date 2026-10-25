/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// gripperBand / classifyGripper — the ONE grasp band shared by SimScene's
// grasp-attach and „Greifer merken" in inserted programs.

import { gripperBand, classifyGripper } from '../armProfile';

describe('gripperBand', () => {
  it('resolves the OMX literals for null / absent / partial caps', () => {
    expect(gripperBand(null)).toEqual({ close: 0.2, open: 0.5 });
    expect(gripperBand(undefined)).toEqual({ close: 0.2, open: 0.5 });
    expect(gripperBand({ arm_joints: 5, gripper_open_rad: 0.8 }))
      .toEqual({ close: 0.2, open: 0.5 });
  });

  it('derives the V1 band from the profile threshold and open command (0.5 / 0.70)', () => {
    const band = gripperBand({ sim_close_threshold_rad: 0.5, gripper_open_rad: 0.9 });
    expect(band).toEqual({ close: 0.5, open: 0.7 });
  });

  it('derives any profile band the same way (threshold, halfway to open)', () => {
    const band = gripperBand({ sim_close_threshold_rad: 1.5, gripper_open_rad: 1.75 });
    expect(band).toEqual({ close: 1.5, open: 1.625 });
  });

  it('ignores a non-finite threshold (OMX literals)', () => {
    expect(gripperBand({ sim_close_threshold_rad: Number.NaN, gripper_open_rad: 1.75 }))
      .toEqual({ close: 0.2, open: 0.5 });
  });
});

describe('classifyGripper', () => {
  const band = { close: 0.2, open: 0.5 };

  it('is closed strictly below close and open strictly above open', () => {
    expect(classifyGripper(-0.5, band)).toBe('closed');
    expect(classifyGripper(0.1999, band)).toBe('closed');
    expect(classifyGripper(0.5001, band)).toBe('open');
    expect(classifyGripper(0.8, band)).toBe('open');
  });

  it('is unknown ON the boundaries and inside the band', () => {
    expect(classifyGripper(0.2, band)).toBeNull();
    expect(classifyGripper(0.35, band)).toBeNull();
    expect(classifyGripper(0.5, band)).toBeNull();
  });

  it('is unknown for a non-number or a missing band', () => {
    expect(classifyGripper(Number.NaN, band)).toBeNull();
    expect(classifyGripper(Infinity, band)).toBeNull();
    expect(classifyGripper('0.1', band)).toBeNull();
    expect(classifyGripper(null, band)).toBeNull();
    expect(classifyGripper(0.1, null)).toBeNull();
  });

  it('classifies against the V1 band', () => {
    const v1 = gripperBand({ sim_close_threshold_rad: 0.5, gripper_open_rad: 0.9 });
    expect(classifyGripper(0.05, v1)).toBe('closed'); // the catalog close
    expect(classifyGripper(0.2, v1)).toBe('closed'); // jaws blocked by the 30 mm cube
    expect(classifyGripper(0.5, v1)).toBeNull();
    expect(classifyGripper(0.6, v1)).toBeNull();
    expect(classifyGripper(0.7, v1)).toBeNull();
    expect(classifyGripper(0.9, v1)).toBe('open');
  });
});
