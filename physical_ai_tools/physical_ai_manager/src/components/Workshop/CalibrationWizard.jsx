/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useRef } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import { setCurrentStep, setCalibrationStatus } from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import IntrinsicCalibStep from './IntrinsicCalibStep';
import HandEyeCalibStep from './HandEyeCalibStep';
import TableTouchStep from './TableTouchStep';

// WS4 (2026-06-17): scene-cam-only calibration. The two gripper-camera
// steps (intrinsic + eye-in-hand) were removed — the gripper camera is not
// modelled in the omx_f URDF and is unused at runtime. The scene extrinsic
// is now a single-shot board-on-table measurement (no arm motion).
//
// 2026-06-22: the optional 4th step (Farbprofil) was DROPPED — Roboter Studio
// unlocks after the 3 geometry steps. The grasp/projection path needs only
// intrinsics + extrinsic + the touch-off z_table; the colour profile gated
// nothing in the runtime (perception builds fine with an empty profile) and
// only enabled the colour-detection blocks. With the step gone, those blocks
// fail loud ("Farbe … nicht kalibriert"); object + marker detection are
// unaffected. ColorProfileStep.jsx / the /calibration capture-colour service
// are left in place but unreferenced.
const STEPS = [
  { key: 'scene_intrinsic', label: 'Szenen-Kamera (intrinsisch)', component: IntrinsicCalibStep, props: { camera: 'scene' } },
  { key: 'scene_handeye', label: 'Szenen-Kamera (Extrinsik)', component: HandEyeCalibStep, props: { camera: 'scene' } },
  { key: 'table_touch', label: 'Tisch vermessen (Höhe)', component: TableTouchStep, props: {} },
];

function CalibrationWizard() {
  const dispatch = useDispatch();
  const { getCalibrationStatus, cancelCalibration } = useRosServiceCaller();
  const currentStep = useSelector((s) => s.workshop.currentStep);
  const hasIntrinsicScene = useSelector((s) => s.workshop.hasIntrinsicScene);
  const hasHandeyeScene = useSelector((s) => s.workshop.hasHandeyeScene);
  const hasTableTouch = useSelector((s) => s.workshop.hasTableTouch);
  const recalibrating = useSelector((s) => s.workshop.recalibrating);
  // Mount-time snapshot: when the wizard was opened via „Kalibrierung neu
  // starten" the per-step flags were deliberately reset, so we must NOT
  // re-hydrate them from the still-present on-disk YAMLs (that bounced the
  // student straight back to the editor — 2026-06-23). useRef captures the
  // value at mount and never updates, which is exactly the lifetime we want.
  const recalAtMountRef = useRef(recalibrating);

  // Hydrate per-step badges from disk so reloading the page doesn't make
  // students redo intrinsic captures. Run once on mount; the underlying
  // /calibration/status read is cheap (just a few file-existence checks).
  // Audit U4: dependencies narrowed to []. The previous deps
  // [dispatch, getCalibrationStatus, cancelCalibration] re-fired the
  // effect — and its cleanup — every time useRosServiceCaller rebound
  // its callbacks (e.g. on a transient rosbridge reconnect), which
  // sent cancelCalibration('') to the server mid-capture and wiped
  // the per-step buffer. dispatch and the service callbacks are
  // stable for the wizard's lifetime; pin to mount/unmount.
  // Stable refs avoid the React-hooks/exhaustive-deps lint complaint
  // without re-introducing the spurious re-fire.
  const dispatchRef = useRef(dispatch);
  const getStatusRef = useRef(getCalibrationStatus);
  const cancelRef = useRef(cancelCalibration);
  useEffect(() => { dispatchRef.current = dispatch; }, [dispatch]);
  useEffect(() => { getStatusRef.current = getCalibrationStatus; }, [getCalibrationStatus]);
  useEffect(() => { cancelRef.current = cancelCalibration; }, [cancelCalibration]);
  useEffect(() => {
    let cancelled = false;
    // Skip disk hydration during an explicit recalibration — the flags were
    // just reset on purpose and the on-disk YAMLs would undo that.
    if (!recalAtMountRef.current) {
      (async () => {
        try {
          const r = await getStatusRef.current();
          if (!cancelled && r && r.success) {
            dispatchRef.current(setCalibrationStatus(r));
          }
        } catch (_) { /* ignore — wizard works without hydration */ }
      })();
    }
    return () => {
      cancelled = true;
      cancelRef.current('').catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stepStatus = {
    scene_intrinsic: hasIntrinsicScene,
    scene_handeye: hasHandeyeScene,
    table_touch: hasTableTouch,
  };

  // A stale persisted currentStep (e.g. a removed 'gripper_intrinsic' from an
  // older session) must not leave the wizard on a non-existent step; fall back
  // to the first step.
  const active = STEPS.find((s) => s.key === currentStep) || STEPS[0];
  const ActiveComponent = active.component;

  return (
    <div className="flex h-full">
      <aside className="w-72 shrink-0 border-r border-[var(--line)] bg-white p-4">
        <h2 className="text-base font-semibold text-[var(--ink)] mb-4">Kalibrierung</h2>
        <ol className="space-y-2">
          {STEPS.map((step, idx) => {
            const done = stepStatus[step.key];
            const isCurrent = step.key === currentStep;
            return (
              <li key={step.key}>
                <button
                  onClick={() => dispatch(setCurrentStep(step.key))}
                  className={
                    'w-full text-left px-3 py-2 rounded-md text-sm transition ' +
                    (isCurrent
                      ? 'bg-[var(--accent-wash)] text-[var(--accent-ink)] font-medium'
                      : done
                      ? 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]')
                  }
                >
                  <span className="inline-block w-5 mr-2 text-center">
                    {done ? '✓' : idx + 1}
                  </span>
                  {step.label}
                </button>
              </li>
            );
          })}
        </ol>
      </aside>
      <section className="flex-1 overflow-auto p-6">
        <ActiveComponent {...active.props} />
      </section>
    </div>
  );
}

export default CalibrationWizard;
