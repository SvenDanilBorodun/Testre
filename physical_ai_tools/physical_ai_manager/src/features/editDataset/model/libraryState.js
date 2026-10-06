// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The page's copy of the library (spec §J.4.1), pure. Three rules live here:
//
// 1. Replies never go backwards (U-3): every library request carries a page
//    sequence number; per dataset the page keeps the number of the request
//    whose reply it shows, and a reply to an OLDER request never replaces a
//    newer one — whatever order replies arrive in.
//
// 2. A `hub=0` reply never replaces the hub view (H-2): it brings `local[]`
//    (and the hints), while the last `hub=1` reply's hub part and sync map
//    stay — except for a dataset whose `meta_digest` changed (its sync comes
//    from the `hub=0` reply: `changed` for a synced record, else unknown) and
//    one that is gone locally (its sync falls back to the hub entry's
//    `online`, or is dropped). So the 5 s hint poll never turns a badge into
//    „Online-Stand unbekannt" and never drops a „Nur online" card.
//
// 3. A hub part read with ANOTHER token than the one this student stores
//    (`hub.token_fp` ≠ the account's fingerprint) is not this student's
//    online list (§C1): its entries are dropped and every verdict that needed
//    the hub reads unknown.
//
// And the crashed card's rule (H-1, T-1, U-3, §G14): `cardPhase`.

import { SYNC_STATES, UNKNOWN_REASONS } from '../datenContract';

export function emptyLibrary() {
  return {
    robotType: null,
    local: {},
    entrySeq: {},
    hub: null,
    sync: {},
    syncSeq: {},
    hubAt: null,
    loaded: false,
    stamps: {},
    stateSeen: false,
  };
}

const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);

function syncEntry(s) {
  if (!isObj(s) || !SYNC_STATES.includes(s.state)) return null;
  return {
    state: s.state,
    reason: UNKNOWN_REASONS.includes(s.reason) ? s.reason : null,
    head: typeof s.head === 'string' && s.head ? s.head : null,
  };
}

const NOT_ASKED = Object.freeze({ state: 'unknown', reason: 'not_asked', head: null });

/**
 * A verdict made against a hub part this page cannot use: only what holds
 * without the hub. The robot's `unreachable` is a fact about asking, true
 * whoever's token asked, so it stays (V2-9: the badge's tooltip is then the
 * unreachable sentence, §G10, never „wurde noch nicht geprüft"); every other
 * verdict about the hub becomes „not asked".
 */
function blindSync(s) {
  if (!s) return null;
  if (s.state === 'changed' || s.state === 'conflict') return { state: 'changed', reason: null, head: s.head };
  if (s.state === 'online') return null;
  if (s.state === 'unknown' && s.reason === 'unreachable') return { state: 'unknown', reason: 'unreachable', head: null };
  return NOT_ASKED;
}

function hubUsable(reply, accountFp) {
  const h = reply && reply.hub;
  return !!(isObj(h) && h.state === 'ok' && accountFp && h.token_fp === accountFp);
}

function hubEntriesById(reply) {
  const out = {};
  const list = reply && isObj(reply.hub) && Array.isArray(reply.hub.entries) ? reply.hub.entries : [];
  list.forEach((e) => { if (isObj(e) && typeof e.id === 'string') out[e.id] = e; });
  return out;
}

/**
 * @param {object} prev the page's library
 * @param {object} reply one `library` answer (§J.4.1)
 * @param {{seq: number, hub: boolean, ids: string[]|null}} req what was asked
 * @param {{accountFp?: string|null, nowMs?: number}} opts
 */
