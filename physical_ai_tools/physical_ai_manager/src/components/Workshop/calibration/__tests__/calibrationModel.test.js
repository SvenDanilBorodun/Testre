/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// calibrationModel is the panel's one decision (spec 40 S4, S8): from the
// robot's /edubotics/calibration_state to a view, a sentence and the actions
// offered. The table below is S4's: every code the calibration service can put
// into the state, with the state it arrives in. The sentences themselves are
// held equal to autocal/texts_de.py by robotis_ai_setup/tests/
// test_calibration_state_lockstep.py.

import { describe, expect, it } from 'vitest';
import {
  CALIBRATION_STATES,
  CALIBRATION_STEPS,
  VIEWS,
  calibrationView,
  isCalibrated,
  isCapturing,
  parseCalibrationState,
} from '../calibrationModel';
import {
  CALIBRATION_DE, CALIBRATION_NOTES_DE, CALIBRATION_SENTENCES_DE, fillDe,
} from '../calibrationCopy';

// [state, code, view, offers] — S4's table and the codes defined beside it.
const ROWS = [
  ['ok', 'ok', VIEWS.OK, { recheck: true, armCheck: true }],
  ['blocked', 'profile_unknown', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'camera_not_tower', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'camera_missing', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'image_size', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'too_dark', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'wrong_mat', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'target_attached', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'fixture_hidden', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'tags_hidden', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'mat_not_flat', VIEWS.BLOCKED, { recheck: true, armCheck: true }],
  ['blocked', 'mat_misplaced', VIEWS.BLOCKED, { recheck: true }],
  ['blocked', 'inconsistent', VIEWS.BLOCKED, { recheck: true, armCheck: true }],
  ['blocked', 'tower_displaced', VIEWS.BLOCKED, { recheck: true, armCheck: true }],
  ['blocked', 'check_failed', VIEWS.BLOCKED, { recheck: true }],
  ['needs_arm_check', 'needs_arm_check/none', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'needs_arm_check/contract_changed', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'needs_arm_check/camera_moved', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'needs_arm_check/fixture_changed', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'too_few_poses', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'lens', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true, handheld: true }],
  ['needs_arm_check', 'rms', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true, handheld: true }],
  ['needs_arm_check', 'tower', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'joint', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'mount', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'mat', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'fixture_layout', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'guard_margin', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'target_not_seen', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'foreign_target', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'stopped', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'handheld_done', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true }],
  ['needs_arm_check', 'handheld_failed', VIEWS.NEEDS_ARM_CHECK, { recheck: true, armCheck: true, handheld: true }],
  ['confirm_target_removed', 'confirm_target_removed', VIEWS.CONFIRM_TARGET_REMOVED, { confirm: true }],
  ['confirm_target_removed', 'target_not_confirmed', VIEWS.CONFIRM_TARGET_REMOVED, { confirm: true }],
  ['arm_check_running', 'arm_check_running', VIEWS.ARM_CHECK_RUNNING, { stop: true }],
  ['handheld_running', 'handheld_running', VIEWS.HANDHELD_RUNNING, { cancelHandheld: true }],
  ['unknown', 'unknown', VIEWS.CHECKING, { recheck: true }],
];
const NOTE_CODES = ['mat_moved', 'camera_followed', 'tag_dropped', 'droop_above_design'];

function state(stateName, code, extra = {}) {
  return {
    v: 1,
    state: stateName,
    code,
    message_de: '',
    notes: [],
    camera: null,
    arm_check: null,
    handheld: null,
    quality: null,
    fixture: { id: 'tower_v1', lift_m: 0.0072 },
    droop_warning: null,
    mat_check: null,
    ...extra,
  };
}

const OFFERS = ['recheck', 'armCheck', 'handheld', 'confirm', 'stop', 'cancelHandheld'];
const offersOf = (v) => ({
  recheck: v.canRecheck,
  armCheck: v.canStartArmCheck,
  handheld: v.canStartHandheld,
  confirm: v.canConfirmTargetRemoved,
  stop: v.canStop,
  cancelHandheld: v.canCancelHandheld,
});

