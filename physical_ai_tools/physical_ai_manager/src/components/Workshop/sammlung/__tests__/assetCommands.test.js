/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/*
 * The Sammlung drawer's commands against a real headless Blockly workspace
 * (real blocks, real event groups, real undo stack) and an `api` of vi.fns.
 */

import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { DE, formatDe } from '../../blocks/messages_de';
import { getDestinationStore, registerDestinationSerializer } from '../destinationStore';
import {
  RECORDING_NAME_RE,
  rewriteReplayBlocks,
  rewriteRefBlocks,
  usageRows,
  renameRecording,
  deleteRecordingRows,
  restoreKeepsPlayedTake,
  restoreRecordingRows,
  renamePlace,
  deletePlace,
  renameVariable,
  deleteVariable,
} from '../assetCommands';

// Blockly dispatches events via requestAnimationFrame(() => setTimeout(fireNow,
// 0)); a bare setTimeout(0) delivers nothing. Same helper shape as
// blocks/__tests__/foreverDeadCode.test.js.
const flushEvents = async () => {
  await new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => { setTimeout(resolve, 0); });
    } else {
      setTimeout(resolve, 0);
    }
  });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};

let ws;

beforeAll(() => {
  Blockly.setLocale(De);
  registerTrajectoryBlocks();
  registerDestinationBlocks();
  registerDestinationSerializer();
});

afterEach(() => {
  vi.restoreAllMocks();
  if (ws) ws.dispose();
  ws = null;
});

const replay = (name, id) => ({
  type: 'edubotics_replay_trajectory', ...(id ? { id } : {}), fields: { NAME: name },
});
const ref = (name, id) => ({
  type: 'edubotics_destination_ref', ...(id ? { id } : {}), fields: { NAME: name },
});

function load(blocks, variables) {
  ws = new Blockly.Workspace();
  Blockly.serialization.workspaces.load({
    blocks: { languageVersion: 0, blocks: blocks.map((b, i) => ({ x: 0, y: i * 100, ...b })) },
    ...(variables ? { variables } : {}),
  }, ws);
  return ws;
}

const names = (type) => ws.getAllBlocks(false)
  .filter((b) => b.type === type)
  .map((b) => b.getFieldValue('NAME'));

function makeApi(overrides = {}) {
  return {
    renameTrajectory: vi.fn(async () => ({})),
    deleteTrajectory: vi.fn(async () => ({})),
    getTrajectory: vi.fn(async () => ({})),
    createTrajectory: vi.fn(async () => ({})),
    listTrajectories: vi.fn(async () => []),
    ...overrides,
  };
}

const ITEMS = [
  { id: 't3', name: 'Winken', created_at: '2026-09-13T10:30:00Z' },
  { id: 't1', name: 'Winken', created_at: '2026-09-13T10:00:00Z' },
  { id: 't9', name: 'Tanz', created_at: '2026-09-13T09:00:00Z' },
];

function renameArgs(api, extra = {}) {
  return {
    workspace: ws,
    api,
    accessToken: 'tok',
    workflowId: 'wf1',
    fromName: 'Winken',
    toName: 'Greifen',
    items: ITEMS,
    saveWorkflowNow: vi.fn(async () => ({ ok: true })),
    confirmReplace: vi.fn(async () => true),
    ...extra,
  };
}

