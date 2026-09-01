// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Pure reducer tests for the 2026-09-01 latching-state pass. Three fields that
// only ever advanced — `detections`, `workflowError`, `breakpoints` — and the
// events that must now retire them. The sign-out half of the same pass is
// proved against the REAL root reducer in utils/__tests__/signOut.test.js,
// because that is the only place the whole store answers at once.

import reducer, {
  setRunState,
  setDetections,
  setWorkflowStatus,
  clearWorkflowError,
  setSelectedWorkflowId,
  addBreakpoint,
} from '../workshopSlice';

const initial = reducer(undefined, { type: '@@INIT' });

const BOXES = [
  { cx: 10, cy: 20, w: 4, h: 4, label: 'wuerfel', confidence: 0.9 },
];

const withBoxes = () => reducer(initial, setDetections({ detections: BOXES }));

describe('detections do not outlive the run that produced them', () => {
  // `active_detections` rides /workflow/status, which the server stops emitting
  // the moment the run is over. CameraFeedOverlay keeps painting the last set
  // over an MJPEG stream that comes from web_video_server, NOT from ROS — so the
  // boxes stay on a moving picture and look live. The same component is mounted
  // by IntrinsicCalibStep and HandEyeCalibStep, so they also leaked out of
  // Roboter Studio entirely and into the calibration wizard.
  test.each(['finished', 'stopped', 'error'])(
    'setRunState(%s) drops them',
    (terminal) => {
      const s = reducer(withBoxes(), setRunState(terminal));
      expect(s.detections).toEqual([]);
    },
  );

  test('a mid-run running tick does NOT drop them', () => {
    // Not a formality: subscribeToWorkflowStatus dispatches setRunState('running')
    // on EVERY per-block running message, so a clear placed in that branch would
    // blank the live overlay several times a second.
    const s = reducer(withBoxes(), setRunState('running'));
    expect(s.detections).toEqual(BOXES);
  });

  test('the terminal clear does not disturb the run error the student must read', () => {
    // The error banner is how a failed run explains itself; only a NEW run
    // clears it (below). Deleting it here would make the failure silent.
    let s = reducer(withBoxes(), setWorkflowStatus({ error: 'Zielpunkt liegt unter der Tischebene.' }));
    s = reducer(s, setRunState('error'));
    expect(s.workflowError).toBe('Zielpunkt liegt unter der Tischebene.');
    expect(s.detections).toEqual([]);
  });
});

describe('clearWorkflowError', () => {
  test('retires the previous run’s alert', () => {
    const s = reducer(
      reducer(initial, setWorkflowStatus({ error: 'Arbeitsbereich nicht erreichbar.' })),
      clearWorkflowError(),
    );
    expect(s.workflowError).toBeNull();
  });

  test('touches nothing else', () => {
    const before = reducer(initial, setWorkflowStatus({
      current_block_id: 'blk-1', phase: 'running', progress: 42, error: 'kaputt',
    }));
    const after = reducer(before, clearWorkflowError());
    expect(after.currentBlockId).toBe('blk-1');
    expect(after.phase).toBe('running');
    expect(after.progress).toBe(42);
  });
});

describe('breakpoints belong to the program that minted them', () => {
  const withBps = (id) => {
    let s = reducer(initial, setSelectedWorkflowId(id));
    s = reducer(s, addBreakpoint('blk-A'));
    return reducer(s, addBreakpoint('blk-B'));
  };

  test('switching to another workflow drops the dead block ids', () => {
    // They are Blockly ids from a document that is about to be replaced.
    // BreakpointList would render them as raw UUIDs the student cannot
    // alt-click off, and handleStart pushes them to /workflow/set_breakpoints.
    const s = reducer(withBps('wf-1'), setSelectedWorkflowId('wf-2'));
    expect(s.breakpoints).toEqual([]);
    expect(s.selectedWorkflowId).toBe('wf-2');
  });

  test('closing a workflow drops them too', () => {
    expect(reducer(withBps('wf-1'), setSelectedWorkflowId(null)).breakpoints).toEqual([]);
  });

  test('saving an UNSAVED workflow keeps them', () => {
    // WorkshopPage.handleSave / GalleryTab: null -> id. The document is the one
    // the student is looking at; only its identity is new. Clearing here would
    // throw away breakpoints they had just set on those very blocks.
    let s = reducer(initial, addBreakpoint('blk-A'));
    s = reducer(s, setSelectedWorkflowId('wf-created'));
    expect(s.breakpoints).toEqual(['blk-A']);
  });

  test('re-picking the workflow already open keeps them', () => {
    const s = reducer(withBps('wf-1'), setSelectedWorkflowId('wf-1'));
    expect(s.breakpoints).toEqual(['blk-A', 'blk-B']);
  });
});
