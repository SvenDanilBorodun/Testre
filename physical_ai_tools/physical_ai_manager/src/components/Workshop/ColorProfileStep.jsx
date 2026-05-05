/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useState, useCallback } from 'react';
import { useDispatch } from 'react-redux';
import toast from 'react-hot-toast';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { markStepComplete } from '../../features/workshop/workshopSlice';

const COLORS = [
  { key: 'rot', label: 'Rot', hex: '#ef4444' },
  { key: 'gruen', label: 'Grün', hex: '#22c55e' },
  { key: 'blau', label: 'Blau', hex: '#3b82f6' },
  { key: 'gelb', label: 'Gelb', hex: '#eab308' },
];

// std > this in any LAB channel suggests the segmented blob mixed
// foreground + background pixels. Warn the teacher rather than
// silently store a noisy cluster.
const STD_WARN_THRESHOLD = 25;

function labCenterToRgb(center) {
  // OpenCV LAB uses scaled ranges (L: 0..255, a/b: 0..255 with 128
  // offset). Convert via a temporary 1x1 image so we don't have to
  // re-implement the colour-space conversion in JS.
  if (!center || center.length !== 3) return null;
  try {
    // Quick approximation: map L→grayscale and tint by (a, b) signs.
    // The exact LAB→sRGB needs the OpenCV transform which we don't
    // want to bundle; this approximation is just for the swatch.
    const L = center[0] / 255;
    const a = (center[1] - 128) / 128;
    const b = (center[2] - 128) / 128;
    // Map (L, a, b) to (R, G, B) heuristically.
    const r = Math.max(0, Math.min(1, L + 0.5 * a));
    const g = Math.max(0, Math.min(1, L - 0.25 * a + 0.25 * b));
    const bl = Math.max(0, Math.min(1, L - 0.5 * b));
    return `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(bl * 255)})`;
  } catch (e) {
    return null;
  }
}

function ColorProfileStep() {
  const dispatch = useDispatch();
  const { captureColor, calibrationSolve } = useRosServiceCaller();
  const [busy, setBusy] = useState(null);
  const [results, setResults] = useState({});

  const handleCapture = useCallback(
    async (color) => {
      setBusy(color.key);
      try {
        const r = await captureColor(color.key);
        if (!r.success) {
          toast.error(r.message || `Erfassen für ${color.label} fehlgeschlagen.`);
          return;
        }
        const std = r.lab_std || [];
        const noisy = std.some((v) => v > STD_WARN_THRESHOLD);
        setResults((prev) => ({
          ...prev,
          [color.key]: {
            center: r.lab_center || [],
            std,
            noisy,
          },
        }));
        if (noisy) {
          toast(
            `${color.label}: gespeichert, aber Streuung ist hoch — `
            + 'bitte Würfel zentraler positionieren und nochmals erfassen.',
            { icon: '⚠️' },
          );
        } else {
          toast.success(r.message || `${color.label} gespeichert.`);
        }
      } catch (e) {
        toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
      } finally {
        setBusy(null);
      }
    },
    [captureColor]
  );

  const allCaptured = COLORS.every((c) => results[c.key]);

  const handleFinish = useCallback(async () => {
    setBusy('finish');
    try {
      const r = await calibrationSolve('scene', 'color_profile');
      if (!r.success) {
        toast.error(r.message || 'Farbprofil nicht abgeschlossen.');
        return;
      }
      dispatch(markStepComplete('color_profile'));
      toast.success(r.message || 'Farbprofil abgeschlossen.');
    } catch (e) {
      toast.error(`Service-Aufruf fehlgeschlagen: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  }, [calibrationSolve, dispatch]);

  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">Farbprofil</h3>
      <p className="text-sm text-[var(--ink-3)] mb-4">
        Lege nacheinander einen Würfel jeder Farbe mittig in das Sichtfeld der
        Szenen-Kamera und drücke "Erfassen". Das Profil hilft, Würfel auch bei
        wechselnder Beleuchtung zuverlässig zu erkennen.
      </p>

      <div className="grid grid-cols-2 gap-3">
        {COLORS.map((color) => {
          const result = results[color.key];
          const swatch = result ? labCenterToRgb(result.center) : null;
          return (
            <div
              key={color.key}
              className="bg-white border border-[var(--line)] rounded-lg p-3 flex items-center gap-3"
            >
              <span
                className="w-6 h-6 rounded-full border border-[var(--line)]"
                style={{ background: swatch || color.hex }}
                title={result ? 'Erfasstes LAB-Mittel' : 'Erwartete Farbe'}
              />
              <div className="flex-1 min-w-0">
                <div className="text-sm text-[var(--ink)] truncate">{color.label}</div>
                {result && (
                  <div className={
                    'text-xs '
                    + (result.noisy ? 'text-amber-600' : 'text-[var(--ink-4)]')
                  }>
                    {result.noisy
                      ? 'Hohe Streuung — neu erfassen empfohlen.'
                      : 'Erfasst.'}
                  </div>
                )}
              </div>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => handleCapture(color)}
                className={
                  'text-xs px-3 py-1.5 rounded-md transition '
                  + (busy === color.key
                    ? 'bg-[var(--ink-4)] text-white cursor-wait'
                    : 'bg-[var(--accent-wash)] text-[var(--accent-ink)] hover:bg-[var(--accent)] hover:text-white')
                }
              >
                {busy === color.key ? '…' : (result ? 'Neu erfassen' : 'Erfassen')}
              </button>
            </div>
          );
        })}
      </div>

      <button
        type="button"
        disabled={!allCaptured || busy !== null}
        onClick={handleFinish}
        className={
          'mt-4 px-4 py-2 rounded-md text-sm font-medium transition '
          + (allCaptured && busy === null
            ? 'bg-[var(--accent)] text-white hover:opacity-90'
            : 'bg-[var(--ink-5)] text-[var(--ink-4)] cursor-not-allowed')
        }
      >
        Farbprofil abschließen
      </button>
    </div>
  );
}

export default ColorProfileStep;
