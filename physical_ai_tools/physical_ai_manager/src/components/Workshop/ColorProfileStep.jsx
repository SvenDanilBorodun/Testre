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

const COLORS = [
  { key: 'rot', label: 'Rot', hex: '#ef4444' },
  { key: 'grün', label: 'Grün', hex: '#22c55e' },
  { key: 'blau', label: 'Blau', hex: '#3b82f6' },
  { key: 'gelb', label: 'Gelb', hex: '#eab308' },
];

function ColorProfileStep() {
  return (
    <div className="max-w-2xl">
      <h3 className="text-lg font-semibold text-[var(--ink)] mb-2">Farbprofil</h3>
      <p className="text-sm text-[var(--ink-3)] mb-4">
        Lege nacheinander einen Würfel jeder Farbe in das Sichtfeld der Szenen-Kamera
        und drücke "Erfassen". Das Profil hilft, Würfel auch bei wechselnder Beleuchtung
        zuverlässig zu erkennen.
      </p>

      <div className="grid grid-cols-2 gap-3">
        {COLORS.map((color) => (
          <div
            key={color.key}
            className="bg-white border border-[var(--line)] rounded-lg p-3 flex items-center gap-3"
          >
            <span
              className="w-6 h-6 rounded-full border border-[var(--line)]"
              style={{ background: color.hex }}
            />
            <span className="text-sm text-[var(--ink)] flex-1">{color.label}</span>
            <button
              type="button"
              className="text-xs px-3 py-1.5 rounded-md bg-[var(--accent-wash)] text-[var(--accent-ink)] hover:bg-[var(--accent)] hover:text-white transition"
            >
              Erfassen
            </button>
          </div>
        ))}
      </div>

      <p className="text-xs text-[var(--ink-4)] italic mt-4">
        Service-Aufrufe werden in einem Folge-Update verdrahtet.
      </p>
    </div>
  );
}

export default ColorProfileStep;
