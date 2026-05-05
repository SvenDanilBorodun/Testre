/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import { setCurrentStep, setCalibrationStatus } from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import IntrinsicCalibStep from './IntrinsicCalibStep';
import HandEyeCalibStep from './HandEyeCalibStep';
import ColorProfileStep from './ColorProfileStep';

const STEPS = [
  { key: 'gripper_intrinsic', label: 'Greifer-Kamera (intrinsisch)', component: IntrinsicCalibStep, props: { camera: 'gripper' } },
  { key: 'scene_intrinsic', label: 'Szenen-Kamera (intrinsisch)', component: IntrinsicCalibStep, props: { camera: 'scene' } },
  { key: 'gripper_handeye', label: 'Greifer-Kamera (Hand-Auge)', component: HandEyeCalibStep, props: { camera: 'gripper' } },
  { key: 'scene_handeye', label: 'Szenen-Kamera (Hand-Auge)', component: HandEyeCalibStep, props: { camera: 'scene' } },
  { key: 'color_profile', label: 'Farbprofil', component: ColorProfileStep, props: {} },
];

function CalibrationWizard() {
  const dispatch = useDispatch();
  const { getCalibrationStatus, cancelCalibration } = useRosServiceCaller();
  const currentStep = useSelector((s) => s.workshop.currentStep);
  const hasIntrinsicGripper = useSelector((s) => s.workshop.hasIntrinsicGripper);
  const hasIntrinsicScene = useSelector((s) => s.workshop.hasIntrinsicScene);
  const hasHandeyeGripper = useSelector((s) => s.workshop.hasHandeyeGripper);
  const hasHandeyeScene = useSelector((s) => s.workshop.hasHandeyeScene);
  const hasColorProfile = useSelector((s) => s.workshop.hasColorProfile);

  // Hydrate per-step badges from disk so reloading the page doesn't make
  // students redo intrinsic captures. Run once on mount; the underlying
  // /calibration/status read is cheap (just a few file-existence checks).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await getCalibrationStatus();
        if (!cancelled && r && r.success) {
          dispatch(setCalibrationStatus(r));
        }
      } catch (_) { /* ignore — wizard works without hydration */ }
    })();
    // On unmount: tell the server to drop in-flight buffers and clear
    // on_calibration so closing the wizard doesn't leave the global mutex
    // stuck (recording/inference/training would otherwise be blocked
    // until a robot-type switch).
    return () => {
      cancelled = true;
      cancelCalibration('').catch(() => {});
    };
  }, [dispatch, getCalibrationStatus, cancelCalibration]);

  const stepStatus = {
    gripper_intrinsic: hasIntrinsicGripper,
    scene_intrinsic: hasIntrinsicScene,
    gripper_handeye: hasHandeyeGripper,
    scene_handeye: hasHandeyeScene,
    color_profile: hasColorProfile,
  };

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
