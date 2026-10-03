// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The reconcile loop that keeps the robot's token slot equal to the signed-in
// student's account. Backed by a REAL store (auth, tasks, ros, jetson, ui,
// hfToken) and the real thunks; only the cloud calls and the rosbridge channel
// are mocked. Timers are faked wherever a delay is the thing under test.

import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import authReducer, { setProfile, setSession } from '../../features/auth/authSlice';
import tasksReducer, { setHeartbeatStatus } from '../../features/tasks/taskSlice';
import rosReducer, { setRosbridgeUrl } from '../../features/ros/rosSlice';
import uiReducer, { setHfUserList } from '../../features/ui/uiSlice';
import jetsonReducer, { setJetsonStatus } from '../../store/jetsonSlice';
import hfReducer, {
  accountLoaded,
  robotStateReceived,
  syncPushed,
} from '../../features/hfToken/hfTokenSlice';
import * as api from '../../services/hfTokenApi';
import { setRobotToken, subscribeTokenState } from '../../features/hfToken/robotChannel';
import {
  SYNC_WATCHDOG_MS,
  WAIT_RECHECK_MS,
  WRITE_SETTLE_MS,
} from '../../features/hfToken/syncDecision';
import useHfTokenSync, { LEGACY_GRACE_MS } from '../useHfTokenSync';

vi.mock('../../services/hfTokenApi', () => ({
  __esModule: true,
  getHfToken: vi.fn(),
  putHfToken: vi.fn(),
  deleteHfToken: vi.fn(),
  revealHfToken: vi.fn(),
  verifyHfToken: vi.fn(),
}));

vi.mock('../../features/hfToken/robotChannel', async () => {
  const actual = await vi.importActual('../../features/hfToken/robotChannel');
  return {
    ...actual,
    subscribeTokenState: vi.fn(),
    setRobotToken: vi.fn(),
    clearRobotHfToken: vi.fn(),
  };
});

let mockCloudOnly = false;
vi.mock('../../utils/cloudMode', () => ({
  __esModule: true,
  isCloudOnlyMode: () => mockCloudOnly,
}));

// Low-entropy fixtures on purpose (the repository's secret scan covers history).
const TOKEN = `hf_${'a'.repeat(34)}`;
const FP = 'c1770a7966b0771e';
const OTHER = 'bbbbbbbbbbbbbbbb';
const LOCAL_URL = 'ws://localhost/rosbridge';

const storedBody = (fp = FP) => ({
  stored: true, usable: true, hf_username: 'anna', hint: 'hf_…aaaa', fp, role: 'write', validated_at: null,
});

// What the mocked subscription hands out, so a test can play the robot.
let subscriptions;
const robotSays = (o = {}) => ({ v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false, ...o });
const emit = (state) => act(async () => { subscriptions.forEach((s) => s.cb(state)); });

function makeStore({ profile = true, role = 'student', heartbeat = 'connected', url = LOCAL_URL, userId = 'u1' } = {}) {
  const store = configureStore({
    reducer: {
      auth: authReducer, tasks: tasksReducer, ros: rosReducer, jetson: jetsonReducer, ui: uiReducer, hfToken: hfReducer,
    },
  });
  store.dispatch(setSession({ access_token: 'jwt-1', user: { id: userId } }));
  if (profile) {
    store.dispatch(setProfile({ role, username: 'anna', full_name: 'Anna', classroom_id: 'c1' }));
  }
  store.dispatch(setHeartbeatStatus(heartbeat));
  store.dispatch(setRosbridgeUrl(url));
  return store;
}

function mount(store, opts = {}) {
  const onRobotTokenReady = vi.fn();
  const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
  const view = renderHook(() => useHfTokenSync({ onRobotTokenReady, ...opts }), { wrapper });
  return { ...view, onRobotTokenReady };
}

// Let queued microtasks and zero-delay timers run.
const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });
const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const hf = (store) => store.getState().hfToken;

