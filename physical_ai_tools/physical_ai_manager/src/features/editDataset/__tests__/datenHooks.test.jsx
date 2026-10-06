// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// useDatenState (one ref-counted /edubotics/daten_state subscription),
// useDatenStartWait (the Aufnahme page's STARTING-only boolean, R-8 / F-5) and
// useGroupNamespaces (GET /me/group-members once per session, D2).

import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import useDatenState, { busyMap, parseDatenState, resetDatenStateForTests } from '../hooks/useDatenState';
import useDatenStartWait, { isUploading } from '../hooks/useDatenStartWait';
import useGroupNamespaces, { resetGroupNamespacesCache } from '../hooks/useGroupNamespaces';
import { getGroupMembers } from '../../../services/meApi';

const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.name = opts.name;
      this.messageType = opts.messageType;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
  },
}));
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));
vi.mock('../../../services/meApi', () => ({
  __esModule: true,
  getGroupMembers: vi.fn(),
}));

function makeStore(auth = {}) {
  return configureStore({
    reducer: {
      ros: () => ({ rosbridgeUrl: 'ws://robot/rosbridge' }),
      auth: () => ({ hfUsername: 'lena-schmidt', session: { access_token: 'jwt', user: { id: 'u1' } }, ...auth }),
    },
  });
}
const wrapper = (store) => ({ children }) => <Provider store={store}>{children}</Provider>;

const send = (payload) => {
  const live = mockTopics.filter((t) => !t.unsubscribed && t.cb);
  live.forEach((t) => t.cb({ data: JSON.stringify(payload) }));
};
const msg = (busy) => ({ v: 1, seq: 1, busy, jobs: [], transfer: null });

beforeEach(() => {
  mockTopics.length = 0;
  resetDatenStateForTests();
});
afterEach(() => resetDatenStateForTests());

describe('parseDatenState', () => {
  it('keeps only well-formed rows of schema v1', () => {
    const p = parseDatenState(JSON.stringify({
      v: 1, seq: 4,
      busy: [{ id: 'a/b', kind: 'record' }, { id: 'x', kind: 'nonsense' }, null],
      jobs: [{ job_id: 'j', op: 'delete', state: 'running', datasets: ['a/b'], outputs: [] }, { job_id: 'k', op: 'zap', state: 'running' }],
      transfer: { kind: 'upload', repo_id: 'a/b', target: null },
    }));
    expect(p.busy).toEqual([{ id: 'a/b', kind: 'record' }]);
    expect(p.jobs.map((j) => j.job_id)).toEqual(['j']);
    expect(p.transfer).toEqual({ kind: 'upload', repo_id: 'a/b', target: null });
    expect(parseDatenState('{"v":2}')).toBeNull();
    expect(parseDatenState('nope')).toBeNull();
    expect(busyMap(p)).toEqual({ 'a/b': 'record' });
  });
});

describe('useDatenState', () => {
  it('one topic for every reader, closed with the last one', async () => {
    const store = makeStore();
    const a = renderHook(() => useDatenState(), { wrapper: wrapper(store) });
    const b = renderHook(() => useDatenState(), { wrapper: wrapper(store) });
    await waitFor(() => expect(mockTopics.length).toBe(1));
    expect(mockTopics[0].name).toBe('/edubotics/daten_state');
    expect(mockTopics[0].messageType).toBe('std_msgs/msg/String');
    expect(a.result.current.received).toBe(false);
    act(() => send(msg([{ id: 'a/b', kind: 'upload' }])));
    expect(a.result.current.received).toBe(true);
    expect(b.result.current.payload.busy).toEqual([{ id: 'a/b', kind: 'upload' }]);
    a.unmount();
    expect(mockTopics[0].unsubscribed).toBe(false);
    b.unmount();
    expect(mockTopics[0].unsubscribed).toBe(true);
  });

  it('a re-render does not reopen the topic', async () => {
    const store = makeStore();
    const r = renderHook(() => useDatenState(), { wrapper: wrapper(store) });
    await waitFor(() => expect(mockTopics.length).toBe(1));
    r.rerender();
    r.rerender();
    expect(mockTopics.length).toBe(1);
    expect(mockTopics[0].unsubscribed).toBe(false);
  });
});

