/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// TeachHost — a Vormachen request becomes an open overlay or a German refusal,
// judged when the request is PROCESSED (a flyout request can outlive the state
// it was made in).

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import toast from 'react-hot-toast';
import TeachHost from '../TeachHost';
import { DE } from '../../blocks/messages_de';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});

const mockGlide = vi.hoisted(() => ({ active: false }));
vi.mock('../../HomeGlidePrompt', () => ({
  __esModule: true,
  useHomeGlide: () => ({ offerHomeGlide: vi.fn(), homeGlideDialog: null, homeGlideActive: mockGlide.active }),
}));

const mockOverlay = vi.hoisted(() => ({ props: null, mounts: 0 }));
vi.mock('../TeachOverlay', async () => {
  const { useEffect } = await vi.importActual('react');
  return {
    __esModule: true,
    // Named + capitalized so react-hooks/rules-of-hooks recognizes it as a component.
    default: function MockTeachOverlay(props) {
      mockOverlay.props = props;
      // One bump per MOUNT, so a remount on the resolved mode is observable.
      useEffect(() => { mockOverlay.mounts += 1; }, []);
      return <div data-testid="teach-overlay-stub" />;
    },
  };
});

const IDLE_BRIDGE = { available: true, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false };
// hooks/useRsBridgeStatus before its first answer, and after an unavailable one.
const PENDING_BRIDGE = { available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false, probed: false };
const DOWN_BRIDGE = { ...PENDING_BRIDGE, probed: true };

function hostProps(over = {}) {
  return {
    isActive: true,
    workspace: { hideChaff: vi.fn() },
    accessToken: 'jwt',
    workflowId: 'wf-1',
    robotType: 'omx_f',
    caps: null,
    heartbeatStatus: 'connected',
    runState: 'idle',
    paused: false,
    simMode: false,
    jogHandGuideOn: false,
    previewActive: false,
    rsBridge: IDLE_BRIDGE,
    saveWorkflowNow: vi.fn(),
    refetchTrajectories: vi.fn(),
    ...over,
  };
}

function teachState(teach) {
  mockState = { studioAssets: { teach: { open: false, requested: null, mode: null, focus: null, ...teach } } };
}

const dispatched = (type) => mockDispatch.mock.calls.map((c) => c[0]).filter((a) => a && a.type === type);

beforeEach(() => {
  mockDispatch.mockClear();
  toast.error.mockClear();
  mockGlide.active = false;
  mockOverlay.props = null;
  mockOverlay.mounts = 0;
  teachState({});
});

