// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Pure reducer tests for jetsonSlice. No Redux store needed; we call
// the reducer with action objects directly so the test surface is the
// state machine itself, not the testing-library plumbing.

import reducer, {
  setNoJetson,
  setJetsonInfo,
  setJetsonStatus,
  setJetsonError,
  setHeartbeatTransient,
  clearJetson,
} from '../jetsonSlice';

const initial = reducer(undefined, { type: '@@INIT' });

describe('jetsonSlice reducer', () => {
  test('initial state has unknown status and no Jetson identity', () => {
    expect(initial.status).toBe('unknown');
    expect(initial.jetsonId).toBeNull();
    expect(initial.heartbeatTransient).toBe(false);
  });

  test('setNoJetson flips status to no_jetson and clears identity', () => {
    const seeded = { ...initial, jetsonId: 'stale-id', status: 'connected' };
    const next = reducer(seeded, setNoJetson());
    expect(next.status).toBe('no_jetson');
    expect(next.jetsonId).toBeNull();
  });

  test('setJetsonInfo populates identity + owner fields from API payload', () => {
    const next = reducer(
      initial,
      setJetsonInfo({
        jetson_id: 'j-1',
        mdns_name: 'edubotics-jetson-abc.local',
        lan_ip: '192.168.1.42',
        current_owner_user_id: 'u-7',
        current_owner_username: 'anna',
        current_owner_full_name: 'Anna Beispiel',
        online: true,
      })
    );
    expect(next.jetsonId).toBe('j-1');
    expect(next.mdnsName).toBe('edubotics-jetson-abc.local');
    expect(next.lanIp).toBe('192.168.1.42');
    expect(next.ownerUserId).toBe('u-7');
    expect(next.ownerUsername).toBe('anna');
    expect(next.ownerFullName).toBe('Anna Beispiel');
    expect(next.online).toBe(true);
    expect(next.error).toBeNull();
  });

  test('setJetsonStatus transitions status and clears stale error', () => {
    const errored = { ...initial, status: 'error', error: 'old' };
    const next = reducer(errored, setJetsonStatus('available'));
    expect(next.status).toBe('available');
    expect(next.error).toBeNull();
  });

  test('setJetsonStatus(error) keeps error message in place', () => {
    const errored = { ...initial, error: 'boom' };
    const next = reducer(errored, setJetsonStatus('error'));
    expect(next.status).toBe('error');
    expect(next.error).toBe('boom');
  });

  test('setJetsonError captures message AND flips to error status', () => {
    const next = reducer(initial, setJetsonError('Datenbank-Fehler'));
    expect(next.error).toBe('Datenbank-Fehler');
    expect(next.status).toBe('error');
  });

  test('setHeartbeatTransient flips the transient flag without touching status', () => {
    const connected = { ...initial, status: 'connected' };
    const next = reducer(connected, setHeartbeatTransient(true));
    expect(next.heartbeatTransient).toBe(true);
    expect(next.status).toBe('connected');
    const back = reducer(next, setHeartbeatTransient(false));
    expect(back.heartbeatTransient).toBe(false);
    expect(back.status).toBe('connected');
  });

  test('clearJetson resets every field to initial', () => {
    const seeded = {
      ...initial,
      status: 'connected',
      jetsonId: 'j-1',
      ownerUserId: 'u-7',
      heartbeatTransient: true,
      error: 'old',
    };
    const next = reducer(seeded, clearJetson());
    expect(next).toEqual(initial);
  });
});

describe('jetsonSlice full state-machine path', () => {
  test('discovery → connected → disconnecting → available end-to-end', () => {
    // 1. discovery returns the Jetson with no owner
    let state = reducer(
      initial,
      setJetsonInfo({
        jetson_id: 'j-1',
        mdns_name: 'm',
        lan_ip: '1.2.3.4',
        current_owner_user_id: null,
        online: true,
      })
    );
    state = reducer(state, setJetsonStatus('available'));
    expect(state.status).toBe('available');

    // 2. user clicks Verbinde
    state = reducer(state, setJetsonStatus('claiming'));
    expect(state.status).toBe('claiming');

    // 3. claim succeeds, server returns owner = me
    state = reducer(
      state,
      setJetsonInfo({
        jetson_id: 'j-1',
        mdns_name: 'm',
        lan_ip: '1.2.3.4',
        current_owner_user_id: 'u-me',
        current_owner_username: 'me',
        current_owner_full_name: 'Me Me',
        online: true,
      })
    );
    state = reducer(state, setJetsonStatus('connected'));
    expect(state.status).toBe('connected');
    expect(state.ownerUserId).toBe('u-me');

    // 4. heartbeat fails twice (network blip)
    state = reducer(state, setHeartbeatTransient(true));
    expect(state.heartbeatTransient).toBe(true);

    // 5. heartbeat recovers
    state = reducer(state, setHeartbeatTransient(false));
    expect(state.heartbeatTransient).toBe(false);

    // 6. user clicks Trennen
    state = reducer(state, setJetsonStatus('disconnecting'));
    expect(state.status).toBe('disconnecting');

    // 7. release returns, lock cleared, flip back to available
    state = reducer(state, setJetsonStatus('available'));
    expect(state.status).toBe('available');
  });

  test('heartbeat 410 path: connected → available (no manual disconnect needed)', () => {
    let state = reducer(initial, setJetsonStatus('connected'));
    // 410 handler in the hook clears transient + flips to available.
    state = reducer(state, setHeartbeatTransient(false));
    state = reducer(state, setJetsonStatus('available'));
    expect(state.status).toBe('available');
    expect(state.heartbeatTransient).toBe(false);
  });
});