beforeEach(() => {
  vi.clearAllMocks();
  mockCloudOnly = false;
  subscriptions = [];
  api.getHfToken.mockResolvedValue(storedBody());
  api.revealHfToken.mockResolvedValue({ token: TOKEN, fp: FP });
  setRobotToken.mockResolvedValue({ success: true, message: '' });
  subscribeTokenState.mockImplementation(async (url, cb) => {
    const stop = vi.fn();
    subscriptions.push({ url, cb, stop });
    return stop;
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useHfTokenSync — when it is allowed to act', () => {
  it('is inert until the profile has loaded: no cloud call, no subscription', async () => {
    const store = makeStore({ profile: false });
    const { result } = mount(store);
    await settle();
    expect(result.current).toEqual({ accountEnabled: false, robotEnabled: false });
    expect(api.getHfToken).not.toHaveBeenCalled();
    expect(subscribeTokenState).not.toHaveBeenCalled();
  });

  it('is inert for a teacher account', async () => {
    const store = makeStore({ role: 'teacher' });
    mount(store);
    await settle();
    expect(api.getHfToken).not.toHaveBeenCalled();
    expect(subscribeTokenState).not.toHaveBeenCalled();
  });

  it('is inert without a session (the offline escape)', async () => {
    const store = makeStore();
    store.dispatch(setSession(null));
    mount(store);
    await settle();
    expect(api.getHfToken).not.toHaveBeenCalled();
    expect(subscribeTokenState).not.toHaveBeenCalled();
  });

  it('is off entirely in cloud-only mode, the account half included', async () => {
    mockCloudOnly = true;
    const store = makeStore();
    const { result } = mount(store);
    await settle();
    expect(result.current).toEqual({ accountEnabled: false, robotEnabled: false });
    expect(api.getHfToken).not.toHaveBeenCalled();
    expect(subscribeTokenState).not.toHaveBeenCalled();
  });

  it('reads the account for a loaded student, but leaves the robot alone while it is not connected', async () => {
    const store = makeStore({ heartbeat: 'disconnected' });
    const { result } = mount(store);
    await settle();
    expect(result.current).toEqual({ accountEnabled: true, robotEnabled: false });
    expect(api.getHfToken).toHaveBeenCalledWith('jwt-1');
    expect(hf(store).account.status).toBe('stored');
    expect(subscribeTokenState).not.toHaveBeenCalled();
  });

  it('leaves the robot alone while a classroom Jetson is claimed', async () => {
    const store = makeStore();
    store.dispatch(setJetsonStatus('connected'));
    const { result } = mount(store);
    await settle();
    expect(result.current.robotEnabled).toBe(false);
    expect(subscribeTokenState).not.toHaveBeenCalled();
    expect(setRobotToken).not.toHaveBeenCalled();
  });

  it('leaves the robot alone when the rosbridge is not the local one (the Jetson proxy)', async () => {
    const store = makeStore({ url: 'ws://10.0.0.5:9091' });
    const { result } = mount(store);
    await settle();
    expect(result.current.robotEnabled).toBe(false);
    expect(subscribeTokenState).not.toHaveBeenCalled();
  });

  it('subscribes when connected to the local rosbridge, and unsubscribes and forgets on unmount', async () => {
    const store = makeStore();
    const view = mount(store);
    await settle();
    expect(subscribeTokenState).toHaveBeenCalledWith(LOCAL_URL, expect.any(Function));
    await emit(robotSays({ present: true, fp: OTHER }));
    expect(hf(store).robot).toMatchObject({ known: true, present: true, fp: OTHER });
    view.unmount();
    expect(subscriptions[0].stop).toHaveBeenCalledTimes(1);
    expect(hf(store).robot.known).toBe(false);
  });

  it('re-subscribes after the link came back', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await act(async () => { store.dispatch(setHeartbeatStatus('disconnected')); });
    expect(subscriptions[0].stop).toHaveBeenCalledTimes(1);
    await act(async () => { store.dispatch(setHeartbeatStatus('connected')); });
    await settle();
    expect(subscriptions).toHaveLength(2);
  });
});

describe('useHfTokenSync — the account load', () => {
  it('reloads when the window regains focus', async () => {
    const store = makeStore({ heartbeat: 'disconnected' });
    mount(store);
    await settle();
    expect(api.getHfToken).toHaveBeenCalledTimes(1);
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    await settle();
    expect(api.getHfToken).toHaveBeenCalledTimes(2);
  });

  it('retries a failed load on the 2 / 5 / 15 s ladder instead of waiting for the next focus', async () => {
    vi.useFakeTimers();
    api.getHfToken.mockRejectedValue(Object.assign(new Error('x'), { status: 500, detail: 'Bad Gateway' }));
    const store = makeStore({ heartbeat: 'disconnected' });
    mount(store);
    await settle();
    expect(api.getHfToken).toHaveBeenCalledTimes(1);
    expect(hf(store).account.status).toBe('error');

    // Retries fall due at 2 s and (5 s after that) 7 s after the first failure.
    await advance(1999);
    expect(api.getHfToken).toHaveBeenCalledTimes(1);
    await advance(2);
    expect(api.getHfToken).toHaveBeenCalledTimes(2);

    await advance(4997);
    expect(api.getHfToken).toHaveBeenCalledTimes(2);
    await advance(2);
    expect(api.getHfToken).toHaveBeenCalledTimes(3);

    // and it stops retrying the moment a load succeeds
    api.getHfToken.mockResolvedValue(storedBody());
    await advance(15_000 + 30);
    expect(api.getHfToken).toHaveBeenCalledTimes(4);
    expect(hf(store).account.status).toBe('stored');
    await advance(120_000);
    expect(api.getHfToken).toHaveBeenCalledTimes(4);
  });

  it('does not retry an answer that is a server decision (404, 503)', async () => {
    vi.useFakeTimers();
    api.getHfToken.mockRejectedValue(Object.assign(new Error('x'), { status: 404, detail: 'Not Found' }));
    const store = makeStore({ heartbeat: 'disconnected' });
    mount(store);
    await settle();
    expect(hf(store).account.status).toBe('unsupported');
    await advance(120_000);
    expect(api.getHfToken).toHaveBeenCalledTimes(1);
  });
});

describe('useHfTokenSync — M11: a different student in the same tab', () => {
  it('resets the account and the bookkeeping, bumps the epoch and reads the new account', async () => {
    const store = makeStore({ heartbeat: 'disconnected', userId: 'student-a' });
    mount(store);
    await settle();
    expect(hf(store).account.fp).toBe(FP);
    store.dispatch(syncPushed({ fp: FP }));
    expect(hf(store).sync.lastOwnFp).toBe(FP);
    const epochBefore = hf(store).epoch;

    api.getHfToken.mockResolvedValue({ stored: false });
    await act(async () => {
      store.dispatch(setSession({ access_token: 'jwt-2', user: { id: 'student-b' } }));
    });
    await settle();

    expect(hf(store).epoch).toBe(epochBefore + 1);
    expect(hf(store).sync.lastOwnFp).toBeNull();
    expect(hf(store).account.status).toBe('none');
    expect(api.getHfToken).toHaveBeenLastCalledWith('jwt-2');
  });

  it('empties the Benutzer-ID list of the previous student too', async () => {
    const store = makeStore({ heartbeat: 'disconnected', userId: 'student-a' });
    mount(store);
    await settle();
    await act(async () => { store.dispatch(setHfUserList(['anna'])); });
    expect(store.getState().ui.hfUserList).toEqual(['anna']);
    api.getHfToken.mockResolvedValue({ stored: false });
    await act(async () => {
      store.dispatch(setSession({ access_token: 'jwt-2', user: { id: 'student-b' } }));
    });
    await settle();
    expect(store.getState().ui.hfUserList).toEqual([]);
  });

  it('does not reset on the first login or on a token refresh of the same student', async () => {
    const store = makeStore({ heartbeat: 'disconnected', userId: 'student-a' });
    mount(store);
    await settle();
    const epochBefore = hf(store).epoch;
    await act(async () => {
      store.dispatch(setSession({ access_token: 'jwt-refreshed', user: { id: 'student-a' } }));
    });
    await settle();
    expect(hf(store).epoch).toBe(epochBefore);
    expect(hf(store).account.status).toBe('stored');
  });

  it('drops a token that was being revealed for the previous student', async () => {
    const store = makeStore({ userId: 'student-a' });
    mount(store);
    await settle();
    let release;
    api.revealHfToken.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    await emit(robotSays());               // robot empty, account stored -> push starts
    expect(hf(store).sync.phase).toBe('pushing');
    // student B has no token stored, so nothing may be pushed for B either
    api.getHfToken.mockResolvedValue({ stored: false });
    await act(async () => {
      store.dispatch(setSession({ access_token: 'jwt-2', user: { id: 'student-b' } }));
    });
    await act(async () => { release({ token: TOKEN, fp: FP }); });
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
  });
});

describe('useHfTokenSync — the reconcile', () => {
  it('pushes a stored token into an empty slot and calls onRobotTokenReady once the robot shows it', async () => {
    const store = makeStore();
    const { onRobotTokenReady } = mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(api.revealHfToken).toHaveBeenCalledWith('jwt-1');
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    expect(setRobotToken).toHaveBeenCalledWith(TOKEN);
    expect(onRobotTokenReady).not.toHaveBeenCalled();

    await emit(robotSays({ seq: 2, present: true, fp: FP }));
    expect(onRobotTokenReady).toHaveBeenCalledTimes(1);
    expect(hf(store).sync.lastOwnFp).toBe(FP);
  });

  it('does not push twice while the robot\'s state has not caught up with the first write', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    // the robot still says "empty" a second later (its message is on its way)
    await advance(1000);
    await emit(robotSays({ seq: 2 }));
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    // the robot caught up: the pause ends, no further write is ever needed
    await emit(robotSays({ seq: 3, present: true, fp: FP }));
    await advance(WRITE_SETTLE_MS * 2);
    expect(setRobotToken).toHaveBeenCalledTimes(1);
  });

  it('writes again after the pause if the slot is STILL not the student\'s', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    await advance(WRITE_SETTLE_MS + 100);
    expect(setRobotToken).toHaveBeenCalledTimes(2);
  });

  it('while the robot is busy it looks again by itself, without asking the cloud again', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ busy: true }));
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(api.getHfToken).toHaveBeenCalledTimes(1);
    // The robot\'s own state is what ends the wait; the periodic look is only
    // the belt. It must not turn into a cloud poll every few seconds.
    await advance(WAIT_RECHECK_MS * 4);
    await emit(robotSays({ seq: 2, busy: true }));
    await advance(WAIT_RECHECK_MS * 4);
    expect(api.getHfToken).toHaveBeenCalledTimes(1);
    expect(api.revealHfToken).not.toHaveBeenCalled();
  });

  it('does not push to a robot that does not take a personal token (the Jetson image)', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ accepts: false }));
    await settle();
    expect(api.revealHfToken).not.toHaveBeenCalled();
    expect(setRobotToken).not.toHaveBeenCalled();
  });

  it('replaces the previous student\'s token in the slot (a hand-over)', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: OTHER }));
    await settle();
    expect(setRobotToken).toHaveBeenCalledWith(TOKEN);
  });

  it('clears a token the account does not own (the student has none stored)', async () => {
    api.getHfToken.mockResolvedValue({ stored: false });
    const store = makeStore();
    mount(store);
    await settle();
    expect(hf(store).account.status).toBe('none');
    await emit(robotSays({ present: true, fp: OTHER }));
    await settle();
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    expect(setRobotToken).toHaveBeenCalledWith('');
    expect(api.revealHfToken).not.toHaveBeenCalled();
  });

  it('leaves an empty slot alone when the account has no token either', async () => {
    api.getHfToken.mockResolvedValue({ stored: false });
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
  });

  it('waits while the robot is busy and acts when it is free again', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ busy: true }));
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
    await emit(robotSays({ seq: 2, busy: false }));
    await settle();
    expect(setRobotToken).toHaveBeenCalledTimes(1);
  });

  it('does not fight another student for the slot: it was MINE, now it is theirs', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: FP }));      // in sync: lastOwnFp = FP
    expect(hf(store).sync.lastOwnFp).toBe(FP);
    await emit(robotSays({ seq: 2, present: true, fp: OTHER }));
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(api.revealHfToken).not.toHaveBeenCalled();
  });

  it('retries a refused write on the backoff, not at once', async () => {
    vi.useFakeTimers();
    setRobotToken.mockResolvedValue({ success: false, message: 'Dein Hugging-Face-Token …' });
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    expect(hf(store).sync).toMatchObject({ failures: 1, phase: 'idle' });
    await advance(1900);
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    await advance(200);
    expect(setRobotToken).toHaveBeenCalledTimes(2);
    expect(hf(store).sync.failures).toBe(2);
  });

  it('does not write while the circuit breaker is open', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await act(async () => {
      const longAgo = Date.now() - 30_000;
      store.dispatch(syncPushed({ fp: FP }, longAgo));
      store.dispatch(syncPushed({ fp: FP }, longAgo + 1));
      store.dispatch(syncPushed({ fp: FP }, longAgo + 2));
    });
    expect(hf(store).sync.breakerOpen).toBe(true);
    await emit(robotSays({ present: true, fp: OTHER }));
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
  });

  it('writes nothing when the robot link is lost', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await act(async () => { store.dispatch(setHeartbeatStatus('disconnected')); });
    await settle();
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(hf(store).robot.known).toBe(false);
  });
});

