// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import CloudModelList, { cloudModelRows } from '../CloudModelList';
import { cloudCopy } from '../cloudCopy';

const JOBS = [
  { id: 1, status: 'succeeded', model_type: 'act', model_name: 'EduBotics-Solutions/alt', dataset_name: 'u/a', terminated_at: '2026-10-01T10:00:00Z' },
  { id: 2, status: 'succeeded', model_type: 'act', model_name: 'EduBotics-Solutions/neu', dataset_name: 'u/b', terminated_at: '2026-10-10T10:00:00Z', workgroup_id: 'wg' },
  { id: 3, status: 'succeeded', model_type: 'diffusion', model_name: 'EduBotics-Solutions/diff', dataset_name: 'u/c', terminated_at: '2026-10-05T10:00:00Z' },
  { id: 4, status: 'running', model_type: 'act', model_name: 'EduBotics-Solutions/laeuft', dataset_name: 'u/d' },
  { id: 5, status: 'failed', model_type: 'act', model_name: 'EduBotics-Solutions/kaputt', dataset_name: 'u/e' },
];

describe('CloudModelList', () => {
  it('lists succeeded trainings only, newest finished first', () => {
    expect(cloudModelRows(JOBS).map((j) => j.id)).toEqual([2, 3, 1]);
  });

  it('shows the name without the archive prefix, the dataset, the date and the group tag', () => {
    render(<CloudModelList jobs={JOBS} loading={false} selectedId={null} disabled={false} onSelect={() => {}} />);
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(3);
    expect(within(buttons[0]).getByText('neu')).toBeInTheDocument();
    expect(within(buttons[0]).getByText(cloudCopy.models.group)).toBeInTheDocument();
    expect(buttons[0]).toHaveTextContent('u/b');
    expect(buttons[0]).toHaveTextContent('10.10.2026');
    expect(screen.queryByText('EduBotics-Solutions/neu')).toBeNull();
  });

  it('a model of another type is disabled with its reason (only ACT runs in the cloud)', () => {
    const onSelect = vi.fn();
    render(<CloudModelList jobs={JOBS} loading={false} selectedId={null} disabled={false} onSelect={onSelect} />);
    const diff = screen.getByRole('button', { name: /^diff/ });
    expect(diff).toBeDisabled();
    expect(diff).toHaveTextContent(cloudCopy.models.notAct);
    fireEvent.click(diff);
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /^neu/ }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 2 }));
  });

  it('every row is disabled while a Start or a run is in progress (G-3)', () => {
    render(<CloudModelList jobs={JOBS} loading={false} selectedId={2} disabled onSelect={() => {}} />);
    for (const b of screen.getAllByRole('button')) expect(b).toBeDisabled();
    expect(screen.getByRole('button', { name: /^neu/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('says loading, then empty — never pre-selects anything', () => {
    const { rerender } = render(<CloudModelList jobs={[]} loading selectedId={null} disabled={false} onSelect={() => {}} />);
    expect(screen.getByText(cloudCopy.models.loading)).toBeInTheDocument();
    rerender(<CloudModelList jobs={[]} loading={false} selectedId={null} disabled={false} onSelect={() => {}} />);
    expect(screen.getByText(cloudCopy.models.empty)).toBeInTheDocument();
    rerender(<CloudModelList jobs={JOBS} loading={false} selectedId={null} disabled={false} onSelect={() => {}} />);
    for (const b of screen.getAllByRole('button')) expect(b).toHaveAttribute('aria-pressed', 'false');
  });
});
