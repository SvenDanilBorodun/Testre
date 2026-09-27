/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Owner decision R2-O3 (review round 2, mi7): while a program runs, nothing
// may replace the open document. The three controls that can — the „Öffnen"
// list, the gallery's clone, the version history — take the page's
// `lockedReason` and refuse with it: every button disabled and saying why,
// a restore refused BEFORE the cloud is asked (a restore the page then did
// not show would leave the cloud row and the editor apart), and a clone that
// finishes after the run started created but not opened.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TemplatePicker from '../TemplatePicker';
import GalleryTab from '../GalleryTab';
import VersionHistoryDropdown from '../VersionHistoryDropdown';
import { DE } from '../blocks/messages_de';

const REASON = DE.STOP_PROGRAM_FIRST;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel({ auth: { session: { access_token: 'jwt', user: { id: 'u1' } } } }),
  useDispatch: () => mockDispatch,
}));
const mockApi = vi.hoisted(() => ({
  cloneWorkflow: vi.fn(),
  listWorkflows: vi.fn(),
  listWorkflowVersions: vi.fn(),
  restoreWorkflowVersion: vi.fn(),
}));
vi.mock('../../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));
vi.mock('../../../hooks/useSupabaseWorkflows', () => ({
  __esModule: true,
  default: () => ({
    loading: false,
    workflows: [
      { id: 'tpl-1', name: 'Vorlage', is_template: true },
      { id: 'wf-1', name: 'Mein Programm', is_template: false, updated_at: '2026-09-27T10:00:00Z' },
    ],
  }),
}));
const mockToast = vi.hoisted(() => Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

beforeEach(() => {
  mockDispatch.mockClear();
  Object.values(mockApi).forEach((f) => f.mockReset());
  mockToast.success.mockClear();
  mockToast.error.mockClear();
});

test('the „Öffnen" list: every choice disabled and saying why', async () => {
  const onPicked = vi.fn();
  render(<TemplatePicker onPicked={onPicked} lockedReason={REASON} />);
  const open = screen.getByRole('button', { name: 'Öffnen' });
  const clone = screen.getByRole('button', { name: 'Klonen' });
  for (const b of [open, clone]) {
    expect(b).toBeDisabled();
    expect(b.getAttribute('title')).toBe(REASON);
  }
  await userEvent.click(open);
  expect(onPicked).not.toHaveBeenCalled();
  expect(mockApi.cloneWorkflow).not.toHaveBeenCalled();
});

test('the „Öffnen" list without a lock is unchanged', () => {
  render(<TemplatePicker onPicked={() => {}} />);
  expect(screen.getByRole('button', { name: 'Öffnen' })).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Klonen' })).toBeEnabled();
});

describe('the gallery', () => {
  beforeEach(() => {
    mockApi.listWorkflows.mockResolvedValue([{ id: 'tpl-1', name: 'Vorlage', is_template: true }]);
  });

  test('a locked gallery offers no clone', async () => {
    render(<GalleryTab onPicked={() => {}} lockedReason={REASON} />);
    const clone = await screen.findByRole('button', { name: /klon/i });
    expect(clone).toBeDisabled();
    expect(clone.getAttribute('title')).toBe(REASON);
  });

  test('a clone that finishes after a run started is created, not opened', async () => {
    let finish;
    mockApi.cloneWorkflow.mockImplementation(() => new Promise((r) => { finish = r; }));
    const onPicked = vi.fn();
    const { rerender } = render(<GalleryTab onPicked={onPicked} />);
    await userEvent.click(await screen.findByRole('button', { name: /klon/i }));
    rerender(<GalleryTab onPicked={onPicked} lockedReason={REASON} />);
    finish({ id: 'wf-klon', name: 'Vorlage (Kopie)' });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(REASON));
    expect(onPicked).not.toHaveBeenCalled();
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  test('without a lock the clone opens through openWorkflow', async () => {
    mockApi.cloneWorkflow.mockResolvedValue({ id: 'wf-klon', name: 'Vorlage (Kopie)' });
    const onPicked = vi.fn();
    render(<GalleryTab onPicked={onPicked} />);
    await userEvent.click(await screen.findByRole('button', { name: /klon/i }));
    await waitFor(() => expect(onPicked).toHaveBeenCalled());
    expect(mockDispatch).toHaveBeenCalledWith({ type: 'workshop/openWorkflow', payload: 'wf-klon' });
  });
});

test('the version history: the toggle is disabled and a restore is refused before the cloud', async () => {
  mockApi.listWorkflowVersions.mockResolvedValue([{ id: 'v1', created_at: '2026-09-27T10:00:00Z' }]);
  const onRestore = vi.fn();
  const { rerender } = render(<VersionHistoryDropdown workflowId="wf-1" onRestore={onRestore} />);
  await userEvent.click(screen.getByRole('button', { name: /Verlauf|Versionen/ }));
  const load = await screen.findByRole('button', { name: DE.VERSION_LOAD });
  rerender(<VersionHistoryDropdown workflowId="wf-1" onRestore={onRestore} lockedReason={REASON} />);
  expect(screen.getByRole('button', { name: /Verlauf|Versionen/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /Verlauf|Versionen/ }).getAttribute('title')).toBe(REASON);
  expect(load).toBeDisabled();
  await userEvent.click(load);
  expect(mockApi.restoreWorkflowVersion).not.toHaveBeenCalled();
  expect(onRestore).not.toHaveBeenCalled();
});