describe('rewrite helpers', () => {
  it('rewrite covers disabled blocks and ignores other names', () => {
    load([replay('Winken', 'a'), replay(' Winken ', 'b'), replay('Tanz', 'c'), ref('Winken', 'r')]);
    ws.getBlockById('b').setDisabledReason(true, 'test');
    expect(rewriteReplayBlocks(ws, 'Winken', 'Greifen')).toBe(2);
    expect(ws.getBlockById('a').getFieldValue('NAME')).toBe('Greifen');
    expect(ws.getBlockById('b').getFieldValue('NAME')).toBe('Greifen');
    expect(ws.getBlockById('c').getFieldValue('NAME')).toBe('Tanz');
    // A reference of the same name is a different asset.
    expect(ws.getBlockById('r').getFieldValue('NAME')).toBe('Winken');
    expect(rewriteRefBlocks(ws, 'Winken', 'Ablage')).toBe(1);
    expect(ws.getBlockById('r').getFieldValue('NAME')).toBe('Ablage');
  });

  it('one ws.undo(false) restores every rewritten field', async () => {
    load([replay('Winken', 'a'), replay('Winken', 'b'), replay('Winken', 'c')]);
    await flushEvents();
    Blockly.Events.setGroup(true);
    rewriteReplayBlocks(ws, 'Winken', 'Greifen');
    Blockly.Events.setGroup(false);
    await flushEvents();
    ws.undo(false);
    await flushEvents();
    expect(names('edubotics_replay_trajectory')).toEqual(['Winken', 'Winken', 'Winken']);
  });

  it('usageRows lists replay uses with the root block label and the disabled flag', () => {
    load([
      replay('Winken', 'a'),
      { type: 'controls_repeat_ext', id: 'loop', inputs: { DO: { block: replay('Winken', 'inner') } } },
      replay('Tanz', 'c'),
    ]);
    ws.getBlockById('loop').setDisabledReason(true, 'test');
    const rows = usageRows(ws, 'recording', 'Winken');
    expect(rows.map((r) => r.blockId)).toEqual(['a', 'inner']);
    expect(rows[0].disabled).toBe(false);
    expect(rows[1].disabled).toBe(true);   // inherited from the disabled loop
    expect(rows[1].label).toBe(ws.getBlockById('loop').toString().slice(0, 60)
      + (ws.getBlockById('loop').toString().length > 60 ? '…' : ''));
    expect(rows.every((r) => r.label.length <= 61)).toBe(true);
  });
});

