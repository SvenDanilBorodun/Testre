/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Sim-stage seam for the 2D table editor: per-type object size/colour from the
// catalog dims map + the per-type placement cap. The lazy 3D twin is mocked away
// (jsdom has no WebGL), so these assertions are pure-DOM on the 2D SVG editor.

import React from 'react';
import { render as rtlRender, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import SimScene from '../SimScene';

// The /sim/objects subscription, under test control. The hook itself has its own
// wire contract; what matters here is WHEN SimScene is allowed to believe it.
const simObjects = vi.hoisted(() => ({ scene: null }));
vi.mock('../../../hooks/useSimObjects', () => ({
  __esModule: true,
  default: () => simObjects.scene,
}));

// „Simulation zurücksetzen" calls /workflow/stop straight through roslib (see the
// callSimReset note in SimScene — useRosServiceCaller reads four store slices
// this component is not mounted with).
const mockServiceCtor = vi.hoisted(() => vi.fn());
const mockCallService = vi.hoisted(() => vi.fn());
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Service: function Service(opts) {
      mockServiceCtor(opts);
      this.callService = mockCallService;
    },
    ServiceRequest: function ServiceRequest(body) { return body || {}; },
  },
}));
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: () => Promise.resolve({ isConnected: true }) },
}));

// SimScene reads the capability manifest (profile-driven reach annulus + grasp
// band, edu6 §4.5) via useSelector — wrap every render in a minimal store.
// null caps = the OMX constants, keeping every pre-edu6 assertion identical.
// `workshop` answers the AUTHORITY question (is a program actually driving the
// virtual arm?) and is mutable through a test action so a single mounted tree can
// cross the running→idle edge, which is the edge the staleness bug lived on.
const SET_WORKSHOP = 'test/setWorkshop';
function makeStore({
  capabilities = null,
  workshop = { runState: 'idle', paused: false },
  rosbridgeUrl = 'ws://student-pc:9090',
} = {}) {
  return configureStore({
    reducer: {
      tasks: () => ({ taskStatus: { capabilities } }),
      ros: () => ({ rosbridgeUrl }),
      workshop: (state = workshop, action) =>
        (action.type === SET_WORKSHOP ? action.payload : state),
    },
  });
}

function render(ui, opts = {}) {
  const store = makeStore(opts);
  const utils = rtlRender(<Provider store={store}>{ui}</Provider>);
  return { ...utils, store };
}

// The 2D editor's SVG viewBox (SimScene-internal consts, mirrored here):
//   SVG_W = PX_PER_M(500) * (VIEW_MAX_Y 0.30 − VIEW_MIN_Y −0.30) = 300
//   SVG_H = PX_PER_M(500) * (VIEW_MAX_X 0.32 − VIEW_MIN_X −0.05) = 185
const SVG_W = 300;
const SVG_H = 185;

