/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-50 — the /workflow/start payload carries the PROGRAM, not the editor's
// plugin state.
//
// `Blockly.serialization.workspaces.save()` emits one key per registered
// workspace serializer, and two editor plugins register their own. handleStart
// used to spread that object verbatim, so both rode to the server on every run
// and counted against MAX_WORKFLOW_JSON_BYTES (256 KiB) and the cloud's 384 KB
// body middleware.
//
// MEASURED headless against the real plugins (Blockly 12.5.1):
//   • @blockly/suggested-blocks registers a `suggested-blocks` serializer whose
//     listener does `recentlyUsedBlocks.unshift(type)` on every BLOCK_CREATE and
//     NEVER trims, plus `defaultJsonForBlockLookup[type] = event.json`. Both
//     round-trip through save/load. 50 drags → 1.3 KB, 500 → 8.3 KB,
//     2000 → 31.7 KB — unbounded and growing for the life of the workflow.
//   • @blockly/workspace-backpack registers a `backpack` serializer holding the
//     student's stashed blocks, i.e. a private clipboard inside a shared
//     workflow's run payload.
// Also measured: an EMPTY workspace serializes to `{}` — no `blocks` key at all.
//
// The run payload is therefore narrowed to `blocks` + `variables`. Autosave and
// save-to-cloud deliberately keep the FULL serializer output (that is what makes
// a backpack and block suggestions survive a reload); the tests below pin that
// the object handed in is not mutated, which is what keeps those paths intact —
// they read the same `editorJson` object.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls from '../RunControls';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(() =>
    Promise.resolve({
      success: true,
      message: 'gestartet',
      unreachable_block_ids: [],
      unreachable_messages: [],
    }),
  ),
  pauseWorkflow: vi.fn(),
  stepWorkflow: vi.fn(),
  continueWorkflow: vi.fn(),
  setWorkflowBreakpoints: vi.fn(),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