describe('renameRecording', () => {
  it('renames the cloud, then rewrites the blocks, then saves', async () => {
    load([replay('Winken', 'a'), replay('Winken', 'b')]);
    const api = makeApi();
    const spy = vi.spyOn(Blockly.Block.prototype, 'setFieldValue');
    const args = renameArgs(api);
    const result = await renameRecording(args);
    expect(result).toEqual({ ok: true });
    expect(api.renameTrajectory).toHaveBeenCalledTimes(1);
    // The NEWEST row of the name is the one the cloud renames (all versions follow).
    expect(api.renameTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't3', 'Greifen');
    const renameOrder = api.renameTrajectory.mock.invocationCallOrder[0];
    const writeOrders = spy.mock.invocationCallOrder;
    const saveOrder = args.saveWorkflowNow.mock.invocationCallOrder[0];
    expect(writeOrders.length).toBe(2);
    expect(renameOrder).toBeLessThan(Math.min(...writeOrders));
    expect(Math.max(...writeOrders)).toBeLessThan(saveOrder);
    expect(args.saveWorkflowNow).toHaveBeenCalledWith({ toastOnSuccess: false, toastOnError: false });
    expect(names('edubotics_replay_trajectory')).toEqual(['Greifen', 'Greifen']);
  });

  it('the replace flow deletes the clashing rows before renaming', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi();
    const args = renameArgs(api, { toName: 'Tanz' });
    const result = await renameRecording(args);
    expect(result).toEqual({ ok: true });
    expect(args.confirmReplace).toHaveBeenCalledWith('Tanz');
    expect(api.deleteTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't9');
    expect(api.deleteTrajectory.mock.invocationCallOrder[0])
      .toBeLessThan(api.renameTrajectory.mock.invocationCallOrder[0]);
  });

  it('a replace whose rename then fails re-creates the replaced recording', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi({
      getTrajectory: vi.fn(async () => ({
        name: 'Tanz', samples: { fps: 25, points: [[0, 0, 0, 0, 0, 0.8, 0]] }, created_at: '2026-09-13T09:00:00Z',
      })),
      renameTrajectory: vi.fn(async () => { throw new Error('offline'); }),
      listTrajectories: vi.fn(async () => [ITEMS[0], ITEMS[1]]),
    });
    const result = await renameRecording(renameArgs(api, { toName: 'Tanz' }));
    expect(result).toEqual({ ok: false, error: formatDe(DE.ERR_RENAME_FAILED, 'offline') });
    expect(api.deleteTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't9');
    expect(api.createTrajectory).toHaveBeenCalledTimes(1);
    expect(api.createTrajectory.mock.calls[0][2]).toMatchObject({ name: 'Tanz', fps: 25 });
    expect(names('edubotics_replay_trajectory')).toEqual(['Winken']);
  });

  it('a replace whose save fails re-creates the replaced recording after the cloud compensation', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi({
      getTrajectory: vi.fn(async () => ({ name: 'Tanz', samples: { fps: 25, points: [] }, created_at: '2026-09-13T09:00:00Z' })),
    });
    const args = renameArgs(api, {
      toName: 'Tanz',
      saveWorkflowNow: vi.fn(async () => ({ ok: false, error: new Error('Netzwerkfehler') })),
    });
    const result = await renameRecording(args);
    expect(result).toEqual({ ok: false, error: formatDe(DE.ERR_RENAME_FAILED, 'Netzwerkfehler') });
    expect(api.renameTrajectory.mock.calls.map((c) => c[3])).toEqual(['Tanz', 'Winken']);
    expect(api.createTrajectory.mock.invocationCallOrder[0])
      .toBeGreaterThan(api.renameTrajectory.mock.invocationCallOrder[1]);
  });

  it('a replaced recording that cannot be re-created is named in the error', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi({
      getTrajectory: vi.fn(async () => ({ name: 'Tanz', samples: { fps: 25, points: [] }, created_at: '2026-09-13T09:00:00Z' })),
      renameTrajectory: vi.fn(async () => { throw new Error('offline'); }),
      createTrajectory: vi.fn(async () => { throw new Error('offline'); }),
    });
    const result = await renameRecording(renameArgs(api, { toName: 'Tanz' }));
    expect(result.ok).toBe(false);
    expect(result.error).toBe(
      `${formatDe(DE.ERR_RENAME_FAILED, 'offline')} ${formatDe(DE.ERR_REPLACED_LOST, 'Tanz')}`);
  });

  it('a declined replace makes zero API calls and changes nothing', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi();
    const args = renameArgs(api, { toName: 'Tanz', confirmReplace: vi.fn(async () => false) });
    const result = await renameRecording(args);
    expect(result).toEqual({ ok: false, cancelled: true });
    Object.values(api).forEach((fn) => expect(fn).not.toHaveBeenCalled());
    expect(args.saveWorkflowNow).not.toHaveBeenCalled();
    expect(names('edubotics_replay_trajectory')).toEqual(['Winken']);
  });

  it('a failed save compensates the CLOUD before the blocks go back', async () => {
    load([replay('Winken', 'a'), replay('Winken', 'b')]);
    const api = makeApi();
    const args = renameArgs(api, {
      saveWorkflowNow: vi.fn(async () => ({ ok: false, error: new Error('Netzwerkfehler') })),
    });
    const spy = vi.spyOn(Blockly.Block.prototype, 'setFieldValue');
    const result = await renameRecording(args);
    expect(result).toEqual({ ok: false, error: formatDe(DE.ERR_RENAME_FAILED, 'Netzwerkfehler') });
    expect(api.renameTrajectory).toHaveBeenCalledTimes(2);
    expect(api.renameTrajectory.mock.calls[1]).toEqual(['tok', 'wf1', 't3', 'Winken']);
    const compensation = api.renameTrajectory.mock.invocationCallOrder[1];
    const backWrites = spy.mock.calls
      .map((call, i) => ({ value: call[0], order: spy.mock.invocationCallOrder[i] }))
      .filter((c) => c.value === 'Winken');
    expect(backWrites.length).toBe(2);
    backWrites.forEach((w) => expect(compensation).toBeLessThan(w.order));
    expect(names('edubotics_replay_trajectory')).toEqual(['Winken', 'Winken']);
  });

  it('a failed save AND a failed compensation keep the blocks at the cloud name', async () => {
    load([replay('Winken', 'a'), replay('Winken', 'b')]);
    let calls = 0;
    const api = makeApi({
      renameTrajectory: vi.fn(async () => {
        calls += 1;
        if (calls > 1) throw new Error('offline');
        return {};
      }),
    });
    const args = renameArgs(api, {
      toName: 'Tanz',
      items: ITEMS.filter((it) => it.name === 'Winken'),
      saveWorkflowNow: vi.fn(async () => ({ ok: false })),
    });
    const result = await renameRecording(args);
    expect(result.ok).toBe(false);
    expect(result.persistent).toBe(true);
    expect(result.error).toBe(formatDe(DE.ERR_RENAME_SPLIT, 'Tanz'));
    expect(names('edubotics_replay_trajectory')).toEqual(['Tanz', 'Tanz']);
  });

  it('an invalid name makes no API call', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi();
    for (const bad of ['', '   ', 'A!', 'x'.repeat(41)]) {
      // eslint-disable-next-line no-await-in-loop
      const result = await renameRecording(renameArgs(api, { toName: bad }));
      expect(result).toEqual({ ok: false, error: DE.ERR_RECORDING_NAME });
    }
    Object.values(api).forEach((fn) => expect(fn).not.toHaveBeenCalled());
    expect(RECORDING_NAME_RE.test('Über die Kiste_2-b')).toBe(true);
  });

  it('a padded name reaches the cloud and every block trimmed', async () => {
    load([replay('Winken', 'a'), replay('Winken', 'b')]);
    const api = makeApi();
    const result = await renameRecording(renameArgs(api, { toName: '  Greifen  ' }));
    expect(result).toEqual({ ok: true });
    expect(api.renameTrajectory.mock.calls[0][3]).toBe('Greifen');
    expect(names('edubotics_replay_trajectory')).toEqual(['Greifen', 'Greifen']);
  });

  it('a rename API failure changes nothing else', async () => {
    load([replay('Winken', 'a')]);
    const api = makeApi({ renameTrajectory: vi.fn(async () => { throw new Error('Name vergeben'); }) });
    const args = renameArgs(api);
    const result = await renameRecording(args);
    expect(result).toEqual({ ok: false, error: formatDe(DE.ERR_RENAME_FAILED, 'Name vergeben') });
    expect(args.saveWorkflowNow).not.toHaveBeenCalled();
    expect(names('edubotics_replay_trajectory')).toEqual(['Winken']);
  });
});

