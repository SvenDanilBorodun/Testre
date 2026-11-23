/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Roboter Studio's calibration panel (spec 40 S8): the robot's calibration
// state decides the view; the panel offers the way out the state names. The
// arm check is ONE deliberate press with a 5 s countdown and „Stopp" (S6 5.),
// for everyone (D13: a student too). The service calls are the hook's; what is
// asserted here is which call a press makes, and when.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import workshopReducer, {
  setCalibrationState, setRunState, setWorkflowStatus,
} from '../../../../features/workshop/workshopSlice';
import CalibrationPanel from '../CalibrationPanel';
import { CALIBRATION_DE, CALIBRATION_NOTES_DE, TARGET_REMOVED_BUTTON_DE } from '../calibrationCopy';
import { ARM_CHECK_COUNTDOWN_S } from '../ArmCheckView';

const mockRos = vi.hoisted(() => ({
  startCalibration: vi.fn(),
  cancelCalibration: vi.fn(),
  stopWorkflow: vi.fn(),
  getCalibrationStatus: vi.fn(),
}));
vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

function makeStore({ heartbeatStatus = 'connected', role = 'student' } = {}) {
  return configureStore({
    reducer: {
      workshop: workshopReducer,
      tasks: () => ({ heartbeatStatus }),
      auth: () => ({ isAuthenticated: true, role }),
    },
  });
}

function calib(state, code = state, extra = {}) {
  return {
    v: 1, state, code, message_de: '', notes: [], camera: null, arm_check: null, handheld: null,
    quality: null, fixture: { id: 'tower_v1', lift_m: 0.0072 }, droop_warning: null, mat_check: null, ...extra,
  };
}

function renderPanel(state, { store = makeStore(), onClose } = {}) {
  if (state) store.dispatch(setCalibrationState(state));
  const utils = render(<Provider store={store}><CalibrationPanel onClose={onClose} /></Provider>);
  return { ...utils, store };
}

// The server's own sentences, read from its source: the Protokoll must show them
// exactly as the robot says them.
function pyConstant(rel, name) {
  const file = path.resolve(process.cwd(), '../physical_ai_server/physical_ai_server', rel);
  const source = fs.readFileSync(file, 'utf8');
  const m = source.match(new RegExp(`^${name} = (\\(?(?:\\s*'[^']*')+\\)?)$`, 'm'));
  expect(m).not.toBeNull();
  return [...m[1].matchAll(/'([^']*)'/g)].map((x) => x[1]).join('');
}

