/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { describe, it, expect } from 'vitest';
import { buildTwinMarkers, MARKER_COLORS, MAX_TWIN_MARKERS } from '../markers';
import { DE } from '../../blocks/messages_de';

const PIN = { id: 'd_00000001', name: 'Ablage', kind: 'pin', x: 0.182, y: -0.064, z: 0.012 };
const POSE = { id: 'd_00000002', name: 'Über der Kiste', kind: 'pose', x: 0.141, y: 0.102, z: 0.118 };

describe('buildTwinMarkers', () => {
  it('exports the three kind colours and the cap', () => {
    expect(MARKER_COLORS).toEqual({ pin: '#f59e0b', pose: '#14b8a6', variable: '#a78bfa' });
    expect(MAX_TWIN_MARKERS).toBe(80);
  });

  it('in the simulator a pin sits on the virtual table (z 0) under its bare name', () => {
    const [pin, pose] = buildTwinMarkers({ entries: [PIN, POSE], simMode: true, highlight: null });
    expect(pin).toEqual({
      id: PIN.id, label: 'Ablage', kind: 'pin', x: 0.182, y: -0.064, z: 0, highlighted: false,
    });
    // A pose is a MEASURED height: drawn where it was captured in both worlds.
    expect(pose).toEqual({
      id: POSE.id, label: 'Über der Kiste', kind: 'pose', x: 0.141, y: 0.102, z: 0.118, highlighted: false,
    });
  });

  it('on a real rig a pin keeps its stored z and says the height is approximate', () => {
    const [pin, pose] = buildTwinMarkers({ entries: [PIN, POSE], simMode: false, highlight: null });
    expect(pin.z).toBe(0.012);
    expect(pin.label).toBe(`Ablage ${DE.MARKER_PIN_REAL_SUFFIX}`);
    expect(pin.label).toBe('Ablage (z ≈)');
    expect(pose.z).toBe(0.118);
    expect(pose.label).toBe('Über der Kiste');
  });

  it('highlights exactly the marker whose id matches', () => {
    const markers = buildTwinMarkers({
      entries: [PIN, POSE],
      variablePoints: [{ name: 'Punkt', point: { x: 0.1, y: 0, z: 0.05 } }],
      simMode: true,
      highlight: { kind: 'pose', id: POSE.id },
    });
    expect(markers.map((m) => m.highlighted)).toEqual([false, true, false]);
    const byVar = buildTwinMarkers({
      entries: [PIN],
      variablePoints: [{ name: 'Punkt', point: { x: 0.1, y: 0, z: 0.05 } }],
      simMode: true,
      highlight: { kind: 'variable', id: 'var:Punkt' },
    });
    expect(byVar[1]).toEqual({
      id: 'var:Punkt', label: 'Punkt', kind: 'variable', x: 0.1, y: 0, z: 0.05, highlighted: true,
    });
    expect(byVar[0].highlighted).toBe(false);
  });

  it('drops non-finite coordinates, unknown kinds and malformed rows', () => {
    const markers = buildTwinMarkers({
      entries: [
        { ...PIN, x: NaN },
        { ...POSE, z: Infinity },
        { ...POSE, id: 'd_00000003', y: '0.1' },
        { id: 'd_00000004', name: 'Komisch', kind: 'zone', x: 0, y: 0, z: 0 },
        null,
        { ...PIN, id: 'd_00000005', name: 'Gut' },
      ],
      variablePoints: [{ name: 'Leer', point: null }, { name: 'Kaputt', point: { x: 1, y: NaN, z: 0 } }],
      simMode: false,
      highlight: null,
    });
    expect(markers.map((m) => m.id)).toEqual(['d_00000005']);
  });

  it('caps the list at MAX_TWIN_MARKERS', () => {
    const entries = Array.from({ length: 70 }, (_, i) => ({ ...PIN, id: `d_${i}`, name: `Ziel ${i}` }));
    const variablePoints = Array.from({ length: 20 }, (_, i) => ({ name: `v${i}`, point: { x: 0, y: 0, z: 0 } }));
    const markers = buildTwinMarkers({ entries, variablePoints, simMode: true, highlight: null });
    expect(markers).toHaveLength(MAX_TWIN_MARKERS);
    expect(markers[79].id).toBe('var:v9');
  });

  it('is total on missing input', () => {
    expect(buildTwinMarkers({})).toEqual([]);
    expect(buildTwinMarkers()).toEqual([]);
  });
});
