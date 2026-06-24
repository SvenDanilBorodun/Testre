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
  finishVerify,
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
// W5 (2026-06-24): the gate also holds for the optional accuracy-verify step.
const showEditor = (s) =>
  s.hasIntrinsicScene && s.hasHandeyeScene && s.hasTableTouch
  && !s.recalibrating && !s.pendingVerify;

describe('workshopSlice — recalibration flow', () => {
  test('initial state is not recalibrating', () => {
    expect(initial.recalibrating).toBe(false);
  });

  test('requestRecalibration opens the wizard and resets ALL geometry steps (FULL scope)', () => {
    const s = reducer(calibratedState, requestRecalibration());
    expect(s.recalibrating).toBe(true);
    expect(s.hasHandeyeScene).toBe(false);
    expect(s.hasTableTouch).toBe(false);
    // W1 (2026-06-24): the force-recal release made intrinsics mandatory again,
    // so the manual button now also redoes them and restarts at the first step.
    expect(s.hasIntrinsicScene).toBe(false);
    expect(s.currentStep).toBe('scene_intrinsic');
    expect(s.pendingVerify).toBe(false);
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
    s = reducer(s, markStepComplete('scene_intrinsic'));
    s = reducer(s, markStepComplete('scene_handeye'));
    expect(s.hasHandeyeScene).toBe(true);
    expect(s.recalibrating).toBe(true); // table touch-off still pending
    expect(showEditor(s)).toBe(false);
  });

  test('re-running all three steps holds for the optional verify, then finishVerify opens the editor', () => {
    let s = reducer(calibratedState, requestRecalibration());
    s = reducer(s, markStepComplete('scene_intrinsic'));
    s = reducer(s, markStepComplete('scene_handeye'));
    s = reducer(s, markStepComplete('table_touch'));
    // recalibration override is cleared, but the optional verify holds the editor.
    expect(s.recalibrating).toBe(false);
    expect(s.pendingVerify).toBe(true);
    expect(s.currentStep).toBe('accuracy_verify');
    expect(showEditor(s)).toBe(false);
    // „Fertig"/„Überspringen" → editor opens.
    s = reducer(s, finishVerify());
    expect(s.pendingVerify).toBe(false);
    expect(showEditor(s)).toBe(true);
  });

  test('step order does not matter — table touch-off completes last regardless', () => {
    let s = reducer(calibratedState, requestRecalibration());
    s = reducer(s, markStepComplete('table_touch'));
    // touch-off done but intrinsic/extrinsic still pending → wizard stays open.
    expect(s.recalibrating).toBe(true);
    s = reducer(s, markStepComplete('scene_intrinsic'));
    s = reducer(s, markStepComplete('scene_handeye'));
    expect(s.recalibrating).toBe(false);
    s = reducer(s, finishVerify());
    expect(showEditor(s)).toBe(true);
  });
});
