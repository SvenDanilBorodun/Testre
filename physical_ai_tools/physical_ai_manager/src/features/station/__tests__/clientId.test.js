// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The per-tab client id (station spec v6.13, §9.1 step 0, D10): kept across a
// reload in sessionStorage, re-minted on sign-out, and re-minted by a
// DUPLICATED tab that finds another live tab answering for the same id on
// BroadcastChannel('edubotics-station'). Real timers and Node's real
// BroadcastChannel: a second channel object in the test plays the other tab.

import {
  CLIENT_ID_KEY,
  _resetClientIdForTests,
  checkDuplicateTab,
  getClientId,
  remintClientId,
} from '../clientId';
import { BROADCAST_CHANNEL } from '../stationContract';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

beforeEach(() => {
  _resetClientIdForTests();
  window.sessionStorage.clear();
});

afterEach(() => {
  _resetClientIdForTests();
});

describe('the station client id', () => {
  it('is a UUID kept in sessionStorage across a reload of the tab', () => {
    const id = getClientId();
    expect(id).toMatch(UUID);
    expect(window.sessionStorage.getItem(CLIENT_ID_KEY)).toBe(id);
    // A reload: the module state is gone, the tab's sessionStorage is not.
    _resetClientIdForTests();
    expect(getClientId()).toBe(id);
  });

  it('is re-minted on sign-out', () => {
    const before = getClientId();
    const after = remintClientId();
    expect(after).not.toBe(before);
    expect(window.sessionStorage.getItem(CLIENT_ID_KEY)).toBe(after);
    expect(getClientId()).toBe(after);
  });

  it('keeps its id when no other tab answers within 300 ms', async () => {
    const id = getClientId();
    await expect(checkDuplicateTab(300)).resolves.toBe(id);
  });

  it('a duplicated tab mints a fresh id when another live tab answers for the same one', async () => {
    window.sessionStorage.setItem(CLIENT_ID_KEY, '11111111-1111-4111-8111-111111111111');
    // The original tab: it answers „mine" for its id.
    const other = new BroadcastChannel(BROADCAST_CHANNEL);
    other.onmessage = (event) => {
      const msg = event.data;
      if (msg && msg.type === 'who' && msg.id === '11111111-1111-4111-8111-111111111111') {
        other.postMessage({ type: 'mine', id: msg.id, nonce: msg.nonce });
      }
    };
    try {
      const id = await checkDuplicateTab(300);
      expect(id).not.toBe('11111111-1111-4111-8111-111111111111');
      expect(id).toMatch(UUID);
      expect(window.sessionStorage.getItem(CLIENT_ID_KEY)).toBe(id);
    } finally {
      other.close();
    }
  });

  it('answers another tab that asks for the id this tab holds', async () => {
    const id = getClientId();
    await checkDuplicateTab(50); // opens this tab's channel
    const asker = new BroadcastChannel(BROADCAST_CHANNEL);
    try {
      const answer = await new Promise((resolve) => {
        asker.onmessage = (event) => resolve(event.data);
        asker.postMessage({ type: 'who', id, nonce: 'n-1' });
      });
      expect(answer).toEqual({ type: 'mine', id, nonce: 'n-1' });
    } finally {
      asker.close();
    }
  });
});
