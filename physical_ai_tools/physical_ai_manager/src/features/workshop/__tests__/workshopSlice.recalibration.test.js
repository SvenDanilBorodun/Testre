// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Pure reducer tests for the „Kalibrierung neu starten" flow (2026-06-23).
//
// Regression: clicking „Kalibrierung neu starten" did nothing on an
// already-calibrated rig. requestRecalibration reset the per-step flags, but
// the wizard's mount-time /calibration/status hydrate re-read the still-present
// on-disk YAMLs and flipped `calibrated` straight back to true, bouncing the
// student to the editor. The `recalibrating` override forces the wizard open
// regardless of disk until the steps are actually re-run.

import reducer, {
  requestRecalibration,
  markStepComplete,
  setCalibrationStatus,
} from '../workshopSlice';

const initial = reducer(undefined, { type: '@@INIT' });

// A fully-calibrated starting state (what an already-set-up rig looks like).
const calibratedState = {
  ...initial,
  hasIntrinsicScene: true,
  hasHandeyeScene: true,
  hasTableTouch: true,
  recalibrating: false,
};

// The WorkshopPage editor gate, mirrored here so the test asserts the actual
// student-visible behaviour (editor vs wizard) rather than just raw flags.
const showEditor = (s) =>
  s.hasIntrinsicScene && s.hasHandeyeScene && s.hasTableTouch && !s.recalibrating;

describe('workshopSlice — recalibration flow', () => {
  test('initial state is not recalibrating', () => {
    expect(initial.recalibrating).toBe(false);
  });

  test('requestRecalibration opens the wizard and resets the geometry steps', () => {
    const s = reducer(calibratedState, requestRecalibration());
    expect(s.recalibrating).toBe(true);
    expect(s.hasHandeyeScene).toBe(false);
    expect(s.hasTableTouch).toBe(false);
    // Intrinsics (the expensive 12-frame step) stay satisfied.
    expect(s.hasIntrinsicScene).toBe(true);
    expect(s.currentStep).toBe('scene_handeye');
    expect(showEditor(s)).toBe(false);
  });

  test('a disk-status hydrate cannot bounce the student back to the editor mid-recalibration', () => {
    // This is the core regression. Even if /calibration/status re-reads the
    // still-present YAMLs and re-sets every flag true, `recalibrating` keeps
    // the wizard open.
    let s = reducer(calibratedState, requestRecalibration());
    s = reducer(s, setCalibrationStatus({
      has_scene_intrinsics: true,
      has_scene_handeye: true,
      has_table_plane: true,
    }));
    expect(s.recalibrating).toBe(true);
    expect(showEditor(s)).toBe(false);
  });

  test('finishing only the extrinsic keeps the wizard open', () => {
    let s = reducer(calibratedState, requestRecalibration());
    s = reducer(s, markStepComplete('scene_handeye'));
    expect(s.hasHandeyeScene).toBe(true);
    expect(s.recalibrating).toBe(true); // table touch-off still pending
    expect(showEditor(s)).toBe(false);
  });

  test('re-running both geometry steps clears recalibrating and returns to the editor', () => {
    let s = reducer(calibratedState, requestRecalibration());
    s = reducer(s, markStepComplete('scene_handeye'));
    s = reducer(s, markStepComplete('table_touch'));
    expect(s.recalibrating).toBe(false);
    expect(showEditor(s)).toBe(true);
  });

  test('step order does not matter — table touch-off first also resolves', () => {
    let s = reducer(calibratedState, requestRecalibration());
    s = reducer(s, markStepComplete('table_touch'));
    expect(s.recalibrating).toBe(true);
    s = reducer(s, markStepComplete('scene_handeye'));
    expect(s.recalibrating).toBe(false);
    expect(showEditor(s)).toBe(true);
  });
});