export function applyLibraryReply(prev, reply, req, { accountFp = null, nowMs = Date.now() } = {}) {
  const seq = Number(req && req.seq) || 0;
  const scope = req && Array.isArray(req.ids) && req.ids.length ? new Set(req.ids) : null;
  const askedHub = !!(req && req.hub);
  const next = {
    ...prev,
    local: { ...prev.local },
    entrySeq: { ...prev.entrySeq },
    sync: { ...prev.sync },
    syncSeq: { ...prev.syncSeq },
    loaded: true,
    robotType: (reply && reply.robot_type) || prev.robotType,
  };

  // ---- local entries (rule 1)
  const replyLocal = {};
  (reply && Array.isArray(reply.local) ? reply.local : []).forEach((e) => {
    if (isObj(e) && typeof e.id === 'string') replyLocal[e.id] = e;
  });
  const localIds = scope ? [...scope] : [...new Set([...Object.keys(prev.local), ...Object.keys(replyLocal)])];
  const touched = new Set();
  localIds.forEach((id) => {
    if ((prev.entrySeq[id] ?? -Infinity) > seq) return;
    if (replyLocal[id]) next.local[id] = replyLocal[id];
    else delete next.local[id];
    next.entrySeq[id] = seq;
    touched.add(id);
  });

  // ---- hub part
  const usable = askedHub && hubUsable(reply, accountFp);
  const replyHub = usable ? hubEntriesById(reply) : {};
  if (askedHub && isObj(reply && reply.hub)) {
    if (!scope) {
      const h = reply.hub;
      next.hub = {
        state: usable ? 'ok' : (h.state === 'ok' ? 'token_changed' : String(h.state || 'unreachable')),
        account: usable ? (h.account || null) : null,
        fetchedAt: h.fetched_at || null,
        hiddenCount: usable ? (Number(h.hidden_count) || 0) : 0,
        completeNs: usable && Array.isArray(h.complete_ns) ? h.complete_ns : [],
        entries: replyHub,
      };
      if (usable) next.hubAt = nowMs;
    } else if (usable && next.hub) {
      const entries = { ...next.hub.entries };
      scope.forEach((id) => {
        if (replyHub[id]) entries[id] = replyHub[id];
        else delete entries[id];
      });
      next.hub = { ...next.hub, entries };
    }
  }

  // ---- sync map
  const replySync = {};
  const rs = reply && isObj(reply.sync) ? reply.sync : {};
  Object.keys(rs).forEach((id) => {
    const e = syncEntry(rs[id]);
    if (e) replySync[id] = e;
  });
  const take = (id, value) => {
    if ((prev.syncSeq[id] ?? -Infinity) > seq) return;
    if (value) next.sync[id] = value;
    else delete next.sync[id];
    next.syncSeq[id] = seq;
  };

  if (askedHub) {
    // hub=1 (rule 3 for a foreign or failed hub part)
    const ids = scope ? [...scope] : [...new Set([...Object.keys(prev.sync), ...Object.keys(replySync), ...touched])];
    ids.forEach((id) => {
      const s = replySync[id] || null;
      take(id, usable ? s : blindSync(s));
    });
  } else {
    // hub=0 (rule 2)
    const ids = scope ? [...scope] : [...new Set([...Object.keys(prev.sync), ...Object.keys(replySync), ...touched])];
    ids.forEach((id) => {
      if (!touched.has(id) && next.local[id]) return; // a newer reply owns it
      const now = next.local[id];
      const before = prev.local[id];
      if (now) {
        if (before && before.meta_digest === now.meta_digest && prev.sync[id]) return; // keep the hub verdict
        take(id, replySync[id] || NOT_ASKED);
        return;
      }
      const hubEntry = next.hub && next.hub.entries ? next.hub.entries[id] : null;
      take(id, hubEntry ? { state: 'online', reason: null, head: hubEntry.head || null } : null);
    });
  }
  return next;
}

// What a LOCAL edit (delete, split, „Beide behalten" aside) makes of a sync
// state when the robot's re-read is not there to say it (V2-5): the copy here
// changed, so a synced copy reads `changed` — and `conflict` when the hub had
// moved too (`newer`). `local` and `unknown` stay what they were.
const AFTER_LOCAL_EDIT = Object.freeze({
  current: 'changed', changed: 'changed', newer: 'conflict', conflict: 'conflict',
});

/** The sync state a dataset has after a local edit, from the one it had before (no re-read). */
export function syncAfterLocalEdit(state) {
  return AFTER_LOCAL_EDIT[state] || state || 'unknown';
}

/**
 * Does a hub entry lack the card's numbers? The robot's re-read of named ids
 * (`library?ids=…&hub=1`) and every entry of a dataset that is also local
 * carry only id, head, visibility and date (§J.4.1); only the whole list reads
 * an online-only dataset's `info.json`.
 */
export function hubEntryLacksNumbers(entry) {
  return !!entry && (entry.total_episodes === undefined || entry.total_episodes === null);
}

/**
 * The ids of `ids` that a reply turned into „Nur online" cards without their
 * numbers (a whole delete with a hub copy, V2-14): the whole list must be read
 * to fill them.
 */
