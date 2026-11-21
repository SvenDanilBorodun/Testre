// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The tower calibration in the workshop slice (spec 40 S8): the robot's
// /edubotics/calibration_state is kept as `calibration`, `calibrated` is
// `calibration.state === 'ok'` and nothing else, the three status flags stay
// hydrated from /calibration/status, and the scene-camera profile the arm
// container states decides whether the simulator and the twin draw the tower.
// All of it describes the RIG, so a sign-out keeps it.

import reducer, {
  selectCalibrated,
  selectCalibrationHasUnsolvedCaptures,
  selectTowerScene,
  setCalibrationState,
  setCalibrationStatus,
  setSceneCameraProfile,
} from '../workshopSlice';
import * as workshopSlice from '../workshopSlice';
import { signedOut } from '../../session/sessionActions';

const initial = reducer(undefined, { type: '@@INIT' });
const root = (workshop) => ({ workshop });
const calib = (state, code = state) => ({
  v: 1, state, code, message_de: '', notes: [], camera: null, arm_check: null, handheld: null,
  quality: null, fixture: { id: 'tower_v1', lift_m: 0.0072 }, droop_warning: null, mat_check: null,
});

describe('workshopSlice — the tower calibration', () => {
  test('nothing is known at first: not calibrated, no profile', () => {
    expect(initial.calibration).toBeNull();
    expect(initial.sceneCameraProfile).toBeNull();
    expect(selectCalibrated(root(initial))).toBe(false);
    expect(selectTowerScene(root(initial))).toBe(false);
  });

  test('only the state ok is calibrated', () => {
    for (const s of ['ok', 'blocked', 'needs_arm_check', 'arm_check_running', 'confirm_target_removed',
      'handheld_running', 'unknown']) {
      const next = reducer(initial, setCalibrationState(calib(s)));
      expect(next.calibration.state).toBe(s);
      expect([s, selectCalibrated(root(next))]).toEqual([s, s === 'ok']);
    }
  });

  test('the three status flags do not unlock the editor any more', () => {
    const next = reducer(initial, setCalibrationStatus({
      has_scene_intrinsics: true, has_scene_handeye: true, has_table_plane: true,
    }));
    expect(next.hasIntrinsicScene).toBe(true);
    expect(next.hasHandeyeScene).toBe(true);
    expect(next.hasTableTouch).toBe(true);
    expect(selectCalibrated(root(next))).toBe(false);
  });

  test('selectCalibrationHasUnsolvedCaptures (name kept) is true while an arm check or the handheld capture runs', () => {
    for (const s of ['ok', 'blocked', 'needs_arm_check', 'arm_check_running', 'confirm_target_removed',
      'handheld_running', 'unknown']) {
      const next = reducer(initial, setCalibrationState(calib(s)));
      expect([s, selectCalibrationHasUnsolvedCaptures(root(next))])
        .toEqual([s, s === 'arm_check_running' || s === 'handheld_running']);
    }
    expect(selectCalibrationHasUnsolvedCaptures(root(initial))).toBe(false);
  });

  test('a null state (the link went away) clears the calibration', () => {
    let next = reducer(initial, setCalibrationState(calib('ok')));
    next = reducer(next, setCalibrationState(null));
    expect(next.calibration).toBeNull();
    expect(selectCalibrated(root(next))).toBe(false);
  });

  test('the stated scene-camera profile: tower_v1 draws the tower, legacy and silence do not', () => {
    let next = reducer(initial, setSceneCameraProfile('tower_v1'));
    expect(selectTowerScene(root(next))).toBe(true);
    next = reducer(next, setSceneCameraProfile('legacy'));
    expect(next.sceneCameraProfile).toBe('legacy');
    expect(selectTowerScene(root(next))).toBe(false);
    next = reducer(next, setSceneCameraProfile('something else'));
    expect(next.sceneCameraProfile).toBeNull();
    next = reducer(next, setSceneCameraProfile(null));
    expect(selectTowerScene(root(next))).toBe(false);
  });

  test('a sign-out keeps the rig\'s calibration and its profile', () => {
    let next = reducer(initial, setCalibrationState(calib('ok')));
    next = reducer(next, setSceneCameraProfile('tower_v1'));
    next = reducer(next, setCalibrationStatus({ has_scene_intrinsics: true }));
    next = reducer(next, signedOut());
    expect(next.calibration.state).toBe('ok');
    expect(next.sceneCameraProfile).toBe('tower_v1');
    expect(next.hasIntrinsicScene).toBe(true);
  });

  test('the wizard\'s reducers and fields are gone', () => {
    for (const name of ['requestRecalibration', 'markStepComplete', 'finishVerify', 'setCalibProgress',
      'addCoverageCell', 'setCharucoPreview', 'setVerifyResult', 'setAccuracyResult', 'setCalibHistory']) {
      expect([name, name in workshopSlice]).toEqual([name, false]);
    }
    for (const field of ['recalibrating', 'pendingVerify', 'framesCaptured', 'currentStep', 'coverageMosaic',
      'accuracyResult', 'calibHistory']) {
      expect([field, field in initial]).toEqual([field, false]);
    }
  });
});
