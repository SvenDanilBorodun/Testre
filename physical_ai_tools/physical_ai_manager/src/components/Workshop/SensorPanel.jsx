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
import { COLORS, DE } from './blocks/messages_de';

const COLOR_LABELS = COLORS.map(([label]) => label);

function fmtRad(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '–';
  const n = Number(value);
  return `${n.toFixed(3)} rad (${(n * 180 / Math.PI).toFixed(1)}°)`;
}

function SensorPanel() {
  const snap = useSelector((s) => s.workshop.sensorSnapshot);
  const ageMs = snap && snap.ts ? Date.now() - snap.ts : null;
  const stale = ageMs !== null && ageMs > 4000;

  return (
    <div className="text-sm text-[var(--ink)]">
      <div className="mb-3">
        <h3 className="font-semibold mb-1">{DE.DEBUG_FOLLOWER_JOINTS}</h3>
        {Array.isArray(snap.follower_joints) && snap.follower_joints.length > 0 ? (
          <ul className="space-y-0.5 font-mono text-xs">
            {snap.follower_joints.map((v, i) => (
              <li key={i} className="flex justify-between gap-2">
                <span>J{i + 1}</span>
                <span>{fmtRad(v)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[var(--ink-4)] text-xs">–</p>
        )}
      </div>

      <div className="mb-3">
        <h3 className="font-semibold mb-1">{DE.DEBUG_GRIPPER_OPENING}</h3>
        <p className="font-mono text-xs">{fmtRad(snap.gripper_opening)}</p>
      </div>

      <div className="mb-3">
        <h3 className="font-semibold mb-1">{DE.DEBUG_VISIBLE_MARKERS}</h3>
        <p className="font-mono text-xs">
          {Array.isArray(snap.visible_apriltag_ids) && snap.visible_apriltag_ids.length > 0
            ? snap.visible_apriltag_ids.join(', ')
            : '–'}
        </p>
      </div>

      <div className="mb-3">
        <h3 className="font-semibold mb-1">{DE.DEBUG_COLOR_COUNTS}</h3>
        <ul className="space-y-0.5 font-mono text-xs">
          {COLOR_LABELS.map((label, i) => (
            <li key={label} className="flex justify-between gap-2">
              <span>{label}</span>
              <span>{(snap.color_counts && snap.color_counts[i]) || 0} px</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="mb-3">
        <h3 className="font-semibold mb-1">{DE.DEBUG_VISIBLE_OBJECTS}</h3>
        <p className="font-mono text-xs break-words">
          {Array.isArray(snap.visible_object_classes) && snap.visible_object_classes.length > 0
            ? snap.visible_object_classes.join(', ')
            : '–'}
        </p>
      </div>

      {stale && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-2 py-1">
          Sensoren-Daten veraltet — Verbindung zum Server prüfen.
        </p>
      )}
    </div>
  );
}

export default SensorPanel;
