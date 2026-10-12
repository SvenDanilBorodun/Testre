/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// layout/ResizeHandle.jsx — the one pane divider (spec §5.1). jsdom has no
// layout, so the parent's box is mocked through getBoundingClientRect, and
// requestAnimationFrame and ResizeObserver are driven by hand.

import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import ResizeHandle from '../ResizeHandle';
import { LAYOUT_DE } from '../layoutCopy';

const rect = (width, height = 500, left = 0, top = 0) => ({
  width, height, left, top, right: left + width, bottom: top + height, x: left, y: top,
});
let rowRect;
let rafQueue;
let observers;
let originalRaf;
let originalCaf;
let originalRO;
let rectSpy;

beforeEach(() => {
  rowRect = rect(1200);
  rafQueue = [];
  observers = [];
  originalRaf = window.requestAnimationFrame;
  originalCaf = window.cancelAnimationFrame;
  originalRO = window.ResizeObserver;
  window.requestAnimationFrame = vi.fn((cb) => { rafQueue.push(cb); return rafQueue.length; });
  window.cancelAnimationFrame = vi.fn((id) => { rafQueue[id - 1] = null; });
  window.ResizeObserver = class {
    constructor(cb) { this.cb = cb; observers.push(this); }

    observe(el) { this.el = el; }

    disconnect() { this.gone = true; }
  };
  rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function boxOf() {
    return this.dataset && this.dataset.testid === 'row' ? rowRect : rect(0, 0);
  });
});

afterEach(() => {
  window.requestAnimationFrame = originalRaf;
  window.cancelAnimationFrame = originalCaf;
  window.ResizeObserver = originalRO;
  rectSpy.mockRestore();
});

const flushFrames = () => act(() => {
  const due = rafQueue;
  rafQueue = [];
  due.forEach((cb) => cb && cb(0));
});

// The editor ↔ dock divider of WI-1.7, in a render-counting owner.
function DockOwner({ onCommit, renders, initial = 380, extra = {} }) {
  // eslint-disable-next-line no-param-reassign
  renders.count += 1;
  const [width, setWidth] = useState(initial);
  return (
    <div data-testid="row">
      <ResizeHandle
        orientation="vertical"
        cssVar="--dock-w"
        unit="px"
        value={width}
        defaultValue={380}
        bounds={(r) => ({ min: 280, max: r ? Math.max(280, Math.min(760, r.width - 360)) : 760 })}
        fromPointer={(e, r) => r.right - e.clientX}
        growKey="ArrowLeft"
        step={16}
        largeStep={64}
        label="Breite der Werkzeuge"
        valueText={(v) => `${Math.round(v)} Pixel`}
        onCommit={(v) => { onCommit(v); setWidth(v); }}
        {...extra}
      />
    </div>
  );
}

function mountDock(props = {}) {
  const onCommit = vi.fn();
  const renders = { count: 0 };
  const utils = render(<DockOwner onCommit={onCommit} renders={renders} {...props} />);
  const row = screen.getByTestId('row');
  const handle = screen.getByRole('separator');
  return { ...utils, onCommit, renders, row, handle };
}

