// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme wiring of the tasks slice (spec §3.2): the phase anchor and the
// session tracker ride the existing reducers, the new actions feed them, and a
// sign-out takes the student's session with it but leaves the rig's anchor.

import reducer, {
  setTaskInfo,
  recordIntent,
  recordNoticeClear,
  recordNoticeSet,
  recordRegisterStatus,
  recordSessionDismiss,
  recordUploadStatus,
  setCollision,
  setHeartbeatStatus,
  setTaskStatus,
} from '../taskSlice';
import { shallowEqual } from 'react-redux';
import { EMPTY_RECORD_SESSION } from '../recordSession';
import * as sel from '../recordSelectors';
import { signedOut } from '../../session/sessionActions';
import TaskPhase from '../../../constants/taskPhases';

const WALL0 = 1_700_000_000_000;
const SNAPSHOT = {
  taskName: 'Würfel', userId: 'schule-A', robotType: 'omx_f', pushToHub: true, privateMode: true,
  numEpisodes: 2, episodeTime: 10, warmupTime: 0, resetTime: 0, fps: 30,
};

const recordTick = (patch = {}) => setTaskStatus({
  taskName: 'Würfel',
  taskType: 'record',
  running: true,
  phase: TaskPhase.RECORDING,
  totalTime: 10,
  proceedTime: 0,
  currentEpisodeNumber: 0,
  numEpisodes: 2,
  episodeTime: 10,
  warmupTime: 0,
  resetTime: 0,
  fps: 30,
  pushToHub: true,
  recordWarn: '',
  error: '',
  topicReceived: true,
  receivedAt: 1000,
  receivedWallMs: WALL0 + 1000,
  ...patch,
});

function started() {
  let s = reducer(undefined, { type: '@@init' });
  s = reducer(s, recordIntent({ kind: 'start', at: WALL0, snapshot: SNAPSHOT }));
  s = reducer(s, recordTick());
  return s;
}

describe('initial state', () => {
  it('adds the anchor, the session and the notice', () => {
    const s = reducer(undefined, { type: '@@init' });
    expect(s.phaseAnchor).toBeNull();
    expect(s.recordSession).toEqual(EMPTY_RECORD_SESSION);
    expect(s.recordNotice).toBeNull();
    expect(s.taskStatus.error).toBe('');
  });
});

describe('setTaskStatus', () => {
  it('advances the anchor and the session', () => {
    const s = started();
    expect(s.recordSession.active).toBe(true);
    expect(s.recordSession.startedHere).toBe(true);
    expect(s.phaseAnchor).toMatchObject({ phase: TaskPhase.RECORDING, value: 0, total: 10, at: 1000 });
    expect(s.taskStatus.taskType).toBe('record');
  });

  it('keeps both references while a tick changes nothing they record', () => {
    const s1 = started();
    const s2 = reducer(s1, recordTick({ receivedAt: 1033, receivedWallMs: WALL0 + 1033 }));
    expect(s2.recordSession).toBe(s1.recordSession);
    expect(s2.phaseAnchor).toBe(s1.phaseAnchor);
    expect(s2.taskStatus.receivedAt).toBe(1033);
  });

  it('keeps the identity guards (a bare tick never wipes robotType)', () => {
    let s = reducer(undefined, setTaskStatus({ robotType: 'omx_f', receivedAt: 1 }));
    s = reducer(s, setTaskStatus({ robotType: '', receivedAt: 2 }));
    expect(s.taskStatus.robotType).toBe('omx_f');
  });
});

describe('setCollision', () => {
  it('records the collision once, on the rising edge', () => {
    let s = started();
    s = reducer(s, recordTick({ proceedTime: 3, receivedAt: 4000, receivedWallMs: WALL0 + 4000 }));
    s = reducer(s, setCollision({ active: true, stage: 'stopped', receivedAt: 4200, receivedWallMs: WALL0 + 4200 }));
    expect(s.recordSession.episodes.map((e) => e.outcome)).toEqual(['collision']);
    expect(s.collision.active).toBe(true);
    s = reducer(s, setCollision({ active: true, stage: 'homing', receivedAt: 4400 }));
    expect(s.recordSession.episodes).toHaveLength(1);
    s = reducer(s, setCollision({ active: false, stage: 'stopped' }));
    expect(s.recordSession.collisionOpen).toBe(true);
  });
});