// Mock the lazy 3D twin (resolves to src/components/UrdfTwin, same module SimScene
// dynamic-imports as `../UrdfTwin`). A trivial component keeps three.js out.
// The twin's props are captured so a test can drive `onEndEffector` — the local
// geometric grasp fallback (used on a server image that does not publish
// /sim/objects) has no other entry point, and a bare <div/> mock left that whole
// path, including the guard that stops a parked arm grabbing on its own, with
// zero coverage.
const twinProps = vi.hoisted(() => ({ current: null }));
vi.mock('../../UrdfTwin', () => ({
  __esModule: true,
  default: (props) => {
    twinProps.current = props;
    return (
      <div
        data-testid="urdf-twin"
        data-held={String(props.heldObjectId)}
        data-sim-positions={JSON.stringify(props.simPositions ?? null)}
        data-sim-epoch={String(props.simEpoch)}
      />
    );
  },
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const CATALOG = [['Würfel', 'wuerfel']];

beforeEach(() => {
  mockToast.mockClear();
  mockToast.error.mockClear();
  mockToast.success.mockClear();
  mockServiceCtor.mockClear();
  mockCallService.mockReset();
  mockCallService.mockImplementation((_req, ok) => ok({ success: true, message: 'ok' }));
  simObjects.scene = null;
  twinProps.current = null;
});

describe('SimScene — sim-stage seam', () => {
  test('default (stack) layout renders the notice + palette', async () => {
    render(<SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(screen.getByText(/Test im Simulator\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Würfel' })).toBeInTheDocument();
  });

  test('2D square size + colour derive from catalogDims', async () => {
    const { container } = render(
      <SimScene
        scene={{ objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] }}
        catalog={CATALOG}
        catalogDims={{ wuerfel: { height_m: 0.05, width_m: 0.04, color: '#00ff00', max_instances: 2 } }}
        onChange={() => {}}
      />
    );
    await screen.findByTestId('urdf-twin');
    // width_m 0.04 × PX_PER_M 500 = 20 px, and the catalog colour. SVG <rect> has
    // no ARIA role, so a DOM query is the only way to assert on it.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const rect = Array.from(container.querySelectorAll('rect')).find(
      (r) => r.getAttribute('fill') === '#00ff00',
    );
    expect(rect).toBeTruthy();
    expect(rect.getAttribute('width')).toBe('20');
  });

  test('an object without catalog dims keeps the 15 px amber fallback square', async () => {
    const { container } = render(
      <SimScene
        scene={{ objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] }}
        catalog={CATALOG}
        catalogDims={{}}
        onChange={() => {}}
      />
    );
    await screen.findByTestId('urdf-twin');
    // SVG <rect> has no ARIA role, so a DOM query is the only way to assert on it.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const rect = Array.from(container.querySelectorAll('rect')).find(
      (r) => r.getAttribute('fill') === '#f59e0b',
    );
    expect(rect).toBeTruthy();
    expect(rect.getAttribute('width')).toBe('15');
  });

  test('placement cap: refuses a 3rd object of a capped type with a German toast', async () => {
    const onChange = vi.fn();
    render(
      <SimScene
        scene={{
          objects: [
            { type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 },
            { type: 'wuerfel', tag_id: 1, x: 0.18, y: 0.02, yaw: 0 },
          ],
          zones: [],
        }}
        catalog={CATALOG}
        catalogDims={{ wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 } }}
        onChange={onChange}
      />
    );
    await screen.findByTestId('urdf-twin');
    const svg = screen.getByRole('application');
    // jsdom returns a zero-size rect by default → eventToBase would bail before
    // reaching the cap; give the SVG a real box so the placement path runs.
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: SVG_W, height: SVG_H, right: SVG_W, bottom: SVG_H, x: 0, y: 0,
    });
    // A pointerdown near the annulus centre (object mode is the default).
    fireEvent.pointerDown(svg, { clientX: SVG_W / 2, clientY: SVG_H / 2 });

    expect(mockToast.error).toHaveBeenCalledTimes(1);
    expect(mockToast.error.mock.calls[0][0]).toMatch(/Höchstens 2 × „Würfel"/);
    // The cap short-circuits before emitting → no 3rd object placed.
    expect(onChange).not.toHaveBeenCalled();
  });

  test('placement cap does NOT fire when the type is under its max_instances', async () => {
    const onChange = vi.fn();
    render(
      <SimScene
        scene={{ objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] }}
        catalog={CATALOG}
        catalogDims={{ wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 } }}
        onChange={onChange}
      />
    );
    await screen.findByTestId('urdf-twin');
    const svg = screen.getByRole('application');
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: SVG_W, height: SVG_H, right: SVG_W, bottom: SVG_H, x: 0, y: 0,
    });
    fireEvent.pointerDown(svg, { clientX: SVG_W / 2, clientY: SVG_H / 2 });

    expect(mockToast.error).not.toHaveBeenCalled();
    // A 2nd wuerfel (under the cap of 2) was placed.
    expect(onChange).toHaveBeenCalledTimes(1);
    const emitted = onChange.mock.calls[0][0];
    expect(emitted.objects).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// Who owns where the objects are.
