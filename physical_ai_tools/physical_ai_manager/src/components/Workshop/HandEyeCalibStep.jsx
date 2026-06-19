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
import CameraFeedOverlay from './CameraFeedOverlay';
import {
  markStepComplete,
  setCalibProgress,
  setCalibError,
  resetCalibProgress,
} from '../../features/workshop/workshopSlice';

// WS4 (2026-06-17): SCENE-camera extrinsic via a SINGLE-SHOT board-on-table
// measurement — NO arm motion. The student lays the ChArUco board flat on the
// table at the marked reference point; the static scene camera sees it once;
// the server computes T_camera_to_base from the known board placement. This
// replaces the old auto-pose eye-to-base flow that drove a gripper-mounted
// board through 14 IK poses (which the deployed crude IK cannot do).
//
// Only `camera === 'scene'` is ever passed here now (the gripper steps were
// removed from the wizard); the `camera` prop is kept for clarity.
function HandEyeCalibStep({ camera }) {
  const dispatch = useDispatch();
  const {
    startCalibration,
    calibrationCaptureFrame,
    calibrationSolve,
  } = useRosServiceCaller();
  const framesCaptured = useSelector((s) => s.workshop.framesCaptured);
  const framesRequired = useSelector((s) => s.workshop.framesRequired);
  const calibError = useSelector((s) => s.workshop.calibError);
  const [busy, setBusy] = useState(null);
  const [started, setStarted] = useState(false);

  // Reset progress on mount / camera change so a switch doesn't show stale
  // counts. The single-shot extrinsic needs exactly one frame.
  useEffect(() => {
    setStarted(false);
    dispatch(resetCalibProgress());
    dispatch(setCalibProgress({ framesCaptured: 0, framesRequired: 1 }));
  }, [camera, dispatch]);

  const handleStart = useCallback(async () => {
    setBusy('start');
    try {
      // 'extrinsic' is the WS4 step name; the server also accepts the legacy
      // 'handeye' synonym, but we send the new name.
      const r = await startCalibration(camera, 'extrinsic');
      if (!r.success) {
        toast.error(r.message || 'Kalibrierung konnte nicht gestartet werden.');
        return;
      }
      setStarted(true);
      dispatch(setCalibError(null));
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [startCalibration, camera, dispatch]);

  const handleCapture = useCallback(async () => {
    setBusy('capture');
    try {
      const cap = await calibrationCaptureFrame(camera);
      if (!cap.success) {
        toast.error(cap.message || 'Bild konnte nicht erfasst werden.');
        return;
      }
      dispatch(setCalibProgress({
        framesCaptured: cap.frames_captured,
        framesRequired: cap.frames_required,
      }));
      toast.success(cap.message || 'Bild erfasst.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [calibrationCaptureFrame, camera, dispatch]);

  const handleSolve = useCallback(async () => {
    setBusy('solve');
    try {
      const r = await calibrationSolve(camera, 'extrinsic');
      if (!r.success) {
        dispatch(setCalibError(r.message || 'Extrinsik-Solver fehlgeschlagen.'));
        toast.error(r.message || 'Extrinsik-Solver fehlgeschlagen.');
        return;
      }
      dispatch(setCalibError(null));
      dispatch(markStepComplete(`${camera}_handeye`));
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [calibrationSolve, camera, dispatch]);

  const haveFrame = framesCaptured >= (framesRequired || 1);

  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">
        Szenen-Kamera — Extrinsik (Tafel auf dem Tisch)
      </h3>
      <p className="text-sm text-[var(--ink-3)] mb-3">
        In diesem Schritt lernt der Roboter, wo die Szenen-Kamera relativ zu
        seiner Basis steht. Dafür reicht <strong>ein einziges Bild</strong> —
        der Roboterarm bewegt sich dabei nicht.
      </p>

      <ol className="text-sm text-[var(--ink-3)] mb-4 list-decimal pl-5 space-y-1">
        <li>
          Lege die ChArUco-Tafel <strong>flach auf den Tisch</strong>, bedruckte
          Seite nach oben.
        </li>
        <li>
          Schiebe die Tafel mittig an den <strong>markierten Punkt</strong> vor
          dem Roboter (Vorderkante ca. 18&nbsp;cm vor der Basis): die lange Kante
          (7 Felder) verläuft <strong>vom Roboter weg</strong> (vorne–hinten),
          die kurze Kante (5 Felder) liegt <strong>quer</strong> (links–rechts).
        </li>
        <li>
          Die Ecke mit dem ersten Marker (Marker&nbsp;0, der Ursprung) muss
          <strong>vorne&nbsp;links</strong> liegen — also dem Roboter am nächsten
          und auf seiner linken Seite. Die Tafel liegt mittig auf der
          Vorwärts-Mittellinie des Roboters.
        </li>
        <li>
          Prüfe im Kamerabild rechts, dass die ganze Tafel gut sichtbar und
          nicht verdeckt ist.
        </li>
        <li>
          „Bild erfassen" drücken, danach „Berechnen &amp; speichern".
        </li>
      </ol>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
        <div className="bg-white border border-[var(--line)] rounded-lg p-4">
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-[var(--ink-3)]">Bild erfasst</span>
            <span className="text-sm font-mono">
              {framesCaptured} / {framesRequired || 1}
            </span>
          </div>
          {haveFrame && (
            <p className="text-xs text-emerald-700 mt-1">
              Tafel erkannt — jetzt „Berechnen &amp; speichern".
            </p>
          )}
          {calibError && (
            <p className="text-xs text-amber-700 mt-2">{calibError}</p>
          )}
          <p className="text-xs text-[var(--ink-4)] italic mt-3">
            Tipp: Den markierten Punkt einmal mit Klebeband fixieren, damit die
            Tafel jederzeit gleich liegt. Die Genauigkeit beim Aufnehmen hängt
            davon ab.
          </p>
        </div>
        <div className="min-h-[180px]">
          <CameraFeedOverlay camera="scene" clickable={false} />
        </div>
      </div>

      <div className="flex gap-2 flex-wrap">
        {!started ? (
          <button
            type="button"
            disabled={busy !== null}
            onClick={handleStart}
            className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === 'start' ? '…' : 'Extrinsik starten'}
          </button>
        ) : (
          <>
            <button
              type="button"
              disabled={busy !== null}
              onClick={handleCapture}
              className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent-wash)] text-[var(--accent-ink)] hover:bg-[var(--accent)] hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy === 'capture' ? 'Erfassen …' : (haveFrame ? 'Neu erfassen' : 'Bild erfassen')}
            </button>
            <button
              type="button"
              disabled={busy !== null || !haveFrame}
              onClick={handleSolve}
              className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy === 'solve' ? '…' : 'Berechnen & speichern'}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default HandEyeCalibStep;
