// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The cloud panel over the REAL controller (the cloud API and the robot calls
// mocked at their module boundary). Pinned: the Leader-Schalter is on the panel
// and Start is disabled while the bridge reports the leader on (Q-A, D23); the
// preparing line while a reservation runs; a camera the model needs and the
// robot lacks blocks Start; a task running on the robot blocks Start and the
// model rows; the after-reload „Stoppen" (R-25); usage unknown is „–", never 0;
// the view has no transport chip (HTTPS only, O7').

import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import TaskPhase from '../../../constants/taskPhases';
import { cloudCopy } from '../cloudCopy';

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

const robot = vi.hoisted(() => ({
  startCloudInference: vi.fn(),
  stopCloudInference: vi.fn(),
  subscribeCloudState: vi.fn(),
}));
vi.mock('../../../features/cloudInference/robotCalls', () => ({ __esModule: true, ...robot }));

// eslint-disable-next-line import/first
import CloudInferencePanel, { missingCameras } from '../CloudInferencePanel';
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

describe('CloudInferencePanel', () => {
  it('renders the Leader-Schalter, the privacy line and the usage', async () => {
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    expect(screen.getByTestId('leader-toggle')).toHaveAttribute('data-active', 'true');
    expect(screen.getByText(cloudCopy.panel.privacy)).toBeInTheDocument();
    expect(screen.getByTestId('cloud-usage')).toHaveTextContent('Heute noch 110 Min. Cloud-Zeit (höchstens 30 Min. pro Lauf)');
  });

  it('usage that did not answer is „–", never 0', async () => {
    api.getInferenceUsage.mockRejectedValue(new Error('down'));
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    expect(screen.getByTestId('cloud-usage')).toHaveTextContent(cloudCopy.usage.unknown);
  });

  it('no model chosen: Start disabled with the hint, and nothing is reserved', async () => {
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    expect(startButton()).toBeDisabled();
    expect(screen.getByTestId('cloud-start-hint')).toHaveTextContent(cloudCopy.panel.chooseModel);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.reserveInferenceSession).not.toHaveBeenCalled();
  });

  it('shows the preparing line while reserving, then the summary, and enables Start', async () => {
    let resolve;
    api.reserveInferenceSession.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(screen.getByText(cloudCopy.panel.preparing)).toBeInTheDocument();
    expect(screen.getByText(cloudCopy.panel.preparingSub)).toBeInTheDocument();
    await act(async () => { resolve({ session_id: SID, summary: SUMMARY, session_s: 1800 }); });
    await flush();
    expect(screen.queryByText(cloudCopy.panel.preparing)).toBeNull();
    expect(screen.getByTestId('cloud-summary')).toHaveTextContent('Kameras im Modell: gripper, scene');
    expect(screen.getByTestId('cloud-summary')).toHaveTextContent('Trainiert mit 30 Bildern pro Sekunde');
    expect(startButton()).toBeEnabled();
  });

  it('Start is disabled while the bridge reports the leader on, with the hint', async () => {
    mockBridge.current = { available: true, followerOnly: false, leaderOn: true, probed: true };
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(screen.getByText(cloudCopy.leader.hint)).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
  });

  it('a camera the model needs and the robot lacks blocks Start', async () => {
    mockState = state({ topics: ['/gripper/image_raw'] });
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(screen.getByRole('alert')).toHaveTextContent('Dieses Modell erwartet die Kamera(s) scene – am Roboter fehlen sie.');
    expect(startButton()).toBeDisabled();
    expect(missingCameras(SUMMARY, [])).toEqual([]); // unknown robot cameras never block
  });

  it('a failed reservation keeps Start enabled and shows the API\'s sentence (R-16)', async () => {
    const err = new Error('x');
    err.status = 503;
    err.detail = 'Die Cloud ist gerade voll ausgelastet. Bitte in ein paar Minuten erneut versuchen.';
    api.reserveInferenceSession.mockRejectedValue(err);
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(screen.getByTestId('cloud-message')).toHaveTextContent(err.detail);
    expect(startButton()).toBeEnabled();
  });

  it('a model refusal (409) disables Start for that row', async () => {
    const err = new Error('x');
    err.status = 409;
    err.detail = 'Dieses Training ist noch nicht fertig.';
    api.reserveInferenceSession.mockRejectedValue(err);
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(startButton()).toBeDisabled();
  });

  it('a task running on the robot blocks Start and every model row (G-3)', async () => {
    mockState = state({ running: true, phase: TaskPhase.RECORDING });
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    expect(screen.getByRole('button', { name: /^neu/ })).toBeDisabled();
    expect(startButton()).toBeDisabled();
    expect(screen.getByTestId('cloud-start-hint')).toHaveTextContent(cloudCopy.panel.runningElsewhere);
  });

  it('after a reload (robot inferring, no session here) the button reads „Stoppen" (R-25)', async () => {
    api.getInferenceUsage.mockResolvedValue({ daily_cap_s: 7200, session_cap_s: 1800, used_today_s: 0, remaining_today_s: 7200, live_session_id: SID });
    api.endInferenceSession.mockResolvedValue({ status: 'ended' });
    mockState = state({ running: true, phase: TaskPhase.INFERENCING });
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await flush();
    const stop = screen.getByRole('button', { name: cloudCopy.panel.stop });
    fireEvent.click(stop);
    await flush();
    expect(robot.stopCloudInference).toHaveBeenCalledTimes(1);
    expect(api.endInferenceSession).toHaveBeenCalledWith('jwt-student', SID);
  });

  it('a disabled cloud keeps Start disabled with its reason', async () => {
    render(<CloudInferencePanel isActive cloud={{ offered: true, enabled: false, reason: 'old_image' }} cloudState={null} />);
    await chooseModel();
    expect(startButton()).toBeDisabled();
    expect(screen.getByTestId('cloud-start-hint')).toHaveTextContent(cloudCopy.disabled.old_image);
  });

  it('unmounting lets a reservation go (release)', async () => {
    const { unmount } = render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(controller.getSnapshot().phase).toBe('reserved');
    unmount();
    expect(controller.getSnapshot().phase).toBe('idle');
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-student', SID);
  });
});