describe('useDatenStartWait (R-8, F-5)', () => {
  it('subscribes only while active, true while the started repo uploads', async () => {
    const store = makeStore();
    let renders = 0;
    const { result, rerender } = renderHook(({ active }) => {
      renders += 1;
      return useDatenStartWait('lena-schmidt/omx_f_wuerfel', active);
    }, { wrapper: wrapper(store), initialProps: { active: false } });
    expect(mockTopics.length).toBe(0);
    expect(result.current).toBe(false);

    rerender({ active: true });
    await waitFor(() => expect(mockTopics.length).toBe(1));
    act(() => send(msg([{ id: 'lena-schmidt/omx_f_wuerfel', kind: 'upload' }])));
    expect(result.current).toBe(true);

    // 30 messages with an unchanged answer: no render
    const before = renders;
    for (let i = 0; i < 30; i += 1) {
      act(() => send(msg([{ id: 'lena-schmidt/omx_f_wuerfel', kind: 'upload' }])));
    }
    expect(renders - before).toBe(0);

    act(() => send(msg([])));
    expect(result.current).toBe(false);

    rerender({ active: false });
    expect(mockTopics[0].unsubscribed).toBe(true);
  });

  it('another repo, or another kind, is no wait', () => {
    expect(isUploading(msg([{ id: 'x/y', kind: 'upload' }]), 'lena-schmidt/omx_f_wuerfel')).toBe(false);
    expect(isUploading(msg([{ id: 'lena-schmidt/omx_f_wuerfel', kind: 'record' }]), 'lena-schmidt/omx_f_wuerfel')).toBe(false);
    expect(isUploading(null, 'a/b')).toBe(false);
  });
});

describe('useGroupNamespaces (D2)', () => {
  beforeEach(() => {
    resetGroupNamespacesCache();
    getGroupMembers.mockReset();
  });

  it('the own account first, then every member with an HF account; one request per session', async () => {
    getGroupMembers.mockResolvedValue({
      workgroup_id: 'g1',
      members: [
        { full_name: 'Lena Schmidt', hf_username: 'lena-schmidt', is_me: true },
        { full_name: 'Max Weber', hf_username: 'max-weber', is_me: false },
        { full_name: 'Tom Klein', hf_username: null, is_me: false },
      ],
    });
    const store = makeStore();
    const a = renderHook(() => useGroupNamespaces(), { wrapper: wrapper(store) });
    await waitFor(() => expect(a.result.current.status).toBe('ready'));
    expect(a.result.current.namespaces).toEqual(['lena-schmidt', 'max-weber']);
    expect(a.result.current.names).toEqual({ 'max-weber': 'Max Weber' });
    const b = renderHook(() => useGroupNamespaces(), { wrapper: wrapper(store) });
    await waitFor(() => expect(b.result.current.status).toBe('ready'));
    expect(getGroupMembers).toHaveBeenCalledTimes(1);
  });

  it('a failure lists the own account alone', async () => {
    getGroupMembers.mockRejectedValue(Object.assign(new Error('x'), { status: 500 }));
    const store = makeStore();
    const { result } = renderHook(() => useGroupNamespaces(), { wrapper: wrapper(store) });
    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(result.current.namespaces).toEqual(['lena-schmidt']);
  });

  it('without an HF account nothing is listed and nothing asked', async () => {
    const store = makeStore({ hfUsername: null });
    const { result } = renderHook(() => useGroupNamespaces(), { wrapper: wrapper(store) });
    expect(result.current.namespaces).toEqual([]);
    expect(getGroupMembers).not.toHaveBeenCalled();
  });
});
