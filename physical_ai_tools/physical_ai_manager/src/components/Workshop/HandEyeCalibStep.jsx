/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useState, useCallback, useEffect } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import {
  markStepComplete,
  setCalibProgress,
  setMethodDisagreement,
  setCalibError,
  resetCalibProgress,
} from '../../features/workshop/workshopSlice';

const SETTLE_MS = 1000;

function HandEyeCalibStep({ camera }) {
  const dispatch = useDispatch();
  const {
    startCalibration,
    autoPoseSuggest,
    executeCalibrationPose,
    calibrationCaptureFrame,
    calibrationSolve,
  } = useRosServiceCaller();
  const framesCaptured = useSelector((s) => s.workshop.framesCaptured);
  const framesRequired = useSelector((s) => s.workshop.framesRequired);
  const methodDisagreement = useSelector((s) => s.workshop.methodDisagreement);
  const calibError = useSelector((s) => s.workshop.calibError);
  const [busy, setBusy] = useState(null);
  const [started, setStarted] = useState(false);

  const cameraLabel = camera === 'gripper'
    ? 'Greifer-Kamera (eye-in-hand)'
    : 'Szenen-Kamera (eye-to-base)';

  // Reset progress on camera change so a switch doesn't show stale
  // counts.
  useEffect(() => {
    setStarted(false);
    dispatch(resetCalibProgress());
    dispatch(setCalibProgress({ framesCaptured: 0, framesRequired: 14 }));
  }, [camera, dispatch]);

  const handleStart = useCallback(async () => {
    setBusy('start');
    try {
      const r = await startCalibration(camera, 'handeye');
      if (!r.success) {
        toast.error(r.message || 'Kalibrierung konnte nicht gestartet werden.');
        return;
      }
      setStarted(true);
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [startCalibration, camera]);

  const handleSuggestAndExecute = useCallback(async () => {
    setBusy('move');
    try {
      const suggest = await autoPoseSuggest(camera);
      if (!suggest.success) {
        toast.error(suggest.message || 'Keine erreichbare Pose gefunden.');
        return;
      }
      const target = {
        target_x: suggest.target_x,
        target_y: suggest.target_y,
        target_z: suggest.target_z,
        target_qx: suggest.target_qx,
        target_qy: suggest.target_qy,
        target_qz: suggest.target_qz,
        target_qw: suggest.target_qw,
      };
      const exec = await executeCalibrationPose(target);
      if (!exec.success) {
        toast.error(exec.message || 'Pose konnte nicht angefahren werden.');
        return;
      }
      // Settle the arm before capturing — the trajectory has finished
      // publishing but the controller may still be tracking residuals.
      await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
      const cap = await calibrationCaptureFrame(camera);
      if (!cap.success) {
        toast.error(cap.message || 'Bild konnte nicht erfasst werden.');
        return;
      }
      dispatch(setCalibProgress({
        framesCaptured: cap.frames_captured,
        framesRequired: cap.frames_required,
        lastViewRms: cap.last_view_rms,
      }));
      toast.success(cap.message || 'Pose erfasst.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [autoPoseSuggest, executeCalibrationPose, calibrationCaptureFrame, camera, dispatch]);

  const handleSolve = useCallback(async () => {
    setBusy('solve');
    try {
      const r = await calibrationSolve(camera, 'handeye');
      if (!r.success) {
        dispatch(setCalibError(r.message || 'Hand-Auge-Solver fehlgeschlagen.'));
        toast.error(r.message || 'Hand-Auge-Solver fehlgeschlagen.');
        return;
      }
      dispatch(setMethodDisagreement(r.method_disagreement));
      dispatch(markStepComplete(`${camera}_handeye`));
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [calibrationSolve, camera, dispatch]);

  const enoughFrames = framesCaptured >= framesRequired;

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
        {calibError && (
          <p className="text-xs text-amber-700 mt-2">{calibError}</p>
        )}
      </div>

      <div className="flex gap-2 flex-wrap">
        {!started ? (
          <button
            type="button"
            disabled={busy !== null}
            onClick={handleStart}
            className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === 'start' ? '…' : 'Kalibrierung starten'}
          </button>
        ) : (
          <>
            <button
              type="button"
              disabled={busy !== null || enoughFrames}
              onClick={handleSuggestAndExecute}
              className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent-wash)] text-[var(--accent-ink)] hover:bg-[var(--accent)] hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy === 'move' ? 'Anfahren …' : 'Nächste Pose anfahren & erfassen'}
            </button>
            <button
              type="button"
              disabled={busy !== null || !enoughFrames}
              onClick={handleSolve}
              className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy === 'solve' ? '…' : 'Berechnen & speichern'}
            </button>
          </>
        )}
      </div>

      <p className="text-xs text-[var(--ink-4)] italic mt-3">
        Tipp: Falls eine Pose ausserhalb des Arbeitsbereichs liegt, einfach
        erneut "Nächste Pose" drücken — der Sampler wählt eine neue Position.
      </p>
    </div>
  );
}

export default HandEyeCalibStep;