export function onlineCardsWithoutNumbers(lib, ids) {
  const entries = (lib && lib.hub && lib.hub.entries) || {};
  return (ids || []).filter((id) => !(lib.local || {})[id] && hubEntryLacksNumbers(entries[id]));
}

/** Stamp a busy change for `ids` at the page's current request number (T-1 a). */
export function stampBusyChange(state, ids, seq) {
  if (!ids || !ids.length) return state;
  const stamps = { ...state.stamps };
  ids.forEach((id) => { stamps[id] = seq; });
  return { ...state, stamps };
}

/**
 * The ids whose busy entry changed between two daten_state busy maps
 * (`{id: kinds[]}`, useDatenState::busyKindsById): an id that LOST a kind —
 * it disappeared, changed kind, or the upload a waiting Start was listed
 * beside ended. A kind that appeared is no change (the overlay simply shows),
 * and the same kinds in another wire order are none (C-2).
 */
export function busyChanges(prevBusy, nextBusy) {
  const out = [];
  Object.keys(prevBusy || {}).forEach((id) => {
    const next = (nextBusy || {})[id] || [];
    if ((prevBusy[id] || []).some((k) => !next.includes(k))) out.push(id);
  });
  return out;
}

// The one kind a card draws when an id is listed with several (C-2): a
// `record` entry beside another kind is a Start still WAITING for that other
// job (R-8: the robot waits for the same dataset's upload), so the job that
// runs wins; at most one of the others can hold a dataset at a time.
const CARD_BUSY_ORDER = Object.freeze(['upload', 'download', 'edit', 'delete', 'record']);

/** The busy kind a card shows for `kinds` (an id's busyKindsById entry), or null. */
export function cardBusyKind(kinds) {
  const list = Array.isArray(kinds) ? kinds : [];
  return CARD_BUSY_ORDER.find((k) => list.includes(k)) || null;
}

/**
 * What a card must show instead of its ordinary state, or null:
 *   'refreshing'  a busy change of this id is newer than the reply the card
 *                 shows (the re-fetch is on its way) — and, for a card the
 *                 robot reports `in_session`, while no daten_state has come
 *                 yet: never infer „crashed" from a stale reply (T-1 a);
 *   'live'        `in_session` and the robot records into it right now
 *                 (a `record` entry among its busy kinds, in any order);
 *   'crashed'     `in_session`, no recording, from a reply whose request was
 *                 sent after the newest busy change (H-1, U-3).
 */
export function cardPhase(entry, { busyKinds = [], stateSeen = false, stamp, entrySeq }) {
  const pending = stamp !== undefined && stamp !== null && !((entrySeq ?? -Infinity) > stamp);
  if (pending) return 'refreshing';
  if (!entry || entry.state !== 'in_session') return null;
  if (Array.isArray(busyKinds) && busyKinds.includes('record')) return 'live';
  if (!stateSeen) return 'refreshing';
  if (stamp === undefined || stamp === null) return 'refreshing';
  return 'crashed';
}

const dateOf = (v) => {
  const t = v ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : 0;
};

/**
 * The cards of the library, newest first: every local dataset of a listed
 * namespace and every hub entry without a local copy („Nur online").
 * `{id, ns, name, local, hub, sync}`.
 */
export function libraryCards(lib, namespaces) {
  const allowed = new Set(namespaces || []);
  const cards = [];
  const seen = new Set();
  Object.values(lib.local || {}).forEach((e) => {
    const ns = e.ns || String(e.id).split('/')[0];
    if (allowed.size && !allowed.has(ns)) return;
    seen.add(e.id);
    const hub = lib.hub && lib.hub.entries ? lib.hub.entries[e.id] || null : null;
    cards.push({
      id: e.id, ns, name: e.name || String(e.id).split('/')[1], local: e, hub,
      sync: lib.sync[e.id] || NOT_ASKED, date: dateOf(e.modified_at),
    });
  });
  Object.values((lib.hub && lib.hub.entries) || {}).forEach((h) => {
    if (seen.has(h.id)) return;
    const ns = String(h.id).split('/')[0];
    if (allowed.size && !allowed.has(ns)) return;
    cards.push({
      id: h.id, ns, name: String(h.id).split('/')[1], local: null, hub: h,
      sync: lib.sync[h.id] || { state: 'online', reason: null, head: h.head || null }, date: dateOf(h.last_modified),
    });
  });
  cards.sort((a, b) => (b.date - a.date) || a.id.localeCompare(b.id));
  return cards;
}
