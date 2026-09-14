/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The camera click no longer prompts for a name. The PAGE decides it through
// `resolveMarkLabel`, handed to CameraFeedOverlay: the last-selected
// „setze Ziel = Pin" block's own name (so one point has one name — on the
// server and in the block), or a new automatic „Ziel n" in the document's
// destination store. This file drives the page's real handlers through the
// props the overlay receives; the overlay's own click path is covered in
// components/Workshop/__tests__/CameraFeedOverlay.destinationName.test.jsx.
//
// Mocking idiom of WorkshopPage.savePayload.test.jsx.

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';
import WorkshopPage from '../WorkshopPage';
import { DE } from '../../components/Workshop/blocks/messages_de';

// ── react-redux: selector-aware stub over a mutable module-level state. ──
let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

// ── BlocklyWorkspace: hands the page a FAKE workspace through the real
//    onWorkspaceReady callback, as the live editor does after inject. ──
const mockWorkspace = vi.hoisted(() => ({ current: null }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  // Named + capitalized so react-hooks/rules-of-hooks recognizes it as a component.
  default: function MockBlocklyWorkspace({ onWorkspaceReady }) {
    React.useEffect(() => {
      if (onWorkspaceReady && mockWorkspace.current) onWorkspaceReady(mockWorkspace.current);
    }, [onWorkspaceReady]);
    return <div data-testid="blockly-workspace" />;
  },
}));

// ── Right-region components. RightDock renders ONLY the camera tab, so the
//    page's real CameraFeedOverlay wiring is what the overlay mock captures. ──
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || [])
        .filter((t) => t.id === 'camera')
        .map((t) => (
          <div key={t.id}>{t.render()}</div>
        ))}
    </div>
  ),
}));
vi.mock('../../components/Workshop/SimStage', () => ({
  __esModule: true,
  default: () => <div data-testid="sim-stage" />,
}));

// ── Remaining children: import-safe no-op stubs (inlined — a vi.mock factory is
//    hoisted above imports, so it can't reference a top-level helper). ──
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/RunControls', () => ({ __esModule: true, default: () => <div data-testid="run-controls" /> }));
// CameraFeedOverlay exposes the props the page passes (latest render wins).
const mockOverlay = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({
  __esModule: true,
  default: function MockCameraFeedOverlay(props) {
    mockOverlay.props = props;
    return <div data-testid="camera-feed" />;
  },
}));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
// ToolbarButtons exposes the real `onSave` (= handleSave) as a button.
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: function MockToolbarButtons({ onSave }) {
    return (
      <button type="button" data-testid="save-button" onClick={() => onSave && onSave()} />
    );
  },
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div data-testid="version-history" /> }));
// JogPanel/RecordPanel: import-safe stubs (the shared idiom's shape).
vi.mock('../../components/Workshop/JogPanel', () => ({
  __esModule: true,
  default: function MockJogPanel({ onHandGuideChange }) {
    return (
      <div data-testid="jog-panel">
        <button
          type="button"
          data-testid="jog-hand-guide-on"
          onClick={() => onHandGuideChange && onHandGuideChange(true)}
        />
      </div>
    );
  },
}));
vi.mock('../../components/Workshop/RecordPanel', () => ({
  __esModule: true,
  default: function MockRecordPanel({ onRecordingChange }) {
    return (
      <div data-testid="record-panel">
        <button
          type="button"
          data-testid="record-panel-recording-on"
          onClick={() => onRecordingChange && onRecordingChange(true)}
        />
      </div>
    );
  },
}));

// ── Blockly + the block-registration modules (they pull in blockly/core). ──
vi.mock('blockly/core', () => ({
  __esModule: true,
  svgResize: vi.fn(),
  Events: { SELECTED: 'selected' },
}));
const mockDestinations = vi.hoisted(() => ({
  setDriveToHandler: vi.fn(),
  applyPinnedCoordinates: vi.fn(),
}));
vi.mock('../../components/Workshop/blocks/destinations', () => ({
  __esModule: true,
  ...mockDestinations,
}));
// The document's destination store: spies over a mutable entry list.
const mockStore = vi.hoisted(() => {
  const store = { entries: [] };
  store.getEntries = vi.fn(() => store.entries);
  store.add = vi.fn();
  store.rename = vi.fn();
  return store;
});
const mockDestinationStoreModule = vi.hoisted(() => ({
  nextAutoName: vi.fn(() => 'Ziel 1'),
  takenDestinationNames: vi.fn(() => ['Ablage']),
}));
vi.mock('../../components/Workshop/sammlung/destinationStore', () => ({
  __esModule: true,
  MAX_DESTINATION_ENTRIES: 64,
  getDestinationStore: () => mockStore,
  nextAutoName: (...a) => mockDestinationStoreModule.nextAutoName(...a),
  takenDestinationNames: (...a) => mockDestinationStoreModule.takenDestinationNames(...a),
}));
vi.mock('../../components/Workshop/blocks/perception', () => ({
  __esModule: true,
  setObjectCatalogOptions: vi.fn(),
  setWorkspaceAccessor: vi.fn(),
}));