describe('setHeartbeatStatus', () => {
  it('flags a lost link while the upload is tracked', () => {
    let s = started();
    s = reducer(s, recordTick({ phase: TaskPhase.SAVING, totalTime: 0, receivedAt: 11000 }));
    s = reducer(s, recordTick({
      phase: TaskPhase.READY, running: false, currentEpisodeNumber: 1, totalTime: 0, receivedAt: 11100,
      receivedWallMs: WALL0 + 11100,
    }));
    expect(s.recordSession.finish.state).toBe('uploading');
    s = reducer(s, setHeartbeatStatus('timeout'));
    expect(s.heartbeatStatus).toBe('timeout');
    expect(s.recordSession.finish.linkLost).toBe(true);
  });
});

describe('recordNoticeSet / recordNoticeClear', () => {
  it('stores the notice and dedupes an identical one within a second', () => {
    let s = reducer(undefined, recordNoticeSet({ kind: 'warn', text: 'Kamera hängt.', at: WALL0 }));
    const first = s.recordNotice;
    expect(first).toEqual({ kind: 'warn', text: 'Kamera hängt.', at: WALL0 });
    s = reducer(s, recordNoticeSet({ kind: 'warn', text: 'Kamera hängt.', at: WALL0 + 999 }));
    expect(s.recordNotice).toBe(first);
    s = reducer(s, recordNoticeSet({ kind: 'warn', text: 'Kamera hängt.', at: WALL0 + 1000 }));
    expect(s.recordNotice).not.toBe(first);
    expect(s.recordNotice.at).toBe(WALL0 + 1000);
    s = reducer(s, recordNoticeClear());
    expect(s.recordNotice).toBeNull();
  });

  it('a hard error ends the active session (R10)', () => {
    let s = started();
    s = reducer(s, recordNoticeSet({ kind: 'error', text: 'Die Kameras senden keine Bilder.', at: WALL0 + 2000 }));
    expect(s.recordNotice.kind).toBe('error');
    expect(s.recordSession.finish.state).toBe('stopped_error');
    expect(s.recordSession.errorText).toBe('Die Kameras senden keine Bilder.');
  });

  it('a new start clears the previous notice', () => {
    let s = reducer(undefined, recordNoticeSet({ kind: 'error', text: 'x', at: WALL0 }));
    s = reducer(s, recordIntent({ kind: 'start', at: WALL0 + 5000, snapshot: SNAPSHOT }));
    expect(s.recordNotice).toBeNull();
    expect(s.recordSession.pendingStart).toMatchObject({ at: WALL0 + 5000 });
  });
});

describe('upload, registration, dismiss', () => {
  function uploading() {
    let s = started();
    s = reducer(s, recordTick({ phase: TaskPhase.SAVING, totalTime: 0, receivedAt: 11000 }));
    s = reducer(s, recordTick({
      phase: TaskPhase.READY, running: false, currentEpisodeNumber: 1, totalTime: 0, receivedAt: 11100,
      receivedWallMs: WALL0 + 11100,
    }));
    return s;
  }

  it('routes upload and register statuses into the finish', () => {
    let s = uploading();
    const repoId = 'schule-A/omx_f_Wuerfel';
    s = reducer(s, recordUploadStatus({ repoId, status: 'Uploading', percentage: 55, message: '', at: WALL0 + 12000 }));
    expect(s.recordSession.finish.uploadPct).toBe(55);
    s = reducer(s, recordUploadStatus({ repoId, status: 'Success', percentage: 100, message: '', at: WALL0 + 13000 }));
    s = reducer(s, recordRegisterStatus({ repoId, state: 'done', at: WALL0 + 13100 }));
    expect(s.recordSession.finish.state).toBe('done');
  });

  it('dismiss clears the card, never while running', () => {
    const running = started();
    expect(reducer(running, recordSessionDismiss()).recordSession).toBe(running.recordSession);
    const s = reducer(uploading(), recordSessionDismiss());
    expect(s.recordSession.episodes).toEqual([]);
    expect(s.recordSession.finish.dismissed).toBe(true);
  });
});

describe('session/signedOut', () => {
  it('takes the student\'s session and notice, keeps the rig\'s anchor', () => {
    let s = started();
    s = reducer(s, recordNoticeSet({ kind: 'warn', text: 'x', at: WALL0 }));
    const anchor = s.phaseAnchor;
    s = reducer(s, signedOut());
    expect(s.recordSession).toEqual(EMPTY_RECORD_SESSION);
    expect(s.recordNotice).toBeNull();
    expect(s.phaseAnchor).toBe(anchor);
    expect(s.taskInfo.userId).toBeUndefined();
  });
});

