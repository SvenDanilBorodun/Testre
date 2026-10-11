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
 * The Sammlung drawer's commands: rename and delete a recording, a Ziel/
 * Position and a variable, plus the usage rows the drawer lists.
 *
 * Two rules hold for every command here:
 * - No recording/Ziel/Position command deletes a block. Deleting one leaves
 *   every block that names it standing; the keyed missing-reference warning
 *   then explains it. The one exception is Blockly's own „Variable löschen",
 *   which removes the variable's uses behind Blockly's own confirmation — the
 *   same thing its context menu has always done.
 * - A rename rewrites the blocks that name the asset inside ONE event group,
 *   so a single „Rückgängig" restores all of them (the caller owns the group
 *   for the two rewrite helpers; the commands open their own).
 *
 * Pure of React and Redux: the drawer passes the workspace, the API module and
 * the page's `saveWorkflowNow`, so every path is testable headless.
 */

import * as Blockly from 'blockly/core';
import { DE, formatDe } from '../blocks/messages_de';
import { REPLAY_BLOCK_TYPE } from '../blocks/trajectories';
import { getDestinationStore } from './destinationStore';
import { isDisplayableVariableName } from '../../../utils/variableName';

const REF_TYPE = 'edubotics_destination_ref';

// Mirrors the cloud's trajectory-name rule (1–40 chars of this alphabet).
export const RECORDING_NAME_RE = /^[A-Za-zÄÖÜäöüß0-9 _-]{1,40}$/;

export const USAGE_LABEL_MAX_CHARS = 60;

function mainBlocks(workspace) {
  if (!workspace || typeof workspace.getAllBlocks !== 'function' || workspace.isFlyout) return [];
  try {
    return workspace.getAllBlocks(false).filter((b) => b && !b.isInFlyout);
  } catch (_) {
    return [];
  }
}

function trimmedName(block) {
  try {
    const raw = block.getFieldValue('NAME');
    return typeof raw === 'string' ? raw.trim() : '';
  } catch (_) {
    return '';
  }
}

function isRunnable(block) {
  try {
    return block.isEnabled() && !block.getInheritedDisabled();
  } catch (_) {
    return false;
  }
}

function rewriteBlocksOfType(workspace, type, fromName, toName) {
  const from = String(fromName ?? '').trim();
  if (!from) return 0;
  let count = 0;
  // Disabled blocks too: a switched-off block is still part of the program and
  // would otherwise point at a name that no longer exists once re-enabled.
  for (const block of mainBlocks(workspace)) {
    if (block.type !== type || trimmedName(block) !== from) continue;
    block.setFieldValue(toName, 'NAME');
    count += 1;
  }
  return count;
}

/** Rename every main-workspace replay block of `fromName`; the caller owns the event group. */
export function rewriteReplayBlocks(workspace, fromName, toName) {
  return rewriteBlocksOfType(workspace, REPLAY_BLOCK_TYPE, fromName, toName);
}

/** Rename every main-workspace destination reference of `fromName`; the caller owns the event group. */
export function rewriteRefBlocks(workspace, fromName, toName) {
  return rewriteBlocksOfType(workspace, REF_TYPE, fromName, toName);
}

// The hats a program starts by (interpreter.py::HAT_BLOCK_TYPES).
const HAT_TYPES = Object.freeze([
  'edubotics_when_broadcast',
  'edubotics_when_object_seen',
  'edubotics_when_counter_gt',
]);
const DEFINITION_TYPES = Object.freeze(['procedures_defnoreturn', 'procedures_defreturn']);
const HAT_TEXT_MAX_CHARS = 40;

const cut = (text, max) => (text.length > max ? `${text.slice(0, max)}…` : text);

function safeText(block) {
  try {
    return String(block.toString());
  } catch (_) {
    return '';
  }
}

function isHat(block) {
  return !!block && !block.previousConnection && !!block.nextConnection && HAT_TYPES.includes(block.type);
}

function isDefinition(block) {
  return !!block && DEFINITION_TYPES.includes(block.type);
}

// A block and every block below it on the same stack: [first, …next].
function chainFrom(first) {
  const chain = [];
  let cur = first;
  while (cur && !chain.includes(cur)) {
    chain.push(cur);
    cur = cur.getNextBlock();
  }
  return chain;
}