describe('ResizeHandle — the element', () => {
  test('a focusable separator with integer ARIA values, the title and the grip', () => {
    const { handle } = mountDock();
    expect(handle).toHaveAttribute('tabindex', '0');
    expect(handle).toHaveAttribute('aria-orientation', 'vertical');
    expect(handle).toHaveAttribute('aria-label', 'Breite der Werkzeuge');
    expect(handle).toHaveAttribute('aria-valuenow', '380');
    expect(handle).toHaveAttribute('aria-valuemin', '280');
    expect(handle).toHaveAttribute('aria-valuemax', '760');
    expect(handle).toHaveAttribute('aria-valuetext', '380 Pixel');
    expect(handle).toHaveAttribute('title', LAYOUT_DE.RESIZE_TITLE);
    // eslint-disable-next-line testing-library/no-node-access
    expect(handle.querySelector('[data-icon="resizeGrip"]')).not.toBeNull();
    expect(handle.className).toContain('hidden md:flex');
    expect(handle.className).toContain('w-2 cursor-col-resize');
  });

  test('a horizontal bar draws the row grip; a fraction reads as percent', () => {
    render(
      <div data-testid="row">
        <ResizeHandle
          orientation="horizontal"
          cssVar="--dock-split"
          unit="fraction"
          value={0.5}
          defaultValue={0.5}
          bounds={() => ({ min: 0.18, max: 0.82 })}
          fromPointer={(e, r) => (e.clientY - r.top) / r.height}
          growKey="ArrowDown"
          step={0.02}
          largeStep={0.1}
          label="Aufteilung"
          valueText={(v) => `${Math.round(v * 100)} Prozent`}
          onCommit={() => {}}
          showBelowMd
        />
      </div>,
    );
    const handle = screen.getByRole('separator');
    expect(handle).toHaveAttribute('aria-orientation', 'horizontal');
    expect(handle).toHaveAttribute('aria-valuenow', '50');
    expect(handle).toHaveAttribute('aria-valuemin', '18');
    expect(handle).toHaveAttribute('aria-valuemax', '82');
    // eslint-disable-next-line testing-library/no-node-access
    expect(handle.querySelector('[data-icon="resizeGripRow"]')).not.toBeNull();
    // showBelowMd: shown at every width — no `hidden` class at all.
    expect(handle.className.split(/\s+/)).not.toContain('hidden');
    expect(handle.className.split(/\s+/)).toContain('flex');
    expect(handle.className).toContain('h-2 cursor-row-resize');
    expect(screen.getByTestId('row').style.getPropertyValue('--dock-split')).toBe('0.5');
  });

  test('the variable is written on the PARENT, clamped into the measured bounds', () => {
    rowRect = rect(900); // max = 900 − 360 = 540
    const { row, handle } = mountDock({ initial: 700 });
    expect(row.style.getPropertyValue('--dock-w')).toBe('540px');
    expect(handle).toHaveAttribute('aria-valuemax', '540');
    expect(handle).toHaveAttribute('aria-valuenow', '540');
  });

  test('a className never decides the display (Tailwind orders hidden after flex)', () => {
    const { handle } = mountDock({ extra: { className: 'flex ml-1' } });
    expect(handle.className).toContain('hidden md:flex');
    expect(handle.className).toContain('ml-1');
  });
});

describe('ResizeHandle — pointer', () => {
  test('a drag writes the variable once per frame and commits ONCE across pointerup + lostpointercapture', () => {
    const { row, handle, onCommit, renders } = mountDock();
    const rendersBefore = renders.count;
    fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 820 });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 800 }); // 1200 − 800 = 400
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 750 }); // 450
    // One frame for two moves, and nothing written before it.
    expect(rafQueue.filter(Boolean)).toHaveLength(1);
    expect(row.style.getPropertyValue('--dock-w')).toBe('380px');
    flushFrames();
    expect(row.style.getPropertyValue('--dock-w')).toBe('450px');
    expect(handle).toHaveAttribute('aria-valuenow', '450');
    expect(handle).toHaveAttribute('aria-valuetext', '450 Pixel');
    // The owner never re-rendered during the drag.
    expect(renders.count).toBe(rendersBefore);
    expect(onCommit).not.toHaveBeenCalled();

    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 100 }); // 1100 → clamped to 760
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 100 });
    fireEvent.lostPointerCapture(handle, { pointerId: 1 });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(760);
    expect(row.style.getPropertyValue('--dock-w')).toBe('760px');
  });

  test('pointercancel ends the drag with one commit too', () => {
    const { handle, onCommit } = mountDock();
    fireEvent.pointerDown(handle, { button: 0, pointerId: 2, clientX: 820 });
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 700 }); // 500
    fireEvent.pointerCancel(handle, { pointerId: 2 });
    fireEvent.lostPointerCapture(handle, { pointerId: 2 });
    fireEvent.pointerUp(handle, { pointerId: 2 });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(500);
  });

  // Verification round 1, F5b: a click on the divider is no resize. Before,
  // it committed the value on screen — a size squeezed by a narrow window was
  // then persisted, and growing the window back no longer restored the
  // student's size.
  test('a click (press + release, no move) on a squeezed divider persists nothing', () => {
    const { row, handle, onCommit } = mountDock({ initial: 600 });
    const observer = observers.find((o) => o.el === row);
    rowRect = rect(800); // max 440
    act(() => observer.cb([]));
    expect(row.style.getPropertyValue('--dock-w')).toBe('440px');
    fireEvent.pointerDown(handle, { button: 0, pointerId: 5, clientX: 360 });
    fireEvent.pointerUp(handle, { pointerId: 5, clientX: 360 });
    fireEvent.lostPointerCapture(handle, { pointerId: 5 });
    flushFrames();
    expect(onCommit).not.toHaveBeenCalled();
    rowRect = rect(1400);
    act(() => observer.cb([]));
    expect(row.style.getPropertyValue('--dock-w')).toBe('600px');
  });

  test('a press whose moves end where they started commits nothing', () => {
    const { row, handle, onCommit } = mountDock();
    fireEvent.pointerDown(handle, { button: 0, pointerId: 6, clientX: 820 });
    fireEvent.pointerMove(handle, { pointerId: 6, clientX: 800 }); // 400
    fireEvent.pointerMove(handle, { pointerId: 6, clientX: 820 }); // back to 380
    flushFrames();
    fireEvent.pointerUp(handle, { pointerId: 6, clientX: 820 });
    expect(onCommit).not.toHaveBeenCalled();
    expect(row.style.getPropertyValue('--dock-w')).toBe('380px');
  });

  test('a move without a press, or a secondary button, does nothing', () => {
    const { row, handle, onCommit } = mountDock();
    fireEvent.pointerMove(handle, { pointerId: 3, clientX: 600 });
    fireEvent.pointerDown(handle, { button: 2, pointerId: 3, clientX: 600 });
    fireEvent.pointerMove(handle, { pointerId: 3, clientX: 600 });
    fireEvent.pointerUp(handle, { pointerId: 3 });
    flushFrames();
    expect(onCommit).not.toHaveBeenCalled();
    expect(row.style.getPropertyValue('--dock-w')).toBe('380px');
  });
});

