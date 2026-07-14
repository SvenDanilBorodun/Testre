/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Sim-stage seam for the 2D table editor: per-type object size/colour from the
// catalog dims map + the per-type placement cap. The lazy 3D twin is mocked away
// (jsdom has no WebGL), so these assertions are pure-DOM on the 2D SVG editor.

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import SimScene from '../SimScene';

// The 2D editor's SVG viewBox (SimScene-internal consts, mirrored here):
//   SVG_W = PX_PER_M(500) * (VIEW_MAX_Y 0.30 − VIEW_MIN_Y −0.30) = 300
//   SVG_H = PX_PER_M(500) * (VIEW_MAX_X 0.32 − VIEW_MIN_X −0.05) = 185
const SVG_W = 300;
const SVG_H = 185;

// Mock the lazy 3D twin (resolves to src/components/UrdfTwin, same module SimScene
// dynamic-imports as `../UrdfTwin`). A trivial component keeps three.js out.
vi.mock('../../UrdfTwin', () => ({
  __esModule: true,
  default: (props) => (
    <div data-testid="urdf-twin" data-held={String(props.heldObjectId)} />
  ),
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const CATALOG = [['Würfel', 'wuerfel']];

beforeEach(() => {
  mockToast.mockClear();
  mockToast.error.mockClear();
  mockToast.success.mockClear();
});

describe('SimScene — sim-stage seam', () => {
  test('default (stack) layout renders the notice + palette', async () => {
    render(<SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(screen.getByText(/Test im Simulator\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Würfel' })).toBeInTheDocument();
  });

  test('2D square size + colour derive from catalogDims', async () => {
    const { container } = render(
      <SimScene
        scene={{ objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] }}
        catalog={CATALOG}
        catalogDims={{ wuerfel: { height_m: 0.05, width_m: 0.04, color: '#00ff00', max_instances: 2 } }}
        onChange={() => {}}
      />
    );
    await screen.findByTestId('urdf-twin');
    // width_m 0.04 × PX_PER_M 500 = 20 px, and the catalog colour. SVG <rect> has
    // no ARIA role, so a DOM query is the only way to assert on it.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const rect = Array.from(container.querySelectorAll('rect')).find(
      (r) => r.getAttribute('fill') === '#00ff00',
    );
    expect(rect).toBeTruthy();
    expect(rect.getAttribute('width')).toBe('20');
  });

  test('an object without catalog dims keeps the 15 px amber fallback square', async () => {
    const { container } = render(
      <SimScene
        scene={{ objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] }}
        catalog={CATALOG}
        catalogDims={{}}
        onChange={() => {}}
      />
    );
    await screen.findByTestId('urdf-twin');
    // SVG <rect> has no ARIA role, so a DOM query is the only way to assert on it.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const rect = Array.from(container.querySelectorAll('rect')).find(
      (r) => r.getAttribute('fill') === '#f59e0b',
    );
    expect(rect).toBeTruthy();
    expect(rect.getAttribute('width')).toBe('15');
  });

  test('placement cap: refuses a 3rd object of a capped type with a German toast', async () => {
    const onChange = vi.fn();
    render(
      <SimScene
        scene={{
          objects: [
            { type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 },
            { type: 'wuerfel', tag_id: 1, x: 0.18, y: 0.02, yaw: 0 },
          ],
          zones: [],
        }}
        catalog={CATALOG}
        catalogDims={{ wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 } }}
        onChange={onChange}
      />
    );
    await screen.findByTestId('urdf-twin');
    const svg = screen.getByRole('application');
    // jsdom returns a zero-size rect by default → eventToBase would bail before
    // reaching the cap; give the SVG a real box so the placement path runs.
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: SVG_W, height: SVG_H, right: SVG_W, bottom: SVG_H, x: 0, y: 0,
    });
    // A pointerdown near the annulus centre (object mode is the default).
    fireEvent.pointerDown(svg, { clientX: SVG_W / 2, clientY: SVG_H / 2 });

    expect(mockToast.error).toHaveBeenCalledTimes(1);
    expect(mockToast.error.mock.calls[0][0]).toMatch(/Höchstens 2 × „Würfel"/);
    // The cap short-circuits before emitting → no 3rd object placed.
    expect(onChange).not.toHaveBeenCalled();
  });

  test('placement cap does NOT fire when the type is under its max_instances', async () => {
    const onChange = vi.fn();
    render(
      <SimScene
        scene={{ objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] }}
        catalog={CATALOG}
        catalogDims={{ wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 } }}
        onChange={onChange}
      />
    );
    await screen.findByTestId('urdf-twin');
    const svg = screen.getByRole('application');
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: SVG_W, height: SVG_H, right: SVG_W, bottom: SVG_H, x: 0, y: 0,
    });
    fireEvent.pointerDown(svg, { clientX: SVG_W / 2, clientY: SVG_H / 2 });

    expect(mockToast.error).not.toHaveBeenCalled();
    // A 2nd wuerfel (under the cap of 2) was placed.
    expect(onChange).toHaveBeenCalledTimes(1);
    const emitted = onChange.mock.calls[0][0];
    expect(emitted.objects).toHaveLength(2);
  });
});
