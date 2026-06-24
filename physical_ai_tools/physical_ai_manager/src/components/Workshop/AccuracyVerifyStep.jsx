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
 * W5 (2026-06-24) — „Genauigkeit prüfen" accuracy + orientation verification.
 *
 * OPTIONAL but recommended. The student places an AprilTag at a KNOWN base
 * position, types its X/Y in metres (+ optionally its yaw in degrees), and
 * captures >= 4 points spread across the workspace (corners + centre). The
 * server projects each detected tag with the current calibration and reports
 * the live residual; „Korrektur berechnen" fits a 2D-similarity correction and
 * reports the residual + yaw bias. A mirror/handedness flip is surfaced loudly
 * (recalibration needed) rather than silently corrected.
 *
 * This step never gates the editor (not part of `calibrated`); the student can
 * „Überspringen" at any time and „Fertig" once a correction is computed.
 */

import React, { useState, useCallback } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { setAccuracyResult, finishVerify } from '../../features/workshop/workshopSlice';

const MIN_POINTS = 4;

function AccuracyVerifyStep() {
  const dispatch = useDispatch();
  const { verifyCalibration } = useRosServiceCaller();
  const accuracyResult = useSelector((s) => s.workshop.accuracyResult);

  const [truthX, setTruthX] = useState('');
  const [truthY, setTruthY] = useState('');
  const [truthYawDeg, setTruthYawDeg] = useState('');
  const [hasYaw, setHasYaw] = useState(false);
  const [pointCount, setPointCount] = useState(0);
  const [lastDetection, setLastDetection] = useState(null);
  const [busy, setBusy] = useState(null);

  const handleAddPoint = useCallback(async () => {
    const x = Number(truthX);
    const y = Number(truthY);
    if (truthX.trim() === '' || truthY.trim() === '' || Number.isNaN(x) || Number.isNaN(y)) {
      toast.error('Bitte gültige X- und Y-Werte in Metern eingeben.');
      return;
    }
    if (hasYaw && (truthYawDeg.trim() === '' || Number.isNaN(Number(truthYawDeg)))) {
      toast.error('Bitte einen gültigen Winkel in Grad eingeben oder das Häkchen entfernen.');
      return;
    }
    setBusy('add');
    try {
      const r = await verifyCalibration({
        command: 'add_point',
        truthX: x,
        truthY: y,
        truthYawDeg: hasYaw ? Number(truthYawDeg) : 0,
        hasYaw,
      });
      if (!r || !r.success) {
        toast.error((r && r.message) || 'Punkt konnte nicht erfasst werden.');
        return;
      }
      setPointCount(r.point_count || 0);
      setLastDetection(
        r.has_detection
          ? {
              x: r.detected_x,
              y: r.detected_y,
              yaw: r.detected_yaw_deg,
              hadYaw: hasYaw,
            }
          : null,
      );
      // A fresh capture invalidates any prior correction readout.
      dispatch(setAccuracyResult(null));
      toast.success(r.message || 'Punkt erfasst.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [truthX, truthY, truthYawDeg, hasYaw, verifyCalibration, dispatch]);

  const handleReset = useCallback(async () => {
    setBusy('reset');
    try {
      const r = await verifyCalibration({ command: 'reset' });
      setPointCount((r && r.point_count) || 0);
      setLastDetection(null);
      dispatch(setAccuracyResult(null));
      toast.success((r && r.message) || 'Zurückgesetzt.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [verifyCalibration, dispatch]);

  const handleSolve = useCallback(async () => {
    setBusy('solve');
    try {
      const r = await verifyCalibration({ command: 'solve' });
      if (r && typeof r.point_count === 'number') setPointCount(r.point_count);
      if (!r || !r.success) {
        // mirror_detected → success=False with a German message; surface it.
        dispatch(setAccuracyResult({
          mirror_detected: !!(r && r.mirror_detected),
          message: (r && r.message) || 'Korrektur fehlgeschlagen.',
          residual_mm_mean: r ? r.residual_mm_mean : 0,
          residual_mm_max: r ? r.residual_mm_max : 0,
          yaw_bias_deg: r ? r.yaw_bias_deg : 0,
          point_count: r ? r.point_count : 0,
          success: false,
        }));
        toast.error((r && r.message) || 'Korrektur fehlgeschlagen.');
        return;
      }
      dispatch(setAccuracyResult({
        mirror_detected: false,
        message: r.message || '',
        residual_mm_mean: r.residual_mm_mean,
        residual_mm_max: r.residual_mm_max,
        yaw_bias_deg: r.yaw_bias_deg,
        point_count: r.point_count,
        success: true,
      }));
      toast.success(r.message || 'Korrektur berechnet.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [verifyCalibration, dispatch]);

  const handleFinish = useCallback(() => {
    dispatch(finishVerify());
  }, [dispatch]);

  const canSolve = pointCount >= MIN_POINTS && busy === null;
  const fmt = (v) => (typeof v === 'number' ? v.toFixed(3) : '—');

  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">
        Genauigkeit prüfen
      </h3>
      <p className="text-sm text-[var(--ink-3)] mb-2 rounded-md bg-sky-50 border border-sky-200 px-3 py-2">
        <strong>Empfohlen</strong>, aber freiwillig. Dieser Schritt prüft, ob die
        Kamera die Welt richtig vermisst, und korrigiert kleine Abweichungen
        sowie die Ausrichtung. Du kannst ihn auch <strong>überspringen</strong> —
        der Editor öffnet sich danach.
      </p>

      <ol className="text-sm text-[var(--ink-3)] mb-4 list-decimal pl-5 space-y-1">
        <li>
          Lege einen <strong>AprilTag</strong> an eine <strong>genau bekannte
          Stelle</strong> auf dem Tisch.
        </li>
        <li>
          Trage seine Position als <strong>X/Y in Metern</strong> im
          Roboter-Koordinatensystem ein (optional auch den Winkel in Grad).
        </li>
        <li>
          „Punkt erfassen" drücken. Wiederhole an <strong>mindestens
          {' '}{MIN_POINTS} verteilten Stellen</strong> — gut über die
          Arbeitsfläche verteilt (Ecken + Mitte).
        </li>
        <li>„Korrektur berechnen" drücken.</li>
      </ol>

      <div className="bg-white border border-[var(--line)] rounded-lg p-4 mb-4">
        <div className="grid grid-cols-2 gap-3 mb-3">
          <label className="text-sm text-[var(--ink-3)]">
            X (Meter)
            <input
              type="number"
              step="0.001"
              value={truthX}
              onChange={(e) => setTruthX(e.target.value)}
              className="mt-1 w-full rounded-md border border-[var(--line)] px-2 py-1 text-sm font-mono"
              placeholder="z. B. 0.150"
            />
          </label>
          <label className="text-sm text-[var(--ink-3)]">
            Y (Meter)
            <input
              type="number"
              step="0.001"
              value={truthY}
              onChange={(e) => setTruthY(e.target.value)}
              className="mt-1 w-full rounded-md border border-[var(--line)] px-2 py-1 text-sm font-mono"
              placeholder="z. B. -0.050"
            />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm text-[var(--ink-3)] mb-2">
          <input
            type="checkbox"
            checked={hasYaw}
            onChange={(e) => setHasYaw(e.target.checked)}
          />
          Bekannten Winkel angeben (Grad)
        </label>
        {hasYaw && (
          <label className="text-sm text-[var(--ink-3)] block mb-1">
            Winkel (Grad)
            <input
              type="number"
              step="1"
              value={truthYawDeg}
              onChange={(e) => setTruthYawDeg(e.target.value)}
              className="mt-1 w-40 rounded-md border border-[var(--line)] px-2 py-1 text-sm font-mono"
              placeholder="z. B. 0"
            />
          </label>
        )}
      </div>

      <div className="bg-white border border-[var(--line)] rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-sm text-[var(--ink-3)]">Punkte erfasst</span>
          <span className="text-sm font-mono">{pointCount} / {MIN_POINTS}</span>
        </div>
        {pointCount < MIN_POINTS && (
          <p className="text-xs text-[var(--ink-4)] mt-1">
            Erfasse mindestens {MIN_POINTS} Punkte und verteile sie gut über die
            Arbeitsfläche.
          </p>
        )}
        {lastDetection && (
          <p className="text-xs text-[var(--ink-4)] mt-2 font-mono">
            Erkannt: X {fmt(lastDetection.x)} m, Y {fmt(lastDetection.y)} m
            {lastDetection.hadYaw && typeof lastDetection.yaw === 'number'
              ? `, Winkel ${lastDetection.yaw.toFixed(1)}°`
              : ''}
          </p>
        )}
        {lastDetection === null && pointCount > 0 && (
          <p className="text-xs text-amber-700 mt-2">
            Beim letzten Punkt wurde kein Tag erkannt.
          </p>
        )}
      </div>

      {accuracyResult && (
        accuracyResult.mirror_detected || accuracyResult.success === false ? (
          <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md p-3 mb-4">
            <strong>Fehler:</strong> {accuracyResult.message
              || 'Die Kalibrierung ist gespiegelt/verdreht — bitte neu kalibrieren.'}
          </div>
        ) : (
          <div className="bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm rounded-md p-3 mb-4">
            <p className="font-medium mb-1">Korrektur berechnet ✓</p>
            <p className="text-xs font-mono">
              Mittlere Abweichung: {fmt(accuracyResult.residual_mm_mean)} mm,
              {' '}max. {fmt(accuracyResult.residual_mm_max)} mm
              {typeof accuracyResult.yaw_bias_deg === 'number'
                ? `, Winkel-Versatz ${accuracyResult.yaw_bias_deg.toFixed(1)}°`
                : ''}
            </p>
          </div>
        )
      )}

      <div className="flex gap-2 flex-wrap">
        <button
          type="button"
          onClick={handleAddPoint}
          disabled={busy !== null}
          className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
        >
          {busy === 'add' ? 'Erfassen …' : 'Punkt erfassen'}
        </button>
        <button
          type="button"
          onClick={handleSolve}
          disabled={!canSolve}
          className="px-4 py-2 rounded-md bg-[var(--accent-wash)] text-[var(--accent-ink)] text-sm font-medium hover:bg-[var(--accent)] hover:text-white disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy === 'solve' ? '…' : 'Korrektur berechnen'}
        </button>
        <button
          type="button"
          onClick={handleReset}
          disabled={busy !== null}
          className="px-4 py-2 rounded-md border border-[var(--line)] text-[var(--ink-3)] text-sm hover:bg-[var(--bg-sunk)] disabled:opacity-50"
        >
          {busy === 'reset' ? '…' : 'Zurücksetzen'}
        </button>
        <button
          type="button"
          onClick={handleFinish}
          disabled={busy !== null}
          className="px-4 py-2 rounded-md border border-[var(--line)] text-[var(--ink-3)] text-sm hover:bg-[var(--bg-sunk)] disabled:opacity-50 ml-auto"
        >
          {accuracyResult && accuracyResult.success ? 'Fertig' : 'Überspringen'}
        </button>
      </div>
    </div>
  );
}

export default AccuracyVerifyStep;
