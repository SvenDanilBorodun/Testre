// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The cloud panel while this tab holds the camera station claim (station spec
// v6.13, §9.2, D13): the server refuses an inference start meanwhile, so Start
// is disabled and says why. Same mocks as CloudInferencePanel.test.jsx, plus
// the station controller at its module boundary.

import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import TaskPhase from '../../../constants/taskPhases';
import { cloudCopy } from '../cloudCopy';
import { STATION_DE } from '../../../features/station/stationCopy';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => vi.fn(),
}));

const mockBridge = { current: { available: false, followerOnly: false, leaderOn: false, probed: true } };
vi.mock('../../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => mockBridge.current,
}));

const mockJobs = { current: [] };
vi.mock('../../../hooks/useSupabaseTrainings', () => ({
  __esModule: true,
  default: () => ({ jobs: mockJobs.current, loading: false, refetch: vi.fn(), isRealtime: true }),
}));

vi.mock('../../Workshop/LeaderToggle', () => ({
  __esModule: true,
  default: ({ isActive }) => <div data-testid="leader-toggle" data-active={String(isActive)} />,
}));

const api = vi.hoisted(() => ({
  reserveInferenceSession: vi.fn(),
  armInferenceSession: vi.fn(),
  heartbeatInferenceSession: vi.fn(),
  endInferenceSession: vi.fn(),
  endInferenceSessionKeepalive: vi.fn(),
  getInferenceUsage: vi.fn(),
}));
vi.mock('../../../services/cloudInferenceApi', () => ({ __esModule: true, ...api }));

const mockStation = vi.hoisted(() => {
  const listeners = new Set();
  const box = { snap: { held: false, phase: 'idle', workflowActive: false } };
  return {
    box,
    api: {
      subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
      getSnapshot: () => box.snap,
    },
  };
});
vi.mock('../../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));

const robot = vi.hoisted(() => ({
  startCloudInference: vi.fn(),
  stopCloudInference: vi.fn(),
  subscribeCloudState: vi.fn(),
}));
vi.mock('../../../features/cloudInference/robotCalls', () => ({ __esModule: true, ...robot }));

// eslint-disable-next-line import/first
import CloudInferencePanel from '../CloudInferencePanel';
// eslint-disable-next-line import/first
import * as controller from '../../../features/cloudInference/controller';

const SID = '0a1b2c3d-0000-4000-8000-00000000abcd';
const JOBS = [
  { id: 7, status: 'succeeded', model_type: 'act', model_name: 'EduBotics-Solutions/neu', dataset_name: 'u/b', terminated_at: '2026-10-10T10:00:00Z' },
];
const SUMMARY = { type: 'act', expected_cameras: ['gripper', 'scene'], trained_fps: 30, model_repo: 'EduBotics-Solutions/neu' };

function state({ running = false, phase = TaskPhase.READY, topics = ['/gripper/image_raw', '/scene/image_raw'] } = {}) {
  return {
    auth: { session: { access_token: 'jwt-student' } },
    tasks: { taskStatus: { running, phase }, taskInfo: { taskName: 'x', taskInstruction: [], tags: [] } },
    ros: { imageTopicList: topics },
  };
}

const CLOUD = { offered: true, enabled: true, reason: null };

async function flush() {
  await act(async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve(); });
}

beforeEach(() => {
  vi.useFakeTimers();
  controller._resetForTests();
  Object.values(api).forEach((f) => f.mockReset());
  Object.values(robot).forEach((f) => f.mockReset());
  api.reserveInferenceSession.mockResolvedValue({ session_id: SID, summary: SUMMARY, session_s: 1800 });
  api.getInferenceUsage.mockResolvedValue({ daily_cap_s: 7200, session_cap_s: 1800, used_today_s: 600, remaining_today_s: 6600, live_session_id: null });
  api.heartbeatInferenceSession.mockResolvedValue({ status: 'reserved', end_reason: null, end_reason_de: '', max_end_in_s: null });
  robot.stopCloudInference.mockResolvedValue(true);
  mockBridge.current = { available: false, followerOnly: false, leaderOn: false, probed: true };
  mockJobs.current = JOBS;
  mockState = state();
  mockStation.box.snap = { held: false, phase: 'idle', workflowActive: false };
});

afterEach(() => {
  controller._resetForTests();
  vi.useRealTimers();
});

async function chooseModel() {
  fireEvent.click(screen.getByRole('button', { name: /^neu/ }));
  await act(async () => { await vi.advanceTimersByTimeAsync(controller.SELECT_DEBOUNCE_MS); });
  await flush();
}

const startButton = () => screen.getByRole('button', { name: cloudCopy.panel.start });

describe('CloudInferencePanel and the camera station', () => {
  it('a held station claim disables Start and says why', async () => {
    mockStation.box.snap = { held: true, phase: 'held', workflowActive: false };
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    await chooseModel();
    expect(startButton()).toBeDisabled();
    expect(screen.getByTestId('cloud-start-hint')).toHaveTextContent(STATION_DE.busyElsewhere);
  });

  it('entering and leaving count as held, too', async () => {
    mockStation.box.snap = { held: false, phase: 'entering', workflowActive: false };
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    await chooseModel();
    expect(startButton()).toBeDisabled();
  });

  it('without a claim Start is enabled as before', async () => {
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    await chooseModel();
    expect(startButton()).not.toBeDisabled();
  });
});
