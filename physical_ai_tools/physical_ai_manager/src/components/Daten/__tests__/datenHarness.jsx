// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Daten page's test world (not a test file): a store with the slices the
// page reads, a sidecar served by a mocked fetch from a `world` object, and
// the library/summary/hubstate shapes of spec §J.4. Each test file mocks the
// rosbridge hooks itself (vi.mock is per file) and drives them through the
// helpers exported here.

import React from 'react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { vi } from 'vitest';

import editDatasetReducer from '../../../features/editDataset/editDatasetSlice';
import uiReducer from '../../../features/ui/uiSlice';
import trainingReducer from '../../../features/training/trainingSlice';

export const FP = 'fp-lena';
export const OWN = 'lena-schmidt';
export const PARTNER = 'max-weber';

const CAMERAS = [
  { index: 0, key: 'observation.images.gripper', name: 'gripper', width: 640, height: 480, codec: 'h264', pix_fmt: 'yuv420p', fps: 30 },
  { index: 1, key: 'observation.images.scene', name: 'scene', width: 640, height: 480, codec: 'h264', pix_fmt: 'yuv420p', fps: 30 },
];
const JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
const STATS = { action: ['count', 'max', 'mean', 'min', 'q01'], 'observation.state': ['count', 'max', 'mean', 'min', 'q01'] };

/** A §J.4.1 `local[]` row. */
export function local(id, patch = {}) {
  const [ns, name] = id.split('/');
  return {
    id, ns, name, display_name: null, state: 'ok', meta_digest: `d-${id}`,
    codebase_version: 'v3.0', robot_type: 'omx_f', fps: 30,
    total_episodes: 12, total_frames: 7200, duration_s: 240, size_bytes: 312e6,
    modified_at: '2026-10-03T12:12:00Z', cameras: CAMERAS,
    joints: { state: JOINTS, action: JOINTS }, stat_names: STATS,
    tasks: ['Lege den Würfel in die Schale.'], hint_episodes: 2, record: null,
    ...patch,
  };
}

/** A §J.4.1 `hub.entries[]` row. */
export function hubEntry(id, patch = {}) {
  return {
    id, private: false, last_modified: '2026-10-03T12:12:00Z', head: `head-${id}`,
    total_episodes: 12, total_frames: 7200, duration_s: 240, fps: 30, robot_type: 'omx_f',
    cameras: CAMERAS, stat_names: STATS, size_bytes: 312e6, ...patch,
  };
}

/** A summary (§J.4.3) of `n` episodes for `id`. */
export function summary(id, n = 3, patch = {}) {
  return {
    v: 1, id, meta_digest: `d-${id}`, fps: 30, robot_type: 'omx_f', total_episodes: n, total_frames: n * 60,
    cameras: CAMERAS, joints: { state: JOINTS, action: JOINTS }, tasks: ['Lege den Würfel in die Schale.'], algo: 'hints-v1',
    episodes: Array.from({ length: n }, (_, i) => ({
      i, length: 60, duration_s: 2, task: 'Lege den Würfel in die Schale.', frames: { 0: 60, 1: 60 }, playable: true,
      bytes: { video: 1e6, data_estimate: 2e4 }, hints: [],
    })),
    ...patch,
  };
}

/** Episode data (§J.4.5) of `length` frames. */
export function episodeData(length = 60) {
  const row = (t) => JOINTS.map((_, k) => Math.sin(t / 10 + k) * 0.5);
  return {
    v: 1, i: 0, fps: 30, length, unit: 'rad', names: { state: JOINTS, action: JOINTS },
    timestamp: Array.from({ length }, (_, t) => t / 30),
    state: Array.from({ length }, (_, t) => row(t)),
    action: Array.from({ length }, (_, t) => row(t + 2)),
  };
}