beforeEach(() => {
  mockRos.startCalibration.mockReset().mockResolvedValue({ success: true, message: '' });
  mockRos.cancelCalibration.mockReset().mockResolvedValue({ success: true, message: '' });
  mockRos.stopWorkflow.mockReset().mockResolvedValue({ success: true, message: '' });
  mockRos.getCalibrationStatus.mockReset().mockResolvedValue({
    success: true, has_scene_intrinsics: true, has_scene_handeye: true, has_table_plane: true,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('CalibrationPanel — needs_arm_check', () => {
  it('shows a student „Arm-Check starten" with the preparation lines and the robot\'s sentence', () => {
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none', {
      message_de: 'Dieser Roboter ist noch nicht eingemessen. Bitte den Arm-Check starten.',
    }), { store: makeStore({ role: 'student' }) });
    expect(screen.getByText('Dieser Roboter ist noch nicht eingemessen. Bitte den Arm-Check starten.'))
      .toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Arm-Check starten' });
    expect(button).toBeEnabled();
    for (const line of CALIBRATION_DE.ARM_CHECK_PREP) expect(screen.getByText(line)).toBeInTheDocument();
    expect(CALIBRATION_DE.ARM_CHECK_PREP).toContain('30 cm hinter dem Roboter frei lassen.');
  });

  it('the countdown runs 5 s before the arm check starts', async () => {
    vi.useFakeTimers();
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    expect(ARM_CHECK_COUNTDOWN_S).toBe(5);
    fireEvent.click(screen.getByRole('button', { name: 'Arm-Check starten' }));
    expect(screen.getByText('Der Arm bewegt sich in 5 s …')).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText('Der Arm bewegt sich in 4 s …')).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(3999); });
    expect(mockRos.startCalibration).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(mockRos.startCalibration).toHaveBeenCalledTimes(1);
    expect(mockRos.startCalibration).toHaveBeenCalledWith('scene', 'arm_check');
  });

  it('„Stopp" during the countdown starts nothing', () => {
    vi.useFakeTimers();
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    fireEvent.click(screen.getByRole('button', { name: 'Arm-Check starten' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stopp' }));
    act(() => { vi.advanceTimersByTime(10000); });
    expect(mockRos.startCalibration).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Arm-Check starten' })).toBeInTheDocument();
  });

  it('a refused start shows the robot\'s sentence', async () => {
    vi.useFakeTimers();
    mockRos.startCalibration.mockResolvedValue({ success: false, message: 'Die Turm-Kamera liefert kein Bild.' });
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    fireEvent.click(screen.getByRole('button', { name: 'Arm-Check starten' }));
    await act(async () => { vi.advanceTimersByTime(5000); });
    vi.useRealTimers();
    expect(await screen.findByText('Die Turm-Kamera liefert kein Bild.')).toBeInTheDocument();
  });

  it('offline, or while a program runs, the button says why and starts nothing', () => {
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'), { store: makeStore({ heartbeatStatus: 'disconnected' }) });
    expect(screen.getByRole('button', { name: 'Arm-Check starten' })).toBeDisabled();
    expect(screen.getByText(CALIBRATION_DE.OFFLINE)).toBeInTheDocument();
  });

  it('a lens gate offers „Kamera von Hand vermessen", which starts the handheld step', async () => {
    renderPanel(calib('needs_arm_check', 'lens'));
    fireEvent.click(screen.getByRole('button', { name: 'Kamera von Hand vermessen' }));
    await waitFor(() => expect(mockRos.startCalibration).toHaveBeenCalledWith('scene', 'handheld'));
  });

  it('no lens gate, no handheld offer', () => {
    renderPanel(calib('needs_arm_check', 'too_few_poses'));
    expect(screen.queryByRole('button', { name: 'Kamera von Hand vermessen' })).toBeNull();
  });
});

describe('CalibrationPanel — the way to the Grundstellung (X-TARGETSTART)', () => {
  const HOME = 'Arm in die Grundstellung fahren';

  it('the preparation starts at the Grundstellung and clips the target on only there', () => {
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    expect(CALIBRATION_DE.ARM_CHECK_PREP[0]).toBe(
      'Zuerst den Arm in die Grundstellung fahren (Knopf unten) – erst dann das Kalibrier-Ziel aufklipsen.',
    );
    const items = within(screen.getByRole('list')).getAllByRole('listitem').map((li) => li.textContent);
    expect(items[0]).toBe(CALIBRATION_DE.ARM_CHECK_PREP[0]);
    expect(items).toContain('Kalibrier-Ziel auf den Greifer aufklipsen.');
  });

  it('„Arm in die Grundstellung fahren" sends step home at once, with no countdown', async () => {
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    expect(CALIBRATION_DE.ARM_CHECK_HOME).toBe(HOME);
    fireEvent.click(screen.getByRole('button', { name: HOME }));
    await waitFor(() => expect(mockRos.startCalibration).toHaveBeenCalledTimes(1));
    expect(mockRos.startCalibration).toHaveBeenCalledWith('scene', 'home');
    expect(screen.queryByText(/Der Arm bewegt sich in/)).toBeNull();
  });

  it('the robot\'s answer is shown: that the arm moves, and a refusal as it comes', async () => {
    const going = pyConstant('workflow/autocal/texts_de.py', 'ARM_GOING_HOME_DE');
    expect(going).toBe('Der Arm fährt in die Grundstellung.');
    mockRos.startCalibration.mockResolvedValueOnce({ success: true, message: going });
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    fireEvent.click(screen.getByRole('button', { name: HOME }));
    expect(await screen.findByText(going)).toBeInTheDocument();
    expect(screen.getByText(going)).toHaveAttribute('role', 'status');
    // an older robot does not know the step: its own refusal reads as it comes
    const old = 'Diesen Kalibrier-Schritt gibt es für diesen Roboter nicht.';
    mockRos.startCalibration.mockResolvedValueOnce({ success: false, message: old });
    fireEvent.click(screen.getByRole('button', { name: HOME }));
    expect(await screen.findByRole('alert')).toHaveTextContent(old);
    expect(screen.queryByText(going)).toBeNull();
  });

  it('a start away from the Grundstellung reads the robot\'s sentence, which names this button', async () => {
    vi.useFakeTimers();
    const needsHome = pyConstant('workflow/autocal/texts_de.py', 'ARM_CHECK_NEEDS_HOME_DE');
    expect(needsHome).toContain(`„${HOME}“`);
    mockRos.startCalibration.mockResolvedValue({ success: false, message: needsHome });
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    fireEvent.click(screen.getByRole('button', { name: 'Arm-Check starten' }));
    await act(async () => { vi.advanceTimersByTime(5000); });
    vi.useRealTimers();
    expect(await screen.findByRole('alert')).toHaveTextContent(needsHome);
  });

  it('disabled while a step runs, offline, and while a program runs — then „Stopp" stops it', async () => {
    let answer;
    mockRos.startCalibration.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    fireEvent.click(screen.getByRole('button', { name: HOME }));
    expect(screen.getByRole('button', { name: HOME })).toBeDisabled();
    await act(async () => { answer({ success: true, message: '' }); });
    expect(screen.getByRole('button', { name: HOME })).toBeEnabled();
    renderPanel(calib('needs_arm_check', 'needs_arm_check/none'), { store: makeStore({ heartbeatStatus: 'disconnected' }) });
    expect(screen.getAllByRole('button', { name: HOME })[1]).toBeDisabled();
  });

  it('while the glide home runs the panel offers „Stopp" (→ /workflow/stop) and clears the answer at its end', async () => {
    const going = 'Der Arm fährt in die Grundstellung.';
    mockRos.startCalibration.mockResolvedValueOnce({ success: true, message: going });
    const { store } = renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    expect(screen.queryByRole('button', { name: 'Stopp' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: HOME }));
    expect(await screen.findByText(going)).toBeInTheDocument();
    act(() => { store.dispatch(setRunState('running')); });
    expect(screen.getByRole('button', { name: HOME })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Arm-Check starten' })).toBeDisabled();
    expect(screen.getByText(CALIBRATION_DE.PROGRAM_RUNNING)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Stopp' }));
    await waitFor(() => expect(mockRos.stopWorkflow).toHaveBeenCalledTimes(1));
    act(() => { store.dispatch(setRunState('finished')); });
    expect(screen.queryByRole('button', { name: 'Stopp' })).toBeNull();
    expect(screen.queryByText(going)).toBeNull();
    expect(screen.getByRole('button', { name: HOME })).toBeEnabled();
  });
});

describe('CalibrationPanel — the arm check runs', () => {
  it('shows „Pose 7 von 20" and „Stopp" calls /workflow/stop', async () => {
    renderPanel(calib('arm_check_running', 'arm_check_running', {
      message_de: 'Arm-Check läuft: Pose 7 von 20.',
      arm_check: { pose: 7, poses: 20, captured: 6 },
    }));
    expect(screen.getByText(/Pose 7 von 20/)).toBeInTheDocument();
    const bar = screen.getByRole('progressbar', { name: CALIBRATION_DE.ARM_CHECK_PROGRESS });
    expect(bar).toHaveAttribute('aria-valuenow', '7');
    expect(bar).toHaveAttribute('aria-valuemax', '20');
    fireEvent.click(screen.getByRole('button', { name: 'Stopp' }));
    await waitFor(() => expect(mockRos.stopWorkflow).toHaveBeenCalledTimes(1));
    expect(mockRos.startCalibration).not.toHaveBeenCalled();
  });

  it('the Protokoll shows the tower guard\'s sentences verbatim', () => {
    const slow = pyConstant('workflow/handlers/trajectory.py', 'TOWER_CHECK_SLOW_DE');
    const droop = pyConstant('workflow/fixture_guard.py', 'TOWER_DROOP_UNJUDGEABLE_DE');
    expect(slow).toBe('Die Bewegung wird auf Abstand zum Kameraturm geprüft – einen Moment …');
    expect(droop).toMatch(/^Dieser Arm hängt zu stark durch/);
    const store = makeStore();
    store.dispatch(setWorkflowStatus({ log_message: slow }));
    store.dispatch(setWorkflowStatus({ error: droop }));
    renderPanel(calib('needs_arm_check', 'stopped'), { store });
    const log = screen.getByRole('log', { name: CALIBRATION_DE.PROTOKOLL });
    expect(within(log).getByText(slow)).toBeInTheDocument();
    expect(within(log).getByText(droop)).toBeInTheDocument();
  });

  it('a new arm check starts with an empty Protokoll', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    store.dispatch(setWorkflowStatus({ log_message: 'alte Zeile' }));
    renderPanel(calib('needs_arm_check', 'stopped'), { store });
    fireEvent.click(screen.getByRole('button', { name: 'Arm-Check starten' }));
    await act(async () => { vi.advanceTimersByTime(5000); });
    expect(store.getState().workshop.log).toEqual([]);
    expect(store.getState().workshop.workflowError).toBeNull();
  });
});

describe('CalibrationPanel — after the arm check', () => {
  it('confirm_target_removed: the button calls step target_removed', async () => {
    renderPanel(calib('confirm_target_removed', 'confirm_target_removed', {
      message_de: 'Bitte das Kalibrier-Ziel jetzt vom Greifer abnehmen.',
    }));
    expect(TARGET_REMOVED_BUTTON_DE).toBe('Kalibrier-Ziel ist abgenommen');
    fireEvent.click(screen.getByRole('button', { name: TARGET_REMOVED_BUTTON_DE }));
    await waitFor(() => expect(mockRos.startCalibration).toHaveBeenCalledWith('scene', 'target_removed'));
  });

  it('shows the droop_above_design note\'s German sentence when the state carries it', () => {
    renderPanel(calib('confirm_target_removed', 'confirm_target_removed', { notes: ['droop_above_design'] }));
    expect(screen.getByText(CALIBRATION_NOTES_DE.droop_above_design)).toBeInTheDocument();
    expect(screen.getByText(/Dieser Arm hängt etwas stärker durch als vorgesehen\./)).toBeInTheDocument();
  });

  it('shows the robot\'s droop warning', () => {
    renderPanel(calib('ok', 'ok', { droop_warning: { joint: 2, deg: 7, message_de: 'Achtung: Satz.' } }));
    expect(screen.getByText('Achtung: Satz.')).toBeInTheDocument();
  });
});

describe('CalibrationPanel — the mat check', () => {
  it('blocked: „Erneut prüfen" runs the mat check, and a refusal reads the robot\'s sentence', async () => {
    mockRos.startCalibration.mockResolvedValue({ success: false, message: 'Das Kamerabild ist zu dunkel.' });
    renderPanel(calib('blocked', 'too_dark', { message_de: 'Das Kamerabild ist zu dunkel. Bitte das Licht einschalten.' }));
    expect(screen.queryByRole('button', { name: 'Arm-Check starten' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Erneut prüfen' }));
    await waitFor(() => expect(mockRos.startCalibration).toHaveBeenCalledWith('scene', 'mat_check'));
  });

  it('nothing heard yet reads „wird geprüft" and offers the check', () => {
    renderPanel(null);
    expect(screen.getByText(CALIBRATION_DE.CHECKING)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Erneut prüfen' })).toBeEnabled();
  });

  it('„Details" shows the quality numbers, „Schärfe" and „Abweichung der Kamera"', () => {
    renderPanel(calib('ok', 'ok', {
      quality: { poses_used: 19, rms_px: 0.31, holdout_rms_px: 0.4, holdout_p95_mm: 1.8, field_tags: 14, sharpness: 80 },
      mat_check: { tags_seen: 14, sharpness: 91.2, lens_offset_mm: 3.26, lens_along_mm: 7.1, lens_drift_mm: 1.0 },
    }));
    expect(screen.queryByText('Schärfe')).toBeNull();
    const toggle = screen.getByRole('button', { name: CALIBRATION_DE.DETAILS });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Schärfe')).toBeInTheDocument();
    expect(screen.getByText('91')).toBeInTheDocument();
    expect(screen.getByText('Abweichung der Kamera')).toBeInTheDocument();
    expect(screen.getByText('3,3 mm')).toBeInTheDocument();
  });

  it('ok, opened from the editor: „Weiter zum Editor" closes the panel', () => {
    const onClose = vi.fn();
    renderPanel(calib('ok', 'ok'), { onClose });
    fireEvent.click(screen.getByRole('button', { name: CALIBRATION_DE.BACK_TO_EDITOR }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('hydrates the three status flags from /calibration/status on mount', async () => {
    const { store } = renderPanel(calib('needs_arm_check', 'needs_arm_check/none'));
    await waitFor(() => expect(store.getState().workshop.hasTableTouch).toBe(true));
    expect(mockRos.getCalibrationStatus).toHaveBeenCalledTimes(1);
  });
});

describe('CalibrationPanel — the handheld lens step', () => {
  it('shows the views, the filled image cells and „Abbrechen" (→ /calibration/cancel)', async () => {
    renderPanel(calib('handheld_running', 'handheld_running', {
      message_de: 'Kamera von Hand vermessen: 2 von 18 Ansichten. Bitte das Kalibrier-Ziel langsam durch das ganze Bild bewegen.',
      handheld: { views: 2, views_needed: 18, cells: 2, cells_needed: 9, grid: [4, 3], filled: [[0, 0, 0], [3, 2, 1]] },
    }));
    expect(screen.getByText(/2 von 18 Ansichten/)).toBeInTheDocument();
    const grid = screen.getByRole('group', { name: CALIBRATION_DE.HANDHELD_GRID });
    // eslint-disable-next-line testing-library/no-node-access
    expect(grid.querySelectorAll('[data-filled="true"]')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: CALIBRATION_DE.HANDHELD_CANCEL }));
    await waitFor(() => expect(mockRos.cancelCalibration).toHaveBeenCalledTimes(1));
  });
});