describe('calibrationModel — every state and code of S4', () => {
  it('the table covers every code the page has a sentence for, the notes aside', () => {
    const tableCodes = new Set(ROWS.map((r) => r[1]));
    for (const code of Object.keys(CALIBRATION_SENTENCES_DE)) {
      if (NOTE_CODES.includes(code)) continue;
      expect([code, tableCodes.has(code)]).toEqual([code, true]);
    }
    expect(new Set(ROWS.map((r) => r[0]))).toEqual(new Set(CALIBRATION_STATES));
  });

  it.each(ROWS)('%s / %s → its view, the actions it offers, and the server\'s sentence', (s, code, view, offers) => {
    const v = calibrationView(state(s, code, { message_de: `Satz für ${code}` }));
    expect(v.view).toBe(view);
    expect(v.state).toBe(s);
    expect(v.code).toBe(code);
    expect(v.sentence).toBe(`Satz für ${code}`);
    const expected = Object.fromEntries(OFFERS.map((k) => [k, !!offers[k]]));
    expect(offersOf(v)).toEqual(expected);
  });

  it.each(ROWS)('%s / %s without a message → the page\'s own copy of the robot\'s sentence', (s, code) => {
    const arm = s === 'arm_check_running';
    const hand = s === 'handheld_running';
    const v = calibrationView(state(s, code, {
      arm_check: arm ? { pose: 7, poses: 20, captured: 6 } : null,
      handheld: hand ? { views: 4, views_needed: 18, cells: 3, cells_needed: 9, grid: [4, 3], filled: [] } : null,
    }));
    // The fields a state carries are filled; one it does not carry reads „–".
    const expected = code === 'unknown'
      ? CALIBRATION_DE.CHECKING
      : fillDe(CALIBRATION_SENTENCES_DE[code], { i: arm ? 7 : null, n: arm ? 20 : null, k: hand ? 4 : null });
    expect(v.sentence).toBe(expected);
    expect(v.sentence).not.toMatch(/[{}]/);
  });

  it('fills the format fields it knows: the pose, the views, the camera\'s deviation', () => {
    expect(calibrationView(state('arm_check_running', 'arm_check_running', {
      arm_check: { pose: 7, poses: 20, captured: 6 },
    })).sentence).toBe('Arm-Check läuft: Pose 7 von 20.');
    expect(calibrationView(state('handheld_running', 'handheld_running', {
      handheld: { views: 4, views_needed: 18 },
    })).sentence).toContain('4 von 18 Ansichten');
    expect(calibrationView(state('blocked', 'tower_displaced', {
      mat_check: { lens_offset_mm: 11.24, lens_drift_mm: 4.0 },
    })).sentence).toContain('(Abweichung 11,2 mm)');
  });

  it('no state yet (nothing heard) is „wird geprüft", never „not calibrated"', () => {
    const v = calibrationView(null);
    expect(v.view).toBe(VIEWS.CHECKING);
    expect(v.sentence).toBe(CALIBRATION_DE.CHECKING);
    expect(v.canRecheck).toBe(true);
    expect(v.canStartArmCheck).toBe(false);
  });

  it('a running arm check carries its progress', () => {
    const v = calibrationView(state('arm_check_running', 'arm_check_running', {
      arm_check: { pose: 7, poses: 20, captured: 6 },
    }));
    expect(v.progress).toEqual({ pose: 7, poses: 20, captured: 6 });
  });

  it('the handheld capture carries its views and cells', () => {
    const v = calibrationView(state('handheld_running', 'handheld_running', {
      handheld: { views: 2, views_needed: 18, cells: 2, cells_needed: 9, grid: [4, 3], filled: [[0, 0, 0], [3, 2, 1]] },
    }));
    expect(v.handheld).toEqual({
      views: 2, viewsNeeded: 18, cells: 2, cellsNeeded: 9, cols: 4, rows: 3, filled: [[0, 0, 0], [3, 2, 1]],
    });
  });

  it('the notes become their sentences — the droop_above_design note of the arm check among them', () => {
    const v = calibrationView(state('confirm_target_removed', 'confirm_target_removed', {
      notes: ['droop_above_design', 'mat_moved', 'not_a_note'],
    }));
    expect(v.notes).toEqual([CALIBRATION_NOTES_DE.droop_above_design, CALIBRATION_NOTES_DE.mat_moved]);
    expect(CALIBRATION_NOTES_DE.droop_above_design).toBe(
      'Dieser Arm hängt etwas stärker durch als vorgesehen. Direkt neben der ausgesparten Ecke der Matte kann '
      + 'der Roboter deshalb ein Greifen ablehnen.',
    );
  });

  it('the droop warning is the robot\'s own sentence', () => {
    const v = calibrationView(state('ok', 'ok', {
      droop_warning: { joint: 2, deg: 7, message_de: 'Achtung: der Satz des Roboters.' },
    }));
    expect(v.droop).toBe('Achtung: der Satz des Roboters.');
  });

  it('„Details": the quality numbers, „Schärfe" and „Abweichung der Kamera" with its two readings beside it', () => {
    const v = calibrationView(state('ok', 'ok', {
      quality: { poses_used: 19, rms_px: 0.312, holdout_rms_px: 0.41, holdout_p95_mm: 1.84, field_tags: 14, sharpness: 88.4 },
      mat_check: { rms_px: 0.16, tags_seen: 14, sharpness: 91.2, lens_offset_mm: 3.26, lens_along_mm: 7.1, lens_drift_mm: 1.04 },
    }));
    const rows = Object.fromEntries(v.details.map((r) => [r.label, r.value]));
    expect(rows[CALIBRATION_DE.DETAIL_POSES]).toBe('19');
    expect(rows[CALIBRATION_DE.DETAIL_RMS]).toBe('0,31 px');
    expect(rows[CALIBRATION_DE.DETAIL_HOLDOUT]).toBe('0,41 px');
    expect(rows[CALIBRATION_DE.DETAIL_P95]).toBe('1,8 mm');
    expect(rows[CALIBRATION_DE.DETAIL_TAGS]).toBe('14');
    expect(rows[CALIBRATION_DE.DETAIL_SHARPNESS]).toBe('91');
    expect(rows[CALIBRATION_DE.DETAIL_LENS_OFFSET]).toBe('3,3 mm');
    expect(rows[CALIBRATION_DE.DETAIL_LENS_ALONG]).toBe('7,1 mm');
    expect(rows[CALIBRATION_DE.DETAIL_LENS_DRIFT]).toBe('1,0 mm');
    expect(CALIBRATION_DE.DETAIL_SHARPNESS).toBe('Schärfe');
    expect(CALIBRATION_DE.DETAIL_LENS_OFFSET).toBe('Abweichung der Kamera');
    // Nothing measured: no details at all, never a row of zeros.
    expect(calibrationView(state('blocked', 'too_dark')).details).toBeNull();
  });
});

