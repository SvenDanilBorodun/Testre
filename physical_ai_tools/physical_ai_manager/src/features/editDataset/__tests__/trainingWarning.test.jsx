// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Training page's Daten warning (owner decision D6, spec §E9, R-4) and the
// nav item (spec §G1): `changed`/`conflict` warn, `local` warns only on proof
// (the robot holds this student's token and the hub said „absent"), anything
// else — unknown, current, newer, an old image, a failed ask, cloud mode — is
// silent; the tab is hardwareOnly, jetsonIncompatible, `editable`, database.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import useDatasetLocalChanges, { warningFor } from '../hooks/useDatasetLocalChanges';

let mockCommand = null;
vi.mock('../hooks/useDatenCommand', () => ({ __esModule: true, default: () => mockCommand }));
let mockCloud = false;
vi.mock('../../../utils/cloudMode', () => ({ __esModule: true, isCloudOnlyMode: () => mockCloud }));

const ID = 'lena-schmidt/omx_f_stift';
const FP = 'fp-lena';

const reply = (state, hub = { state: 'ok', token_fp: FP }) => ({
  v: 1, local: [{ id: ID }], hub, sync: { [ID]: { state, reason: null, head: null } },
});

describe('warningFor', () => {
  const ctx = { inSync: true, accountFp: FP };
  it('changed and conflict warn', () => {
    expect(warningFor(reply('changed'), ID, ctx)).toBe('changed');
    expect(warningFor(reply('conflict'), ID, ctx)).toBe('changed');
  });
  it('local warns only on proof', () => {
    expect(warningFor(reply('local'), ID, ctx)).toBe('local');
    expect(warningFor(reply('local'), ID, { inSync: false, accountFp: FP })).toBeNull();
    expect(warningFor(reply('local', { state: 'unreachable', token_fp: FP }), ID, ctx)).toBeNull();
    expect(warningFor(reply('local', { state: 'ok', token_fp: 'other' }), ID, ctx)).toBeNull();
  });
  it('unknown, current, newer and a dataset not on this robot are silent', () => {
    ['unknown', 'current', 'newer'].forEach((s) => expect(warningFor(reply(s), ID, ctx)).toBeNull());
    expect(warningFor({ local: [], sync: { [ID]: { state: 'changed' } } }, ID, ctx)).toBeNull();
    expect(warningFor(null, ID, ctx)).toBeNull();
  });
});

describe('useDatasetLocalChanges', () => {
  const store = (connected = true) => configureStore({
    reducer: {
      tasks: () => ({ heartbeatStatus: connected ? 'connected' : 'disconnected' }),
      hfToken: () => ({ account: { status: 'stored', fp: FP }, robot: { present: true, fp: FP } }),
    },
  });
  const wrapper = (s) => ({ children }) => <Provider store={s}>{children}</Provider>;
  beforeEach(() => {
    mockCloud = false;
    mockCommand = vi.fn(async () => ({ ok: true, result: { library_token: 'LIB', tokens: {} } }));
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, headers: { get: () => null }, json: () => Promise.resolve(reply('changed')),
    }));
  });
  afterEach(() => { delete global.fetch; });

  it('asks library?ids=<id>&hub=1 for the picked dataset and reports changed', async () => {
    const { result } = renderHook(() => useDatasetLocalChanges(ID), { wrapper: wrapper(store()) });
    await waitFor(() => expect(result.current).toBe('changed'));
    expect(global.fetch.mock.calls[0][0]).toBe('/daten-api/v1/lib/LIB/library?ns=lena-schmidt&hub=1&ids=lena-schmidt%2Fomx_f_stift');
  });

  it('silent for an old image, a failed ask, cloud mode, no link', async () => {
    mockCommand = vi.fn(async () => ({ ok: false, code: 'old_image', oldImage: true, result: {} }));
    const { result: a } = renderHook(() => useDatasetLocalChanges(ID), { wrapper: wrapper(store()) });
    await new Promise((r) => { setTimeout(r, 20); });
    expect(a.current).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();

    mockCommand = vi.fn(async () => ({ ok: true, result: { library_token: 'LIB' } }));
    global.fetch = vi.fn(() => Promise.reject(new TypeError('down')));
    const { result: b } = renderHook(() => useDatasetLocalChanges(ID), { wrapper: wrapper(store()) });
    await new Promise((r) => { setTimeout(r, 20); });
    expect(b.current).toBeNull();

    mockCloud = true;
    const { result: c } = renderHook(() => useDatasetLocalChanges(ID), { wrapper: wrapper(store()) });
    expect(c.current).toBeNull();
    mockCloud = false;
    const { result: d } = renderHook(() => useDatasetLocalChanges(ID), { wrapper: wrapper(store(false)) });
    expect(d.current).toBeNull();
  });
});

describe('the Daten nav item (spec §G1)', () => {
  it('is hardwareOnly (hidden in ?cloud=1), jetsonIncompatible, editable-gated and uses the database icon', () => {
    const src = fs.readFileSync(path.resolve(__dirname, '../../../StudentApp.js'), 'utf8');
    const line = src.split('\n').find((l) => l.includes('key: PageType.EDIT_DATASET'));
    expect(line).toMatch(/icon: 'database'/);
    expect(line).toMatch(/hardwareOnly: true/);
    expect(line).toMatch(/jetsonIncompatible: true/);
    expect(line).toMatch(/capabilityKey: 'editable'/);
  });
});
