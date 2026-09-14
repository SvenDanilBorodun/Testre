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
import { buildAssetIndex, pointFromValue } from '../assetIndex';

const use = (enabled, disabled = 0) => ({ enabled, disabled, blockIds: [] });

function usage(overrides = {}) {
  return {
    replay: new Map(),
    refs: new Map(),
    pinStatements: new Map(),
    currentStatements: new Map(),
    variableUses: new Map(),
    ...overrides,
  };
}

const rec = (id, name, created_at, extra = {}) => ({
  id, name, created_at, point_count: 105, duration_s: 4.2, fps: 25, robot_profile: 'omx_f', ...extra,
});

const chipsOf = (vm) => vm.chips.map((c) => [c.text, c.level]);

const NOW = 1_800_000_000_000;

describe('buildAssetIndex — recordings', () => {
  it('groups versions by name, newest first, with chips in contract order', () => {
    const idx = buildAssetIndex({
      usage: usage({ replay: new Map([['Tanz', use(1, 2)]]) }),
      trajectories: {
        status: 'ready',
        items: [
          rec('b', 'Tanz', '2026-09-13T10:00:00Z'),
          rec('c', 'Tanz', '2026-09-13T12:00:00Z', { robot_profile: 'edu6_studio' }),
          rec('a', 'Tanz', '2026-09-12T10:00:00Z'),
        ],
      },
      robotType: 'omx_f',
      lastPreviewResult: { 'rec:c': { status: 'refused' } },
      capabilities: { preview: true },
      now: NOW,
    });
    expect(idx.recordings).toHaveLength(1);
    const [vm] = idx.recordings;
    expect(vm.assetId).toBe('c');
    expect(vm.versions.map((v) => v.id)).toEqual(['c', 'b', 'a']);
    // Usage counts disabled blocks too (1 + 2).
    expect(chipsOf(vm)).toEqual([
      ['3× benutzt', 'ok'],
      ['3 Versionen', 'warn'],
      ['anderer Roboter', 'bad'],
      ['im Simulator abgelehnt', 'bad'],
    ]);
    expect(vm.meta).toBe('4,2 s · 105 Punkte');
    expect(vm.canPreview).toBe(false);
    expect(idx.counts.aufnahmen).toBe(1);
  });

  it('an unused, rig-matching single version can be previewed', () => {
    const idx = buildAssetIndex({
      usage: usage(),
      trajectories: { status: 'ready', items: [rec('x', 'Winken', '2026-09-13T10:00:00Z')] },
      robotType: 'omx_f',
      capabilities: { preview: true },
      now: NOW,
    });
    expect(chipsOf(idx.recordings[0])).toEqual([['nicht benutzt', 'ok']]);
    expect(idx.recordings[0].canPreview).toBe(true);
  });

  it('missing names count ENABLED blocks only, and only once the list is ready', () => {
    const replay = new Map([
      ['Fehlt', use(2, 1)],
      ['NurAus', use(0, 3)],
      ['Einer', use(1)],
    ]);
    const base = { usage: usage({ replay }), now: NOW };
    expect(buildAssetIndex({ ...base, trajectories: { status: 'loading', items: [] } })
      .missingRecordings).toEqual([]);
    const idx = buildAssetIndex({ ...base, trajectories: { status: 'ready', items: [] } });
    expect(idx.missingRecordings.map((m) => m.assetName)).toEqual(['Fehlt', 'Einer']);
    expect(chipsOf(idx.missingRecordings[0])).toEqual([
      ['fehlt', 'bad'], ['2 Blöcke suchen diese Aufnahme', 'bad'],
    ]);
    expect(chipsOf(idx.missingRecordings[1])).toEqual([
      ['fehlt', 'bad'], ['1 Block sucht diese Aufnahme', 'bad'],
    ]);
    expect(idx.missingRecordings[0].canPreview).toBe(false);
  });
});