describe('calibrationModel — the wire', () => {
  it('parses the robot\'s JSON and refuses anything else', () => {
    const good = state('needs_arm_check', 'needs_arm_check/none', { message_de: 'x' });
    expect(parseCalibrationState(JSON.stringify(good))).toEqual(good);
    expect(parseCalibrationState('')).toBeNull();
    expect(parseCalibrationState('{')).toBeNull();
    expect(parseCalibrationState(JSON.stringify({ ...good, v: 2 }))).toBeNull();
    expect(parseCalibrationState(JSON.stringify({ ...good, state: 'calibrated' }))).toBeNull();
    expect(parseCalibrationState(JSON.stringify([good]))).toBeNull();
    expect(parseCalibrationState(null)).toBeNull();
  });

  it('a malformed field is dropped, never trusted', () => {
    const parsed = parseCalibrationState(JSON.stringify(state('ok', 'ok', {
      notes: 'mat_moved', arm_check: 5, quality: [1], message_de: 7,
    })));
    expect(parsed.notes).toEqual([]);
    expect(parsed.arm_check).toBeNull();
    expect(parsed.quality).toBeNull();
    expect(parsed.message_de).toBe('');
  });

  it('only ok is calibrated; only the arm check and the handheld step capture', () => {
    for (const s of CALIBRATION_STATES) {
      expect([s, isCalibrated(state(s, s))]).toEqual([s, s === 'ok']);
      expect([s, isCapturing(state(s, s))]).toEqual([s, s === 'arm_check_running' || s === 'handheld_running']);
    }
    expect(isCalibrated(null)).toBe(false);
    expect(isCapturing(null)).toBe(false);
  });

  it('names the five steps of /calibration/start (home: the way to the Grundstellung, X-TARGETSTART)', () => {
    expect(CALIBRATION_STEPS).toEqual({
      MAT_CHECK: 'mat_check', ARM_CHECK: 'arm_check', TARGET_REMOVED: 'target_removed', HANDHELD: 'handheld',
      HOME: 'home',
    });
  });
});
