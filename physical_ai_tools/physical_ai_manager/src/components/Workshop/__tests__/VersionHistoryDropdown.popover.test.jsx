/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Verlauf" (VersionHistoryDropdown) behaves like every Roboter-Studio popover
// (owner decision R1-O3, usePopoverDismiss.js). Its „laden" buttons and every
// refusal rule stay exactly as documentSwitchLock.test.jsx pins them; this
// file pins only the popover behaviour around them.

import React from 'react';
import {
  act, fireEvent, render, screen, waitFor,
} from '@testing-library/react';
import VersionHistoryDropdown from '../VersionHistoryDropdown';
import { DE } from '../blocks/messages_de';

vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel({ auth: { session: { access_token: 'jwt', user: { id: 'u1' } } } }),
}));
const mockApi = vi.hoisted(() => ({ listWorkflowVersions: vi.fn(), restoreWorkflowVersion: vi.fn() }));
vi.mock('../../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));
const mockToast = vi.hoisted(() => Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const VERSIONS = [
  { id: 'v3', created_at: '2026-09-27T12:00:00Z' },
  { id: 'v2', created_at: '2026-09-27T11:00:00Z' },
  { id: 'v1', created_at: '2026-09-27T10:00:00Z' },
];
const toggle = () => screen.getByRole('button', { name: new RegExp(DE.VERSION_HISTORY) });
const panel = () => screen.queryByRole('dialog', { name: DE.VERSION_HISTORY });
const loads = () => screen.getAllByRole('button', { name: DE.VERSION_LOAD });

beforeEach(() => {
  mockApi.listWorkflowVersions.mockReset().mockResolvedValue(VERSIONS);
  mockApi.restoreWorkflowVersion.mockReset();
});

async function openList(props = {}) {
  const view = render(
    <div>
      <VersionHistoryDropdown workflowId="wf-1" onRestore={() => {}} {...props} />
      <button type="button">Danach</button>
    </div>,
  );
  toggle().focus();
  fireEvent.click(toggle());
  await waitFor(() => expect(loads()).toHaveLength(3));
  return view;
}

test('the toggle announces a dialog it controls; the list rows are plain rows, the „laden" buttons buttons', async () => {
  await openList();
  expect(toggle()).toHaveAttribute('aria-haspopup', 'dialog');
  expect(toggle()).toHaveAttribute('aria-expanded', 'true');
  expect(toggle().getAttribute('aria-controls')).toBe(panel().id);
  expect(screen.queryByRole('menuitem')).toBeNull();
  for (const b of loads()) expect(b.tagName).toBe('BUTTON');
});

test('focus lands on the newest „laden" once the list has loaded', async () => {
  await openList();
  expect(loads()[0]).toHaveFocus();
});

test('ArrowDown/ArrowUp/Home/End move between the „laden" buttons, never reaching the page', async () => {
  const outside = vi.fn();
  document.addEventListener('keydown', outside);
  try {
    await openList();
    const [a, b, c] = loads();
    fireEvent.keyDown(a, { key: 'ArrowDown' });
    expect(b).toHaveFocus();
    fireEvent.keyDown(b, { key: 'End' });
    expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: 'ArrowDown' });
    expect(a).toHaveFocus();
    fireEvent.keyDown(a, { key: 'ArrowUp' });
    expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: 'Home' });
    expect(a).toHaveFocus();
    expect(outside).not.toHaveBeenCalled();
  } finally {
    document.removeEventListener('keydown', outside);
  }
});

test('Esc closes the list and hands focus back to „Verlauf"', async () => {
  await openList();
  fireEvent.keyDown(loads()[1], { key: 'Escape' });
  expect(panel()).toBeNull();
  expect(toggle()).toHaveFocus();
});

test('a pointerdown outside closes it even when its target stops the event (Blockly does)', async () => {
  await openList();
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

test('focus moving outside closes it; focus going nowhere does not', async () => {
  await openList();
  fireEvent.focusOut(loads()[0], { relatedTarget: null });
  expect(panel()).not.toBeNull();
  fireEvent.focusOut(loads()[0], { relatedTarget: screen.getByRole('button', { name: 'Danach' }) });
  expect(panel()).toBeNull();
});

test('R2-O3: a lock arriving while the list is open keeps it, every „laden" clickable and refusing', async () => {
  const onRestore = vi.fn();
  const { rerender } = await openList({ onRestore });
  rerender(
    <div>
      <VersionHistoryDropdown workflowId="wf-1" onRestore={onRestore} lockedReason={DE.STOP_PROGRAM_FIRST} />
      <button type="button">Danach</button>
    </div>,
  );
  expect(panel()).not.toBeNull();
  const [first] = loads();
  expect(first).toBeEnabled();
  expect(first).toHaveAttribute('title', DE.STOP_PROGRAM_FIRST);
  fireEvent.click(first);
  await act(async () => { await Promise.resolve(); });
  expect(mockToast.error).toHaveBeenCalledWith(DE.STOP_PROGRAM_FIRST);
  expect(mockApi.restoreWorkflowVersion).not.toHaveBeenCalled();
});

test('a student who moves on before the list arrives is never pulled back into it', async () => {
  let finish;
  mockApi.listWorkflowVersions.mockImplementation(() => new Promise((r) => { finish = r; }));
  render(
    <div>
      <VersionHistoryDropdown workflowId="wf-1" onRestore={() => {}} />
      <input aria-label="Suche" />
    </div>,
  );
  toggle().focus();
  fireEvent.click(toggle());
  const other = screen.getByRole('textbox', { name: 'Suche' });
  // Focus leaving the popover closes it (and so drops the pending focus).
  fireEvent.focusOut(toggle(), { relatedTarget: other });
  other.focus();
  expect(panel()).toBeNull();
  await act(async () => { finish(VERSIONS); });
  expect(other).toHaveFocus();
});