const mockGetTrajectory = vi.hoisted(() => vi.fn());
vi.mock('../../../services/workflowApi', () => ({
  __esModule: true,
  getTrajectoryByName: mockGetTrajectory,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

function baseState() {
  return {
    workshop: {
      runState: 'idle',
      phase: '',
      currentBlockId: null,
      paused: false,
      log: [],
      workflowError: null,
      debuggerVisible: false,
      debuggerWarnings: [],
      breakpoints: [],
    },
    auth: { session: { access_token: 'jwt-1' } },
  };
}

// A serializer output shaped exactly like the editor's: the two program keys
// plus the two plugin keys, with the plugin payloads in their real shapes.
function editorJsonWithPluginState() {
  return {
    blocks: { languageVersion: 0, blocks: [{ type: 'edubotics_home', id: 'b1' }] },
    variables: [{ name: 'meine Zahl', id: 'v1' }],
    'suggested-blocks': {
      // The real plugin unshifts one entry per drag and never trims.
      recentlyUsedBlocks: Array.from({ length: 400 }, (_, i) =>
        (i % 2 ? 'edubotics_home' : 'controls_repeat_ext')),
      defaultJsonForBlockLookup: {
        edubotics_home: { type: 'edubotics_home', id: 'seed1', x: 0, y: 0 },
        controls_repeat_ext: { type: 'controls_repeat_ext', id: 'seed2', x: 0, y: 0 },
      },
    },
    backpack: [
      { type: 'edubotics_open_gripper', id: 'stash1' },
      { type: 'edubotics_close_gripper', id: 'stash2' },
    ],
  };
}

async function startAndCapture(props) {
  render(<RunControls workflowId="wf-1" simMode={false} simScene={null} {...props} />);
  await userEvent.click(screen.getByRole('button', { name: /Start/ }));
  await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
  const [name, , payload] = mockRos.callService.mock.calls[0];
  expect(name).toBe('/workflow/start');
  return JSON.parse(payload.workflow_json);
}

beforeEach(() => {
  mockState = baseState();
  mockDispatch.mockClear();
  mockRos.callService.mockClear();
  mockGetTrajectory.mockReset();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

describe('RunControls — run payload excludes editor-plugin serializer keys', () => {
  test('suggested-blocks and backpack never reach /workflow/start', async () => {
    const parsed = await startAndCapture({ blocklyJson: editorJsonWithPluginState() });

    expect(parsed['suggested-blocks']).toBeUndefined();
    expect(parsed.backpack).toBeUndefined();
    // …and the program itself is untouched.
    expect(parsed.blocks).toEqual({
      languageVersion: 0,
      blocks: [{ type: 'edubotics_home', id: 'b1' }],
    });
    expect(parsed.variables).toEqual([{ name: 'meine Zahl', id: 'v1' }]);
  });

  test('the four run-only siblings the server parses still ride along', async () => {
    // zones / tempo / trajectories (and sim in simMode) are NOT serializer keys —
    // they are added by this payload and each one is parsed server-side, so the
    // strip must not take them with it.
    const parsed = await startAndCapture({
      blocklyJson: editorJsonWithPluginState(),
      simScene: { objects: [], zones: [{ x: 0, y: 0, w: 0.1, h: 0.1 }] },
    });
    expect(parsed.zones).toEqual([{ x: 0, y: 0, w: 0.1, h: 0.1 }]);
    expect(typeof parsed.tempo).toBe('number');
    expect(parsed.trajectories).toEqual({});
  });

  test('simMode still injects the sim sibling on the slimmed base', async () => {
    render(
      <RunControls
        workflowId="wf-1"
        blocklyJson={editorJsonWithPluginState()}
        simMode
        simScene={{ objects: [{ type: 'wuerfel', x: 0.2, y: 0 }], zones: [] }}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);

    expect(parsed.sim).toEqual({
      enabled: true,
      objects: [{ type: 'wuerfel', x: 0.2, y: 0 }],
    });
    expect(parsed['suggested-blocks']).toBeUndefined();
    expect(parsed.backpack).toBeUndefined();
    expect(parsed.blocks).toBeTruthy();
  });

  test('the slimming measurably shrinks the payload', async () => {
    const withPlugins = await startAndCapture({ blocklyJson: editorJsonWithPluginState() });
    const utf8 = (o) => new TextEncoder().encode(JSON.stringify(o)).length;

    // What the OLD code would have sent: the same payload with the plugin keys.
    const oldShape = {
      ...editorJsonWithPluginState(),
      zones: [],
      tempo: withPlugins.tempo,
      trajectories: {},
    };
    expect(utf8(withPlugins)).toBeLessThan(utf8(oldShape));
    // The 400-entry recentlyUsedBlocks array alone is multiple KB.
    expect(utf8(oldShape) - utf8(withPlugins)).toBeGreaterThan(2000);
  });

  test('the caller-owned serializer object is NOT mutated (autosave / cloud-save keep it whole)', async () => {
    // WorkshopPage hands the SAME `editorJson` object to RunControls and to
    // handleSave, and useAutosave re-serializes from the live workspace. If the
    // strip mutated in place, a run would silently wipe the student's backpack
    // and block suggestions out of the next save.
    const original = editorJsonWithPluginState();
    const snapshot = JSON.parse(JSON.stringify(original));
    await startAndCapture({ blocklyJson: original });
    expect(original).toEqual(snapshot);
    expect(original['suggested-blocks']).toBeTruthy();
    expect(original.backpack).toHaveLength(2);
  });

  test('an EMPTY workspace ({} — no blocks key) keeps the key absent', async () => {
    // Measured: Blockly serializes an empty workspace to `{}`. A `blocks:
    // undefined` would serialize away anyway, but `variables` must not be
    // invented either — the interpreter reads `data['blocks']` and an absent key
    // is its "no program" signal.
    const parsed = await startAndCapture({ blocklyJson: { 'suggested-blocks': { recentlyUsedBlocks: [] } } });
    expect(Object.prototype.hasOwnProperty.call(parsed, 'blocks')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(parsed, 'variables')).toBe(false);
    expect(parsed['suggested-blocks']).toBeUndefined();
  });

  test('a workspace with no variables does not gain an empty variables key', async () => {
    const parsed = await startAndCapture({
      blocklyJson: { blocks: { blocks: [{ type: 'edubotics_home' }] }, backpack: [] },
    });
    expect(Object.prototype.hasOwnProperty.call(parsed, 'variables')).toBe(false);
    expect(parsed.blocks).toBeTruthy();
  });
});