/** The sidecar the page sees: answers from `world`, records every request. */
export function installSidecar(world) {
  const requests = [];
  const ok = (body) => ({ ok: true, status: 200, headers: { get: () => null }, json: () => Promise.resolve(body) });
  const err = (status, code) => ({ ok: false, status, headers: { get: () => null }, json: () => Promise.resolve({ error: code }) });
  global.fetch = vi.fn((url) => {
    requests.push(url);
    const u = new URL(url, 'http://robot');
    const p = u.pathname;
    if (p.endsWith('/library')) {
      const hub = u.searchParams.get('hub') === '1';
      const ids = u.searchParams.get('ids') ? u.searchParams.get('ids').split(',') : null;
      const keep = (id) => !ids || ids.includes(id);
      const reply = {
        v: 1,
        robot_type: 'omx_f',
        local: world.local.filter((e) => keep(e.id)),
        hub: hub
          ? { state: world.hubState || 'ok', token_fp: world.hubFp || FP, account: OWN, fetched_at: 'x', hidden_count: world.hidden || 0, complete_ns: [OWN], entries: world.hub.filter((e) => keep(e.id)) }
          : { state: 'skipped', token_fp: null },
        sync: Object.fromEntries(Object.entries(world.sync).filter(([id]) => keep(id))),
      };
      return Promise.resolve(ok(reply));
    }
    if (p.endsWith('/hub/probe')) {
      return Promise.resolve(ok(world.probe(u.searchParams.get('repo'))));
    }
    const ds = /\/ds\/([^/]+)\/(.+)$/.exec(p);
    if (ds) {
      const token = decodeURIComponent(ds[1]);
      const id = token.replace(/^ds-/, '');
      const rest = ds[2];
      if (rest === 'summary') {
        const sm = typeof world.summaries[id] === 'function' ? world.summaries[id]() : world.summaries[id];
        return Promise.resolve(sm ? ok(sm) : err(409, 'in_session'));
      }
      if (rest === 'hubstate') return Promise.resolve(ok(world.hubstate(id)));
      if (/^episode\/\d+\/data$/.test(rest)) return Promise.resolve(ok(episodeData(60)));
      if (rest === 'thumb.jpg') return Promise.resolve(ok(null));
      if (/^episode\/\d+\/video\/\d+\.mp4$/.test(rest)) {
        const e = world.clip ? world.clip(rest) : null;
        return Promise.resolve(e ? err(e.status, e.code) : { ok: true, status: 206, headers: { get: () => null }, json: () => Promise.resolve(null) });
      }
      return Promise.resolve(ok(null));
    }
    return Promise.resolve(err(404, 'not_found'));
  });
  return requests;
}

/** A store with the slices the Daten page reads. */
export function makeStore({
  connected = true, inSync = true, authenticated = true, hfUsername = OWN, profileLoaded = true, robotType = 'omx_f',
} = {}) {
  const hfToken = {
    account: { status: 'stored', fp: FP, hfUsername },
    robot: { known: true, accepts: true, present: inSync, fp: inSync ? FP : 'fp-other' },
    sync: {},
    epoch: 0,
    kick: 0,
  };
  return configureStore({
    reducer: {
      editDataset: editDatasetReducer,
      ui: uiReducer,
      training: trainingReducer,
      tasks: () => ({ heartbeatStatus: connected ? 'connected' : 'disconnected', taskStatus: { robotType, capabilities: null } }),
      auth: () => ({
        isAuthenticated: authenticated, isLoading: false, profileLoaded, hfUsername: authenticated ? hfUsername : null,
        session: authenticated ? { access_token: 'jwt', user: { id: 'u-lena' } } : null,
      }),
      hfToken: () => hfToken,
      ros: () => ({ rosbridgeUrl: 'ws://robot/rosbridge' }),
      jetson: () => ({ status: 'idle' }),
    },
  });
}

export function wrap(store, ui) {
  return <Provider store={store}>{ui}</Provider>;
}

/** The default command: tokens on `link`, a job per mutating action. */
export function makeCommand(overrides = {}) {
  let n = 0;
  return vi.fn(async (action, args) => {
    if (overrides[action]) return overrides[action](args);
    if (action === 'link') {
      const tokens = {};
      (args.datasets || []).forEach((id) => { tokens[id] = `ds-${id}`; });
      return { ok: true, code: '', message: '', result: { ttl_s: 1800, library_token: 'LIB', tokens, missing: [] }, oldImage: false, unreachable: false };
    }
    if (action === 'upload') return { ok: true, code: '', message: '', result: { repo_id: args.dataset } };
    if (action === 'cancel') return { ok: true, code: '', message: '', result: {} };
    if (action === 'state') return { ok: true, code: '', message: '', result: { v: 1, busy: [], jobs: [], transfer: null } };
    n += 1;
    return { ok: true, code: '', message: '', result: { job_id: `job-${n}`, outputs: [], target: args.target } };
  });
}