// The name of the stack `root` heads, and the chain of its steps.
function stackOf(root, workspace) {
  if (isDefinition(root)) {
    let name = '';
    try {
      name = String(root.getFieldValue('NAME') || '');
    } catch (_) {
      name = '';
    }
    const body = root.getInputTargetBlock ? root.getInputTargetBlock('STACK') : null;
    return { name: formatDe(DE.USAGE_STACK_FUNCTION, name), chain: chainFrom(body) };
  }
  if (isHat(root)) {
    return {
      name: formatDe(DE.USAGE_STACK_HAT, cut(safeText(root), HAT_TEXT_MAX_CHARS)),
      chain: chainFrom(root.getNextBlock()),
    };
  }
  // A plain statement stack: the k-th of the program's (creation = run order).
  let stacks = [];
  try {
    stacks = (workspace ? workspace.getTopBlocks(false) : [])
      .filter((b) => b && !b.outputConnection && !isHat(b) && !isDefinition(b));
  } catch (_) {
    stacks = [];
  }
  const k = stacks.indexOf(root) + 1;
  const name = stacks.length === 1 || k === 0 ? DE.USAGE_STACK_MAIN : formatDe(DE.USAGE_STACK_PART, k);
  return { name, chain: chainFrom(root) };
}

/**
 * WHERE a block uses an asset (owner decision R0-7): „Schritt N in
 * Hauptprogramm" / „… in Programmteil k" / „… in Funktion „tanzen“" / „… in
 * „<text of the hat>“", „in <stack>" for a use outside the stack's own steps
 * (e.g. inside a definition's return value), „liegt lose auf der
 * Arbeitsfläche" for a loose value block. The frame carries no quotes; a name
 * carries its own.
 */
export function usageLabel(block) {
  let root = block;
  try {
    root = block.getRootBlock() || block;
  } catch (_) {
    root = block;
  }
  if (root && root.outputConnection) return DE.USAGE_LOOSE;
  let label = '';
  try {
    const { name, chain } = stackOf(root, block.workspace);
    let step = block;
    while (step && !chain.includes(step)) step = step.getParent();
    label = step ? formatDe(DE.USAGE_STEP_IN, chain.indexOf(step) + 1, name) : formatDe(DE.USAGE_IN, name);
  } catch (_) {
    label = safeText(root);
  }
  return cut(label, USAGE_LABEL_MAX_CHARS);
}

/**
 * The drawer's „Benutzt in" rows. `kind`: 'recording' (replay blocks by name),
 * 'pin' / 'pose' / 'ref' (reference blocks by name) or 'variable' (uses by id).
 * @returns {{blockId:string, label:string, disabled:boolean}[]}
 */
export function usageRows(workspace, kind, key) {
  if (!workspace || !key) return [];
  let blocks = [];
  if (kind === 'variable') {
    try {
      blocks = (Blockly.Variables.getVariableUsesById(workspace, key) || [])
        .filter((b) => b && !b.isInFlyout);
    } catch (_) {
      blocks = [];
    }
  } else {
    const type = kind === 'recording' ? REPLAY_BLOCK_TYPE : REF_TYPE;
    const name = String(key).trim();
    blocks = mainBlocks(workspace).filter((b) => b.type === type && trimmedName(b) === name);
  }
  return blocks.map((block) => ({
    blockId: block.id,
    label: usageLabel(block),
    disabled: !isRunnable(block),
  }));
}

// ── recordings (cloud rows) ────────────────────────────────────────────────

function messageOf(err) {
  if (!err) return '';
  if (typeof err === 'string') return err;
  if (typeof err.message === 'string' && err.message) return err.message;
  try {
    return String(err);
  } catch (_) {
    return '';
  }
}

