/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The calibration panel's one decision (spec 40 S4, S8), pure: the robot's
// /edubotics/calibration_state → the view, its sentence and the actions it
// offers. The STATE decides the view (the robot is the authority on where the
// calibration stands); the CODE decides only which way out is offered beside
// it. Nothing heard yet is „wird geprüft", never „nicht kalibriert".
//
// The states, the version and the steps equal the calibration service's
// (autocal/service.py; robotis_ai_setup/tests/test_calibration_state_lockstep.py).

import {
  CALIBRATION_DE,
  CALIBRATION_NOTES_DE,
  CALIBRATION_SENTENCES_DE,
  fillDe,
} from './calibrationCopy';

export const CALIBRATION_STATE_VERSION = 1;

export const CALIBRATION_STATES = Object.freeze(['ok', 'blocked', 'needs_arm_check', 'arm_check_running',
  'confirm_target_removed', 'handheld_running', 'unknown']);

// /calibration/start's steps (StartCalibration.step).
export const CALIBRATION_STEPS = Object.freeze({
  MAT_CHECK: 'mat_check',
  ARM_CHECK: 'arm_check',
  TARGET_REMOVED: 'target_removed',
  HANDHELD: 'handheld',
});

export const VIEWS = Object.freeze({
  CHECKING: 'checking',
  OK: 'ok',
  BLOCKED: 'blocked',
  NEEDS_ARM_CHECK: 'needs_arm_check',
  ARM_CHECK_RUNNING: 'arm_check_running',
  CONFIRM_TARGET_REMOVED: 'confirm_target_removed',
  HANDHELD_RUNNING: 'handheld_running',
});

const VIEW_OF_STATE = Object.freeze({
  ok: VIEWS.OK,
  blocked: VIEWS.BLOCKED,
  needs_arm_check: VIEWS.NEEDS_ARM_CHECK,
  arm_check_running: VIEWS.ARM_CHECK_RUNNING,
  confirm_target_removed: VIEWS.CONFIRM_TARGET_REMOVED,
  handheld_running: VIEWS.HANDHELD_RUNNING,
  unknown: VIEWS.CHECKING,
});

// A blocked mat check whose own sentence ends in „… den Arm-Check starten":
// the button is offered beside „Erneut prüfen".
const BLOCKED_CODES_OFFERING_ARM_CHECK = new Set(['mat_not_flat', 'inconsistent', 'tower_displaced']);
// A lens or fit gate offers the handheld lens step (S4), and so does its own failure.
const CODES_OFFERING_HANDHELD = new Set(['lens', 'rms', 'handheld_failed']);

const isObject = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * One state payload (std_msgs/String data) → a normalised object, or null for
 * anything this page cannot trust: a malformed message leaves the panel on
 * „wird geprüft", never on a guessed state.
 */
export function parseCalibrationState(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!isObject(raw) || raw.v !== CALIBRATION_STATE_VERSION) return null;
  if (!CALIBRATION_STATES.includes(raw.state)) return null;
  return {
    v: raw.v,
    state: raw.state,
    code: typeof raw.code === 'string' ? raw.code : '',
    message_de: typeof raw.message_de === 'string' ? raw.message_de : '',
    notes: Array.isArray(raw.notes) ? raw.notes.filter((n) => typeof n === 'string') : [],
    camera: isObject(raw.camera) ? raw.camera : null,
    arm_check: isObject(raw.arm_check) ? raw.arm_check : null,
    handheld: isObject(raw.handheld) ? raw.handheld : null,
    quality: isObject(raw.quality) ? raw.quality : null,
    fixture: isObject(raw.fixture) ? raw.fixture : null,
    droop_warning: isObject(raw.droop_warning) ? raw.droop_warning : null,
    mat_check: isObject(raw.mat_check) ? raw.mat_check : null,
  };
}

/** Only `ok` unlocks Roboter Studio on the real robot. */
export function isCalibrated(state) {
  return !!state && state.state === 'ok';
}

/** An arm check or the handheld capture is under way (leaving would throw it away). */
export function isCapturing(state) {
  return !!state && (state.state === 'arm_check_running' || state.state === 'handheld_running');
}