//
// The server's SimWorld is only MEANINGFUL while a program is driving it, but
// the node re-broadcasts the last world with force=True every 0.5 s forever and
// only resets it at the NEXT run start. Believing it unconditionally is what made
// the simulator go stale after „Stopp": the 3D pane stayed pinned to wherever the
// run left the cubes, so dragging one in the 2D editor moved the square while the
// mesh snapped back half a second later, and a cube stopped mid-carry stayed glued
// to the gripper for good.
// ---------------------------------------------------------------------------

const ONE_CUBE = { objects: [{ type: 'wuerfel', tag_id: 7, x: 0.15, y: 0, yaw: 0 }], zones: [] };
// The server says that cube ended the run somewhere else, still in the jaws.
const SERVER_SCENE = {
  epoch: 3,
  held: 0, // the object's KEY = its index in the list the editor sent
  objects: [{ key: 0, type: 'wuerfel', tag_id: 7, x: 0.22, y: -0.05, yaw: 0.4 }],
};

function twin() {
  return screen.getByTestId('urdf-twin');
}

describe('SimScene — the server owns the scene only while a run is in flight', () => {
  test('mid-run the server positions + grasp win', async () => {
    simObjects.scene = SERVER_SCENE;
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'running', paused: false } });
    await screen.findByTestId('urdf-twin');

    expect(JSON.parse(twin().getAttribute('data-sim-positions'))).toEqual({
      7: { x: 0.22, y: -0.05, yaw: 0.4 },
    });
    expect(twin()).toHaveAttribute('data-held', '7');
    expect(twin()).toHaveAttribute('data-sim-epoch', '3');
  });

  test('a debugger pause still counts as a run (paused holds on_workflow)', async () => {
    simObjects.scene = SERVER_SCENE;
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'idle', paused: true } });
    await screen.findByTestId('urdf-twin');

    expect(twin()).toHaveAttribute('data-held', '7');
  });

  test('between runs the EDITOR wins — the stale server scene is ignored', async () => {
    simObjects.scene = SERVER_SCENE;
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'idle', paused: false } });
    await screen.findByTestId('urdf-twin');

    // null → UrdfTwin's simPosOf falls back to the placement coordinates, which
    // is what makes dragging a cube in the 2D editor move the 3D mesh again.
    expect(twin()).toHaveAttribute('data-sim-positions', 'null');
    expect(twin()).toHaveAttribute('data-held', 'null');
  });

  test('crossing running → idle DROPS a latched grasp', async () => {
    simObjects.scene = SERVER_SCENE;
    const { store } = render(
      <SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'running', paused: false } },
    );
    await screen.findByTestId('urdf-twin');
    expect(twin()).toHaveAttribute('data-held', '7');

    // „Stopp". The server keeps publishing `held` — the client must stop believing it.
    store.dispatch({ type: SET_WORKSHOP, payload: { runState: 'idle', paused: false } });
    await waitFor(() => expect(twin()).toHaveAttribute('data-held', 'null'));
    expect(twin()).toHaveAttribute('data-sim-positions', 'null');
  });
});

