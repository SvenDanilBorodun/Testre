/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The WIRE half of the preview finalization rule: `studioAssets` finalizes a
// simulator preview only on a /workflow/status carrying the preview's OWN
// `workflow_id`. That id must therefore reach `setWorkflowStatus` — without it
// every status looks foreign and a preview never ends.

import { renderHook, act } from '@testing-library/react';
import { useRosTopicSubscription } from '../useRosTopicSubscription';

const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useDispatch: () => mockDispatch,
  useSelector: (sel) => sel({ ros: { rosbridgeUrl: 'ws://localhost:9090' } }),
}));

vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.custom = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn };
});

vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: {
    getConnection: vi.fn(() =>
      Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })
    ),
  },
}));

vi.mock('../../store/store', () => ({
  __esModule: true,
  default: { dispatch: vi.fn() },
}));

const mockTopicSubscribe = vi.fn();
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.name = opts ? opts.name : undefined;
      this.subscribe = (cb) => { mockTopicSubscribe(this.name, cb); };
      this.unsubscribe = () => {};
    },
  },
}));

async function mountAndSubscribe() {
  const { result } = renderHook(() => useRosTopicSubscription());
  await act(async () => { await result.current.subscribeToWorkflowStatus(); });
  await act(async () => { await Promise.resolve(); });
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/workflow/status');
  return calls.length ? calls[calls.length - 1][1] : null;
}

const statusPayload = () => {
  const a = mockDispatch.mock.calls
    .map(([x]) => x)
    .find((x) => x && x.type && x.type.endsWith('/setWorkflowStatus'));
  return a ? a.payload : undefined;
};

beforeEach(() => {
  mockDispatch.mockClear();
  mockTopicSubscribe.mockClear();
});

describe('/workflow/status → setWorkflowStatus carries the workflow_id', () => {
  test('a preview id is forwarded verbatim, other fields unchanged', async () => {
    const cb = await mountAndSubscribe();
    act(() => cb({
      workflow_id: 'vorschau-aufnahme-3f9a1c0d',
      current_block_id: 'vorschau-1',
      phase: 'running',
      progress: 0.5,
      error: '',
      log_message: 'Bewegung läuft',
    }));
    expect(statusPayload()).toEqual({
      workflow_id: 'vorschau-aufnahme-3f9a1c0d',
      current_block_id: 'vorschau-1',
      phase: 'running',
      progress: 0.5,
      error: '',
      log_message: 'Bewegung läuft',
    });
  });

  test('a message without workflow_id forwards an empty string', async () => {
    const cb = await mountAndSubscribe();
    act(() => cb({ current_block_id: 'b1', phase: 'running', progress: 0, log_message: 'x' }));
    expect(statusPayload()).toEqual({
      workflow_id: '',
      current_block_id: 'b1',
      phase: 'running',
      progress: 0,
      error: '',
      log_message: 'x',
    });
  });
});