describe('recordSelectors', () => {
  it('selectRecordStatus leaves the arrival times out, so a bare tick is shallow-equal', () => {
    const s1 = { tasks: started() };
    const s2 = { tasks: reducer(s1.tasks, recordTick({ receivedAt: 1033, receivedWallMs: WALL0 + 1033 })) };
    const a = sel.selectRecordStatus(s1);
    const b = sel.selectRecordStatus(s2);
    expect(a).not.toHaveProperty('receivedAt');
    expect(a).not.toHaveProperty('receivedWallMs');
    expect(shallowEqual(a, b)).toBe(true);
    const c = sel.selectRecordStatus({ tasks: reducer(s2.tasks, recordTick({ proceedTime: 1, receivedAt: 2000 })) });
    expect(shallowEqual(b, c)).toBe(false);
  });

  it('the plain selectors read their slice fields', () => {
    const s = { tasks: started() };
    expect(sel.selectPhaseAnchor(s)).toBe(s.tasks.phaseAnchor);
    expect(sel.selectRecordSession(s)).toBe(s.tasks.recordSession);
    expect(sel.selectRecordNotice(s)).toBe(s.tasks.recordNotice);
    expect(sel.selectRecordForm(s)).toMatchObject({ fps: 30, episodeTime: 20, privateMode: false });
  });
});

describe('reference stability across identical ticks (V2-2)', () => {
  // The /task/status adopt path dispatches setTaskInfo on EVERY running tick
  // with freshly built arrays (task_instruction, tags). Identical content must
  // leave the store's objects alone, or every subscriber re-renders at 30 Hz.
  const adopt = () => setTaskInfo({
    taskName: 'Würfel',
    taskInstruction: ['Greife den Würfel.'],
    tags: ['omx_f', 'edubotics'],
    fps: 30,
    episodeTime: 20,
    pushToHub: true,
  });

  it('setTaskInfo with equal content keeps taskInfo and its arrays', () => {
    const s1 = reducer(reducer(undefined, { type: '@@init' }), adopt());
    const s2 = reducer(s1, adopt());
    expect(s2.taskInfo).toBe(s1.taskInfo);
    expect(s2.taskInfo.tags).toBe(s1.taskInfo.tags);
    expect(s2.taskInfo.taskInstruction).toBe(s1.taskInfo.taskInstruction);
    const s3 = reducer(s2, setTaskInfo({ tags: ['omx_f'] }));
    expect(s3.taskInfo).not.toBe(s2.taskInfo);
    expect(s3.taskInfo.tags).toEqual(['omx_f']);
    expect(s3.taskInfo.taskInstruction).toBe(s1.taskInfo.taskInstruction);
  });

  it('selectRecordForm returns the same object for equal content, even from new arrays', () => {
    const base = reducer(undefined, { type: '@@init' });
    const a = sel.selectRecordForm({ tasks: { ...base, taskInfo: { ...base.taskInfo, tags: ['x'], taskInstruction: ['y'] } } });
    const b = sel.selectRecordForm({ tasks: { ...base, taskInfo: { ...base.taskInfo, tags: ['x'], taskInstruction: ['y'] } } });
    expect(b).toBe(a);
    const c = sel.selectRecordForm({ tasks: { ...base, taskInfo: { ...base.taskInfo, tags: ['x', 'z'], taskInstruction: ['y'] } } });
    expect(c).not.toBe(b);
    expect(c.tags).toEqual(['x', 'z']);
  });

  it('selectRecordStatus returns the same object across ticks that differ only in their arrival', () => {
    const s1 = { tasks: started() };
    const s2 = { tasks: reducer(s1.tasks, recordTick({ receivedAt: 1033, receivedWallMs: WALL0 + 1033 })) };
    expect(sel.selectRecordStatus(s2)).toBe(sel.selectRecordStatus(s1));
    const s3 = { tasks: reducer(s2.tasks, recordTick({ proceedTime: 1, receivedAt: 2000 })) };
    expect(sel.selectRecordStatus(s3)).not.toBe(sel.selectRecordStatus(s2));
  });
});