describe('SimScene — „Simulator zurücksetzen"', () => {
  test('is always offered, even with an empty table', async () => {
    render(<SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(screen.getByRole('button', { name: 'Simulator zurücksetzen' })).toBeEnabled();
    // „Tisch leeren" stays conditional on there being something to clear.
    expect(screen.queryByRole('button', { name: 'Tisch leeren' })).toBeNull();
  });

  test('calls /workflow/stop and reports success in German', async () => {
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');

    fireEvent.click(screen.getByRole('button', { name: 'Simulator zurücksetzen' }));

    await waitFor(() => expect(mockCallService).toHaveBeenCalledTimes(1));
    expect(mockServiceCtor).toHaveBeenCalledWith(
      expect.objectContaining({
        name: '/workflow/stop',
        serviceType: 'physical_ai_interfaces/srv/StopWorkflow',
      }),
    );
    await waitFor(() =>
      expect(mockToast.success).toHaveBeenCalledWith('Simulator zurückgesetzt.'));
  });

  test('is DISABLED while a program runs — it must never halt a real arm', async () => {
    // /workflow/stop resolves the ACTIVE manager, which on this page can be the
    // REAL one (start a normal program, then press „Test im Simulator"). Enabled,
    // the button would stop the physical arm and report a simulator reset.
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'running', paused: false } });
    await screen.findByTestId('urdf-twin');

    const btn = screen.getByRole('button', { name: 'Simulator zurücksetzen' });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', expect.stringContaining('Stopp'));

    fireEvent.click(btn);
    expect(mockCallService).not.toHaveBeenCalled();
  });

  test('a paused debugger run counts as running for the same reason', async () => {
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'idle', paused: true } });
    await screen.findByTestId('urdf-twin');
    expect(screen.getByRole('button', { name: 'Simulator zurücksetzen' })).toBeDisabled();
  });

  test('a refused service still resets locally and says so — never a bare error', async () => {
    mockCallService.mockImplementation((_req, _ok, fail) => fail('no such service'));
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');

    fireEvent.click(screen.getByRole('button', { name: 'Simulator zurücksetzen' }));

    await waitFor(() => expect(mockToast).toHaveBeenCalledWith(
      'Simulator lokal zurückgesetzt — der Roboter-Dienst hat nicht geantwortet.',
      expect.anything(),
    ));
    expect(mockToast.error).not.toHaveBeenCalled();
    // Re-armed for the next press rather than stuck on „Wird zurückgesetzt …".
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Simulator zurücksetzen' })).toBeEnabled());
  });
});

// ---------------------------------------------------------------------------
// The LOCAL geometric grasp fallback — the path taken on a server image that
// does not publish /sim/objects at all. `simObjects.scene` stays null in every
// test here, which is exactly that image.
// ---------------------------------------------------------------------------

function closeGripperOnTheCube() {
  // The cube sits at (0.15, 0); the OMX close threshold is 0.2 rad.
  act(() => twinProps.current.onEndEffector({ x: 0.15, y: 0, gripper: 0.0 }));
}

describe('SimScene — a parked arm must not grasp on its own', () => {
  test('mid-run, closing the gripper over a cube captures it (control)', async () => {
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'running', paused: false } });
    await screen.findByTestId('urdf-twin');

    closeGripperOnTheCube();
    await waitFor(() => expect(twin()).toHaveAttribute('data-held', '7'));
  });

  test('between runs the same callback grabs NOTHING', async () => {
    // /sim/joint_states keeps idling at 2 Hz with the last commanded pose, so an
    // arm left parked with a closed gripper would otherwise capture a cube the
    // student had just dragged under it — a grasp with no program behind it.
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'idle', paused: false } });
    await screen.findByTestId('urdf-twin');

    closeGripperOnTheCube();
    expect(twin()).toHaveAttribute('data-held', 'null');
  });

  test('the grasp latch is cleared on the run→idle edge, so the NEXT run can grab', async () => {
    // Without `graspRef.current.closed = false` on that edge, a run that ended
    // with the gripper closed leaves the latch set: the next run never sees a
    // closing EDGE and the fallback silently never captures again.
    const { store } = render(
      <SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />,
      { workshop: { runState: 'running', paused: false } },
    );
    await screen.findByTestId('urdf-twin');

    closeGripperOnTheCube();
    await waitFor(() => expect(twin()).toHaveAttribute('data-held', '7'));

    // „Stopp" — with the gripper still closed, as a mid-carry stop leaves it.
    store.dispatch({ type: SET_WORKSHOP, payload: { runState: 'idle', paused: false } });
    await waitFor(() => expect(twin()).toHaveAttribute('data-held', 'null'));

    // A new run. The dispatch is awaited inside act() because `runningRef` is
    // written by an effect, and the grasp callback reads that ref — an unflushed
    // re-render would make this test pass or fail for the wrong reason.
    await act(async () => {
      store.dispatch({ type: SET_WORKSHOP, payload: { runState: 'running', paused: false } });
    });
    // No intervening OPEN — only the latch reset can make the next line work.
    closeGripperOnTheCube();
    await waitFor(() => expect(twin()).toHaveAttribute('data-held', '7'));
  });
});
