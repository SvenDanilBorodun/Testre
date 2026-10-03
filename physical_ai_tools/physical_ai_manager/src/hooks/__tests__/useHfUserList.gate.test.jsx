// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// THE S1 GATE. The Benutzer-ID list is the account + organisations of the token
// in the ROBOT's slot. If the slot still holds the previous student's token (they
// closed the tab, or the sign-out clear was refused during an upload), loading
// the list hands the next student that student's namespace, and the upload
// namespace guard agrees because both sides name the same account. So
// `useHfUserList().reload` — the single choke point of all four callers — asks
// the robot only while the slot is provably this student's.

import React from 'react';
import { act, render } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import uiReducer from '../../features/ui/uiSlice';
import jetsonReducer, { setJetsonStatus } from '../../store/jetsonSlice';
import hfReducer, {
  accountFailed,
  accountLoaded,
  robotLegacyConfirmed,
  robotStateReceived,
} from '../../features/hfToken/hfTokenSlice';
import { signedOut } from '../../features/session/sessionActions';
import { useHfUserList } from '../useHfUserList';

const calls = { n: 0 };
let whoamiGate = null;
vi.mock('../useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({
    getRegisteredHFUser: async () => {
      calls.n += 1;
      if (whoamiGate) await whoamiGate;
      return { success: true, user_id_list: ['alice'] };
    },
  }),
}));

const A = 'a'.repeat(16);
const B = 'b'.repeat(16);

const robot = (o = {}) => robotStateReceived(
  { v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false, ...o },
  1,
);
const stored = (fp) => accountLoaded({ status: 'stored', fp, hfUsername: 'alice' });

/** A store with the real reducers; `slice: false` builds one WITHOUT hfToken (every page test's shape). */
function setup({ slice = true, actions = [] } = {}) {
  calls.n = 0;
  whoamiGate = null;
  const reducer = { ui: uiReducer, jetson: jetsonReducer, ...(slice ? { hfToken: hfReducer } : {}) };
  const store = configureStore({ reducer });
  actions.forEach((a) => store.dispatch(a));
  let api;
  function Probe() {
    api = useHfUserList();
    return null;
  }
  render(<Provider store={store}><Probe /></Provider>);
  return { store, api: () => api };
}

const reload = async (t) => {
  let result;
  await act(async () => { result = await t.api().reload(); });
  return result;
};

describe('the Benutzer-ID list reload is gated at the one choke point', () => {
  it('a store without the hfToken slice (every page test): allowed', async () => {
    const t = setup({ slice: false });
    expect(await reload(t)).toEqual(['alice']);
    expect(calls.n).toBe(1);
  });

  it('the robot holds a foreign token and the account has none: NOT called', async () => {
    const t = setup({ actions: [accountLoaded({ status: 'none' }), robot({ present: true, fp: B })] });
    expect(await reload(t)).toBeNull();
    expect(calls.n).toBe(0);
    expect(t.store.getState().ui.hfUserList).toEqual([]);
  });

  it('the robot holds a foreign token and the account state failed to load: NOT called', async () => {
    const t = setup({ actions: [accountFailed('error'), robot({ present: true, fp: B })] });
    expect(await reload(t)).toBeNull();
    expect(calls.n).toBe(0);
  });

  it('a stored token still on its way to the robot: NOT called', async () => {
    const t = setup({ actions: [stored(A), robot({ present: true, fp: B })] });
    expect(await reload(t)).toBeNull();
    expect(calls.n).toBe(0);
  });

  it('in sync: called, and the list lands in Redux', async () => {
    const t = setup({ actions: [stored(A), robot({ present: true, fp: A })] });
    expect(await reload(t)).toEqual(['alice']);
    expect(calls.n).toBe(1);
    expect(t.store.getState().ui.hfUserList).toEqual(['alice']);
  });

  it('the robot state unknown (offline escape, the first seconds): NOT called', async () => {
    const t = setup({ actions: [] });
    expect(await reload(t)).toBeNull();
    expect(calls.n).toBe(0);
  });

  it('an old image that PROVED itself silent: called', async () => {
    const t = setup({ actions: [robotLegacyConfirmed()] });
    expect(await reload(t)).toEqual(['alice']);
    expect(calls.n).toBe(1);
  });

  it('a robot that takes no personal token (the Jetson image): called', async () => {
    const t = setup({ actions: [accountLoaded({ status: 'none' }), robot({ accepts: false })] });
    expect(await reload(t)).toEqual(['alice']);
    expect(calls.n).toBe(1);
  });

  it('a claimed classroom Jetson: called, whatever the local slot says', async () => {
    const t = setup({
      actions: [
        accountLoaded({ status: 'none' }),
        robot({ present: true, fp: B }),
        setJetsonStatus('connected'),
      ],
    });
    expect(await reload(t)).toEqual(['alice']);
    expect(calls.n).toBe(1);
  });

  it('reads the gate when it is CALLED, not when the hook rendered', async () => {
    // The gate opens the moment the robot reports the student's own token; a
    // reload bound at render time would stay shut until the next render.
    const t = setup({ actions: [stored(A), robot({ present: true, fp: B })] });
    expect(await reload(t)).toBeNull();
    await act(async () => { t.store.dispatch(robot({ present: true, fp: A })); });
    expect(await reload(t)).toEqual(['alice']);
    expect(calls.n).toBe(1);
  });

  it('does not store an answer that arrives after the gate closed (a sign-out in between)', async () => {
    const t = setup({ actions: [stored(A), robot({ present: true, fp: A })] });
    let release;
    whoamiGate = new Promise((resolve) => { release = resolve; });
    let result = 'pending';
    await act(async () => {
      const p = t.api().reload().then((r) => { result = r; });
      t.store.dispatch(signedOut());
      release();
      await p;
    });
    expect(calls.n).toBe(1);
    expect(result).toBeNull();
    expect(t.store.getState().ui.hfUserList).toEqual([]);
  });
});