describe('TeachHost', () => {
  test('a request during a home glide is refused with the glide reason and never opens', () => {
    mockGlide.active = true;
    teachState({ requested: { focus: null, token: 1 } });
    render(<TeachHost {...hostProps()} />);
    expect(toast.error).toHaveBeenCalledWith(DE.TEACH_BLOCK_GLIDE);
    expect(dispatched('studioAssets/teachRequestHandled')).toHaveLength(1);
    expect(dispatched('studioAssets/teachOpened')).toHaveLength(0);
  });

  test.each([
    ['offline', { heartbeatStatus: 'disconnected' }, DE.TEACH_BLOCK_OFFLINE],
    ['a running program', { runState: 'running' }, DE.TEACH_BLOCK_RUNNING],
    ['a paused program', { paused: true }, DE.TEACH_BLOCK_RUNNING],
    ['a running preview', { runState: 'running', previewActive: true }, DE.TEACH_BLOCK_PREVIEW],
    ['the simulator', { simMode: true }, DE.TEACH_BLOCK_SIM],
    ['a hand-guide in „Steuern"', { jogHandGuideOn: true }, DE.TEACH_BLOCK_JOG],
  ])('refuses during %s with its own text', (_label, over, text) => {
    teachState({ requested: { focus: 'recording', token: 7 } });
    const props = hostProps(over);
    render(<TeachHost {...props} />);
    expect(toast.error).toHaveBeenCalledWith(text);
    expect(dispatched('studioAssets/teachOpened')).toHaveLength(0);
    expect(dispatched('studioAssets/teachRequestHandled')).toHaveLength(1);
    expect(props.workspace.hideChaff).not.toHaveBeenCalled();
  });

  test('a valid request closes Blockly\'s flyout and opens hand mode with the focus', () => {
    teachState({ requested: { focus: 'pose', token: 3 } });
    const props = hostProps();
    render(<TeachHost {...props} />);
    expect(props.workspace.hideChaff).toHaveBeenCalledTimes(1);
    const opened = dispatched('studioAssets/teachOpened');
    expect(opened).toHaveLength(1);
    expect(opened[0].payload).toEqual({ mode: 'hand', focus: 'pose' });
    expect(toast.error).not.toHaveBeenCalled();
  });

  test.each([
    ['a live leader on a leader-capable profile', { rsBridge: { ...IDLE_BRIDGE, leaderOn: true }, caps: { has_leader: true } }, 'leader'],
    ['a live leader with unknown caps', { rsBridge: { ...IDLE_BRIDGE, leaderOn: true }, caps: null }, 'leader'],
    ['a live leader on a leader-less profile', { rsBridge: { ...IDLE_BRIDGE, leaderOn: true }, caps: { has_leader: false } }, 'hand'],
    ['no leader', { rsBridge: IDLE_BRIDGE, caps: { has_leader: true } }, 'hand'],
    // R7 (2026-09-15): a leader-less profile needs no bridge answer at all.
    ['a failed bridge probe on a leader-less profile', { rsBridge: DOWN_BRIDGE, caps: { has_leader: false } }, 'hand'],
    ['an unanswered bridge on a leader-less profile', { rsBridge: PENDING_BRIDGE, caps: { has_leader: false } }, 'hand'],
    ['the bridge itself saying has_leader false', { rsBridge: { ...IDLE_BRIDGE, followerOnly: true, hasLeader: false, probed: true }, caps: null }, 'hand'],
  ])('D8: %s opens %s mode, never a refusal', (_label, over, mode) => {
    teachState({ requested: { focus: 'recording', token: 9 } });
    render(<TeachHost {...hostProps(over)} />);
    expect(toast.error).not.toHaveBeenCalled();
    const opened = dispatched('studioAssets/teachOpened');
    expect(opened).toHaveLength(1);
    expect(opened[0].payload).toEqual({ mode, focus: 'recording' });
  });

  // R7 (2026-09-15): a rig that may have a leader never opens silently in hand
  // mode while the bridge cannot report the leader state.
  test.each([
    ['the bridge has not answered yet', { rsBridge: PENDING_BRIDGE, caps: { has_leader: true } }],
    ['the bridge answered unavailable', { rsBridge: DOWN_BRIDGE, caps: { has_leader: true } }],
    ['a failed probe of an older hook (no probed field)', { rsBridge: { available: false, followerOnly: false, leaderOn: false }, caps: { has_leader: true } }],
    ['no bridge at all', { rsBridge: null, caps: { has_leader: true } }],
    ['unknown caps and no answer', { rsBridge: PENDING_BRIDGE, caps: null }],
  ])('R7: %s opens UNRESOLVED (mode null), never a refusal', (_label, over) => {
    teachState({ requested: { focus: 'pose', token: 11 } });
    render(<TeachHost {...hostProps(over)} />);
    expect(toast.error).not.toHaveBeenCalled();
    const opened = dispatched('studioAssets/teachOpened');
    expect(opened).toHaveLength(1);
    expect(opened[0].payload).toEqual({ mode: null, focus: 'pose' });
  });

  test('R7: an unresolved session renders the overlay as pending', () => {
    teachState({ open: true, mode: null, focus: 'ziel' });
    render(<TeachHost {...hostProps({ rsBridge: PENDING_BRIDGE, caps: { has_leader: true } })} />);
    expect(mockOverlay.props.mode).toBe('pending');
    expect(dispatched('studioAssets/teachModeResolved')).toHaveLength(0);
  });

  test.each([
    ['answered „follower only" → hand', { ...IDLE_BRIDGE, followerOnly: true, probed: true }, 'hand'],
    ['answered „leader on" → leader', { ...IDLE_BRIDGE, leaderOn: true, probed: true }, 'leader'],
  ])('R7: the bridge %s resolves the open session', (_label, answer, mode) => {
    teachState({ open: true, mode: null, focus: null });
    const { rerender } = render(<TeachHost {...hostProps({ rsBridge: PENDING_BRIDGE, caps: { has_leader: true } })} />);
    rerender(<TeachHost {...hostProps({ rsBridge: DOWN_BRIDGE, caps: { has_leader: true } })} />);
    expect(dispatched('studioAssets/teachModeResolved')).toHaveLength(0);
    rerender(<TeachHost {...hostProps({ rsBridge: answer, caps: { has_leader: true } })} />);
    const resolved = dispatched('studioAssets/teachModeResolved');
    expect(resolved).toHaveLength(1);
    expect(resolved[0].payload).toEqual({ mode });
  });

  test('R7: the resolved mode REMOUNTS the overlay (a fresh session); a resolved session is never re-resolved', () => {
    teachState({ open: true, mode: null });
    const { rerender } = render(<TeachHost {...hostProps({ rsBridge: PENDING_BRIDGE, caps: { has_leader: true } })} />);
    expect(mockOverlay.mounts).toBe(1);
    teachState({ open: true, mode: 'hand' });
    rerender(<TeachHost {...hostProps({ rsBridge: { ...IDLE_BRIDGE, followerOnly: true, probed: true }, caps: { has_leader: true } })} />);
    expect(mockOverlay.props.mode).toBe('hand');
    expect(mockOverlay.mounts).toBe(2);
    mockDispatch.mockClear();
    // The bridge going away in an open, resolved session changes nothing here.
    rerender(<TeachHost {...hostProps({ rsBridge: DOWN_BRIDGE, caps: { has_leader: true } })} />);
    expect(mockOverlay.props.mode).toBe('hand');
    expect(mockOverlay.mounts).toBe(2);
    expect(dispatched('studioAssets/teachModeResolved')).toHaveLength(0);
  });

  test('renders the overlay in leader mode when the slice says so', () => {
    teachState({ open: true, mode: 'leader' });
    render(<TeachHost {...hostProps({ rsBridge: { ...IDLE_BRIDGE, leaderOn: true } })} />);
    expect(mockOverlay.props.mode).toBe('leader');
  });

  test('a request with no editor on screen is dropped silently', () => {
    teachState({ requested: { focus: null, token: 4 } });
    render(<TeachHost {...hostProps({ workspace: null })} />);
    expect(dispatched('studioAssets/teachRequestHandled')).toHaveLength(1);
    expect(dispatched('studioAssets/teachOpened')).toHaveLength(0);
    expect(toast.error).not.toHaveBeenCalled();
  });

  test('each NEW token is a new request', () => {
    teachState({ requested: { focus: null, token: 1 }, });
    const props = hostProps({ simMode: true });
    const { rerender } = render(<TeachHost {...props} />);
    expect(toast.error).toHaveBeenCalledTimes(1);
    teachState({ requested: { focus: null, token: 2 } });
    rerender(<TeachHost {...hostProps()} />);
    expect(dispatched('studioAssets/teachOpened')).toHaveLength(1);
  });

  test('renders the overlay while open, with the rig props, and its close dispatches teachClosed', () => {
    teachState({ open: true, mode: 'hand', focus: 'ziel' });
    render(<TeachHost {...hostProps({ heartbeatStatus: 'connected' })} />);
    expect(screen.getByTestId('teach-overlay-stub')).toBeInTheDocument();
    expect(mockOverlay.props).toMatchObject({
      mode: 'hand', focus: 'ziel', accessToken: 'jwt', workflowId: 'wf-1', robotType: 'omx_f', heartbeatOk: true,
    });
    mockDispatch.mockClear();
    act(() => { mockOverlay.props.onClose(); });
    expect(dispatched('studioAssets/teachClosed')).toHaveLength(1);
  });

  test('offline, the overlay is told the heartbeat is gone', () => {
    teachState({ open: true, mode: 'hand' });
    render(<TeachHost {...hostProps({ heartbeatStatus: 'disconnected' })} />);
    expect(mockOverlay.props.heartbeatOk).toBe(false);
  });

  test('renders nothing while closed', () => {
    render(<TeachHost {...hostProps()} />);
    expect(screen.queryByTestId('teach-overlay-stub')).toBeNull();
  });

  test('unmounting while open dispatches teachClosed', () => {
    teachState({ open: true, mode: 'hand' });
    const { unmount } = render(<TeachHost {...hostProps()} />);
    mockDispatch.mockClear();
    unmount();
    expect(dispatched('studioAssets/teachClosed')).toHaveLength(1);
  });
});