// ── Hooks + services. ──
const mockRos = vi.hoisted(() => ({
  getObjectCatalog: vi.fn(() =>
    Promise.resolve({
      success: true,
      type_names: ['wuerfel'],
      labels_de: ['Würfel'],
      object_height_m: [0.03],
      object_width_m: [0.03],
      color_hex: ['#f59e0b'],
      max_instances: [2],
    }),
  ),
  capturePose: vi.fn(),
  jogArm: vi.fn(),
}));
vi.mock('../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));
vi.mock('../../hooks/useRosTopicSubscription', () => ({
  __esModule: true,
  useRosTopicSubscription: () => ({
    subscribeToWorkflowStatus: vi.fn(),
    subscribeToWorkflowSensors: vi.fn(),
    connected: true,
  }),
}));
vi.mock('../../components/Workshop/useAutosave', () => ({
  __esModule: true,
  useAutosave: () => ({ lastSavedAt: null }),
}));
const mockApi = vi.hoisted(() => ({
  getWorkflow: vi.fn(() => Promise.resolve(null)),
  createWorkflow: vi.fn(() => Promise.resolve({ id: 'wf-new' })),
  updateWorkflow: vi.fn(() => Promise.resolve({})),
}));
vi.mock('../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  }),
  // RS-34: WorkshopPage now runs the shared TOAST_LIMIT dismissal loop, so
  // the store hook has to exist on the mock (same idiom as
  // TrainingPage.gate.test.js). An empty stack means the loop is a no-op
  // here; the cap itself is covered by WorkshopPage.toastCap.test.jsx.
  useToasterStore: () => ({ toasts: [] }),
}));

function baseState(over = {}) {
  return {
    workshop: {
      hasIntrinsicScene: true,
      hasHandeyeScene: true,
      hasTableTouch: true,
      recalibrating: false,
      pendingVerify: false,
      selectedWorkflowId: null,
      unsavedBlocklyJson: null,
      restrictedBlocks: null,
      activeTutorialId: null,
      runState: 'idle',
      log: [],
      debuggerWarnings: [],
      ...over,
    },
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected', taskStatus: { robotType: 'omx_f' } },
  };
}

// A fake Blockly workspace: SELECTED listeners are captured so a test can
// "select" a pin block the way Blockly's change event does.
function makeWorkspace(blocks = {}) {
  const listeners = [];
  return {
    listeners,
    addChangeListener: vi.fn((fn) => { listeners.push(fn); }),
    getBlockById: vi.fn((id) => blocks[id] || null),
    select(id) { listeners.forEach((fn) => fn({ type: 'selected', newElementId: id })); },
  };
}

function pinBlock(name) {
  return { type: 'edubotics_destination_pin', getFieldValue: vi.fn(() => name) };
}

async function mountWith(ws) {
  mockWorkspace.current = ws;
  render(<WorkshopPage isActive />);
  await waitFor(() => expect(mockOverlay.props).not.toBeNull());
  await waitFor(() => expect(ws.addChangeListener).toHaveBeenCalled());
  return mockOverlay.props;
}

beforeEach(() => {
  mockState = baseState();
  mockOverlay.props = null;
  mockDispatch.mockClear();
  mockStore.entries = [];
  mockStore.getEntries.mockClear();
  mockStore.add.mockReset();
  mockStore.add.mockImplementation((input) => ({
    ok: true, entry: { id: 'd_00000001', name: input.name, kind: input.kind },
  }));
  mockStore.rename.mockReset();
  mockStore.rename.mockImplementation(() => ({ ok: true }));
  mockDestinationStoreModule.nextAutoName.mockClear();
  mockDestinationStoreModule.takenDestinationNames.mockClear();
  mockDestinations.applyPinnedCoordinates.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
  toast.mockClear();
});

