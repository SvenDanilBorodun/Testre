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
  setCharucoPreview,
  addCoverageCell,
} from '../../features/workshop/workshopSlice';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import CameraFeedOverlay from './CameraFeedOverlay';

function IntrinsicCalibStep({ camera }) {
  const dispatch = useDispatch();
  const {
    startCalibration,
    calibrationCaptureFrame,
    calibrationSolve,
    calibrationPreview,
  } = useRosServiceCaller();

  const framesCaptured = useSelector((s) => s.workshop.framesCaptured);
  const framesRequired = useSelector((s) => s.workshop.framesRequired);
  const lastViewRms = useSelector((s) => s.workshop.lastViewRms);
  const calibError = useSelector((s) => s.workshop.calibError);
  const charucoPreview = useSelector((s) => s.workshop.charucoPreview);
  const coverageMosaic = useSelector((s) => s.workshop.coverageMosaic);
  const qualityHistory = useSelector((s) => s.workshop.qualityHistory);

  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(false);
  // Final reprojection error from the most recent successful solve.
  // Different from lastViewRms (which is the per-capture estimate); this
  // is the consolidated full-set RMS the solver actually persisted (the
  // intrinsic step captures framesRequired views — 20 since the wide-lens
  // rational-distortion upgrade, 2026-06-24 W4).
  const [solvedRms, setSolvedRms] = useState(null);

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
        // Tier-b: record which 4x4 coverage cell the board landed in + the
        // 1/2/3 quality badge the backend computed for this capture.
        dispatch(addCoverageCell({ cell: r.coverage_cell, quality: r.quality }));
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
    // Clear any prior "Gespeichert" badge so a re-solve that fails
    // doesn't leave a stale green line lingering above a new error.
    setSolvedRms(null);
    try {
      const r = await calibrationSolve(camera, 'intrinsic');
      if (!r.success) {
        toast.error(r.message || 'Solver fehlgeschlagen.');
        dispatch(setCalibError(r.message));
      } else {
        dispatch(markStepComplete(`${camera}_intrinsic`));
        dispatch(setCalibError(null));
        // Render the final consolidated reprojection error so the
        // student gets feedback beyond a flash toast.
        if (typeof r.reprojection_error === 'number' && r.reprojection_error > 0) {
          setSolvedRms(r.reprojection_error);
        }
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
    setSolvedRms(null);
    dispatch(resetCalibProgress());
  }, [camera, dispatch]);

  // Tier-b: live ChArUco corner preview. While the intrinsic step is active,
  // poll /calibration/preview at ~3 Hz (client-side interval only — no
  // EDUBOTICS_* env / compose change) and push the detected corners into Redux
  // so CameraFeedOverlay draws them on top of the live stream. Preview is
  // advisory: a dropped poll clears the overlay but never toasts the student.
  useEffect(() => {
    if (!started) return undefined;
    let cancelled = false;
    let inFlight = false;
    const tick = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const r = await calibrationPreview(camera);
        if (cancelled) return;
        const xs = r.corners_x || [];
        const ys = r.corners_y || [];
        const n = Math.min(xs.length, ys.length);
        const corners = [];
        for (let i = 0; i < n; i += 1) {
          corners.push({ x: xs[i], y: ys[i] });
        }
        dispatch(setCharucoPreview({ detected: !!r.detected, corners }));
      } catch (e) {
        if (!cancelled) dispatch(setCharucoPreview({ detected: false, corners: [] }));
      } finally {
        inFlight = false;
      }
    };
    const id = setInterval(tick, 300);
    tick();
    return () => {
      cancelled = true;
      clearInterval(id);
      dispatch(setCharucoPreview({ detected: false, corners: [] }));
    };
  }, [started, camera, calibrationPreview, dispatch]);

  const canSolve = framesCaptured >= framesRequired;
  const previewCorners =
    charucoPreview && charucoPreview.detected ? charucoPreview.corners : [];

  return (
    <div className="max-w-3xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">
        Intrinsische Kalibrierung — {cameraLabel}
      </h3>
      <p className="text-sm text-[var(--ink-3)] mb-4">
        Halte die ChArUco-Tafel langsam aus möglichst <strong>verschiedenen
        Winkeln</strong> vor die Kamera. Es werden etwa <strong>{framesRequired}
        Aufnahmen</strong> benötigt — je unterschiedlicher die Ansichten, desto
        genauer.
      </p>
      <ul className="text-sm text-[var(--ink-3)] mb-4 list-disc pl-5 space-y-1">
        <li>Die Tafel aus <strong>verschiedenen Winkeln</strong> zeigen und
        jeweils um <strong>bis zu ±45°</strong> kippen (nach links/rechts und
        oben/unten).</li>
        <li>Die Tafel auch in die <strong>Bildecken und an die Bildränder</strong>
        bewegen, nicht nur in die Mitte.</li>
        <li>Abstand variieren (nah und weiter weg) und die Tafel ruhig halten,
        bevor du erfasst.</li>
      </ul>
      <p className="text-sm text-[var(--ink-3)] mb-4 rounded-md bg-amber-50 border border-amber-200 px-3 py-2">
        <strong>Dieser Schritt ist erforderlich.</strong> Er misst die echte
        Brennweite deiner Kamera — ohne ihn wären alle berechneten Positionen
        seitlich verschoben. Die Tischvermessung korrigiert nur die Höhe, nicht
        den seitlichen Maßstab. Erst danach lässt sich die Ausrichtung
        kalibrieren.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
        {/* Left column: live progress + capture quality / coverage. */}
        <div className="space-y-3">
          <div className="bg-white border border-[var(--line)] rounded-lg p-4">
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
            {solvedRms !== null && solvedRms !== undefined && solvedRms > 0 && (
              <p className="text-xs text-emerald-700 mt-1 font-medium">
                Gespeichert — Gesamt-Reprojektionsfehler: {Number(solvedRms).toFixed(2)} px
              </p>
            )}
          </div>

          {/* Tier-b: 4x4 coverage mosaic — which parts of the frame the board
              has already covered. Nudges the student to push the board into
              the corners so the wide-lens distortion is well-constrained. */}
          <div className="bg-white border border-[var(--line)] rounded-lg p-4">
            <p className="text-sm text-[var(--ink-3)] mb-2">
              Abdeckung der Tafel im Bild
            </p>
            <div className="grid grid-cols-4 gap-1 w-28">
              {coverageMosaic.map((count, idx) => (
                <div
                  key={idx}
                  className={
                    'aspect-square rounded-sm border ' +
                    (count > 0
                      ? 'bg-emerald-400 border-emerald-500'
                      : 'bg-[var(--bg-sunk)] border-[var(--line)]')
                  }
                  title={count > 0 ? `${count} Aufnahme(n)` : 'noch nicht abgedeckt'}
                />
              ))}
            </div>
            <p className="text-xs text-[var(--ink-4)] mt-2">
              Bewege die Tafel auch in die <strong>Ecken</strong>, bis möglichst
              alle Felder grün sind.
            </p>
            {qualityHistory.length > 0 && (
              <div className="mt-3">
                <p className="text-xs text-[var(--ink-4)] mb-1">
                  Qualität je Aufnahme
                </p>
                <div className="flex flex-wrap gap-1">
                  {qualityHistory.map((q, idx) => (
                    <span
                      key={idx}
                      className={
                        'inline-block w-3 h-3 rounded-full ' +
                        (q === 'good'
                          ? 'bg-emerald-500'
                          : q === 'ok'
                            ? 'bg-amber-400'
                            : 'bg-red-500')
                      }
                      title={
                        q === 'good' ? 'gut' : q === 'ok' ? 'in Ordnung' : 'schwach'
                      }
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Right column: live camera feed with the detected-corner overlay. */}
        <div className="min-h-[180px]">
          <CameraFeedOverlay
            camera={camera}
            clickable={false}
            charucoCorners={previewCorners}
          />
          <p className="text-xs text-[var(--ink-4)] mt-1">
            {charucoPreview && charucoPreview.detected
              ? 'Tafel erkannt — die markierten Punkte zeigen die erkannten Ecken.'
              : 'Tafel ins Bild halten, bis die Ecken markiert werden.'}
          </p>
        </div>
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
