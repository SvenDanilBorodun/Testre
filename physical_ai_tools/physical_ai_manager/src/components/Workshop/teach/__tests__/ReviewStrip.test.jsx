/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewStrip from '../ReviewStrip';
import { DE } from '../../blocks/messages_de';

const TIMES = [0, 100, 200, 300, 400, 1400];

function Harness({ initial, onChange, disabled }) {
  const [range, setRange] = useState(initial);
  return (
    <ReviewStrip
      activity={[0.1, 0.4, 0, 0, 0, 0, 0.2]}
      timesMs={TIMES}
      startIndex={range.startIndex}
      endIndex={range.endIndex}
      disabled={disabled}
      onChange={(next) => { onChange(next); setRange(next); }}
    />
  );
}

const start = () => screen.getByRole('slider', { name: DE.TEACH_HANDLE_START });
const end = () => screen.getByRole('slider', { name: DE.TEACH_HANDLE_END });

describe('ReviewStrip', () => {
  test('a labelled group with two sliders carrying sample-index values', () => {
    render(<Harness initial={{ startIndex: 1, endIndex: 4 }} onChange={vi.fn()} />);
    expect(screen.getByRole('group', { name: DE.TEACH_STRIP_ARIA })).toBeInTheDocument();
    expect(start()).toHaveAttribute('aria-valuenow', '1');
    expect(start()).toHaveAttribute('aria-valuemin', '0');
    expect(start()).toHaveAttribute('aria-valuemax', '3');
    expect(end()).toHaveAttribute('aria-valuenow', '4');
    expect(end()).toHaveAttribute('aria-valuemin', '2');
    expect(end()).toHaveAttribute('aria-valuemax', '5');
    expect(start()).toHaveAttribute('tabindex', '0');
  });

  test('ArrowLeft / ArrowRight move a handle by exactly one sample', () => {
    const onChange = vi.fn();
    render(<Harness initial={{ startIndex: 1, endIndex: 4 }} onChange={onChange} />);
    fireEvent.keyDown(start(), { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith({ startIndex: 2, endIndex: 4 });
    fireEvent.keyDown(end(), { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenLastCalledWith({ startIndex: 2, endIndex: 3 });
    fireEvent.keyDown(end(), { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith({ startIndex: 2, endIndex: 4 });
  });

  test('handles cannot cross, cannot leave fewer than 2 samples, cannot leave the take', () => {
    const onChange = vi.fn();
    render(<Harness initial={{ startIndex: 2, endIndex: 3 }} onChange={onChange} />);
    fireEvent.keyDown(start(), { key: 'ArrowRight' });
    fireEvent.keyDown(end(), { key: 'ArrowLeft' });
    expect(onChange).not.toHaveBeenCalled();
    expect(start()).toHaveAttribute('aria-valuenow', '2');
    expect(end()).toHaveAttribute('aria-valuenow', '3');
    fireEvent.keyDown(end(), { key: 'ArrowRight' });
    fireEvent.keyDown(end(), { key: 'ArrowRight' });
    fireEvent.keyDown(end(), { key: 'ArrowRight' });
    expect(end()).toHaveAttribute('aria-valuenow', '5');
    for (let i = 0; i < 4; i += 1) fireEvent.keyDown(start(), { key: 'ArrowLeft' });
    expect(start()).toHaveAttribute('aria-valuenow', '0');
  });

  test('a pointer drag snaps to the sample nearest in time', () => {
    const onChange = vi.fn();
    render(<Harness initial={{ startIndex: 0, endIndex: 5 }} onChange={onChange} />);
    const group = screen.getByRole('group', { name: DE.TEACH_STRIP_ARIA });
    group.getBoundingClientRect = () => ({ left: 0, width: 1400, top: 0, height: 60, right: 1400, bottom: 60 });
    fireEvent.pointerDown(end(), { pointerId: 1, clientX: 1400 });
    // 700 ms is nearest to the 400 ms sample (index 4), not the 1400 ms one.
    fireEvent.pointerMove(end(), { pointerId: 1, clientX: 700 });
    expect(onChange).toHaveBeenLastCalledWith({ startIndex: 0, endIndex: 4 });
    fireEvent.pointerUp(end(), { pointerId: 1 });
    fireEvent.pointerMove(end(), { pointerId: 1, clientX: 100 });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  test('disabled: keys and drags change nothing', () => {
    const onChange = vi.fn();
    render(<Harness initial={{ startIndex: 1, endIndex: 4 }} onChange={onChange} disabled />);
    fireEvent.keyDown(start(), { key: 'ArrowRight' });
    expect(onChange).not.toHaveBeenCalled();
    expect(start()).toHaveAttribute('tabindex', '-1');
  });
});
