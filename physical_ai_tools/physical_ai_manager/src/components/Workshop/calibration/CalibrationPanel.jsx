/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Roboter Studio's calibration panel (spec 40 S8), shown on the real robot
// until the calibration state is `ok` (and when the student opens it from the
// toolbar). It replaces the four-step wizard: the robot checks its mat on its
// own (the page asks once on entering Roboter Studio), and the panel shows
// what the robot's /edubotics/calibration_state says and offers the way out it
// names — „Erneut prüfen" (the mat check), „Arm-Check starten" with „Arm in die
// Grundstellung fahren" beside it (ArmCheckView; the arm check starts only at
// the Grundstellung, X-TARGETSTART), „Kalibrier-Ziel ist abgenommen", „Kamera
// von Hand vermessen" (HandheldView). The view and the sentence are
// calibrationModel's; every string is calibrationCopy's. A refusal of a press
// reads the robot's own answer, and so does the way home when it moves or need
// not (the calibration state does not change on that glide).
//
// The panel is an in-page panel, never a fixed overlay: the nav rail stays
// clickable (utils/signOut.js names that escape).

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import {
  clearWorkflowError,
  clearWorkflowLog,
  selectWorkflowRunning,
  setCalibrationStatus,
} from '../../../features/workshop/workshopSlice';
import Icon from '../../icons/Icon';
import ArmCheckView from './ArmCheckView';
import HandheldView from './HandheldView';
import { CALIBRATION_DE, TARGET_REMOVED_BUTTON_DE } from './calibrationCopy';
import { CALIBRATION_STEPS, VIEWS, calibrationView } from './calibrationModel';

// The camera StartCalibration names; the tower calibration has only the scene camera.
const CAMERA = 'scene';
// The Protokoll shows the newest lines of the run.
const PROTOKOLL_LINES = 8;

const SECONDARY = 'inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border '
  + 'border-[var(--line)] bg-white text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] '
  + 'disabled:opacity-50 disabled:cursor-not-allowed';
const PRIMARY = 'inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border '
  + 'bg-[var(--accent)] text-white border-[var(--accent)] hover:opacity-90 '
  + 'disabled:opacity-50 disabled:cursor-not-allowed';

const VIEW_ICON = {
  [VIEWS.OK]: { name: 'checkCircle', className: 'text-emerald-600' },
  [VIEWS.BLOCKED]: { name: 'warning', className: 'text-amber-600' },
  [VIEWS.NEEDS_ARM_CHECK]: { name: 'info', className: 'text-[var(--accent)]' },
  [VIEWS.CONFIRM_TARGET_REMOVED]: { name: 'info', className: 'text-[var(--accent)]' },
  [VIEWS.ARM_CHECK_RUNNING]: { name: 'loading', className: 'text-[var(--accent)] animate-spin' },
  [VIEWS.HANDHELD_RUNNING]: { name: 'camera', className: 'text-[var(--accent)]' },
  [VIEWS.CHECKING]: { name: 'loading', className: 'text-[var(--ink-3)] animate-spin' },
};