function deNumber(value, digits) {
  return value.toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// The robot's sentence when it sent one, else the page's copy with the fields it knows.
function sentenceOf(state) {
  if (!state || state.state === 'unknown') {
    return state && state.message_de ? state.message_de : CALIBRATION_DE.CHECKING;
  }
  if (state.message_de) return state.message_de;
  const template = CALIBRATION_SENTENCES_DE[state.code];
  if (!template) return CALIBRATION_DE.CHECKING;
  const arm = state.arm_check || {};
  const hand = state.handheld || {};
  const mat = state.mat_check || {};
  const readings = [mat.lens_offset_mm, mat.lens_drift_mm].filter(finite);
  return fillDe(template, {
    i: finite(arm.pose) ? arm.pose : null,
    n: finite(arm.poses) ? arm.poses : null,
    k: finite(hand.views) ? hand.views : null,
    mm: readings.length ? deNumber(Math.max(...readings), 1) : null,
  });
}

function progressOf(state) {
  const arm = state && state.arm_check;
  if (!arm || !finite(arm.pose) || !finite(arm.poses) || arm.poses <= 0) return null;
  return { pose: arm.pose, poses: arm.poses, captured: finite(arm.captured) ? arm.captured : 0 };
}

function handheldOf(state) {
  const h = state && state.handheld;
  if (!h || !finite(h.views)) return null;
  const grid = Array.isArray(h.grid) && h.grid.length === 2 && h.grid.every(finite) ? h.grid : [4, 3];
  return {
    views: h.views,
    viewsNeeded: finite(h.views_needed) ? h.views_needed : 18,
    cells: finite(h.cells) ? h.cells : 0,
    cellsNeeded: finite(h.cells_needed) ? h.cells_needed : 9,
    cols: grid[0],
    rows: grid[1],
    filled: Array.isArray(h.filled)
      ? h.filled.filter((s) => Array.isArray(s) && s.length === 3 && s.every(finite))
      : [],
  };
}

function detailsOf(state) {
  const q = (state && state.quality) || {};
  const m = (state && state.mat_check) || {};
  const rows = [];
  const add = (label, value, format) => {
    if (finite(value)) rows.push({ label, value: format(value) });
  };
  const px = (v) => `${deNumber(v, 2)} px`;
  const mm = (v) => `${deNumber(v, 1)} mm`;
  add(CALIBRATION_DE.DETAIL_POSES, q.poses_used, (v) => String(Math.round(v)));
  add(CALIBRATION_DE.DETAIL_RMS, q.rms_px, px);
  add(CALIBRATION_DE.DETAIL_HOLDOUT, q.holdout_rms_px, px);
  add(CALIBRATION_DE.DETAIL_P95, q.holdout_p95_mm, mm);
  add(CALIBRATION_DE.DETAIL_TAGS, finite(m.tags_seen) ? m.tags_seen : q.field_tags, (v) => String(Math.round(v)));
  add(CALIBRATION_DE.DETAIL_SHARPNESS, finite(m.sharpness) ? m.sharpness : q.sharpness, (v) => String(Math.round(v)));
  add(CALIBRATION_DE.DETAIL_LENS_OFFSET, m.lens_offset_mm, mm);
  add(CALIBRATION_DE.DETAIL_LENS_ALONG, m.lens_along_mm, mm);
  add(CALIBRATION_DE.DETAIL_LENS_DRIFT, m.lens_drift_mm, mm);
  return rows.length ? rows : null;
}

/**
 * The panel's view of a state (null = nothing heard yet): `view` (VIEWS), the
 * `sentence`, what it offers (`canRecheck`, `canStartArmCheck`,
 * `canStartHandheld`, `canConfirmTargetRemoved`, `canStop`,
 * `canCancelHandheld`), the arm check's `progress`, the `handheld` capture,
 * the `notes` and the `droop` warning as sentences, and the `details` rows.
 */
export function calibrationView(state) {
  const view = state ? VIEW_OF_STATE[state.state] || VIEWS.CHECKING : VIEWS.CHECKING;
  const code = state ? state.code : '';
  const settled = view === VIEWS.OK || view === VIEWS.BLOCKED || view === VIEWS.NEEDS_ARM_CHECK;
  const droop = state && state.droop_warning;
  return {
    view,
    state: state ? state.state : null,
    code,
    sentence: sentenceOf(state),
    canRecheck: settled || view === VIEWS.CHECKING,
    canStartArmCheck: view === VIEWS.OK || view === VIEWS.NEEDS_ARM_CHECK
      || (view === VIEWS.BLOCKED && BLOCKED_CODES_OFFERING_ARM_CHECK.has(code)),
    canStartHandheld: view === VIEWS.NEEDS_ARM_CHECK && CODES_OFFERING_HANDHELD.has(code),
    canConfirmTargetRemoved: view === VIEWS.CONFIRM_TARGET_REMOVED,
    canStop: view === VIEWS.ARM_CHECK_RUNNING,
    canCancelHandheld: view === VIEWS.HANDHELD_RUNNING,
    progress: progressOf(state),
    handheld: handheldOf(state),
    notes: state ? state.notes.filter((n) => n in CALIBRATION_NOTES_DE).map((n) => CALIBRATION_NOTES_DE[n]) : [],
    droop: droop && typeof droop.message_de === 'string' && droop.message_de ? droop.message_de : null,
    details: state ? detailsOf(state) : null,
  };
}
