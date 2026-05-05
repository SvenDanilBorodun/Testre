/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React from 'react';
import { useSelector } from 'react-redux';

function HandEyeCalibStep({ camera }) {
  const framesCaptured = useSelector((s) => s.workshop.framesCaptured);
  const framesRequired = useSelector((s) => s.workshop.framesRequired);
  const methodDisagreement = useSelector((s) => s.workshop.methodDisagreement);

  const cameraLabel = camera === 'gripper' ? 'Greifer-Kamera (eye-in-hand)' : 'Szenen-Kamera (eye-to-base)';

  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">
        Hand-Auge-Kalibrierung — {cameraLabel}
      </h3>
      <p className="text-sm text-[var(--ink-3)] mb-4">
        Der Roboter fährt eine Reihe von Kalibrier-Posen automatisch an. Bitte
        kontrolliere bei jeder Pose, dass die ChArUco-Tafel im Bild zu sehen ist.
      </p>

      <div className="bg-white border border-[var(--line)] rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-[var(--ink-3)]">Posen erfasst</span>
          <span className="text-sm font-mono">
            {framesCaptured} / {framesRequired}
          </span>
        </div>
        {methodDisagreement !== null && methodDisagreement !== undefined && (
          <p className="text-xs text-[var(--ink-4)] mt-2">
            Übereinstimmung PARK ↔ TSAI: {methodDisagreement.toFixed(2)}°
          </p>
        )}
      </div>

      <p className="text-xs text-[var(--ink-4)] italic">
        Auto-Pose-Sampler und Service-Aufrufe werden in einem Folge-Update verdrahtet.
      </p>
    </div>
  );
}

export default HandEyeCalibStep;