describe('recording rows: delete and restore', () => {
  it('deleteRecordingRows fetches every row before deleting any', async () => {
    const api = makeApi({
      getTrajectory: vi.fn(async (_t, _w, id) => ({
        id,
        name: 'Winken',
        samples: { fps: 25, points: [[0, 0, 0, 0, 0, 0.8, 0], [0, 0, 0, 0, 0, 0.8, 0.04]] },
        duration_s: 0.04,
        robot_profile: id === 't3' ? 'omx_f' : null,
        created_at: id === 't3' ? '2026-09-13T10:30:00Z' : '2026-09-13T10:00:00Z',
      })),
    });
    const rows = ITEMS.filter((it) => it.name === 'Winken');
    const result = await deleteRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', rows });
    expect(result.ok).toBe(true);
    expect(result.failed).toEqual([]);
    expect(api.getTrajectory).toHaveBeenCalledTimes(2);
    expect(api.deleteTrajectory).toHaveBeenCalledTimes(2);
    const lastFetch = Math.max(...api.getTrajectory.mock.invocationCallOrder);
    const firstDelete = Math.min(...api.deleteTrajectory.mock.invocationCallOrder);
    expect(lastFetch).toBeLessThan(firstDelete);
    expect(result.deleted[0]).toEqual({
      name: 'Winken',
      fps: 25,
      points: [[0, 0, 0, 0, 0, 0.8, 0], [0, 0, 0, 0, 0, 0.8, 0.04]],
      duration_s: 0.04,
      robot_profile: 'omx_f',
      created_at: '2026-09-13T10:30:00Z',
    });
  });

  it('a failed fetch deletes nothing', async () => {
    const api = makeApi({ getTrajectory: vi.fn(async () => { throw new Error('offline'); }) });
    const result = await deleteRecordingRows({
      api, accessToken: 'tok', workflowId: 'wf1', rows: [ITEMS[0]],
    });
    expect(result.ok).toBe(false);
    expect(result.error).toBe(formatDe(DE.ERR_DELETE_FAILED, 'offline'));
    expect(api.deleteTrajectory).not.toHaveBeenCalled();
  });

  it('restoreRecordingRows re-creates the oldest first, with robot_profile', async () => {
    const api = makeApi();
    const deleted = [
      { name: 'Winken', fps: 25, points: [[1]], duration_s: 1.2, robot_profile: 'edu6_studio', created_at: '2026-09-13T10:30:00Z' },
      { name: 'Winken', fps: 25, points: [[2], [3]], duration_s: 0.5, robot_profile: null, created_at: '2026-09-13T10:00:00Z' },
    ];
    const result = await restoreRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', deleted });
    expect(result).toEqual({ ok: true, restored: 2 });
    expect(api.createTrajectory.mock.calls.map((c) => c[2])).toEqual([
      { name: 'Winken', fps: 25, points: [[2], [3]], point_count: 2, duration_s: 0.5 },
      {
        name: 'Winken', fps: 25, points: [[1]], point_count: 1, duration_s: 1.2, robot_profile: 'edu6_studio',
      },
    ]);
  });

  // An in-memory cloud with the real route's two properties that matter here:
  // an insert is stamped NOW, and a replay plays the newest row of a name.
  function fakeCloud() {
    let clock = Date.parse('2026-09-13T12:00:00Z');
    let seq = 0;
    const db = [
      { id: 't3', name: 'Winken', fps: 25, points: [[3]], duration_s: 4.2, created_at: '2026-09-13T10:30:00Z' },
      { id: 't1', name: 'Winken', fps: 25, points: [[1]], duration_s: 3.1, created_at: '2026-09-13T10:00:00Z' },
    ];
    const byNewest = () => [...db].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
    const api = makeApi({
      listTrajectories: vi.fn(async () => byNewest().map(({ points, ...meta }) => meta)),
      getTrajectory: vi.fn(async (_t, _w, id) => {
        const row = db.find((r) => r.id === id);
        return { ...row, samples: { fps: row.fps, points: row.points } };
      }),
      deleteTrajectory: vi.fn(async (_t, _w, id) => { db.splice(db.findIndex((r) => r.id === id), 1); }),
      createTrajectory: vi.fn(async (_t, _w, p) => {
        clock += 1000;
        const row = { id: `n${++seq}`, ...p, created_at: new Date(clock).toISOString() };
        db.push(row);
        return row;
      }),
    });
    const played = (name) => byNewest().find((r) => r.name === name);
    return { api, db, played };
  }

  it('undoing the delete of an OLDER version is refused: the played take stays the played take', async () => {
    const { api, played } = fakeCloud();
    const versions = await api.listTrajectories();
    const del = await deleteRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', rows: [versions[1]] });
    expect(del.ok).toBe(true);
    // The drawer's decision: a newer row of the name is left, so no „Rückgängig".
    expect(restoreKeepsPlayedTake(del.deleted, [versions[0]])).toBe(false);
    const result = await restoreRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', deleted: del.deleted });
    expect(result).toEqual({ ok: false, restored: 0, error: formatDe(DE.ERR_UNDO_NEWER_VERSION, 'Winken') });
    expect(api.createTrajectory).not.toHaveBeenCalled();
    expect(played('Winken').id).toBe('t3');
  });

  it('undoing a delete of EVERY version brings the same take back as the played one', async () => {
    const { api, db, played } = fakeCloud();
    const versions = await api.listTrajectories();
    const del = await deleteRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', rows: versions });
    expect(restoreKeepsPlayedTake(del.deleted, [])).toBe(true);
    const result = await restoreRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', deleted: del.deleted });
    expect(result).toEqual({ ok: true, restored: 2 });
    expect(db).toHaveLength(2);
    expect(played('Winken').points).toEqual([[3]]);
  });

  it('a take recorded under the name before „Rückgängig" blocks the restore', async () => {
    const { api, played } = fakeCloud();
    const versions = await api.listTrajectories();
    const del = await deleteRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', rows: versions });
    await api.createTrajectory('tok', 'wf1', { name: 'Winken', fps: 25, points: [[9]] });
    api.createTrajectory.mockClear();
    const result = await restoreRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', deleted: del.deleted });
    expect(result.ok).toBe(false);
    expect(api.createTrajectory).not.toHaveBeenCalled();
    expect(played('Winken').points).toEqual([[9]]);
  });

  it('an unreadable cloud list restores nothing', async () => {
    const api = makeApi({ listTrajectories: vi.fn(async () => { throw new Error('offline'); }) });
    const deleted = [{ name: 'Winken', fps: 25, points: [[1]], created_at: '2026-09-13T10:30:00Z' }];
    const result = await restoreRecordingRows({ api, accessToken: 'tok', workflowId: 'wf1', deleted });
    expect(result).toEqual({ ok: false, restored: 0, error: formatDe(DE.ERR_UNDO_FAILED, 'offline') });
    expect(api.createTrajectory).not.toHaveBeenCalled();
  });

  it('restoreKeepsPlayedTake: older leftovers and other names are fine; newer, equal or unreadable stamps are not', () => {
    const copy = { name: 'Winken', created_at: '2026-09-13T10:30:00Z' };
    expect(restoreKeepsPlayedTake([copy], [{ name: 'Winken', created_at: '2026-09-13T10:00:00Z' }])).toBe(true);
    expect(restoreKeepsPlayedTake([copy], [{ name: 'Tanz', created_at: '2026-09-13T11:00:00Z' }])).toBe(true);
    expect(restoreKeepsPlayedTake([copy], [{ name: 'Winken', created_at: '2026-09-13T10:30:00Z' }])).toBe(false);
    expect(restoreKeepsPlayedTake([copy], [{ name: 'Winken', created_at: null }])).toBe(false);
    expect(restoreKeepsPlayedTake([{ name: 'Winken', created_at: null }], [])).toBe(false);
    expect(restoreKeepsPlayedTake([], [])).toBe(false);
  });
});