describe('ResizeHandle — keyboard', () => {
  test('the grow arrow grows, the opposite arrow shrinks, Shift takes the large step; each commits', () => {
    const { row, handle, onCommit } = mountDock();
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expect(onCommit).toHaveBeenLastCalledWith(396);
    expect(row.style.getPropertyValue('--dock-w')).toBe('396px');
    fireEvent.keyDown(handle, { key: 'ArrowRight', shiftKey: true });
    expect(onCommit).toHaveBeenLastCalledWith(332);
    fireEvent.keyDown(handle, { key: 'ArrowRight' });
    expect(onCommit).toHaveBeenLastCalledWith(316);
    expect(onCommit).toHaveBeenCalledTimes(3);
  });

  test('Home and End jump to the bounds; a step past a bound is clamped', () => {
    const { handle, onCommit } = mountDock();
    fireEvent.keyDown(handle, { key: 'End' });
    expect(onCommit).toHaveBeenLastCalledWith(760);
    fireEvent.keyDown(handle, { key: 'ArrowLeft', shiftKey: true });
    expect(onCommit).toHaveBeenLastCalledWith(760);
    fireEvent.keyDown(handle, { key: 'Home' });
    expect(onCommit).toHaveBeenLastCalledWith(280);
    fireEvent.keyDown(handle, { key: 'ArrowRight' });
    expect(onCommit).toHaveBeenLastCalledWith(280);
  });

  test('handled keys never reach the document (Blockly listens there); others pass', () => {
    const { handle, onCommit } = mountDock();
    const heard = vi.fn();
    document.addEventListener('keydown', heard);
    try {
      fireEvent.keyDown(handle, { key: 'ArrowLeft' });
      fireEvent.keyDown(handle, { key: 'Home' });
      expect(heard).not.toHaveBeenCalled();
      fireEvent.keyDown(handle, { key: 'a' });
      expect(heard).toHaveBeenCalledTimes(1);
      expect(onCommit).toHaveBeenCalledTimes(2);
    } finally {
      document.removeEventListener('keydown', heard);
    }
  });
});

describe('ResizeHandle — double-click and a resized parent', () => {
  test('a double-click returns to the default and commits it', () => {
    const { row, handle, onCommit } = mountDock({ initial: 600 });
    expect(row.style.getPropertyValue('--dock-w')).toBe('600px');
    fireEvent.doubleClick(handle);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(380);
    expect(row.style.getPropertyValue('--dock-w')).toBe('380px');
  });

  test('a narrower parent re-clamps WITHOUT committing; growing it back restores the committed size', () => {
    const { row, onCommit } = mountDock({ initial: 600 });
    const observer = observers.find((o) => o.el === row);
    expect(observer).toBeTruthy();
    rowRect = rect(800); // max 440
    act(() => observer.cb([]));
    expect(row.style.getPropertyValue('--dock-w')).toBe('440px');
    rowRect = rect(1400); // max 760 again
    act(() => observer.cb([]));
    expect(row.style.getPropertyValue('--dock-w')).toBe('600px');
    expect(onCommit).not.toHaveBeenCalled();
  });

  test('a parent resize during a drag leaves the drag alone', () => {
    const { row, handle } = mountDock();
    const observer = observers.find((o) => o.el === row);
    fireEvent.pointerDown(handle, { button: 0, pointerId: 4, clientX: 820 });
    fireEvent.pointerMove(handle, { pointerId: 4, clientX: 700 });
    flushFrames();
    expect(row.style.getPropertyValue('--dock-w')).toBe('500px');
    act(() => observer.cb([]));
    expect(row.style.getPropertyValue('--dock-w')).toBe('500px');
  });

  test('unmounting disconnects the observer', () => {
    const { row, unmount } = mountDock();
    const observer = observers.find((o) => o.el === row);
    unmount();
    expect(observer.gone).toBe(true);
  });
});
