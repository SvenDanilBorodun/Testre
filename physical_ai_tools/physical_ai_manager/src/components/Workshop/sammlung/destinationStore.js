/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Ziele and Positionen as DOCUMENT content: the `edubotics-destinations`
 * workspace serializer and the per-workspace store behind it.
 *
 * WHY A SERIALIZER. A student's saved points belong to the workflow they were
 * made in — saved with it, restored with a version, autosaved, cloned. The
 * robot-local `_persisted_destinations` on the server is shared by every
 * student at one rig under automatic names, so it cannot be that home. A
 * workspace serializer rides every path the document already takes
 * (`workspaces.save`/`load`, autosave, version restore) with no second store.
 * It is on the SAVE allowlist (`utils/blocklyPayload.js`) and NOT on RUN:
 * `RunControls` sends `destinations: [{name, kind, x, y, z}]` as an explicit
 * sibling instead, so the server never parses a Blockly internal.
 *
 * FOUR RULES this module is built around:
 *
 *   1. ONE STORE PER WORKSPACE, in a module `WeakMap` — never a module-level
 *      list. Two editors (a page and a template preview, or two test
 *      workspaces) must never share points, and a disposed workspace must not
 *      keep its store alive.
 *   2. `load` IS TOTAL. It drops invalid entries one by one and never throws:
 *      `workspaces.load` aborts on a throwing serializer, `BlocklyWorkspace.jsx`
 *      swallows that to console.error, and the next edit autosaves the
 *      truncated program — the MissingConnection trap documented in
 *      `blocks/control.js`.
 *   3. `save` RETURNS NULL WHEN EMPTY, so a workflow with no Ziele serializes
 *      byte-identically to one saved before this serializer existed.
 *   4. EVERY MUTATION FIRES the undoable `edubotics_destinations_change` event.
 *      A plain JS store emits no Blockly event, so autosave, the unsaved marker,
 *      `handleChange` → the run payload, and Ctrl+Z would all miss a new Ziel.
 *      The event inherits the current event group, so a rename together with
 *      the `destination_ref` field edits undoes as ONE step. `load`/`clear`
 *      never fire: a load must create no undo step and no unsaved marker.
 *
 * NO TOP-LEVEL `Blockly.*` ACCESS. `blocklyPayload.test.js` pins „exactly three
 * CORE serializers before any plugin loads" and many page tests mock
 * `blockly/core` minimally, so importing this module must register nothing and
 * touch no Blockly member. The event class (which `extends
 * Blockly.Events.Abstract`) and the serializer object are built inside
 * `registerDestinationSerializer()`, called from `registerAllBlocksOnce`.
 */

import * as Blockly from 'blockly/core';
import { DE, formatDe } from '../blocks/messages_de';
import { destinationNameErrorDe } from '../blocks/destinations';

export const DESTINATIONS_SERIALIZER_NAME = 'edubotics-destinations';
// VARIABLES 100 > 90 > PROCEDURES 75 > BLOCKS 50: the store is loaded before
// any block, so a block that reads it during its own load already sees it.
export const DESTINATIONS_SERIALIZER_PRIORITY = 90;
export const DESTINATIONS_STATE_VERSION = 1;
export const MAX_DESTINATION_ENTRIES = 64;
// = blocks/destinations.js NAME_MAX_LEN (not exported there). A Ziel name is
// used as a destination_ref block NAME, whose field caps at 24.
export const DESTINATION_NAME_MAX_LEN = 24;
export const DESTINATION_KINDS = Object.freeze(['pin', 'pose']);
export const DESTINATION_SOURCES = Object.freeze(['camera', 'capture', 'sim']);
export const DESTINATION_ROBOT_TYPES = Object.freeze(['omx_f', 'edu6_studio', 'edu1_studio']);
export const DESTINATIONS_CHANGE_EVENT = 'edubotics_destinations_change';

// The server's destination-name alphabet (handlers/destinations.py), capped at
// this editor's 24 characters.
const NAME_RE = /^[A-Za-zÄÖÜäöüß0-9 _-]{1,24}$/;
const NAME_DISALLOWED_RE = /[^A-Za-zÄÖÜäöüß0-9 _-]/g;
const ID_RE = /^d_[0-9a-f]{8}$/;
const CREATED_AT_MAX_LEN = 40;
const JOINTS_MIN = 2;
const JOINTS_MAX = 9;
const JOINT_NAME_MAX_LEN = 40;
// Statement blocks that DEFINE a name inside the program itself.
const NAMED_DESTINATION_BLOCK_TYPES = Object.freeze([
  'edubotics_destination_pin',
  'edubotics_destination_current',
]);