describe('useHfTokenSync — the watchdog (audit M10)', () => {
  it('declares a push that never came back failed after 15 s and frees the loop', async () => {
    vi.useFakeTimers();
    api.revealHfToken.mockReturnValue(new Promise(() => {}));
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(hf(store).sync.phase).toBe('pushing');
    await advance(SYNC_WATCHDOG_MS - 100);
    expect(hf(store).sync.phase).toBe('pushing');
    await advance(200);
    expect(hf(store).sync.phase).toBe('idle');
    expect(hf(store).sync.failures).toBe(1);
  });

  it('does nothing for a call that returned in time', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    await emit(robotSays({ seq: 2, present: true, fp: FP }));
    await advance(SYNC_WATCHDOG_MS * 2);
    expect(hf(store).sync.failures).toBe(0);
  });
});

describe('useHfTokenSync — an old image is told from a slow one (audit S1)', () => {
  it('declares the robot legacy after 8 s of silence on a live subscription', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    expect(hf(store).robot.legacy).toBe(false);
    await advance(LEGACY_GRACE_MS - 100);
    expect(hf(store).robot.legacy).toBe(false);
    await advance(200);
    expect(hf(store).robot.legacy).toBe(true);
    expect(hf(store).robot.known).toBe(false);
  });

  it('never declares it when the first state message arrives in time', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await advance(3000);
    // the robot keeps talking, once a second, for twice the grace period
    for (let i = 0; i < 16; i += 1) {
      await emit(robotSays({ seq: i + 1 }));
      await advance(1000);
    }
    expect(hf(store).robot.legacy).toBe(false);
    expect(hf(store).robot.known).toBe(true);
  });

  it('clears legacy again the moment a state message does arrive', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await advance(LEGACY_GRACE_MS + 100);
    expect(hf(store).robot.legacy).toBe(true);
    await emit(robotSays({ present: true, fp: OTHER }));
    expect(hf(store).robot.legacy).toBe(false);
  });

  it('does not count while there is no subscription (the offline escape, a Jetson)', async () => {
    vi.useFakeTimers();
    const store = makeStore({ heartbeat: 'disconnected' });
    mount(store);
    await settle();
    await advance(LEGACY_GRACE_MS * 3);
    expect(hf(store).robot.legacy).toBe(false);
  });

  it('starts counting again for a new subscription and forgets the verdict of the old one', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await advance(LEGACY_GRACE_MS + 100);
    expect(hf(store).robot.legacy).toBe(true);
    await act(async () => { store.dispatch(setHeartbeatStatus('disconnected')); });
    expect(hf(store).robot.legacy).toBe(false);
    await act(async () => { store.dispatch(setHeartbeatStatus('connected')); });
    await settle();
    await advance(LEGACY_GRACE_MS - 100);
    expect(hf(store).robot.legacy).toBe(false);
    await advance(200);
    expect(hf(store).robot.legacy).toBe(true);
  });
});

