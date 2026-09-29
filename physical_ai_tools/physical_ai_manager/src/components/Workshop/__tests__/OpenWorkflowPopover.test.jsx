/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Öffnen" (OpenWorkflowPopover) behaves like every Roboter-Studio popover
// (owner decision R1-O3, usePopoverDismiss.js): outside pointerdown in the
// capture phase, focus leaving, Esc back to the button, focus into the list
// once it has loaded, arrow keys between the choices — and the R2-O3 lock
// never closes a list that is already open.

import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import OpenWorkflowPopover from '../OpenWorkflowPopover';
import { DE } from '../blocks/messages_de';

const mockPicker = vi.hoisted(() => ({ resolve: null, lockedReason: null }));
vi.mock('../TemplatePicker', async () => {
  const ReactMod = await import('react');
  return {
    __esModule: true,
    default: function MockTemplatePicker({ onPicked, lockedReason }) {
      mockPicker.lockedReason = lockedReason;
      const [rows, setRows] = ReactMod.useState(null);
      ReactMod.useEffect(() => {
        let live = true;
        new Promise((r) => { mockPicker.resolve = r; }).then((list) => { if (live) setRows(list); });
        return () => { live = false; };
      }, []);
      if (!rows) return <p>Workflows werden geladen ...</p>;
      return (
        <ul>
          {rows.map((w) => (
            <li key={w.id}>
              <button type="button" disabled={!!lockedReason} onClick={() => onPicked(w)}>{`Öffnen ${w.name}`}</button>
            </li>
          ))}
        </ul>
      );
    },
  };
});

const ROWS = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }, { id: 'c', name: 'C' }];
const trigger = () => screen.getByRole('button', { name: DE.DOCK_OPEN_WORKFLOW });
const panel = () => screen.queryByRole('dialog', { name: DE.DOCK_OPEN_WORKFLOW });
const choice = (n) => screen.getByRole('button', { name: `Öffnen ${n}` });

async function openAndLoad(props = {}) {
  const view = render(
    <div>
      <OpenWorkflowPopover onPicked={() => {}} {...props} />
      <button type="button">Danach</button>
    </div>,
  );
  trigger().focus();
  fireEvent.click(trigger());
  expect(panel()).not.toBeNull();
  await act(async () => { mockPicker.resolve(ROWS); });
  return view;
}

test('the button announces the popover it controls', async () => {
  await openAndLoad();
  expect(trigger()).toHaveAttribute('aria-haspopup', 'dialog');
  expect(trigger()).toHaveAttribute('aria-expanded', 'true');
  expect(trigger().getAttribute('aria-controls')).toBe(panel().id);
});

test('focus lands on the first choice as soon as the list has loaded', async () => {
  await openAndLoad();
  expect(choice('A')).toHaveFocus();
});

test('ArrowDown/ArrowUp wrap and Home/End jump between the choices, never reaching the page', async () => {
  const outside = vi.fn();
  document.addEventListener('keydown', outside);
  try {
    await openAndLoad();
    fireEvent.keyDown(choice('A'), { key: 'ArrowDown' });
    expect(choice('B')).toHaveFocus();
    fireEvent.keyDown(choice('B'), { key: 'End' });
    expect(choice('C')).toHaveFocus();
    fireEvent.keyDown(choice('C'), { key: 'ArrowDown' });
    expect(choice('A')).toHaveFocus();
    fireEvent.keyDown(choice('A'), { key: 'ArrowUp' });
    expect(choice('C')).toHaveFocus();
    fireEvent.keyDown(choice('C'), { key: 'Home' });
    expect(choice('A')).toHaveFocus();
    expect(outside).not.toHaveBeenCalled();
  } finally {
    document.removeEventListener('keydown', outside);
  }
});

test('Esc closes it and hands focus back to the button', async () => {
  await openAndLoad();
  fireEvent.keyDown(choice('A'), { key: 'Escape' });
  expect(panel()).toBeNull();
  expect(trigger()).toHaveFocus();
});

test('ArrowDown on the closed button opens it and moves into the list', async () => {
  render(<OpenWorkflowPopover onPicked={() => {}} />);
  trigger().focus();
  fireEvent.keyDown(trigger(), { key: 'ArrowDown' });
  expect(panel()).not.toBeNull();
  await act(async () => { mockPicker.resolve(ROWS); });
  expect(choice('A')).toHaveFocus();
});

test('a pointerdown outside closes it even when its target stops the event (Blockly does)', async () => {
  await openAndLoad();
  const blocker = document.createElement('div');
  blocker.addEventListener('pointerdown', (e) => { e.stopPropagation(); e.preventDefault(); });
  document.body.appendChild(blocker);
  try {
    fireEvent.pointerDown(blocker);
    expect(panel()).toBeNull();
  } finally {
    blocker.remove();
  }
});

test('a pointerdown inside keeps it open', async () => {
  await openAndLoad();
  fireEvent.pointerDown(choice('B'));
  expect(panel()).not.toBeNull();
});

test('focus moving outside closes it; focus going nowhere does not', async () => {
  await openAndLoad();
  fireEvent.focusOut(choice('A'), { relatedTarget: null });
  expect(panel()).not.toBeNull();
  fireEvent.focusOut(choice('A'), { relatedTarget: screen.getByRole('button', { name: 'Danach' }) });
  expect(panel()).toBeNull();
});

test('choosing closes it and hands the row on', async () => {
  const onPicked = vi.fn();
  await openAndLoad({ onPicked });
  fireEvent.click(choice('B'));
  expect(onPicked).toHaveBeenCalledWith({ id: 'b', name: 'B' });
  expect(panel()).toBeNull();
});

test('R2-O3: a lock arriving while it is open keeps the list, every choice disabled and the button saying why', async () => {
  const { rerender } = await openAndLoad();
  rerender(
    <div>
      <OpenWorkflowPopover onPicked={() => {}} lockedReason={DE.STOP_PROGRAM_FIRST} />
      <button type="button">Danach</button>
    </div>,
  );
  expect(panel()).not.toBeNull();
  expect(mockPicker.lockedReason).toBe(DE.STOP_PROGRAM_FIRST);
  expect(trigger()).toBeDisabled();
  expect(trigger()).toHaveAttribute('title', DE.STOP_PROGRAM_FIRST);
  for (const n of ['A', 'B', 'C']) expect(choice(n)).toBeDisabled();
});