describe('WorkshopPage — the camera click resolves ONE name for one point', () => {
  test('no remembered pin → an automatic „Ziel n" for the document store', async () => {
    const ws = makeWorkspace();
    const props = await mountWith(ws);
    expect(typeof props.resolveMarkLabel).toBe('function');
    expect(typeof props.onRenameMark).toBe('function');

    let intent;
    act(() => { intent = props.resolveMarkLabel(); });
    expect(intent).toEqual({ label: 'Ziel 1', target: 'store' });
    expect(mockDestinationStoreModule.nextAutoName).toHaveBeenCalledWith('Ziel %1', ['Ablage']);
  });

  test('a store target adds a camera pin stamped with the rig and toasts the new name', async () => {
    const ws = makeWorkspace();
    const props = await mountWith(ws);
    let created;
    act(() => {
      created = mockOverlay.props.onMark({
        label: 'Ziel 1', target: 'store', world_x: 0.2, world_y: -0.05, world_z: 0.01,
      });
    });
    expect(props).toBeTruthy();
    expect(mockStore.add).toHaveBeenCalledTimes(1);
    expect(mockStore.add.mock.calls[0][0]).toMatchObject({
      name: 'Ziel 1', kind: 'pin', source: 'camera', robot_type: 'omx_f', x: 0.2, y: -0.05, z: 0.01,
    });
    expect(toast.success).toHaveBeenCalledWith('Ziel „Ziel 1" gesetzt.');
    expect(created).toEqual({ entryId: 'd_00000001', name: 'Ziel 1' });
    expect(mockDestinations.applyPinnedCoordinates).not.toHaveBeenCalled();
    // The old „Tipp: Wähle zuerst einen …" nag is gone.
    expect(toast).not.toHaveBeenCalled();
  });

  test('a refused add toasts the store sentence and opens no rename field', async () => {
    const ws = makeWorkspace();
    await mountWith(ws);
    mockStore.add.mockImplementation(() => ({ ok: false, error: 'Der Name „Ziel 1" ist schon vergeben.' }));
    let created;
    act(() => {
      created = mockOverlay.props.onMark({
        label: 'Ziel 1', target: 'store', world_x: 0.2, world_y: 0, world_z: 0.01,
      });
    });
    expect(created).toBeNull();
    expect(toast.error).toHaveBeenCalledWith('Der Name „Ziel 1" ist schon vergeben.');
  });

  test('a remembered pin block → its own name; onMark writes the block, never the store', async () => {
    const block = pinBlock('Ablage');
    const ws = makeWorkspace({ pin1: block });
    await mountWith(ws);
    act(() => { ws.select('pin1'); });

    let intent;
    act(() => { intent = mockOverlay.props.resolveMarkLabel(); });
    expect(intent).toEqual({ label: 'Ablage', target: 'block', blockId: 'pin1' });

    let created;
    act(() => {
      created = mockOverlay.props.onMark({ ...intent, world_x: 0.1, world_y: 0.02, world_z: 0.03 });
    });
    expect(created).toBeNull();
    expect(mockDestinations.applyPinnedCoordinates).toHaveBeenCalledWith(block, 0.1, 0.02, 0.03);
    expect(mockStore.add).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith('Koordinaten in Block „Ablage" geschrieben.');
  });

  test('a store already at 64 entries → null and the „full" sentence', async () => {
    const ws = makeWorkspace();
    await mountWith(ws);
    mockStore.entries = Array.from({ length: 64 }, (_, i) => ({ id: `d_${i}`, name: `Ziel ${i + 1}` }));
    let intent;
    act(() => { intent = mockOverlay.props.resolveMarkLabel(); });
    expect(intent).toBeNull();
    expect(toast.error).toHaveBeenCalledWith(DE.ERR_STORE_FULL);
  });

  test('the inline rename goes through the store and reports a refusal', async () => {
    const ws = makeWorkspace();
    await mountWith(ws);
    let res;
    act(() => { res = mockOverlay.props.onRenameMark('d_00000001', 'Kiste'); });
    expect(mockStore.rename).toHaveBeenCalledWith('d_00000001', 'Kiste');
    expect(res).toEqual({ ok: true });

    mockStore.rename.mockImplementation(() => ({ ok: false, error: 'Der Name „Kiste" ist schon vergeben.' }));
    act(() => { res = mockOverlay.props.onRenameMark('d_00000001', 'Kiste'); });
    expect(res.ok).toBe(false);
    expect(toast.error).toHaveBeenCalledWith('Der Name „Kiste" ist schon vergeben.');
  });
});
