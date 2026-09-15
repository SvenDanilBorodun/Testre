/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Twin + sim-table markers for the document's Ziele (pins) and Positionen
 * (poses), plus variable points. Pure: no three, no Blockly, no Redux — the
 * SAME list feeds UrdfTwin (3D) and SimScene's 2D table.
 *
 * Height is PROVENANCE, not geometry (motion.resolve_destination_z): a pin is a
 * point ON THE TABLE, so in the simulator it sits on the virtual table (z 0),
 * while on a real rig its stored z is only the plane height at click time and
 * the run re-asks the measured plane — hence the „(z ≈)" suffix there. A pose
 * is a MEASURED height and is drawn where it was captured, in both worlds.
 */

import { DE } from '../blocks/messages_de';

export const MARKER_COLORS = Object.freeze({
  pin: '#f59e0b',
  pose: '#14b8a6',
  variable: '#a78bfa',
});

// 64 store entries (MAX_DESTINATION_ENTRIES) + 16 variable points (WP13).
export const MAX_TWIN_MARKERS = 80;

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * @param {object} args
 * @param {Array} args.entries destination-store entries ({id, name, kind, x, y, z})
 * @param {Array} [args.variablePoints] [{name, point: {x, y, z}}]
 * @param {boolean} args.simMode
 * @param {{kind, id}|null} args.highlight studioAssets.highlight
 * @returns {Array<{id, label, kind, x, y, z, highlighted}>}
 */
export function buildTwinMarkers({
  entries, variablePoints = [], simMode, highlight,
} = {}) {
  const hid = highlight && typeof highlight === 'object' ? highlight.id : undefined;
  const out = [];
  const push = (m) => {
    if (out.length >= MAX_TWIN_MARKERS) return;
    if (!finite(m.x) || !finite(m.y) || !finite(m.z)) return;
    out.push({ ...m, highlighted: !!highlight && hid === m.id });
  };
  for (const e of Array.isArray(entries) ? entries : []) {
    if (!e || typeof e.name !== 'string') continue;
    if (e.kind === 'pin') {
      push({
        id: e.id,
        label: simMode ? e.name : `${e.name} ${DE.MARKER_PIN_REAL_SUFFIX}`,
        kind: 'pin',
        x: e.x,
        y: e.y,
        z: simMode ? 0 : e.z,
      });
    } else if (e.kind === 'pose') {
      push({ id: e.id, label: e.name, kind: 'pose', x: e.x, y: e.y, z: e.z });
    }
  }
  for (const p of Array.isArray(variablePoints) ? variablePoints : []) {
    if (!p || typeof p.name !== 'string') continue;
    const pt = p.point && typeof p.point === 'object' ? p.point : null;
    if (!pt) continue;
    push({
      id: `var:${p.name}`, label: p.name, kind: 'variable', x: pt.x, y: pt.y, z: pt.z,
    });
  }
  return out;
}