describe('places', () => {
  const PIN = { name: 'Ablage', kind: 'pin', source: 'camera', x: 0.1, y: 0.2, z: 0.01 };

  it('renamePlace renames the store entry and every reference as ONE undo step', async () => {
    load([ref('Ablage', 'r1'), ref('Ablage', 'r2'), ref('Kiste', 'r3')]);
    ws.getBlockById('r2').setDisabledReason(true, 'test');
    const store = getDestinationStore(ws);
    const { entry } = store.add(PIN);
    await flushEvents();
    const result = renamePlace({ workspace: ws, entryId: entry.id, toName: '  Tisch ' });
    expect(result.ok).toBe(true);
    expect(store.getById(entry.id).name).toBe('Tisch');
    expect(names('edubotics_destination_ref')).toEqual(['Tisch', 'Tisch', 'Kiste']);
    await flushEvents();
    ws.undo(false);
    await flushEvents();
    expect(store.getById(entry.id).name).toBe('Ablage');
    expect(names('edubotics_destination_ref')).toEqual(['Ablage', 'Ablage', 'Kiste']);
  });

  it('a refused place rename rewrites no block', () => {
    load([ref('Ablage', 'r1')]);
    const store = getDestinationStore(ws);
    const { entry } = store.add(PIN);
    store.add({ ...PIN, name: 'Kiste' });
    const result = renamePlace({ workspace: ws, entryId: entry.id, toName: 'Kiste' });
    expect(result).toEqual({ ok: false, error: formatDe(DE.ERR_NAME_TAKEN, 'Kiste') });
    expect(names('edubotics_destination_ref')).toEqual(['Ablage']);
  });

  it('deletePlace removes the entry and leaves every block', () => {
    load([ref('Ablage', 'r1'), ref('Ablage', 'r2')]);
    const store = getDestinationStore(ws);
    const { entry } = store.add(PIN);
    const result = deletePlace({ workspace: ws, entryId: entry.id });
    expect(result.ok).toBe(true);
    expect(store.getEntries()).toEqual([]);
    expect(ws.getAllBlocks(false).map((b) => b.id).sort()).toEqual(['r1', 'r2']);
  });
});

