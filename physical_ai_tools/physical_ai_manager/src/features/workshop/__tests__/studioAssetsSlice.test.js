/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// studioAssets — the Sammlung state around one document. The load-bearing rows:
// a preview is finalized ONLY from its own workflow id (a previous run's late
// terminal on the shared /workflow/status topic must never record its result),
// a document switch retires everything that belonged to the old document, and
// sign-out retires everything.

import reducer, {
  fetchTrajectories,
  previewFailed,
  previewStarted,
  previewUnreachable,
  requestTeach,
  selectDrawer,
  selectHighlight,
  selectLastPreviewResult,
  selectPreview,
  selectPreviewActive,
  selectRenameSplit,
  selectTeachOpen,
  selectTeachState,
  selectTrajectoryList,
  setDrawerFocus,
  setHighlight,
  setPreviewTempo,
  setRenameSplit,
  teachOpened,
  openDrawer,
} from '../studioAssetsSlice';
import {
  markWorkflowSaved,
  setRunState,
  setSelectedWorkflowId,
  setWorkflowStatus,
} from '../workshopSlice';
import { signedOut } from '../../session/sessionActions';
import { DE } from '../../../components/Workshop/blocks/messages_de';
import { REPLAY_LEAD_IN_BELOW_TABLE_SERVER } from '../../../utils/simPreview';

const OWN = 'vorschau-ziel-d_00000001';
const KEY = 'dest:d_00000001';

const init = () => reducer(undefined, { type: '@@init' });
const run = (state, ...actions) => actions.reduce((s, a) => reducer(s, a), state);
const started = (state = init()) => reducer(state, previewStarted({
  key: KEY, kind: 'pin', name: 'Ablage', workflowId: OWN,
}));

describe('studioAssets — initial state and sign-out', () => {
  test('initial state is exactly the documented shape', () => {
    expect(init()).toEqual({
      trajectories: { workflowId: null, status: 'none', items: [], error: null, fetchedAt: 0 },
      drawer: { open: false, tab: 'aufnahmen', focusId: null, previewTempo: 1.0 },
      preview: null,
      lastPreviewResult: {},
      teach: { open: false, requested: null, mode: null, focus: null },
      highlight: null,
      renameSplit: null,
    });
  });

  test('signedOut resets everything, including a set preview/teach/highlight/renameSplit', () => {
    const dirty = run(
      started(),
      setSelectedWorkflowId('wf-1'),
      openDrawer({ tab: 'ziele', focusId: 'd_1' }),
      teachOpened({ mode: 'hand', focus: 'pose' }),
      setHighlight({ kind: 'pin', id: 'd_1' }),
      setRenameSplit({ cloudName: 'Greifen' }),
      previewStarted({ key: KEY, kind: 'pin', name: 'Ablage', workflowId: OWN }),
    );
    expect(dirty.preview).not.toBeNull();
    const after = reducer(dirty, signedOut());
    expect(after).toEqual(init());
    // A fresh copy, never the object the dirty state shared.
    expect(after.trajectories).not.toBe(dirty.trajectories);
  });
});

describe('studioAssets — the open document changes identity', () => {
  test('a different id resets trajectories to idle and clears focus/results/highlight/renameSplit', () => {
    let s = run(init(), setSelectedWorkflowId('wf-1'));
    s = reducer(s, fetchTrajectories.fulfilled([{ id: 't1', name: 'A' }], 'r', { accessToken: 't', workflowId: 'wf-1' }));
    s = run(
      s,
      setDrawerFocus('A'),
      setHighlight({ kind: 'recording', id: 't1' }),
      setRenameSplit({ cloudName: 'A' }),
      previewFailed({ key: 'rec:t1', message: 'x' }),
    );
    const next = reducer(s, setSelectedWorkflowId('wf-2'));
    expect(next.trajectories).toEqual({ workflowId: 'wf-2', status: 'idle', items: [], error: null, fetchedAt: 0 });
    expect(next.drawer.focusId).toBeNull();
    expect(next.lastPreviewResult).toEqual({});
    expect(next.highlight).toBeNull();
    expect(next.renameSplit).toBeNull();
  });

  test('the same id is a no-op', () => {
    let s = run(init(), setSelectedWorkflowId('wf-1'));
    s = reducer(s, fetchTrajectories.fulfilled([{ id: 't1', name: 'A' }], 'r', { accessToken: 't', workflowId: 'wf-1' }));
    s = run(s, setDrawerFocus('A'), setRenameSplit({ cloudName: 'A' }));
    expect(reducer(s, setSelectedWorkflowId('wf-1'))).toBe(s);
  });

  test('null → status none', () => {
    const s = run(init(), setSelectedWorkflowId('wf-1'), setSelectedWorkflowId(null));
    expect(s.trajectories.status).toBe('none');
    expect(s.trajectories.workflowId).toBeNull();
  });

  test('markWorkflowSaved clears renameSplit', () => {
    const s = run(init(), setRenameSplit({ cloudName: 'Greifen' }), markWorkflowSaved());
    expect(s.renameSplit).toBeNull();
  });
});

