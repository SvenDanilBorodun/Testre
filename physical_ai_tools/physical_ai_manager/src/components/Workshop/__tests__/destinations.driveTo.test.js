/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Batch 2b — the per-block „fahre dorthin" button bridge on destination_pin.
// The FieldImage click can't be simulated headlessly, so we test the two pieces
// it wires: driveToFromBlock (reads coords → registered handler) and the field's
// presence on the pin block (and absence on the other destination blocks).

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import {
  DRIVE_ICON,
  registerDestinationBlocks,
  setDriveToHandler,
  driveToFromBlock,
} from '../blocks/destinations';

beforeAll(() => {
  registerDestinationBlocks();
});

afterEach(() => {
  setDriveToHandler(null);
});

function fakeBlock(fields) {
  return { getFieldValue: (k) => fields[k] };
}

describe('driveToFromBlock', () => {
  test('passes the block name + parsed coordinates to the registered handler', () => {
    const spy = vi.fn();
    setDriveToHandler(spy);
    driveToFromBlock(fakeBlock({ NAME: 'A', X: '0.100', Y: '-0.200', Z: '0.050' }));
    expect(spy).toHaveBeenCalledWith({ name: 'A', x: 0.1, y: -0.2, z: 0.05 });
  });

  test('an un-pinned coordinate ("—") arrives as NaN for the handler to reject', () => {
    const spy = vi.fn();
    setDriveToHandler(spy);
    driveToFromBlock(fakeBlock({ NAME: 'B', X: '—', Y: '—', Z: '—' }));
    expect(spy).toHaveBeenCalledTimes(1);
    const arg = spy.mock.calls[0][0];
    expect(arg.name).toBe('B');
    expect(Number.isNaN(arg.x)).toBe(true);
    expect(Number.isNaN(arg.y)).toBe(true);
    expect(Number.isNaN(arg.z)).toBe(true);
  });

  test('is a safe no-op when no handler is registered or block is null', () => {
    setDriveToHandler(null);
    expect(() => driveToFromBlock(fakeBlock({ NAME: 'A', X: '0', Y: '0', Z: '0' }))).not.toThrow();
    setDriveToHandler(vi.fn());
    expect(() => driveToFromBlock(null)).not.toThrow();
  });
});

describe('destination_pin drive button field', () => {
  test('only the pin block carries the DRIVE_BTN field', () => {
    const ws = new Blockly.Workspace();
    try {
      const pin = ws.newBlock('edubotics_destination_pin');
      expect(pin.getField('DRIVE_BTN')).toBeTruthy();
      // The name/coordinate fields still exist alongside the button.
      expect(pin.getField('NAME')).toBeTruthy();
      expect(pin.getField('X')).toBeTruthy();

      const current = ws.newBlock('edubotics_destination_current');
      expect(current.getField('DRIVE_BTN')).toBeNull();
      const ref = ws.newBlock('edubotics_destination_ref');
      expect(ref.getField('DRIVE_BTN')).toBeNull();
    } finally {
      ws.dispose();
    }
  });
});

// Review round 1 (R1-O1): the drive button draws the app's own solid play icon
// (components/icons), not a hand-drawn triangle.
describe('the drive button image', () => {
  const svg = atob(DRIVE_ICON.replace(/^data:image\/svg\+xml;base64,/, ''));

  test('is the Lucide play polygon, filled white on the teal chip', () => {
    expect(svg).toMatch(/^<svg xmlns='http:\/\/www\.w3\.org\/2000\/svg' width='22' height='16'/);
    expect(svg).toContain("fill='#0f766e'");
    expect(svg).toContain('data-icon="play"');
    expect(svg).toContain('<polygon points="6 3 20 12 6 21 6 3"></polygon>');
    expect(svg).toMatch(/<g [^>]*fill="#ffffff" stroke="#ffffff"/);
    expect(svg).not.toContain('M8 4 L15 8');
  });

  test('is well-formed SVG', () => {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
    expect(doc.getElementsByTagName('parsererror')).toHaveLength(0);
    expect(doc.querySelector('g[data-icon="play"] polygon')).not.toBeNull();
  });
});