describe('variables', () => {
  const VARS = [{ name: 'A', id: 'va' }, { name: 'B', id: 'vb' }];
  const getter = (id, blockId) => ({ type: 'variables_get', id: blockId, fields: { VAR: { id } } });

  function loadVars() {
    load([getter('va', 'g1'), getter('va', 'g2'), getter('vb', 'g3')], VARS);
  }

  function snapshot() {
    return {
      vars: ws.getVariableMap().getAllVariables().map((v) => [v.getId(), v.getName()]).sort(),
      getters: ['g1', 'g2', 'g3'].map((id) => ws.getBlockById(id).getField('VAR').getVariable().getId()),
    };
  }

  it("another variable's name is refused", () => {
    loadVars();
    const before = snapshot();
    expect(renameVariable({ workspace: ws, variableId: 'va', toName: 'B' }))
      .toEqual({ ok: false, error: formatDe(DE.ERR_NAME_TAKEN, 'B') });
    expect(snapshot()).toEqual(before);
  });

  it('a case variant of another variable is refused and merges nothing', () => {
    loadVars();
    const before = snapshot();
    expect(before.getters).toEqual(['va', 'va', 'vb']);
    expect(renameVariable({ workspace: ws, variableId: 'va', toName: 'b' }))
      .toEqual({ ok: false, error: formatDe(DE.ERR_NAME_TAKEN, 'b') });
    expect(snapshot()).toEqual(before);
  });

  it('a padded case variant is checked by its trimmed name', () => {
    loadVars();
    const before = snapshot();
    expect(renameVariable({ workspace: ws, variableId: 'va', toName: '  b  ' }))
      .toEqual({ ok: false, error: formatDe(DE.ERR_NAME_TAKEN, 'b') });
    expect(snapshot()).toEqual(before);
    expect(ws.getVariableMap().getAllVariables()).toHaveLength(2);
  });

  it("a case change of the variable's own name is allowed", () => {
    loadVars();
    expect(renameVariable({ workspace: ws, variableId: 'va', toName: 'a' })).toEqual({ ok: true });
    expect(ws.getVariableMap().getVariableById('va').getName()).toBe('a');
    expect(snapshot().getters).toEqual(['va', 'va', 'vb']);
  });

  it('a free name renames; an undisplayable one is refused', () => {
    loadVars();
    expect(renameVariable({ workspace: ws, variableId: 'va', toName: ' Anzahl Würfel ' })).toEqual({ ok: true });
    expect(ws.getVariableMap().getVariableById('va').getName()).toBe('Anzahl Würfel');
    const refused = renameVariable({ workspace: ws, variableId: 'va', toName: 'a=b' });
    expect(refused.ok).toBe(false);
    expect(ws.getVariableMap().getVariableById('va').getName()).toBe('Anzahl Würfel');
  });

  it('usageRows and deleteVariable work by variable id', () => {
    loadVars();
    expect(usageRows(ws, 'variable', 'vb').map((r) => r.blockId)).toEqual(['g3']);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    expect(deleteVariable({ workspace: ws, variableId: 'vb' })).toEqual({ ok: true });
    expect(ws.getVariableMap().getVariableById('vb')).toBeNull();
    expect(ws.getVariableMap().getVariableById('va')).not.toBeNull();
  });
});