describe('studioAssets — fetchTrajectories', () => {
  const onWf1 = () => run(init(), setSelectedWorkflowId('wf-1'));

  test('pending → loading, fulfilled → ready with normalised items', () => {
    let s = reducer(onWf1(), fetchTrajectories.pending('r', { accessToken: 't', workflowId: 'wf-1' }));
    expect(s.trajectories.status).toBe('loading');
    s = reducer(s, fetchTrajectories.fulfilled([
      {
        id: 't1', name: 'Greifen', point_count: 40, duration_s: 1.6, fps: 25,
        robot_profile: 'omx_f', created_at: '2026-09-01', updated_at: '2026-09-02', owner_user_id: 'u1',
      },
      null,
      { name: 'ohne id' },
    ], 'r', { accessToken: 't', workflowId: 'wf-1' }));
    expect(s.trajectories.status).toBe('ready');
    expect(s.trajectories.error).toBeNull();
    expect(s.trajectories.fetchedAt).toBeGreaterThan(0);
    expect(s.trajectories.items).toEqual([{
      id: 't1', name: 'Greifen', point_count: 40, duration_s: 1.6, fps: 25,
      robot_profile: 'omx_f', created_at: '2026-09-01', updated_at: '2026-09-02',
    }]);
  });

  test('rejected → error with the message', () => {
    const s = reducer(onWf1(), fetchTrajectories.rejected(new Error('Netz weg'), 'r', { accessToken: 't', workflowId: 'wf-1' }));
    expect(s.trajectories.status).toBe('error');
    expect(s.trajectories.error).toBe('Netz weg');
  });

  test('pending/fulfilled/rejected of a stale workflowId are ignored', () => {
    const s = onWf1();
    const old = { accessToken: 't', workflowId: 'old' };
    expect(reducer(s, fetchTrajectories.pending('r', old))).toBe(s);
    expect(reducer(s, fetchTrajectories.fulfilled([{ id: 'x', name: 'X' }], 'req', old))).toBe(s);
    expect(reducer(s, fetchTrajectories.rejected(new Error('e'), 'r', old))).toBe(s);
  });
});