describe('useHfTokenSync — a silent robot is unknown, never "no token"', () => {
  it('returns to unknown when no state message arrives for longer than the stale limit', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: OTHER }));
    expect(hf(store).robot.known).toBe(true);
    await advance(7000);
    expect(hf(store).robot.known).toBe(false);
    expect(hf(store).robot.present).toBe(false);
  });
});

describe('useHfTokenSync — the Benutzer-ID list follows the slot', () => {
  it('empties the list when the robot affirmatively reports an empty slot after holding a token', async () => {
    api.getHfToken.mockResolvedValue({ stored: false });
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: OTHER }));
    await act(async () => { store.dispatch(setHfUserList(['anna'])); });
    await emit(robotSays({ seq: 2, present: false }));
    expect(store.getState().ui.hfUserList).toEqual([]);
  });

  it('keeps the list when the robot merely goes quiet', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: FP }));
    await act(async () => { store.dispatch(setHfUserList(['anna'])); });
    await advance(7000);
    expect(hf(store).robot.known).toBe(false);
    expect(store.getState().ui.hfUserList).toEqual(['anna']);
  });

  it('calls onRobotTokenReady on EVERY false-to-true edge of "in sync"', async () => {
    const store = makeStore();
    const { onRobotTokenReady } = mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: FP }));
    expect(onRobotTokenReady).toHaveBeenCalledTimes(1);
    await emit(robotSays({ seq: 2, present: true, fp: FP }));
    expect(onRobotTokenReady).toHaveBeenCalledTimes(1);   // still in sync: no new edge
    await emit(robotSays({ seq: 3, present: true, fp: OTHER, busy: true }));
    await emit(robotSays({ seq: 4, present: true, fp: FP }));
    expect(onRobotTokenReady).toHaveBeenCalledTimes(2);
  });

  it('an account loaded after the first state message still produces the edge', async () => {
    api.getHfToken.mockImplementation(() => new Promise(() => {}));
    const store = makeStore();
    const { onRobotTokenReady } = mount(store);
    await settle();
    await emit(robotSays({ present: true, fp: FP }));
    expect(onRobotTokenReady).not.toHaveBeenCalled();
    await act(async () => {
      store.dispatch(accountLoaded({ status: 'stored', fp: FP, hfUsername: 'anna' }));
    });
    expect(onRobotTokenReady).toHaveBeenCalledTimes(1);
  });
});

describe('useHfTokenSync — holds no token', () => {
  it('keeps the token out of the state while it pushes one', async () => {
    const store = makeStore();
    mount(store);
    await settle();
    await emit(robotSays());
    await settle();
    expect(setRobotToken).toHaveBeenCalledWith(TOKEN);
    expect(JSON.stringify(store.getState())).not.toContain(TOKEN.slice(3));
    // robotStateReceived is how a robot's message enters the state
    expect(robotStateReceived(robotSays(), 1).payload).not.toHaveProperty('token');
  });
});
