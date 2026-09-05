/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/*
 * Touch-off table measurement ("Tisch vermessen"). The student hand-guides the
 * torque-off follower so the gripper tip taps the table at >= 3 spread points;
 * the server reads each contact pose via forward kinematics and fits a
 * least-squares plane → the MEASURED table height (+ tilt) in the robot's own
 * frame, which is exactly what the grasp descends to. Reuses the calibration
 * services with the 'table_touch' step (start torques the arm off; solve
 * re-torques it).
 */

import React, { useState, useCallback, useEffect, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { armGeometry } from '../../utils/armProfile';
import {
  markStepComplete,
  setCalibProgress,
  setCalibError,
  resetCalibProgress,
} from '../../features/workshop/workshopSlice';

function TableTouchStep() {
  const dispatch = useDispatch();
  const {
    startCalibration, calibrationCaptureFrame, calibrationSolve, cancelCalibration,
  } = useRosServiceCaller();
  // A ROTATING claw's fingertip swings BACK as the jaws open, so the point the
  // FK model calls the TCP is only under the student's finger when the claw is
  // CLOSED. Touching off with it open measures the table too low by the whole
  // difference (~17 mm on the Edu:1) and every later grasp inherits the error.
  // No guard can catch this — the readback is a legal pose — so it has to be an
  // instruction, and it is shown ONLY on an arm it is true for.
  const caps = useSelector((s) => (s.tasks && s.tasks.taskStatus
    ? s.tasks.taskStatus.capabilities : null));
  const closeClawFirst = armGeometry(caps).toolTipTracksGripper;
  const framesCaptured = useSelector((s) => s.workshop.framesCaptured);
  const framesRequired = useSelector((s) => s.workshop.framesRequired);
  const calibError = useSelector((s) => s.workshop.calibError);
  const [busy, setBusy] = useState(null);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    setStarted(false);
    dispatch(resetCalibProgress());
    dispatch(setCalibProgress({ framesCaptured: 0, framesRequired: 3 }));
  }, [dispatch]);

  // On unmount (the student clicks another wizard step) cancel the touch-off so
  // the server re-torques the follower — it was torqued OFF for hand-guiding
  // and must never be left limp. Solve already re-torques on success; this
  // covers a step-switch before solving.
  const startedRef = useRef(false);
  const cancelRef = useRef(cancelCalibration);
  useEffect(() => { startedRef.current = started; }, [started]);
  useEffect(() => { cancelRef.current = cancelCalibration; }, [cancelCalibration]);
  useEffect(() => () => {
    if (startedRef.current) { cancelRef.current('').catch(() => {}); }
  }, []);

  const handleStart = useCallback(async () => {
    setBusy('start');
    try {
      const r = await startCalibration('scene', 'table_touch');
      if (!r.success) { toast.error(r.message || 'Konnte nicht gestartet werden.'); return; }
      setStarted(true);
      dispatch(setCalibError(null));
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally { setBusy(null); }
  }, [startCalibration, dispatch]);

  const handleCapture = useCallback(async () => {
    setBusy('capture');
    try {
      const cap = await calibrationCaptureFrame('scene');
      if (!cap.success) { toast.error(cap.message || 'Punkt konnte nicht erfasst werden.'); return; }
      dispatch(setCalibProgress({
        framesCaptured: cap.frames_captured,
        framesRequired: cap.frames_required,
      }));
      toast.success(cap.message || 'Punkt erfasst.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally { setBusy(null); }
  }, [calibrationCaptureFrame, dispatch]);

  const handleSolve = useCallback(async () => {
    setBusy('solve');
    try {
      const r = await calibrationSolve('scene', 'table_touch');
      if (!r.success) {
        dispatch(setCalibError(r.message || 'Tischvermessung fehlgeschlagen.'));
        toast.error(r.message || 'Tischvermessung fehlgeschlagen.');
        return;
      }
      dispatch(setCalibError(null));
      dispatch(markStepComplete('table_touch'));
      toast.success(r.message);
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally { setBusy(null); }
  }, [calibrationSolve, dispatch]);

  const haveEnough = framesCaptured >= (framesRequired || 3);

  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">
        Tisch vermessen (Höhe)
      </h3>
      <p className="text-sm text-[var(--ink-3)] mb-3">
        Hier misst der Roboter die <strong>genaue Tischhöhe</strong> selbst — er
        tippt mit der Greiferspitze auf den Tisch. Beim Start wird der Arm
        <strong> freigeschaltet</strong> (er wird weich), damit du ihn von Hand
        führen kannst.
      </p>

      <ol className="text-sm text-[var(--ink-3)] mb-4 list-decimal pl-5 space-y-1">
        <li>„Tischvermessung starten" drücken — der Arm wird freigeschaltet.</li>
        {closeClawFirst && (
          <li>
            <strong>Greifer ganz schließen</strong>, bevor du den Tisch
            berührst — bei diesem Arm wandert die Greiferspitze nach hinten,
            wenn der Greifer offen ist. Mit offenem Greifer misst du den Tisch
            zu tief.
          </li>
        )}
        <li>
          Führe den Greifer von Hand, bis die <strong>Spitze den Tisch
          berührt</strong>. Halte den Greifer dabei <strong>senkrecht nach
          unten</strong>, während die Spitze den Tisch berührt — dann
          {' '}„Punkt erfassen" drücken.
        </li>
        <li>
          Wiederhole an <strong>mindestens 3 verschiedenen Stellen</strong>,
          gut über die Arbeitsfläche verteilt (Ecken + Mitte).
        </li>
        <li>„Berechnen &amp; speichern" drücken — der Arm wird wieder fest.</li>
      </ol>

      <div className="bg-white border border-[var(--line)] rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-sm text-[var(--ink-3)]">Punkte erfasst</span>
          <span className="text-sm font-mono">{framesCaptured} / {framesRequired || 3}</span>
        </div>
        {haveEnough && (
          <p className="text-xs text-emerald-700 mt-1">
            Genug Punkte — jetzt „Berechnen &amp; speichern".
          </p>
        )}
        {calibError && <p className="text-xs text-amber-700 mt-2">{calibError}</p>}
        <p className="text-xs text-[var(--ink-4)] italic mt-3">
          Tipp: Tippe mit einem festen, immer gleichen Punkt der Greiferspitze.
          Mehr und weiter verteilte Punkte = genauere Höhe.
        </p>
      </div>

      <div className="flex gap-2 flex-wrap">
        {!started ? (
          <button
            type="button"
            disabled={busy !== null}
            onClick={handleStart}
            className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === 'start' ? '…' : 'Tischvermessung starten'}
          </button>
        ) : (
          <>
            <button
              type="button"
              disabled={busy !== null}
              onClick={handleCapture}
              className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--accent-wash)] text-[var(--accent-ink)] hover:bg-[var(--accent)] hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy === 'capture' ? 'Erfassen …' : 'Punkt erfassen'}
            </button>
            <button
              type="button"
              disabled={busy !== null || !haveEnough}
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

export default TableTouchStep;