describe('studioAssets — a preview is finalized only from its OWN workflow id', () => {
  test('own running → own error: refused with that message, preview null', () => {
    const s = run(
      started(),
      setWorkflowStatus({ workflow_id: OWN, phase: 'running' }),
      setWorkflowStatus({ workflow_id: OWN, phase: 'error', error: 'Zielpunkt liegt unter der Tischebene.' }),
    );
    expect(s.preview).toBeNull();
    expect(s.lastPreviewResult[KEY]).toMatchObject({
      status: 'refused', message: 'Zielpunkt liegt unter der Tischebene.', unreachable: false,
    });
  });

  test('own running → own finished: ok', () => {
    const s = run(
      started(),
      setWorkflowStatus({ workflow_id: OWN, phase: 'running' }),
      setWorkflowStatus({ workflow_id: OWN, phase: 'finished' }),
    );
    expect(s.preview).toBeNull();
    expect(s.lastPreviewResult[KEY]).toMatchObject({ status: 'ok', message: '' });
  });

  test('own running → the local Stopp (setRunState stopped): stopped', () => {
    const s = run(started(), setWorkflowStatus({ workflow_id: OWN, phase: 'running' }), setRunState('stopped'));
    expect(s.preview).toBeNull();
    expect(s.lastPreviewResult[KEY].status).toBe('stopped');
  });

  test('a stale terminal of an earlier run never finalizes the preview', () => {
    const s = run(
      started(),
      setWorkflowStatus({ workflow_id: 'wf-old', phase: 'stopped' }),
      setRunState('stopped'),
    );
    expect(s.preview).not.toBeNull();
    expect(s.preview.sawOwnStatus).toBe(false);
    expect(s.lastPreviewResult).toEqual({});
  });

  test('a terminal setRunState with no own status yet leaves the preview set', () => {
    const s = run(started(), setRunState('finished'));
    expect(s.preview).not.toBeNull();
    expect(s.lastPreviewResult).toEqual({});
  });

  test('an error on another id never becomes lastError', () => {
    const s = run(
      started(),
      setWorkflowStatus({ workflow_id: 'wf-old', phase: 'error', error: 'Fremder Fehler' }),
      setWorkflowStatus({ workflow_id: OWN, phase: 'running' }),
      setWorkflowStatus({ workflow_id: OWN, phase: 'finished' }),
    );
    expect(s.lastPreviewResult[KEY]).toMatchObject({ status: 'ok', message: '' });
  });

  test('a status with no workflow_id never touches the preview', () => {
    const before = started();
    const s = reducer(before, setWorkflowStatus({ phase: 'error', error: 'x' }));
    expect(s.preview).toEqual(before.preview);
  });

  test('the server lead-in sentence on the own id is mapped for the simulator', () => {
    const s = reducer(started(), setWorkflowStatus({
      workflow_id: OWN, phase: 'running', error: REPLAY_LEAD_IN_BELOW_TABLE_SERVER,
    }));
    expect(s.preview.lastError).toBe(DE.PREVIEW_LEAD_IN_BELOW_TABLE);
    const done = reducer(s, setWorkflowStatus({ workflow_id: OWN, phase: 'error' }));
    expect(done.lastPreviewResult[KEY]).toMatchObject({
      status: 'refused', message: DE.PREVIEW_LEAD_IN_BELOW_TABLE,
    });
  });

  test('previewUnreachable then own finished → ok with unreachable true', () => {
    const s = run(
      started(),
      previewUnreachable({ key: KEY, message: 'Nicht erreichbar.' }),
      setWorkflowStatus({ workflow_id: OWN, phase: 'running' }),
      setWorkflowStatus({ workflow_id: OWN, phase: 'finished' }),
    );
    expect(s.lastPreviewResult[KEY]).toMatchObject({
      status: 'ok', unreachable: true, unreachableMessage: 'Nicht erreichbar.',
    });
  });

  test('previewFailed → refused, message mapped through previewMessageDe', () => {
    const s = reducer(started(), previewFailed({ key: KEY, message: REPLAY_LEAD_IN_BELOW_TABLE_SERVER }));
    expect(s.preview).toBeNull();
    expect(s.lastPreviewResult[KEY]).toMatchObject({ status: 'refused', message: DE.PREVIEW_LEAD_IN_BELOW_TABLE });
    const other = reducer(started(), previewFailed({ key: KEY, message: 'Zu groß.' }));
    expect(other.lastPreviewResult[KEY].message).toBe('Zu groß.');
  });

  test("setRunState('running') never clears a preview", () => {
    const s = run(started(), setWorkflowStatus({ workflow_id: OWN, phase: 'running' }), setRunState('running'));
    expect(s.preview).not.toBeNull();
    expect(s.lastPreviewResult).toEqual({});
  });
});

describe('studioAssets — small reducers', () => {
  test('setPreviewTempo is clamped to 0.5 / 1.0 / 2.0', () => {
    expect(reducer(init(), setPreviewTempo(2)).drawer.previewTempo).toBe(2.0);
    expect(reducer(init(), setPreviewTempo(7)).drawer.previewTempo).toBe(2.0);
    expect(reducer(init(), setPreviewTempo(0.1)).drawer.previewTempo).toBe(0.5);
    expect(reducer(init(), setPreviewTempo('x')).drawer.previewTempo).toBe(1.0);
  });

  test('requestTeach stamps a token; teachOpened consumes the request', () => {
    const s = reducer(init(), requestTeach({ focus: 'ziel' }));
    expect(s.teach.requested).toMatchObject({ focus: 'ziel' });
    expect(typeof s.teach.requested.token).toBe('number');
    const opened = reducer(s, teachOpened({ mode: 'hand', focus: 'ziel' }));
    expect(opened.teach).toEqual({ open: true, requested: null, mode: 'hand', focus: 'ziel' });
  });
});

describe('studioAssets — selectors are safe on partial states', () => {
  test('{} returns the defaults', () => {
    const st = {};
    expect(selectTrajectoryList(st)).toEqual({ workflowId: null, status: 'none', items: [], error: null, fetchedAt: 0 });
    expect(selectPreview(st)).toBeNull();
    expect(selectPreviewActive(st)).toBe(false);
    expect(selectTeachOpen(st)).toBe(false);
    expect(selectTeachState(st)).toEqual({ open: false, requested: null, mode: null, focus: null });
    expect(selectDrawer(st).tab).toBe('aufnahmen');
    expect(selectLastPreviewResult(st)).toEqual({});
    expect(selectHighlight(st)).toBeNull();
    expect(selectRenameSplit(st)).toBeNull();
    expect(selectTrajectoryList(undefined).status).toBe('none');
  });

  test('a state carrying only one field still resolves the others', () => {
    const st = { studioAssets: { teach: { open: true } } };
    expect(selectTeachOpen(st)).toBe(true);
    expect(selectPreviewActive(st)).toBe(false);
    expect(selectTrajectoryList(st).items).toEqual([]);
  });
});