function CalibrationPanel({ onClose = null }) {
  const dispatch = useDispatch();
  const { startCalibration, cancelCalibration, stopWorkflow, getCalibrationStatus } = useRosServiceCaller();
  const state = useSelector((s) => s.workshop.calibration);
  const heartbeatStatus = useSelector((s) => (s.tasks ? s.tasks.heartbeatStatus : null));
  const programRunning = useSelector(selectWorkflowRunning);
  const log = useSelector((s) => s.workshop.log);
  const workflowError = useSelector((s) => s.workshop.workflowError);
  const view = useMemo(() => calibrationView(state), [state]);

  const [pending, setPending] = useState(null); // the step whose answer is awaited
  const [answer, setAnswer] = useState(null); // the robot's refusal of the last press
  const [notice, setNotice] = useState(null); // the robot's answer to the way home when it moves or need not
  const [counting, setCounting] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);

  // A refusal belongs to the state it was given in: a new state or code clears it
  // (the 1 Hz re-send of the same state does not).
  const stateKey = state ? `${state.state}/${state.code}` : '';
  useEffect(() => { setAnswer(null); setNotice(null); }, [stateKey]);

  // The three status flags once per mount (/calibration/status: „a valid arm
  // check exists"). Read through a ref so a re-bound caller never re-asks.
  const getStatusRef = useRef(getCalibrationStatus);
  getStatusRef.current = getCalibrationStatus;
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await getStatusRef.current();
        if (!cancelled && r && r.success) dispatch(setCalibrationStatus(r));
      } catch (_) { /* the state topic is the panel's truth; the flags are a courtesy */ }
    })();
    return () => { cancelled = true; };
  }, [dispatch]);

  // „Der Arm fährt in die Grundstellung." is true only until that glide ends.
  const wasRunningRef = useRef(programRunning);
  useEffect(() => {
    if (wasRunningRef.current && !programRunning) setNotice(null);
    wasRunningRef.current = programRunning;
  }, [programRunning]);

  const offline = heartbeatStatus !== 'connected';
  const moveBlocked = offline ? CALIBRATION_DE.OFFLINE
    : (programRunning && view.view !== VIEWS.ARM_CHECK_RUNNING ? CALIBRATION_DE.PROGRAM_RUNNING : null);

  const call = useCallback(async (key, request, { showSuccess = false } = {}) => {
    setPending(key);
    setAnswer(null);
    setNotice(null);
    try {
      const r = await request();
      if (!r || r.success === false) setAnswer((r && r.message) || CALIBRATION_DE.NO_ANSWER);
      else if (showSuccess && r.message) setNotice(r.message);
    } catch (_) {
      setAnswer(CALIBRATION_DE.NO_ANSWER);
    } finally {
      setPending(null);
    }
  }, []);

  const runStep = useCallback((step) => call(step, () => startCalibration(CAMERA, step)), [call, startCalibration]);
  const startArmCheck = useCallback(() => {
    // The Protokoll of the panel is the arm check's own.
    dispatch(clearWorkflowLog());
    dispatch(clearWorkflowError());
    return runStep(CALIBRATION_STEPS.ARM_CHECK);
  }, [dispatch, runStep]);
  const goHome = useCallback(
    () => call(CALIBRATION_STEPS.HOME, () => startCalibration(CAMERA, CALIBRATION_STEPS.HOME), { showSuccess: true }),
    [call, startCalibration],
  );
  const stopArmCheck = useCallback(() => call('stop', () => stopWorkflow()), [call, stopWorkflow]);
  const cancelHandheld = useCallback(() => call('cancel', () => cancelCalibration('')), [call, cancelCalibration]);

  const icon = VIEW_ICON[view.view] || VIEW_ICON[VIEWS.CHECKING];
  const busy = pending !== null || counting;
  const protokoll = (Array.isArray(log) ? log : []).slice(-PROTOKOLL_LINES);

  return (
    <div className="flex h-full justify-center overflow-auto p-4 sm:p-6">
      <section
        aria-labelledby="calibration-title"
        className="flex w-full max-w-2xl flex-col gap-4 self-start rounded-lg border border-[var(--line)] bg-white p-4 sm:p-6"
      >
        <h2 id="calibration-title" className="text-base font-semibold text-[var(--ink)]">
          {CALIBRATION_DE.TITLE}
        </h2>

        <div className="flex items-start gap-2">
          <Icon name={icon.name} className={`mt-0.5 shrink-0 ${icon.className}`} />
          <p className="text-sm text-[var(--ink)]" aria-live="polite">{view.sentence}</p>
        </div>

        {view.droop && (
          <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900" role="status">
            {view.droop}
          </p>
        )}

        {view.notes.length > 0 && (
          <ul aria-label={CALIBRATION_DE.NOTES} className="flex flex-col gap-1 text-sm text-[var(--ink-3)]">
            {view.notes.map((note) => (
              <li key={note} className="flex items-start gap-1.5">
                <Icon name="info" className="mt-0.5 shrink-0" />
                <span>{note}</span>
              </li>
            ))}
          </ul>
        )}

        {answer && (
          <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800" role="alert">
            {answer}
          </p>
        )}

        {notice && (
          <p className="rounded-md border border-[var(--line)] bg-[var(--bg-sunk)] px-3 py-2 text-sm text-[var(--ink)]" role="status">
            {notice}
          </p>
        )}

        {view.view === VIEWS.HANDHELD_RUNNING && (
          <HandheldView handheld={view.handheld} onCancel={cancelHandheld} disabled={pending !== null} />
        )}

        <ArmCheckView
          view={view}
          blockedReason={moveBlocked || (pending !== null ? CALIBRATION_DE.CHECKING : null)}
          onStart={startArmCheck}
          onStop={stopArmCheck}
          onCountdownChange={setCounting}
          onHome={goHome}
          programRunning={programRunning}
          onStopProgram={stopArmCheck}
        />

        {(view.canConfirmTargetRemoved || view.canStartHandheld || view.canRecheck
          || (view.view === VIEWS.OK && onClose)) && (
          <div className="flex flex-wrap items-center gap-2">
            {view.canConfirmTargetRemoved && (
              <button
                type="button"
                className={PRIMARY}
                disabled={offline || busy}
                onClick={() => runStep(CALIBRATION_STEPS.TARGET_REMOVED)}
              >
                <Icon name="check" />
                {TARGET_REMOVED_BUTTON_DE}
              </button>
            )}
            {view.canStartHandheld && (
              <button
                type="button"
                className={SECONDARY}
                disabled={!!moveBlocked || busy}
                title={moveBlocked || undefined}
                onClick={() => runStep(CALIBRATION_STEPS.HANDHELD)}
              >
                <Icon name="camera" />
                {CALIBRATION_DE.HANDHELD_START}
              </button>
            )}
            {view.canRecheck && (
              <button
                type="button"
                className={SECONDARY}
                disabled={offline || busy}
                title={offline ? CALIBRATION_DE.OFFLINE : undefined}
                onClick={() => runStep(CALIBRATION_STEPS.MAT_CHECK)}
              >
                <Icon name="refresh" />
                {pending === CALIBRATION_STEPS.MAT_CHECK ? CALIBRATION_DE.RECHECKING : CALIBRATION_DE.RECHECK}
              </button>
            )}
            {view.view === VIEWS.OK && onClose && (
              <button type="button" className={PRIMARY} onClick={onClose} disabled={counting}>
                <Icon name="arrowRight" />
                {CALIBRATION_DE.BACK_TO_EDITOR}
              </button>
            )}
          </div>
        )}

        {(protokoll.length > 0 || workflowError) && (
          <div
            role="log"
            aria-label={CALIBRATION_DE.PROTOKOLL}
            className="flex flex-col gap-0.5 rounded-md border border-[var(--line)] bg-[var(--bg-sunk)] px-3 py-2 text-xs text-[var(--ink-3)]"
          >
            <span className="font-medium text-[var(--ink)]">{CALIBRATION_DE.PROTOKOLL}</span>
            {protokoll.map((line, i) => (
              // Lines repeat (a warning per pose): the time and the position name one.
              <span key={`${line.ts}-${i}`}>{line.text}</span>
            ))}
            {workflowError && <span className="text-red-700">{workflowError}</span>}
          </div>
        )}

        {view.details && (
          <div className="flex flex-col gap-2">
            <button
              type="button"
              aria-expanded={detailsOpen}
              onClick={() => setDetailsOpen((v) => !v)}
              className="inline-flex items-center gap-1 self-start text-xs text-[var(--ink-3)] hover:text-[var(--ink)]"
            >
              <Icon name={detailsOpen ? 'chevronUp' : 'chevronDown'} />
              {CALIBRATION_DE.DETAILS}
            </button>
            {detailsOpen && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                {view.details.map((row) => (
                  <React.Fragment key={row.label}>
                    <dt className="text-[var(--ink-3)]">{row.label}</dt>
                    <dd className="font-mono text-[var(--ink)]">{row.value}</dd>
                  </React.Fragment>
                ))}
              </dl>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

export default CalibrationPanel;