function timeOf(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

function rowsNamed(items, name) {
  return (Array.isArray(items) ? items : [])
    .filter((it) => it && typeof it === 'object' && it.name === name);
}

// Newest by created_at; equal or unparsable stamps keep API order (newest first).
function newestRow(rows) {
  let best = null;
  for (const row of rows) {
    if (!best || timeOf(row.created_at) > timeOf(best.created_at)) best = row;
  }
  return best;
}

function stampOf(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? null : t;
}

/**
 * Whether re-creating `deleted` keeps the take that plays. The cloud stamps a
 * re-created row NOW (its insert takes no created_at), and a replay plays the
 * NEWEST row of a name — so a restore is right only when every row of that
 * name still in the cloud (`remaining`) is OLDER than every copy restored.
 * Undoing the delete of an older version is never right: the played take is
 * newer, and the restore would silently make the old take the one that plays.
 * An unreadable stamp proves nothing and answers false.
 */
export function restoreKeepsPlayedTake(deleted, remaining) {
  const copies = (Array.isArray(deleted) ? deleted : []).filter((d) => d && typeof d === 'object');
  if (copies.length === 0) return false;
  const oldest = new Map();
  for (const d of copies) {
    const t = stampOf(d.created_at);
    if (t === null) return false;
    if (!oldest.has(d.name) || t < oldest.get(d.name)) oldest.set(d.name, t);
  }
  for (const row of Array.isArray(remaining) ? remaining : []) {
    if (!row || typeof row !== 'object' || !oldest.has(row.name)) continue;
    const t = stampOf(row.created_at);
    if (t === null || t >= oldest.get(row.name)) return false;
  }
  return true;
}

function rewriteReplayGrouped(workspace, fromName, toName) {
  Blockly.Events.setGroup(true);
  try {
    return rewriteReplayBlocks(workspace, fromName, toName);
  } finally {
    Blockly.Events.setGroup(false);
  }
}

/**
 * Rename a recording (every cloud row of that name), then every replay block
 * that plays it, then save the document. The CLOUD is renamed first and, when
 * the save fails, compensated first: the blocks go back only once the cloud
 * says `fromName` again. When that compensation fails too the blocks KEEP the
 * cloud's new name — then only the saved document is behind, and saving again
 * (the advice in ERR_RENAME_SPLIT) really fixes it.
 *
 * WHAT is rewritten is the caller's: `workspace` (its replay blocks, in one
 * event group — the Blockly document) or `rewrite(from, to)` (a code program's
 * `replay("…")` string arguments — code/codeAssetDocument.js). The order above
 * is the same for both.
 *
 * Replacing a clashing recording deletes its rows FIRST (fetched before the
 * delete, like deleteRecordingRows) and re-creates them whenever the rename does
 * not end ok but the cloud is back at `fromName` — „nothing else changed" has to
 * include the recording the student agreed to replace. If that restore fails
 * too, the error says the replaced recording is gone.
 *
 * @returns {Promise<{ok:boolean, error?:string, cancelled?:boolean, persistent?:boolean}>}
 */
export async function renameRecording({
  workspace, rewrite, api, accessToken, workflowId, fromName, toName, items, saveWorkflowNow, confirmReplace,
}) {
  const rewriteRefs = typeof rewrite === 'function'
    ? rewrite
    : (from, to) => rewriteReplayGrouped(workspace, from, to);
  // Trimmed FIRST: the cloud stores the trimmed form, so an untrimmed block
  // NAME would never match the renamed row.
  const to = String(toName ?? '').trim();
  if (!RECORDING_NAME_RE.test(to)) return { ok: false, error: DE.ERR_RECORDING_NAME };
  if (to === fromName) return { ok: true };
  const newest = newestRow(rowsNamed(items, fromName));
  if (!newest) return { ok: false, error: formatDe(DE.ERR_RENAME_FAILED, 'Bewegung nicht gefunden.') };

  let replaced = [];
  const giveBackReplaced = async (failure) => {
    if (replaced.length === 0) return failure;
    const back = await restoreRecordingRows({ api, accessToken, workflowId, deleted: replaced });
    if (back.ok) return failure;
    return { ...failure, error: `${failure.error} ${formatDe(DE.ERR_REPLACED_LOST, to)}` };
  };

  const clashes = rowsNamed(items, to);
  if (clashes.length > 0) {
    const replace = await confirmReplace(to);
    if (!replace) return { ok: false, cancelled: true };
    const removed = await deleteRecordingRows({ api, accessToken, workflowId, rows: clashes });
    replaced = removed.deleted;
    if (!removed.ok) return giveBackReplaced({ ok: false, error: removed.error });
  }

  try {
    await api.renameTrajectory(accessToken, workflowId, newest.id, to);
  } catch (err) {
    return giveBackReplaced({ ok: false, error: formatDe(DE.ERR_RENAME_FAILED, messageOf(err)) });
  }

  rewriteRefs(fromName, to);

  let saved = null;
  try {
    // The drawer reports a failure itself (ERR_RENAME_FAILED carries the reason).
    saved = await saveWorkflowNow({ toastOnSuccess: false, toastOnError: false });
  } catch (err) {
    saved = { ok: false, error: err };
  }
  if (saved && saved.ok) return { ok: true };

  const saveError = messageOf(saved && saved.error) || '—';
  try {
    await api.renameTrajectory(accessToken, workflowId, newest.id, fromName);
  } catch (_) {
    // Cloud = `to`, blocks = `to`; only the saved document is behind.
    return { ok: false, persistent: true, error: formatDe(DE.ERR_RENAME_SPLIT, to) };
  }
  rewriteRefs(to, fromName);
  return giveBackReplaced({ ok: false, error: formatDe(DE.ERR_RENAME_FAILED, saveError) });
}

/**
 * Delete recording rows. Every row is FETCHED before anything is deleted — the
 * full samples are what „Rückgängig" re-creates — so a failed fetch deletes
 * nothing at all.
 * @returns {Promise<{ok:boolean, deleted:object[], failed:object[], error?:string}>}
 */
export async function deleteRecordingRows({ api, accessToken, workflowId, rows }) {
  const list = (Array.isArray(rows) ? rows : []).filter((r) => r && r.id);
  const kept = [];
  for (const row of list) {
    try {
      const full = await api.getTrajectory(accessToken, workflowId, row.id);
      const samples = full && full.samples && typeof full.samples === 'object' ? full.samples : {};
      kept.push({
        row,
        copy: {
          name: (full && full.name) || row.name,
          fps: samples.fps ?? (full && full.fps) ?? row.fps,
          points: Array.isArray(samples.points) ? samples.points : [],
          duration_s: (full && full.duration_s) ?? row.duration_s ?? null,
          robot_profile: (full && full.robot_profile) ?? row.robot_profile ?? null,
          created_at: (full && full.created_at) || row.created_at || null,
        },
      });
    } catch (err) {
      return {
        ok: false, deleted: [], failed: list, error: formatDe(DE.ERR_DELETE_FAILED, messageOf(err)),
      };
    }
  }
  const deleted = [];
  const failed = [];
  let error;
  for (const { row, copy } of kept) {
    try {
      await api.deleteTrajectory(accessToken, workflowId, row.id);
      deleted.push(copy);
    } catch (err) {
      failed.push(row);
      if (!error) error = formatDe(DE.ERR_DELETE_FAILED, messageOf(err));
    }
  }
  return { ok: failed.length === 0, deleted, failed, ...(error ? { error } : {}) };
}

/**
 * Re-create deleted rows, OLDEST first, so the newest version is the newest
 * row again (by-name newest-wins picks the same take as before the delete).
 * Re-created rows are stamped NOW, so the current cloud list is read FIRST and
 * nothing is created when a row of the same name that is not older than every
 * copy is still there (an older version was deleted, a newest delete failed,
 * or a new take was recorded meanwhile) — see restoreKeepsPlayedTake.
 * A 429 (more than 10 versions inside the cloud's per-minute POST budget) is
 * waited out and the same version re-tried; any other failure stops the restore
 * and the German error names how many versions came back and how many did not.
 * @returns {Promise<{ok:boolean, restored:number, error?:string}>}
 */
export const RESTORE_RATE_LIMIT_WAIT_MS = 15000;
export const RESTORE_RATE_LIMIT_RETRIES = 5;

const defaultSleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

export async function restoreRecordingRows({
  api, accessToken, workflowId, deleted, sleep = defaultSleep, onRateLimited,
}) {
  const list = (Array.isArray(deleted) ? deleted : [])
    .filter((d) => d && typeof d === 'object')
    .map((d, index) => ({ d, index }))
    // Oldest first; equal stamps keep reverse input order (the input is newest first).
    .sort((a, b) => (timeOf(a.d.created_at) - timeOf(b.d.created_at)) || (b.index - a.index))
    .map(({ d }) => d);
  if (list.length === 0) return { ok: true, restored: 0 };
  let current;
  try {
    current = await api.listTrajectories(accessToken, workflowId);
  } catch (err) {
    return { ok: false, restored: 0, error: formatDe(DE.ERR_UNDO_FAILED, messageOf(err) || '—') };
  }
  if (!Array.isArray(current)) {
    return { ok: false, restored: 0, error: formatDe(DE.ERR_UNDO_FAILED, '—') };
  }
  if (!restoreKeepsPlayedTake(list, current)) {
    return { ok: false, restored: 0, error: formatDe(DE.ERR_UNDO_NEWER_VERSION, list[0].name) };
  }
  let restored = 0;
  for (const d of list) {
    const payload = {
      name: d.name,
      fps: d.fps,
      points: d.points,
      point_count: Array.isArray(d.points) ? d.points.length : undefined,
      ...(Number.isFinite(d.duration_s) ? { duration_s: d.duration_s } : {}),
      ...(d.robot_profile ? { robot_profile: d.robot_profile } : {}),
    };
    let lastErr = null;
    for (let attempt = 0; attempt <= RESTORE_RATE_LIMIT_RETRIES; attempt += 1) {
      try {
        await api.createTrajectory(accessToken, workflowId, payload);
        lastErr = null;
        break;
      } catch (err) {
        lastErr = err;
        // The cloud holds POST /workflows/** at 10/min per user over a
        // SLIDING window that does not count refusals, so a 429 always clears
        // within a minute: wait and re-try THIS version rather than skipping it.
        if (!(err && err.status === 429) || attempt === RESTORE_RATE_LIMIT_RETRIES) break;
        if (typeof onRateLimited === 'function') onRateLimited();
        await sleep(RESTORE_RATE_LIMIT_WAIT_MS);
      }
    }
    if (lastErr) {
      // STOP at the first version that could not be re-created: the copies run
      // oldest → newest, so creating a later one after a gap would still not
      // bring back what is missing, and the student has to be told how much.
      const reason = messageOf(lastErr) || '—';
      if (restored === 0) {
        return { ok: false, restored, error: formatDe(DE.ERR_UNDO_FAILED, reason) };
      }
      return {
        ok: false,
        restored,
        error: formatDe(DE.ERR_UNDO_PARTIAL, restored, list.length, list.length - restored, reason),
      };
    }
    restored += 1;
  }
  return { ok: true, restored };
}

// ── Ziele / Positionen (the document's destination store) ──────────────────

/** Rename a store entry and every reference block of its old name: ONE undo step. */
export function renamePlace({ workspace, entryId, toName }) {
  const store = getDestinationStore(workspace);
  Blockly.Events.setGroup(true);
  try {
    const result = store.rename(entryId, toName);
    if (result.ok && result.oldName !== result.entry.name) {
      rewriteRefBlocks(workspace, result.oldName, result.entry.name);
    }
    return result;
  } finally {
    Blockly.Events.setGroup(false);
  }
}

/** Remove a store entry. Blocks naming it stay and get the missing-name warning. */
export function deletePlace({ workspace, entryId }) {
  return getDestinationStore(workspace).remove(entryId);
}

// ── variables ──────────────────────────────────────────────────────────────

function variableById(workspace, variableId) {
  try {
    return workspace.getVariableMap().getVariableById(variableId) || null;
  } catch (_) {
    return null;
  }
}

/**
 * Rename a variable. Blockly matches variable names CASE-INSENSITIVELY and its
 * `renameVariable` silently MERGES into an existing variable of that name, so
 * a name another variable already holds (in any case) is refused first.
 */
export function renameVariable({ workspace, variableId, toName }) {
  const variable = variableById(workspace, variableId);
  if (!variable) return { ok: false, error: DE.ERR_DESTINATION_MISSING };
  // Trimmed FIRST and used for BOTH the clash check and the rename: Blockly's
  // lookup ignores case but not surrounding whitespace.
  const to = String(toName ?? '').trim();
  if (!to || !isDisplayableVariableName(to)) {
    return { ok: false, error: formatDe(DE.ERR_RENAME_FAILED, `„${to}"`) };
  }
  if (to === variable.getName()) return { ok: true };
  const map = workspace.getVariableMap();
  const existing = map.getVariable(to, variable.getType());
  if (existing && existing.getId() !== variable.getId()) {
    return { ok: false, error: formatDe(DE.ERR_NAME_TAKEN, to) };
  }
  map.renameVariable(variable, to);
  return { ok: true };
}

/** Delete a variable through Blockly (its own German confirmation; deletes its uses, as always). */
export function deleteVariable({ workspace, variableId }) {
  const variable = variableById(workspace, variableId);
  if (!variable) return { ok: false, error: DE.ERR_DESTINATION_MISSING };
  Blockly.Variables.deleteVariable(workspace, variable);
  return { ok: true };
}
