// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The chronophotography trail's pure logic (M6, spec §6.6): snapshots, fade,
// the idle loop and reduced motion. UrdfTwin wires it to the clones
// (components/__tests__/UrdfTwin.test.jsx, „chronoTrail").

import {
  CHRONO_DEFAULTS, CHRONO_MAX_OPACITY, chronoTick, createChronoTrail, isSoftwareRenderer,
  resolveChronoTrail, webglRendererName,
} from '../chronoTrail';

const pose = (j1, j2 = 0) => new Map([['joint1', j1], ['joint2', j2]]);

describe('resolveChronoTrail', () => {
  it('off for false / null / 0', () => {
    expect(resolveChronoTrail(false)).toBeNull();
    expect(resolveChronoTrail(null)).toBeNull();
    expect(resolveChronoTrail(undefined)).toBeNull();
  });
  it('true = the defaults (8 ghosts, 120 ms, 1200 ms)', () => {
    expect(resolveChronoTrail(true)).toEqual({ count: 8, stepMs: 120, fadeMs: 1200 });
    expect(CHRONO_DEFAULTS).toEqual({ count: 8, stepMs: 120, fadeMs: 1200 });
  });
  it('an object overrides key by key; nonsense falls back', () => {
    expect(resolveChronoTrail({ count: 3 })).toEqual({ count: 3, stepMs: 120, fadeMs: 1200 });
    expect(resolveChronoTrail({ count: -1, stepMs: 'x', fadeMs: 500 })).toEqual({ count: 8, stepMs: 120, fadeMs: 500 });
    expect(resolveChronoTrail({ count: 999 }).count).toBe(32);
  });
});

describe('chronoTick', () => {
  const cfg = { count: 3, stepMs: 120, fadeMs: 1200 };

  it('the first look only records the pose: a resting arm leaves no ghost', () => {
    const t = createChronoTrail(cfg);
    const r = chronoTick(t, 0, pose(0));
    expect(r.snap).toBe(-1);
    expect(r.opacities).toEqual([0, 0, 0]);
    expect(r.needsRender).toBe(false);
    for (let now = 16; now < 2000; now += 16) {
      expect(chronoTick(t, now, pose(0)).needsRender).toBe(false);
    }
  });

  it('a move > 0.01 rad after stepMs snapshots into the oldest ghost, round robin', () => {
    const t = createChronoTrail(cfg);
    chronoTick(t, 0, pose(0));
    expect(chronoTick(t, 60, pose(0.5)).snap).toBe(-1); // not due yet
    const a = chronoTick(t, 120, pose(0.5));
    expect(a.snap).toBe(0);
    expect(t.slots[0].pose).toEqual(pose(0.5));
    expect(a.opacities[0]).toBeCloseTo(CHRONO_MAX_OPACITY);
    expect(a.needsRender).toBe(true);
    expect(chronoTick(t, 240, pose(1)).snap).toBe(1);
    expect(chronoTick(t, 360, pose(1.5)).snap).toBe(2);
    expect(chronoTick(t, 480, pose(2)).snap).toBe(0); // the oldest again
  });

  it('a move of 0.01 rad or less is no motion', () => {
    const t = createChronoTrail(cfg);
    chronoTick(t, 0, pose(0));
    expect(chronoTick(t, 120, pose(0.01)).snap).toBe(-1);
    expect(chronoTick(t, 240, pose(0.011)).snap).toBe(0);
  });

  it('the pose is asked for only when a check is due (once per stepMs)', () => {
    const t = createChronoTrail(cfg);
    const get = vi.fn(() => pose(0));
    for (let now = 0; now <= 480; now += 16) chronoTick(t, now, get);
    expect(get.mock.calls.length).toBe(4); // at 0, 128, 256, 384: every ≥ 120 ms
  });

  it('a ghost fades linearly .35 → 0 over fadeMs; then the loop idles after one hiding frame', () => {
    const t = createChronoTrail(cfg);
    chronoTick(t, 0, pose(0));
    chronoTick(t, 120, pose(1));
    expect(chronoTick(t, 720, pose(1)).opacities[0]).toBeCloseTo(CHRONO_MAX_OPACITY / 2);
    const last = chronoTick(t, 1319, pose(1));
    expect(last.opacities[0]).toBeGreaterThan(0);
    const gone = chronoTick(t, 1320, pose(1));
    expect(gone.opacities[0]).toBe(0);
    expect(gone.needsRender).toBe(true); // the frame that hides it
    expect(chronoTick(t, 1336, pose(1)).needsRender).toBe(false);
  });

  it('reduced motion: no snapshot, every ghost at 0', () => {
    const t = createChronoTrail(cfg);
    chronoTick(t, 0, pose(0));
    chronoTick(t, 120, pose(1));
    const r = chronoTick(t, 140, pose(2), { reduced: true });
    expect(r.snap).toBe(-1);
    expect(r.opacities).toEqual([0, 0, 0]);
    expect(r.needsRender).toBe(true); // hides the one that was visible
    expect(chronoTick(t, 400, pose(3), { reduced: true }).needsRender).toBe(false);
  });

  it('no pose yet (robot without joints): nothing', () => {
    const t = createChronoTrail(cfg);
    expect(chronoTick(t, 0, () => null)).toEqual({ snap: -1, opacities: [0, 0, 0], needsRender: false });
  });
});

// D30: the trail is off on a software WebGL renderer, decided from its name.
describe('software WebGL renderer (D30)', () => {
  const gl = (unmasked, masked = 'WebKit WebGL', { ext = true, throws = false } = {}) => ({
    RENDERER: 0x1f01,
    getExtension: (n) => {
      if (throws) throw new Error('lost');
      return ext && n === 'WEBGL_debug_renderer_info' ? { UNMASKED_RENDERER_WEBGL: 0x9246 } : null;
    },
    getParameter: (p) => (p === 0x9246 ? unmasked : masked),
  });

  it('reads the unmasked name, else the masked one, else null', () => {
    expect(webglRendererName(gl('ANGLE (Apple, ANGLE Metal Renderer: Apple M5 Pro)'))).toBe('ANGLE (Apple, ANGLE Metal Renderer: Apple M5 Pro)');
    expect(webglRendererName(gl(null, 'Mesa llvmpipe', { ext: false }))).toBe('Mesa llvmpipe');
    expect(webglRendererName(gl(null, 'llvmpipe', { throws: true }))).toBe('llvmpipe');
    expect(webglRendererName(null)).toBeNull();
    expect(webglRendererName({})).toBeNull();
    expect(webglRendererName({ getParameter: () => { throw new Error('x'); } })).toBeNull();
  });

  it('SwiftShader, llvmpipe and the Basic Render Driver are software; GPUs and unknowns are not', () => {
    [
      'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0) (0x0000C0DE)), SwiftShader driver)',
      'Google SwiftShader',
      'llvmpipe (LLVM 15.0.7, 256 bits)',
      'ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0, D3D11)',
      'Software Rasterizer',
    ].forEach((n) => expect(isSoftwareRenderer(n)).toBe(true));
    [
      'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)',
      'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)',
      'Mali-G610 MC4',
      'WebKit WebGL',
      '',
      null,
      undefined,
    ].forEach((n) => expect(isSoftwareRenderer(n)).toBe(false));
  });
});