function randomId() {
  const bytes = new Uint8Array(4);
  const c = globalThis.crypto;
  if (c && typeof c.getRandomValues === 'function') {
    c.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return `d_${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`;
}

function isFiniteNumber(v) {
  // typeof first: booleans and numeric strings are refused, not coerced.
  return typeof v === 'number' && Number.isFinite(v);
}

// 4 decimals = 0.1 mm, far below any camera or FK precision. -0 → 0 so the
// saved JSON never reads "-0". Returns NaN when rounding overflows (1.7e308).
function round4(v) {
  const r = Math.round(v * 1e4) / 1e4;
  if (!Number.isFinite(r)) return NaN;
  return Object.is(r, -0) ? 0 : r;
}

function normalizeJoints(joints, jointNames) {
  if (!Array.isArray(joints) || !Array.isArray(jointNames)) return null;
  if (joints.length < JOINTS_MIN || joints.length > JOINTS_MAX) return null;
  if (jointNames.length !== joints.length) return null;
  if (!joints.every(isFiniteNumber)) return null;
  const rounded = joints.map(round4);
  if (!rounded.every(Number.isFinite)) return null;
  const namesOk = jointNames.every(
    (n) => typeof n === 'string' && n.length > 0 && n.length <= JOINT_NAME_MAX_LEN);
  if (!namesOk) return null;
  return { joints: Object.freeze(rounded), jointNames: Object.freeze(jointNames.slice()) };
}

/**
 * One raw entry → a frozen, normalised entry, or null. Total: never throws.
 * Key order is the saved order: id, name, kind, x, y, z, source, robot_type?,
 * created_at?, joints?, joint_names?.
 */
export function normalizeEntry(raw) {
  try {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const name = typeof raw.name === 'string' ? raw.name.trim() : '';
    if (!NAME_RE.test(name)) return null;
    const { kind } = raw;
    if (!DESTINATION_KINDS.includes(kind)) return null;
    const coords = [raw.x, raw.y, raw.z];
    if (!coords.every(isFiniteNumber)) return null;
    const [x, y, z] = coords.map(round4);
    if (![x, y, z].every(Number.isFinite)) return null;
    const id = typeof raw.id === 'string' && ID_RE.test(raw.id) ? raw.id : randomId();
    const source = DESTINATION_SOURCES.includes(raw.source)
      ? raw.source
      : (kind === 'pose' ? 'capture' : 'camera');
    const entry = { id, name, kind, x, y, z, source };
    if (DESTINATION_ROBOT_TYPES.includes(raw.robot_type)) entry.robot_type = raw.robot_type;
    const createdAt = raw.created_at;
    if (typeof createdAt === 'string' && createdAt.length <= CREATED_AT_MAX_LEN
        && !Number.isNaN(Date.parse(createdAt))) {
      entry.created_at = createdAt;
    }
    // joints and joint_names travel together or not at all: a joint vector
    // without its names cannot be matched to an arm.
    const pair = normalizeJoints(raw.joints, raw.joint_names);
    if (pair) {
      entry.joints = pair.joints;
      entry.joint_names = pair.jointNames;
    }
    return Object.freeze(entry);
  } catch (_) {
    return null;
  }
}

// Normalise a list: invalid entries dropped one by one, duplicate NAMES keep
// the first, duplicate ids regenerated, at most MAX_DESTINATION_ENTRIES.
function normalizeList(rawEntries) {
  const out = [];
  const names = new Set();
  const ids = new Set();
  for (const raw of rawEntries) {
    if (out.length >= MAX_DESTINATION_ENTRIES) break;
    const normalized = normalizeEntry(raw);
    if (!normalized || names.has(normalized.name)) continue;
    let entry = normalized;
    if (ids.has(entry.id)) {
      let id = randomId();
      while (ids.has(id)) id = randomId();
      // Spreading keeps `id` in its first position.
      entry = Object.freeze({ ...entry, id });
    }
    names.add(entry.name);
    ids.add(entry.id);
    out.push(entry);
  }
  return out;
}

/** Saved state → a list of normalised entries. Total: never throws. */
export function loadState(state) {
  try {
    if (!state || typeof state !== 'object' || Array.isArray(state)) return [];
    if (state.version !== DESTINATIONS_STATE_VERSION) return [];
    if (!Array.isArray(state.entries)) return [];
    return normalizeList(state.entries);
  } catch (_) {
    return [];
  }
}

/** Entries → the saved state, as fresh plain objects in the canonical key order. */
export function serializeState(entries) {
  const list = Array.isArray(entries) ? entries : [];
  return {
    version: DESTINATIONS_STATE_VERSION,
    entries: list.map((e) => {
      const out = {
        id: e.id, name: e.name, kind: e.kind, x: e.x, y: e.y, z: e.z, source: e.source,
      };
      if (e.robot_type !== undefined) out.robot_type = e.robot_type;
      if (e.created_at !== undefined) out.created_at = e.created_at;
      if (e.joints !== undefined) out.joints = e.joints.slice();
      if (e.joint_names !== undefined) out.joint_names = e.joint_names.slice();
      return out;
    }),
  };
}

// ── the store ───────────────────────────────────────────────────────────────

// Built by registerDestinationSerializer(). Until then a mutation fires no
// Blockly event — which keeps this module import-safe.
let ChangeEventClass = null;

const stores = new WeakMap();

function nameOf(raw) {
  return typeof raw === 'string' ? raw.trim() : '';
}

class DestinationStore {
  constructor(workspace) {
    this.workspace_ = workspace || null;
    this.entries_ = Object.freeze([]);
    this.listeners_ = new Set();
  }

  /**
   * The current entries: a frozen array of frozen entries, stable order. A new
   * array replaces it on every change, so a caller can hold it as a snapshot.
   */
  getEntries() {
    return this.entries_;
  }

  getById(id) {
    return this.entries_.find((e) => e.id === id) || null;
  }

  getByName(name) {
    return this.entries_.find((e) => e.name === name) || null;
  }

  /** fn(entries), called synchronously after every change. Returns unsubscribe. */
  subscribe(fn) {
    if (typeof fn !== 'function') return () => {};
    this.listeners_.add(fn);
    return () => { this.listeners_.delete(fn); };
  }

  /** The load / clear path: replace, notify, fire NO Blockly event. */
  replaceSilently(entries) {
    this.entries_ = Object.freeze(normalizeList(Array.isArray(entries) ? entries : []));
    this.notify_();
  }

  add(input) {
    const src = input && typeof input === 'object' ? input : {};
    const name = nameOf(src.name);
    if (!NAME_RE.test(name)) return { ok: false, error: destinationNameErrorDe(src.name) };
    if (this.getByName(name)) return { ok: false, error: formatDe(DE.ERR_NAME_TAKEN, name) };
    if (![src.x, src.y, src.z].every(isFiniteNumber)) {
      return { ok: false, error: DE.ERR_COORDINATES };
    }
    if (this.entries_.length >= MAX_DESTINATION_ENTRIES) {
      return { ok: false, error: DE.ERR_STORE_FULL };
    }
    const entry = normalizeEntry({
      id: this.freshId_(),
      name,
      kind: src.kind,
      x: src.x,
      y: src.y,
      z: src.z,
      source: src.source,
      robot_type: src.robot_type,
      created_at: new Date().toISOString(),
      joints: src.joints,
      joint_names: src.joint_names,
    });
    // Only a kind outside the closed enum reaches this: every caller passes a
    // literal 'pin'/'pose', so there is no dedicated sentence for it — a place
    // of unknown kind is refused as a place without usable coordinates.
    if (!entry) return { ok: false, error: DE.ERR_COORDINATES };
    this.commit_([...this.entries_, entry]);
    return { ok: true, entry };
  }

  rename(id, rawName) {
    const index = this.entries_.findIndex((e) => e.id === id);
    if (index < 0) return { ok: false, error: DE.ERR_DESTINATION_MISSING };
    const name = nameOf(rawName);
    if (!NAME_RE.test(name)) return { ok: false, error: destinationNameErrorDe(rawName) };
    const current = this.entries_[index];
    const oldName = current.name;
    // Same name: nothing to change, and no empty undo step either.
    if (name === oldName) return { ok: true, entry: current, oldName };
    if (this.getByName(name)) return { ok: false, error: formatDe(DE.ERR_NAME_TAKEN, name) };
    const entry = Object.freeze({ ...current, name });
    const next = this.entries_.slice();
    next[index] = entry;
    this.commit_(next);
    return { ok: true, entry, oldName };
  }

  remove(id) {
    const index = this.entries_.findIndex((e) => e.id === id);
    if (index < 0) return { ok: false, error: DE.ERR_DESTINATION_MISSING };
    const entry = this.entries_[index];
    this.commit_(this.entries_.filter((_, i) => i !== index));
    return { ok: true, entry, index };
  }

  /** Re-insert a removed entry at `index` (the „Rückgängig" toast). */
  restore(entry, index) {
    const normalized = normalizeEntry(entry);
    if (!normalized) return { ok: false, error: DE.ERR_COORDINATES };
    if (this.getByName(normalized.name)) {
      return { ok: false, error: formatDe(DE.ERR_NAME_TAKEN, normalized.name) };
    }
    if (this.entries_.length >= MAX_DESTINATION_ENTRIES) {
      return { ok: false, error: DE.ERR_STORE_FULL };
    }
    const restored = this.getById(normalized.id)
      ? Object.freeze({ ...normalized, id: this.freshId_() })
      : normalized;
    const len = this.entries_.length;
    const at = Number.isInteger(index) ? Math.min(Math.max(index, 0), len) : len;
    this.commit_([...this.entries_.slice(0, at), restored, ...this.entries_.slice(at)]);
    return { ok: true };
  }

  /**
   * Undo/redo path (DestinationsChangeEvent.run). `Workspace.undo` has set
   * recordUndo=false, so the fresh event pushes onto no stack — but it still
   * reaches `handleChange`/`useAutosave`, which must re-serialize.
   */
  applyFromEvent(entries) {
    const prev = this.entries_;
    this.entries_ = Object.freeze(normalizeList(Array.isArray(entries) ? entries : []));
    this.notify_();
    this.fire_(prev, this.entries_);
  }

  commit_(next) {
    const prev = this.entries_;
    this.entries_ = Object.freeze(next);
    this.notify_();
    this.fire_(prev, this.entries_);
  }

  fire_(prev, next) {
    if (!ChangeEventClass || !this.workspace_) return;
    if (!Blockly.Events.isEnabled()) return;
    Blockly.Events.fire(new ChangeEventClass(this.workspace_, prev, next));
  }

  notify_() {
    for (const fn of Array.from(this.listeners_)) {
      try {
        fn(this.entries_);
      } catch (e) {
        // A broken subscriber (a React view) must not abort the mutation that
        // already happened — the store and the event stay consistent.
        console.error('destinationStore subscriber failed:', e);
      }
    }
  }

  freshId_() {
    let id = randomId();
    while (this.getById(id)) id = randomId();
    return id;
  }
}

/** The store of `workspace`, created on first use. */
export function getDestinationStore(workspace) {
  if (!workspace || (typeof workspace !== 'object' && typeof workspace !== 'function')) {
    // No workspace: a detached, empty store that fires nothing and is kept by
    // nobody — never a shared module-level list.
    return new DestinationStore(null);
  }
  let store = stores.get(workspace);
  if (!store) {
    store = new DestinationStore(workspace);
    stores.set(workspace, store);
  }
  return store;
}

// ── helpers ─────────────────────────────────────────────────────────────────

/** The entries a serializer output carries (none when absent or invalid). */
export function readDestinationEntries(blocklyJson) {
  if (!blocklyJson || typeof blocklyJson !== 'object') return [];
  return loadState(blocklyJson[DESTINATIONS_SERIALIZER_NAME]);
}

/**
 * The run-payload sibling: exactly {name, kind, x, y, z} per entry. id, source,
 * robot type, timestamps and joints stay in the editor — the server resolves a
 * destination by NAME and decides plane tracking from `kind` in code.
 */
export function entriesForRunPayload(entries) {
  return (Array.isArray(entries) ? entries : [])
    .filter((e) => e && typeof e === 'object')
    .map(({ name, kind, x, y, z }) => ({ name, kind, x, y, z }));
}

/** Live typing: strip characters outside the alphabet and cap — no trim. */
export function sanitizeDestinationNameInput(raw) {
  return String(raw ?? '').replace(NAME_DISALLOWED_RE, '').slice(0, DESTINATION_NAME_MAX_LEN);
}

/** `'Ziel %1'` → the smallest „Ziel n" (n ≥ 1) not in `takenNames`. */
export function nextAutoName(template, takenNames) {
  let taken;
  try {
    taken = new Set(Array.from(takenNames || []));
  } catch (_) {
    taken = new Set();
  }
  // At most taken.size names can be in the way, so n = taken.size + 1 is
  // always reachable — the bound also ends a template without %1.
  for (let n = 1; n <= taken.size + 1; n += 1) {
    const candidate = formatDe(template, n);
    if (!taken.has(candidate)) return candidate;
  }
  return formatDe(template, taken.size + 1);
}

/**
 * Every name already in use for this workspace: the store's names plus the NAME
 * of every „Ziel setzen"/„Ziel hier merken" statement on the MAIN workspace (a
 * flyout's blocks are offers, not definitions).
 */
export function takenDestinationNames(workspace) {
  const main = workspace && workspace.isFlyout && workspace.targetWorkspace
    ? workspace.targetWorkspace
    : workspace;
  const names = [];
  const seen = new Set();
  const push = (n) => {
    if (n && !seen.has(n)) {
      seen.add(n);
      names.push(n);
    }
  };
  for (const e of getDestinationStore(main).getEntries()) push(e.name);
  if (main && typeof main.getAllBlocks === 'function') {
    for (const block of main.getAllBlocks(false)) {
      if (!NAMED_DESTINATION_BLOCK_TYPES.includes(block.type)) continue;
      push(String(block.getFieldValue('NAME') ?? '').trim());
    }
  }
  return names;
}

// ── registration ────────────────────────────────────────────────────────────

function buildChangeEventClass() {
  class DestinationsChangeEvent extends Blockly.Events.Abstract {
    constructor(workspace, oldEntries, newEntries) {
      super();
      this.isBlank = false;
      this.type = DESTINATIONS_CHANGE_EVENT;
      this.isUiEvent = false;
      if (!workspace) {
        this.isBlank = true;
        return;
      }
      this.workspaceId = workspace.id;
      this.oldEntries = oldEntries;
      this.newEntries = newEntries;
    }

    toJson() {
      return { ...super.toJson(), oldEntries: this.oldEntries, newEntries: this.newEntries };
    }

    static fromJson(json, workspace, event) {
      const e = super.fromJson(json, workspace, event ?? new DestinationsChangeEvent());
      e.oldEntries = json.oldEntries;
      e.newEntries = json.newEntries;
      return e;
    }

    run(forward) {
      getDestinationStore(this.getEventWorkspace_())
        .applyFromEvent(forward ? this.newEntries : this.oldEntries);
    }
  }
  return DestinationsChangeEvent;
}

/**
 * Register the serializer and the change event. Idempotent (a second call —
 * React StrictMode is off, but a remount or HMR re-runs registration — does
 * nothing). Called from `BlocklyWorkspace.jsx::registerAllBlocksOnce`.
 */
export function registerDestinationSerializer() {
  if (!ChangeEventClass) ChangeEventClass = buildChangeEventClass();
  const { registry } = Blockly;
  if (!registry.hasItem(registry.Type.EVENT, DESTINATIONS_CHANGE_EVENT)) {
    registry.register(registry.Type.EVENT, DESTINATIONS_CHANGE_EVENT, ChangeEventClass);
  }
  if (!registry.hasItem(registry.Type.SERIALIZER, DESTINATIONS_SERIALIZER_NAME)) {
    Blockly.serialization.registry.register(DESTINATIONS_SERIALIZER_NAME, {
      priority: DESTINATIONS_SERIALIZER_PRIORITY,
      save(workspace) {
        const entries = getDestinationStore(workspace).getEntries();
        return entries.length ? serializeState(entries) : null;
      },
      load(state, workspace) {
        // loadState is total; the catch is the belt for a store that cannot
        // even be reached — a throw here would truncate the next autosave.
        try {
          getDestinationStore(workspace).replaceSilently(loadState(state));
        } catch (e) {
          console.error('edubotics-destinations load failed:', e);
        }
      },
      clear(workspace) {
        try {
          getDestinationStore(workspace).replaceSilently([]);
        } catch (e) {
          console.error('edubotics-destinations clear failed:', e);
        }
      },
    });
  }
}
