// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The per-tab client id of the camera station (station spec v6.13, §9.1 step
// 0, owner decision D10): a UUID minted once per tab and kept across reloads in
// sessionStorage, so a reloaded tab resumes its own session. It names no
// student and holds no student data; utils/sessionScope.js lists it as IGNORED
// with that reason. It is re-minted on sign-out (§9.1 step 4), so a lost
// `leave` can never let the next student resume the previous one's robot check.
//
// A DUPLICATED browser tab copies sessionStorage, and would share the id. So
// every tab answers on BroadcastChannel('edubotics-station'): at load a tab
// asks „who holds my id?", and an answer from another live tab within
// DUPLICATE_TAB_WAIT_MS makes it mint a fresh one. Without BroadcastChannel two
// such tabs could act as one owner; both still pass the same server gates
// (§15).

import { BROADCAST_CHANNEL, DUPLICATE_TAB_WAIT_MS } from './stationContract';

// sessionStorage, per tab (registered in utils/sessionScope.js as IGNORED).
// Declared here, in the one file that touches storage, so the storage-key scan
// of utils/__tests__/sessionScope.test.js resolves it.
export const CLIENT_ID_KEY = 'edubotics_station_client_id';

function newUuid() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  // RFC 4122 v4 shape from Math.random — only where crypto.randomUUID is absent.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = Math.floor(Math.random() * 16);
    return (c === 'x' ? r : ((r % 4) + 8)).toString(16);
  });
}

function readStored() {
  try {
    const v = window.sessionStorage.getItem(CLIENT_ID_KEY);
    return typeof v === 'string' && v ? v : null;
  } catch {
    return null;
  }
}

function writeStored(id) {
  try {
    window.sessionStorage.setItem(CLIENT_ID_KEY, id);
  } catch {
    /* storage unavailable: the id lives for this document only */
  }
}

let current = null;

/** This tab's id; minted (and stored) on first use. */
export function getClientId() {
  if (current) return current;
  current = readStored() || newUuid();
  writeStored(current);
  return current;
}

/** A fresh id (sign-out, a duplicated tab). */
export function remintClientId() {
  current = newUuid();
  writeStored(current);
  return current;
}

let channel = null;

function openChannel() {
  if (channel) return channel;
  try {
    if (typeof BroadcastChannel !== 'function') return null;
    channel = new BroadcastChannel(BROADCAST_CHANNEL);
    // Every tab answers for the id it holds right now, whatever page is open.
    channel.addEventListener('message', (event) => {
      const msg = event && event.data;
      if (!msg || msg.type !== 'who' || typeof msg.id !== 'string') return;
      if (current && msg.id === current) {
        try { channel.postMessage({ type: 'mine', id: msg.id, nonce: msg.nonce }); } catch { /* closed */ }
      }
    });
  } catch {
    channel = null;
  }
  return channel;
}

/**
 * The duplicated-tab check (§9.1 step 0). Resolves the id this tab keeps:
 * the stored one, or a fresh one when another live tab answered for it.
 * @param {number} [waitMs]
 */
export function checkDuplicateTab(waitMs = DUPLICATE_TAB_WAIT_MS) {
  const id = getClientId();
  const ch = openChannel();
  if (!ch) return Promise.resolve(id);
  const nonce = newUuid();
  return new Promise((resolve) => {
    let answered = false;
    const onMessage = (event) => {
      const msg = event && event.data;
      if (msg && msg.type === 'mine' && msg.id === id && msg.nonce === nonce) answered = true;
    };
    ch.addEventListener('message', onMessage);
    try {
      ch.postMessage({ type: 'who', id, nonce });
    } catch {
      /* closed: nobody can answer */
    }
    setTimeout(() => {
      ch.removeEventListener('message', onMessage);
      resolve(answered ? remintClientId() : getClientId());
    }, waitMs);
  });
}

/** Tests only. */
export function _resetClientIdForTests() {
  current = null;
  if (channel) {
    try { channel.close(); } catch { /* closed */ }
  }
  channel = null;
}