const GPU_DE = 'Die Cloud-GPU konnte gerade nicht gestartet werden. Bitte versuche es gleich noch einmal.';
const TWO_JOBS = [
  ...JOBS,
  { id: 8, status: 'succeeded', model_type: 'act', model_name: 'EduBotics-Solutions/alt', dataset_name: 'u/a', terminated_at: '2026-10-01T10:00:00Z' },
];

function gpuNotStarted() {
  const err = new Error(GPU_DE);
  err.status = 503;
  err.detail = GPU_DE;
  err.code = 'gpu_not_started';
  return err;
}

describe('CloudInferencePanel while the GPU starts (owner, round 2)', () => {
  it('says that a first start can take some minutes', () => {
    expect(cloudCopy.panel.preparingSub).toBe('Beim ersten Start kann das einige Minuten dauern.');
  });

  it('locks every other model while reserving, and a click on the chosen one sends nothing', async () => {
    mockJobs.current = TWO_JOBS;
    let resolve;
    api.reserveInferenceSession.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    const other = screen.getByRole('button', { name: /^alt/ });
    expect(other).toBeDisabled();
    expect(other).toHaveAttribute('title', cloudCopy.models.gpuStarting);
    expect(screen.getByTestId('cloud-models-locked')).toHaveTextContent(cloudCopy.models.gpuStarting);
    fireEvent.click(screen.getByRole('button', { name: /^neu/ }));
    fireEvent.click(other);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().phase).toBe('reserving');
    expect(screen.getByText(cloudCopy.panel.preparing)).toBeInTheDocument();
    await act(async () => { resolve({ session_id: SID, summary: SUMMARY, session_s: 1800 }); });
    await flush();
    expect(screen.getByRole('button', { name: /^alt/ })).toBeEnabled();
    expect(screen.queryByTestId('cloud-models-locked')).toBeNull();
    expect(startButton()).toBeEnabled();
  });

  it('asks again by itself on the API\'s code, keeps the list locked meanwhile, then shows today\'s error and unlocks', async () => {
    mockJobs.current = TWO_JOBS;
    api.reserveInferenceSession.mockRejectedValue(gpuNotStarted());
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(screen.getByText(cloudCopy.panel.preparingSub)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^alt/ })).toBeDisabled();
    expect(screen.queryByTestId('cloud-message')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(controller.RESERVE_RETRY_DELAY_MS); });
    await flush();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: /^alt/ })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(controller.RESERVE_RETRY_DELAY_MS); });
    await flush();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId('cloud-message')).toHaveTextContent(GPU_DE);
    expect(screen.queryByText(cloudCopy.panel.preparing)).toBeNull();
    expect(screen.getByRole('button', { name: /^alt/ })).toBeEnabled();
    expect(startButton()).toBeEnabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(3);
  });

  it('the waiting card: one clock from the first attempt on, the retry named', async () => {
    api.reserveInferenceSession
      .mockRejectedValueOnce(gpuNotStarted())
      .mockReturnValueOnce(new Promise(() => {}));
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    fireEvent.click(screen.getByRole('button', { name: /^neu/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(controller.SELECT_DEBOUNCE_MS); });
    await flush();
    const card = screen.getByTestId('cloud-wait');
    expect(card).toHaveAttribute('data-kind', 'gpu');
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:00');
    // the first attempt already answered „no GPU": the page says it asks again
    expect(screen.getByTestId('cloud-wait-retry')).toHaveTextContent('Versuch 2 von 3');
    await act(async () => { await vi.advanceTimersByTimeAsync(controller.RESERVE_RETRY_DELAY_MS); });
    await flush();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(2);
    // the clock kept counting through the retry (never reset)
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:02');
    await act(async () => { await vi.advanceTimersByTimeAsync(40 * 1000); });
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:42');
    expect(screen.getByTestId('cloud-wait')).toBe(card);
  });

  it('the first attempt shows no retry line, and the card goes once the GPU answers', async () => {
    let resolve;
    api.reserveInferenceSession.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(screen.getByTestId('cloud-wait')).toBeInTheDocument();
    expect(screen.queryByTestId('cloud-wait-retry')).toBeNull();
    await act(async () => { resolve({ session_id: SID, summary: SUMMARY, session_s: 1800 }); });
    await flush();
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
  });

  it('leaving the panel while a retry waits: no reserve after the unmount', async () => {
    api.reserveInferenceSession.mockRejectedValue(gpuNotStarted());
    const { unmount } = render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * controller.RESERVE_RETRY_DELAY_MS); });
    await flush();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().phase).toBe('idle');
  });

  it('a refusal without the code is shown at once and unlocks (no retry)', async () => {
    mockJobs.current = TWO_JOBS;
    const err = new Error('x');
    err.status = 503;
    err.detail = 'Die Cloud ist gerade voll ausgelastet. Bitte in ein paar Minuten erneut versuchen.';
    api.reserveInferenceSession.mockRejectedValue(err);
    render(<CloudInferencePanel isActive cloud={CLOUD} cloudState={null} />);
    await chooseModel();
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * controller.RESERVE_RETRY_DELAY_MS); });
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('cloud-message')).toHaveTextContent(err.detail);
    expect(screen.getByRole('button', { name: /^alt/ })).toBeEnabled();
  });
});

describe('the cloud view has no transport chip (O7\')', () => {
  it('names no WebSocket / transport anywhere in the cloud components', async () => {
    const fs = await vi.importActual('fs');
    const path = await vi.importActual('path');
    const dir = path.join(__dirname, '..');
    for (const name of fs.readdirSync(dir).filter((n) => /\.(js|jsx)$/.test(n))) {
      const src = fs.readFileSync(path.join(dir, name), 'utf8');
      expect({ name, hit: /websocket|ws_url|transport/i.test(src) }).toEqual({ name, hit: false });
    }
  });
});
