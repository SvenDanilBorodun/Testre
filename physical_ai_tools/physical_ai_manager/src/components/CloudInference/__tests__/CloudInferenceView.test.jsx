// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The cloud view's stage while the robot loads the model (INFERENCE_LOADING):
// the waiting card in its dark tone over the cameras, with its own clock that
// starts when the loading starts, and nothing once the run is under way. The
// overlay around it is not a live region itself, so the clock is never read
// out every second.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import TaskPhase from '../../../constants/taskPhases';
import { cloudCopy } from '../cloudCopy';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => vi.fn(),
}));

vi.mock('../../ImageGrid', () => ({ __esModule: true, default: () => <div data-testid="image-grid" /> }));
vi.mock('../../HeartbeatStatus', () => ({ __esModule: true, default: () => <div data-testid="heartbeat" /> }));
vi.mock('../CloudInferencePanel', () => ({ __esModule: true, default: () => <div data-testid="cloud-panel" /> }));
vi.mock('../../../hooks/useCameraLiveness', () => ({
  __esModule: true,
  useCameraLiveness: () => ({ liveCount: 2 }),
  default: () => ({ liveCount: 2 }),
}));
vi.mock('../../../hooks/useCloudInferenceState', () => ({ __esModule: true, default: () => null }));

// eslint-disable-next-line import/first
import CloudInferenceView from '../CloudInferenceView';

function state(phase) {
  return {
    tasks: { taskStatus: { robotType: 'omx_f', phase } },
    ros: { imageTopicList: ['/gripper/image_raw', '/scene/image_raw'], rosbridgeUrl: 'ws://localhost/rosbridge' },
  };
}

const CLOUD = { offered: true, enabled: true, reason: null };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-12T10:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('CloudInferenceView while the model loads', () => {
  it('shows the dark waiting card with the loading texts and a clock from the start of the loading', () => {
    mockState = state(TaskPhase.INFERENCE_LOADING);
    render(<CloudInferenceView isActive cloud={CLOUD} />);
    const card = screen.getByTestId('cloud-wait');
    expect(card).toHaveAttribute('data-kind', 'model');
    expect(card).toHaveClass('ciw-dark');
    expect(screen.getByRole('status')).toHaveTextContent(cloudCopy.status.loading);
    expect(screen.getByRole('status')).toHaveTextContent(cloudCopy.status.loadingSub);
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:00');
    act(() => { vi.advanceTimersByTime(7000); });
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:07');
    expect(screen.queryByTestId('cloud-wait-retry')).toBeNull();
  });

  it('the overlay is not a live region of its own (the clock is never announced)', () => {
    mockState = state(TaskPhase.INFERENCE_LOADING);
    render(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.getAllByRole('status')).toHaveLength(1);
    // eslint-disable-next-line testing-library/no-node-access -- an ancestor walk is the point
    const liveAncestor = screen.getByTestId('cloud-wait-elapsed').closest('[aria-live], [role="status"]');
    expect(liveAncestor).toBeNull();
  });

  it('nothing over the cameras once the run is under way, or before it', () => {
    mockState = state(TaskPhase.INFERENCING);
    const { rerender } = render(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
    mockState = state(TaskPhase.READY);
    rerender(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
  });

  it('the clock is gone with the overlay', () => {
    mockState = state(TaskPhase.INFERENCE_LOADING);
    const { rerender } = render(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(vi.getTimerCount()).toBe(1);
    mockState = state(TaskPhase.INFERENCING);
    rerender(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });
});
