// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: hook classes, custom properties and the DOM shape. */

// The Stepper primitive (spec §4.7, D16): the Aufnahme page's TaskCard
// stepper DOM, plus ↑/↓ inside the control and the wheel over the NUMBER only.

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import Stepper from '../Stepper';
import TaskCard from '../../Record/TaskCard';

const S = {
  field: 'episodeTime', label: 'Episode', unit: 's', min: 3, max: 120, color: 'var(--rec-run)',
};
const LABELS = { dec: (l) => `${l} verringern`, inc: (l) => `${l} erhöhen` };

function renderStepper(props = {}) {
  const onChange = vi.fn();
  const utils = render(
    <Stepper
      {...S}
      value={20}
      decLabel="Episode verringern"
      incLabel="Episode erhöhen"
      onChange={onChange}
      {...props}
    />,
  );
  return { onChange, ...utils };
}

describe('Stepper', () => {
  it('the DOM is TaskCard\'s stepper DOM (only the su-stp class is added)', () => {
    const view = render(
      <TaskCard
        labels={{
          title: 'Aufgabe', chipLocked: 'gesperrt', chipEditable: 'bearbeitbar', name: 'Aufgabenname',
          namePlaceholder: '', instruction: 'Aufgabenanweisung', instructionPlaceholder: '', flow: 'Ablauf',
          ...LABELS,
        }}
        form={{ episodeTime: 20 }}
        editable
        steppers={[S]}
        onChange={() => {}}
      />,
    );
    const theirs = view.container.querySelector('.rec-stp').outerHTML;
    view.unmount();
    const { container } = renderStepper({ className: 'rec-stp' });
    const ours = container.querySelector('.su-stp');
    expect(ours).toHaveClass('rec-stp');
    expect(ours.outerHTML.replace('class="su-stp rec-stp"', 'class="rec-stp"')).toBe(theirs);
  });

  it('clicks step by one and stop at the limits', () => {
    const { onChange, rerender } = renderStepper();
    fireEvent.click(screen.getByRole('button', { name: 'Episode erhöhen' }));
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 21);
    fireEvent.click(screen.getByRole('button', { name: 'Episode verringern' }));
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 19);
    rerender(<Stepper {...S} value={120} decLabel="d" incLabel="Episode erhöhen" onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'Episode erhöhen' })).toBeDisabled();
  });

  it('an unusable value reads as min', () => {
    renderStepper({ value: 'abc' });
    expect(screen.getByRole('button', { name: 'Episode verringern' })).toBeDisabled();
    expect(screen.getByRole('status', { name: 'Episode' })).toHaveTextContent('3s');
  });

  it('a pointer click releases the focus, a keyboard click keeps it', () => {
    renderStepper();
    const plus = screen.getByRole('button', { name: 'Episode erhöhen' });
    plus.focus();
    fireEvent.click(plus, { detail: 1 });
    expect(plus).not.toHaveFocus();
    plus.focus();
    fireEvent.click(plus, { detail: 0 });
    expect(plus).toHaveFocus();
  });

  it('↑/↓ with the focus inside step by one (preventDefault); ←/→ do nothing', () => {
    const { onChange } = renderStepper();
    const minus = screen.getByRole('button', { name: 'Episode verringern' });
    minus.focus();
    const up = fireEvent.keyDown(minus, { key: 'ArrowUp' });
    expect(up).toBe(false); // default prevented
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 21);
    fireEvent.keyDown(minus, { key: 'ArrowDown' });
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 19);
    onChange.mockClear();
    expect(fireEvent.keyDown(minus, { key: 'ArrowLeft' })).toBe(true);
    expect(fireEvent.keyDown(minus, { key: 'ArrowRight' })).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('↑/↓ at a limit and while disabled change nothing', () => {
    const { onChange, rerender } = renderStepper({ value: 120 });
    const minus = screen.getByRole('button', { name: 'Episode verringern' });
    fireEvent.keyDown(minus, { key: 'ArrowUp' });
    expect(onChange).not.toHaveBeenCalled();
    rerender(<Stepper {...S} value={20} decLabel="Episode verringern" incLabel="Episode erhöhen" onChange={onChange} disabled />);
    expect(fireEvent.keyDown(minus, { key: 'ArrowUp' })).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('the wheel over the number steps one per event and keeps the page from scrolling', () => {
    const { onChange } = renderStepper();
    const out = screen.getByRole('status', { name: 'Episode' });
    expect(fireEvent.wheel(out, { deltaY: -100 })).toBe(false);
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 21);
    expect(fireEvent.wheel(out, { deltaY: 3 })).toBe(false);
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 19);
    onChange.mockClear();
    fireEvent.wheel(out, { deltaY: 0.4 });
    expect(onChange).not.toHaveBeenCalled();
  });

  it('the wheel anywhere else does nothing; a disabled stepper lets the page scroll', () => {
    const { onChange, rerender } = renderStepper();
    expect(fireEvent.wheel(screen.getByRole('button', { name: 'Episode erhöhen' }), { deltaY: -100 })).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
    rerender(<Stepper {...S} value={20} decLabel="d" incLabel="i" onChange={onChange} disabled />);
    expect(fireEvent.wheel(screen.getByRole('status', { name: 'Episode' }), { deltaY: -100 })).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('the wheel listener is not passive (it must prevent the scroll)', () => {
    const spy = vi.spyOn(HTMLElement.prototype, 'addEventListener');
    renderStepper();
    const out = screen.getByRole('status', { name: 'Episode' });
    const i = spy.mock.calls.findIndex(([type], n) => type === 'wheel' && spy.mock.contexts[n] === out);
    expect(i).toBeGreaterThan(-1);
    expect(spy.mock.calls[i][2]).toEqual({ passive: false });
    spy.mockRestore();
  });

  it('the number rolls when it changes (aria-hidden columns, the output keeps its name)', () => {
    const { container, rerender } = renderStepper();
    rerender(<Stepper {...S} value={21} decLabel="d" incLabel="i" onChange={() => {}} />);
    expect(container.querySelector('.su-roll__cols')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByRole('status', { name: 'Episode' })).toBeInTheDocument();
  });
});
