/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  setCalibProgress,
  setCalibError,
  markStepComplete,
  resetCalibProgress,
} from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';

function IntrinsicCalibStep({ camera }) {
  const dispatch = useDispatch();
  const { startCalibration, calibrationCaptureFrame, calibrationSolve } = useRosServiceCaller();

  const framesCaptured = useSelector((s) => s.workshop.framesCaptured);
  const framesRequired = useSelector((s) => s.workshop.framesRequired);
  const lastViewRms = useSelector((s) => s.workshop.lastViewRms);
  const calibError = useSelector((s) => s.workshop.calibError);

  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(false);

  const cameraLabel = camera === 'gripper' ? 'Greifer-Kamera' : 'Szenen-Kamera';

  const handleStart = useCallback(async () => {
    setBusy(true);
    try {
      const r = await startCalibration(camera, 'intrinsic');
      if (!r.success) {
        toast.error(r.message || 'Start fehlgeschlagen.');
        dispatch(setCalibError(r.message));
      } else {
        dispatch(resetCalibProgress());
        dispatch(setCalibError(null));
        setStarted(true);
        toast.success(r.message);
      }
    } catch (e) {
      toast.error(`Verbindung zur ROS-Brücke fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [camera, dispatch, startCalibration]);

  const handleCapture = useCallback(async () => {
    setBusy(true);
    try {
      const r = await calibrationCaptureFrame(camera);
      if (!r.success) {
        toast.error(r.message || 'Bild wurde nicht erfasst.');
      } else {
        dispatch(setCalibProgress({
          framesCaptured: r.frames_captured,
          framesRequired: r.frames_required,
          lastViewRms: r.last_view_rms,
        }));
        toast.success(r.message);
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [camera, dispatch, calibrationCaptureFrame]);

  const handleSolve = useCallback(async () => {
    setBusy(true);
    try {
      const r = await calibrationSolve(camera, 'intrinsic');
      if (!r.success) {
        toast.error(r.message || 'Solver fehlgeschlagen.');
        dispatch(setCalibError(r.message));
      } else {
        dispatch(markStepComplete(`${camera}_intrinsic`));
        dispatch(setCalibError(null));
        toast.success(r.message);
      }
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [camera, dispatch, calibrationSolve]);

  useEffect(() => {
    setStarted(false);
    dispatch(resetCalibProgress());
  }, [camera, dispatch]);

  const canSolve = framesCaptured >= framesRequired;

  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">
        Intrinsische Kalibrierung — {cameraLabel}
      </h3>
      <p className="text-sm text-[var(--ink-3)] mb-4">
        Halte die ChArUco-Tafel langsam aus verschiedenen Winkeln vor die Kamera.
        Mindestens {framesRequired} verschiedene Ansichten werden benötigt.
      </p>

      <div className="bg-white border border-[var(--line)] rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-[var(--ink-3)]">Bilder erfasst</span>
          <span className="text-sm font-mono">
            {framesCaptured} / {framesRequired}
          </span>
        </div>
        <div className="h-2 bg-[var(--bg-sunk)] rounded">
          <div
            className="h-2 bg-[var(--accent)] rounded transition-all"
            style={{ width: `${Math.min(100, (framesCaptured / framesRequired) * 100)}%` }}
          />
        </div>
        {lastViewRms !== null && lastViewRms !== undefined && lastViewRms > 0 && (
          <p className="text-xs text-[var(--ink-4)] mt-2">
            Aktueller Reprojektionsfehler: {Number(lastViewRms).toFixed(2)} px
          </p>
        )}
      </div>

      {calibError && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md p-3 mb-4">
          {calibError}
        </div>
      )}

      <div className="flex gap-2">
        {!started ? (
          <button
            type="button"
            onClick={handleStart}
            disabled={busy}
            className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            Kalibrierung starten
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={handleCapture}
              disabled={busy || canSolve}
              className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
            >
              Bild erfassen
            </button>
            <button
              type="button"
              onClick={handleSolve}
              disabled={busy || !canSolve}
              className="px-4 py-2 rounded-md bg-[var(--accent-wash)] text-[var(--accent-ink)] text-sm font-medium hover:bg-[var(--accent)] hover:text-white disabled:opacity-50"
            >
              Berechnen & speichern
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default IntrinsicCalibStep;
