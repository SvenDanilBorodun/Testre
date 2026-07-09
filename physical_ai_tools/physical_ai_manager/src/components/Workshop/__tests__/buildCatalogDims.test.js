/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// buildCatalogDims — reduces a GetObjectCatalog service response (parallel per-
// type arrays) to the { [type]: {height_m,width_m,color,max_instances} } render
// map SimScene + UrdfTwin consume. Defensive per-index reads: any missing/short/
// invalid field is OMITTED so the consumer's own fallbacks kick in; a type with
// no valid field at all is skipped entirely (old server → {}).

import { buildCatalogDims } from '../simConstants';

describe('buildCatalogDims', () => {
  test('happy path: all arrays present → full per-type entries', () => {
    const res = {
      success: true,
      type_names: ['wuerfel', 'banane'],
      labels_de: ['Würfel', 'Banane'],
      object_height_m: [0.03, 0.04],
      object_width_m: [0.03, 0.02],
      color_hex: ['#f59e0b', '#22c55e'],
      max_instances: [2, 1],
    };
    expect(buildCatalogDims(res)).toEqual({
      wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 },
      banane: { height_m: 0.04, width_m: 0.02, color: '#22c55e', max_instances: 1 },
    });
  });

  test('old server (no render arrays at all) → empty map {}', () => {
    const res = {
      success: true,
      type_names: ['wuerfel'],
      labels_de: ['Würfel'],
      // object_height_m / object_width_m / color_hex / max_instances all absent
    };
    expect(buildCatalogDims(res)).toEqual({});
  });

  test('partial: only object_height_m present → entry carries just height_m', () => {
    const res = {
      type_names: ['wuerfel'],
      object_height_m: [0.03],
    };
    expect(buildCatalogDims(res)).toEqual({ wuerfel: { height_m: 0.03 } });
  });

  test('length mismatch: a dim array shorter than type_names omits the missing keys', () => {
    const res = {
      type_names: ['a', 'b', 'c'],
      object_height_m: [0.03, 0.05], // no index 2
      object_width_m: [0.03], //          only index 0
      color_hex: ['#f59e0b', '#00ff00', '#0000ff'],
      max_instances: [2, 2, 2],
    };
    expect(buildCatalogDims(res)).toEqual({
      a: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 },
      b: { height_m: 0.05, color: '#00ff00', max_instances: 2 }, // no width_m
      c: { color: '#0000ff', max_instances: 2 }, //                 no height/width
    });
  });

  test('bad values are dropped per field (non-finite / non-positive / bad hex / non-integer max)', () => {
    const res = {
      type_names: ['a', 'b'],
      object_height_m: [NaN, 0], //     NaN + non-positive → both dropped
      object_width_m: [-0.01, 0.03], // negative dropped; valid kept
      color_hex: ['f59e0b', '#GGGGGG'], // missing # / non-hex → both dropped
      max_instances: [2.5, -1], //      non-integer + negative → both dropped
    };
    expect(buildCatalogDims(res)).toEqual({
      // a: everything invalid → skipped entirely
      b: { width_m: 0.03 }, // only the valid width survives
    });
  });

  test('max_instances of 0 is a valid (non-negative integer) cap', () => {
    const res = { type_names: ['a'], max_instances: [0] };
    expect(buildCatalogDims(res)).toEqual({ a: { max_instances: 0 } });
  });

  test('uppercase hex is accepted', () => {
    const res = { type_names: ['a'], color_hex: ['#ABCDEF'] };
    expect(buildCatalogDims(res)).toEqual({ a: { color: '#ABCDEF' } });
  });

  test('a non-string / empty type name is skipped', () => {
    const res = {
      type_names: ['a', '', 42],
      object_height_m: [0.03, 0.03, 0.03],
    };
    expect(buildCatalogDims(res)).toEqual({ a: { height_m: 0.03 } });
  });

  test('guards: null / missing / non-array type_names → {}', () => {
    expect(buildCatalogDims(null)).toEqual({});
    expect(buildCatalogDims(undefined)).toEqual({});
    expect(buildCatalogDims({})).toEqual({});
    expect(buildCatalogDims({ type_names: 'wuerfel' })).toEqual({});
    expect(buildCatalogDims({ type_names: [] })).toEqual({});
  });

  test('returns a fresh object (no shared reference across calls)', () => {
    const res = { type_names: ['a'], object_height_m: [0.03] };
    const a = buildCatalogDims(res);
    const b = buildCatalogDims(res);
    expect(a).not.toBe(b);
    expect(a).toEqual(b);
  });
});