describe('buildAssetIndex — Ziele, Positionen, program pins', () => {
  const pin = {
    id: 'd_1', name: 'Ablage', kind: 'pin', x: 0.182, y: -0.064, z: 0.012,
    source: 'camera', robot_type: 'edu6_studio',
  };
  const pose = {
    id: 'd_2', name: 'Oben', kind: 'pose', x: 0.1, y: 0, z: 0.15, source: 'capture', robot_type: 'omx_f',
  };

  it('pins: usage → other robot → overridden → unreachable → refused', () => {
    const idx = buildAssetIndex({
      usage: usage({
        refs: new Map([['Ablage', use(0, 1)]]),
        pinStatements: new Map([['Ablage', { blockId: 'b1', enabled: true, x: NaN, y: NaN, z: NaN }]]),
      }),
      destinations: [pin, pose],
      robotType: 'omx_f',
      lastPreviewResult: { 'dest:d_1': { status: 'refused', unreachable: true } },
      capabilities: { preview: true },
      now: NOW,
    });
    expect(chipsOf(idx.pins[0])).toEqual([
      ['1× benutzt', 'ok'],
      ['anderer Roboter', 'warn'],
      ['im Programm überschrieben', 'warn'],
      ['nicht erreichbar', 'warn'],
      ['im Simulator abgelehnt', 'bad'],
    ]);
    expect(idx.pins[0].meta).toBe('x 182 · y −64 mm · Kamera');
    expect(idx.pins[0].canPreview).toBe(true);
    expect(idx.poses[0].meta).toBe('z 150 mm · OMX');
    expect(chipsOf(idx.poses[0])).toEqual([['nicht benutzt', 'ok']]);
    expect(idx.counts).toMatchObject({ ziele: 1, positionen: 1 });
    // An unpinned program pin shows „—".
    expect(idx.programPins).toEqual([expect.objectContaining({
      assetKind: 'programPin', assetId: 'b1', meta: 'im Programm · x — · y — mm', canPreview: false,
    })]);
  });

  it('a pose of unknown robot names the measurement; a current statement also overrides', () => {
    const idx = buildAssetIndex({
      usage: usage({ currentStatements: new Map([['Oben', { blockId: 'c', enabled: true }]]) }),
      destinations: [{ ...pose, robot_type: undefined }],
      robotType: '',
      now: NOW,
    });
    expect(idx.poses[0].meta).toBe('z 150 mm · gemessen');
    expect(chipsOf(idx.poses[0])).toEqual([['nicht benutzt', 'ok'], ['im Programm überschrieben', 'warn']]);
    expect(idx.programPins).toEqual([]);
  });
});

describe('buildAssetIndex — variables', () => {
  it('sorts German-aware, shows JSON values with age, and previews only points', () => {
    const idx = buildAssetIndex({
      usage: usage({ variableUses: new Map([['v2', 2]]) }),
      variables: [{ id: 'v1', name: 'Zahl' }, { id: 'v2', name: 'Äpfel' }, { id: 'v3', name: 'Punkt' }],
      variableValues: {
        Zahl: { value: 'ein sehr langer Text über zwanzig', ts: NOW - 4500 },
        Punkt: { value: { x: 0.1, y: 0.2, z: 0.3 }, ts: NOW - 1000 },
      },
      capabilities: { previewVariables: true },
      now: NOW,
    });
    expect(idx.variables.map((v) => v.assetName)).toEqual(['Äpfel', 'Punkt', 'Zahl']);
    expect(idx.variables[0].meta).toBe('noch kein Wert');
    expect(chipsOf(idx.variables[0])).toEqual([['2× benutzt', 'ok']]);
    expect(idx.variables[1].meta).toBe('{"x":0.1,"y":0.2,"z"… · vor 1 s');
    expect(idx.variables[1].canPreview).toBe(true);
    expect(idx.variables[2].meta).toBe('ein sehr langer Text… · vor 4 s');
    expect(idx.variables[2].canPreview).toBe(false);
    expect(idx.counts.variablen).toBe(3);
  });

  it('no variable shows ▶ before the page enables variable previews', () => {
    const idx = buildAssetIndex({
      variables: [{ id: 'v', name: 'P' }],
      variableValues: { P: { value: { x: 1, y: 2, z: 3 }, ts: NOW } },
      capabilities: { preview: true },
      now: NOW,
    });
    expect(idx.variables[0].canPreview).toBe(false);
  });

  it('titles are cut at 22 characters with an ellipsis', () => {
    const idx = buildAssetIndex({
      variables: [{ id: 'v', name: 'abcdefghijklmnopqrstuvwxyz' }],
      now: NOW,
    });
    expect(idx.variables[0].title).toBe('abcdefghijklmnopqrstuv…');
    expect(idx.variables[0].assetName).toBe('abcdefghijklmnopqrstuvwxyz');
  });
});

describe('pointFromValue', () => {
  it('accepts a plain object of three finite numbers only', () => {
    expect(pointFromValue({ x: 1, y: 2, z: 3, extra: 'ok' })).toEqual({ x: 1, y: 2, z: 3 });
    for (const bad of [[1, 2, 3], '1,2,3', null, 5, { x: 1, y: 2 }, { x: NaN, y: 1, z: 2 },
      { x: '1', y: 2, z: 3 }, { x: Infinity, y: 1, z: 1 }]) {
      expect(pointFromValue(bad)).toBeNull();
    }
  });
});
