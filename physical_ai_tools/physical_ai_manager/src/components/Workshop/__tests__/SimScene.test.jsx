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
import { render as rtlRender, screen, fireEvent, waitFor, act, within } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import SimScene from '../SimScene';
import { DE } from '../blocks/messages_de';

// The /sim/objects subscription, under test control. The hook itself has its own
// wire contract; what matters here is WHEN SimScene is allowed to believe it.
const simObjects = vi.hoisted(() => ({ scene: null, args: null }));
vi.mock('../../../hooks/useSimObjects', () => ({
  __esModule: true,
  default: (...args) => {
    simObjects.args = args;
    return simObjects.scene;
  },
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
// band) via useSelector — wrap every render in a minimal store.
// null caps = the OMX constants, keeping every OMX assertion identical.
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


// ── the simulator's 3D stream is dense AND in step with the scene (2026-09-11) ─
// SimArm now plays every waypoint at its own time (~30 Hz). The twin must let
// all of them through, and the scene events that ride along with them must be
// held back by the same delay the twin draws the arm with, or a grasp attaches
// the cube to jaws still 100 ms of motion away.
describe('SimScene — the 3D stream and the scene share one timeline', () => {
  test('the twin subscribes to /sim/joint_states below the 30 Hz waypoint spacing', async () => {
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(twinProps.current.jointTopic).toBe('/sim/joint_states');
    expect(twinProps.current.jointThrottleMs).toBe(20);
  });

  test('the scene is delayed by exactly the twin\'s interpolation delay', async () => {
    const { INTERP_DELAY_MS } = await import('../../../utils/jointStateInterpolator');
    render(<SimScene scene={ONE_CUBE} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(simObjects.args[2]).toEqual({ delayMs: INTERP_DELAY_MS });
    expect(INTERP_DELAY_MS).toBe(100);
  });
});

describe('SimScene — Ziel setzen and Sammlung markers', () => {
  const giveSvgABox = () => {
    const svg = screen.getByRole('application', {
      name: 'Simulator-Tisch — Objekte, Sperrzonen und Ziele platzieren',
    });
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: SVG_W, height: SVG_H, right: SVG_W, bottom: SVG_H, x: 0, y: 0,
    });
    return svg;
  };

  test('the „Ziel setzen" mode is offered only when the page can create a Ziel', async () => {
    const { unmount } = render(
      <SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />,
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.queryByRole('button', { name: 'Ziel setzen' })).toBeNull();
    unmount();
    render(
      <SimScene
        scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}}
        onCreateDestination={() => {}}
      />,
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.getByRole('button', { name: 'Ziel setzen' })).toHaveAttribute('aria-pressed', 'false');
  });

  test('a tap in ziel mode creates a Ziel at base coordinates and places no object', async () => {
    const onChange = vi.fn();
    const onCreateDestination = vi.fn();
    render(
      <SimScene
        scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={onChange}
        onCreateDestination={onCreateDestination}
      />,
    );
    await screen.findByTestId('urdf-twin');
    fireEvent.click(screen.getByRole('button', { name: 'Ziel setzen' }));
    expect(screen.getByText('Tippe auf den Tisch — dort entsteht ein Ziel.')).toBeInTheDocument();
    const svg = giveSvgABox();
    // px 150 → y = 0.30 − 150/500 = 0; py 92.5 → x = 0.32 − 92.5/500 = 0.135.
    fireEvent.pointerDown(svg, { clientX: SVG_W / 2, clientY: SVG_H / 2 });
    expect(onCreateDestination).toHaveBeenCalledTimes(1);
    expect(onCreateDestination).toHaveBeenCalledWith({ x: 0.135, y: 0 });
    expect(onChange).not.toHaveBeenCalled();
    // NOT clamped into the reach annulus: a tap at the base (r ≈ 0) stays there.
    fireEvent.pointerDown(svg, { clientX: 150, clientY: 160 });
    expect(onCreateDestination).toHaveBeenLastCalledWith({ x: 0, y: 0 });
    expect(onChange).not.toHaveBeenCalled();
  });

  test('a new requestedMode token switches the editor into „Ziel setzen"', async () => {
    const props = {
      scene: { objects: [], zones: [] }, catalog: CATALOG, onChange: () => {}, onCreateDestination: () => {},
    };
    const { rerender, store } = render(<SimScene {...props} requestedMode={null} />);
    await screen.findByTestId('urdf-twin');
    expect(screen.getByRole('button', { name: 'Objekte platzieren' })).toHaveAttribute('aria-pressed', 'true');
    rerender(<Provider store={store}><SimScene {...props} requestedMode={{ mode: 'ziel', token: 1 }} /></Provider>);
    expect(screen.getByRole('button', { name: 'Ziel setzen' })).toHaveAttribute('aria-pressed', 'true');
    // The student switches back; the SAME token does not grab the mode again…
    fireEvent.click(screen.getByRole('button', { name: 'Objekte platzieren' }));
    rerender(<Provider store={store}><SimScene {...props} requestedMode={{ mode: 'ziel', token: 1 }} /></Provider>);
    expect(screen.getByRole('button', { name: 'Objekte platzieren' })).toHaveAttribute('aria-pressed', 'true');
    // …a NEW request does.
    rerender(<Provider store={store}><SimScene {...props} requestedMode={{ mode: 'ziel', token: 2 }} /></Provider>);
    expect(screen.getByRole('button', { name: 'Ziel setzen' })).toHaveAttribute('aria-pressed', 'true');
  });

  test('losing onCreateDestination while in „Ziel setzen" leaves ziel mode: a tap places an object again', async () => {
    const onChange = vi.fn();
    const onCreateDestination = vi.fn();
    const props = { scene: { objects: [], zones: [] }, catalog: CATALOG, onChange };
    const { rerender, store } = render(
      <SimScene {...props} onCreateDestination={onCreateDestination} requestedMode={{ mode: 'ziel', token: 1 }} />,
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.getByText('Tippe auf den Tisch — dort entsteht ein Ziel.')).toBeInTheDocument();
    rerender(<Provider store={store}><SimScene {...props} onCreateDestination={null} requestedMode={{ mode: 'ziel', token: 1 }} /></Provider>);
    expect(screen.queryByText('Tippe auf den Tisch — dort entsteht ein Ziel.')).toBeNull();
    const svg = giveSvgABox();
    fireEvent.pointerDown(svg, { clientX: SVG_W / 2, clientY: SVG_H / 2 });
    expect(onCreateDestination).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  test('ghostJoints reach the one twin by identity (null by default)', async () => {
    const ghostJoints = { names: ['joint1', 'joint2'], positions: [0.1, -0.4] };
    const props = { scene: { objects: [], zones: [] }, catalog: CATALOG, onChange: () => {} };
    const { rerender, store } = render(<SimScene {...props} />);
    await screen.findByTestId('urdf-twin');
    expect(twinProps.current.ghostJoints).toBeNull();
    rerender(<Provider store={store}><SimScene {...props} ghostJoints={ghostJoints} /></Provider>);
    expect(twinProps.current.ghostJoints).toBe(ghostJoints);
  });

  test('markers render on the 2D table with their labels and reach the twin', async () => {
    const markers = [
      { id: 'd_00000001', label: 'Ablage', kind: 'pin', x: 0.18, y: -0.06, z: 0, highlighted: false },
      { id: 'd_00000002', label: 'Über der Kiste', kind: 'pose', x: 0.14, y: 0.1, z: 0.12, highlighted: true },
    ];
    const { container } = render(
      <SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} markers={markers} />,
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.getByText('Ablage')).toBeInTheDocument();
    expect(screen.getByText('Über der Kiste')).toBeInTheDocument();
    expect(twinProps.current.markers).toBe(markers);
    // SVG groups have no ARIA role, so a DOM query is the only way to assert on them.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const pin = container.querySelector('g[data-marker-kind="pin"]');
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const pose = container.querySelector('g[data-marker-kind="pose"]');
    expect(pin.getAttribute('pointer-events')).toBe('none');
    // eslint-disable-next-line testing-library/no-node-access
    expect(pin.querySelector('circle').getAttribute('fill')).toBe('#f59e0b');
    // eslint-disable-next-line testing-library/no-node-access
    expect(pin.querySelector('circle').getAttribute('r')).toBe('5');
    // eslint-disable-next-line testing-library/no-node-access
    const poseRect = pose.querySelector('rect');
    expect(poseRect.getAttribute('fill')).toBe('#14b8a6');
    expect(poseRect.getAttribute('stroke-width')).toBe('3');
    expect(poseRect.getAttribute('width')).toBe('10');
  });
});

// R0-3 / S-3: the split stage gives the table its room — a 3D ↔ table divider,
// a notice that can be closed, and from md up the whole table FITTED into its
// area. The stack layout keeps everything as it was.
// N6/N11 (owner decision Q6): the V1 reaches ±90° only. Its back half is
// out of reach on the rig, so the simulator draws the sector, snaps a tap
// behind the arm onto the nearest sector edge, and marks — never moves — a
// saved object that lies outside the shape.
describe('SimScene — the reach shape of the arm (N6/N11)', () => {
  const V1_REACH = { reach_inner_m: 0.10, reach_outer_m: 0.34, reach_sector_deg: [-90, 90] };
  const giveBox = () => {
    const svg = screen.getByRole('application', {
      name: 'Simulator-Tisch — Objekte, Sperrzonen und Ziele platzieren',
    });
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: SVG_W, height: SVG_H, right: SVG_W, bottom: SVG_H, x: 0, y: 0,
    });
    return svg;
  };

  test('a_tap_behind_the_v1_is_snapped_into_the_sector', async () => {
    const onChange = vi.fn();
    render(
      <SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={onChange} />,
      { capabilities: V1_REACH },
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.getByTestId('reach-sector')).toBeInTheDocument();
    const svg = giveBox();
    // px 75 → y = 0.30 − 75/500 = 0.15; py 175 → x = 0.32 − 175/500 = −0.03:
    // behind the arm, bearing ≈ 101°, nearest the +90° edge.
    fireEvent.pointerDown(svg, { clientX: 75, clientY: 175 });
    expect(onChange).toHaveBeenCalledTimes(1);
    const [placed] = onChange.mock.calls[0][0].objects;
    expect(placed.x).toBe(0);
    expect(placed.y).toBe(0.153);
    expect(mockToast).toHaveBeenCalledWith('Objekt in den Greifbereich verschoben.', expect.anything());
  });

  test('the OMX keeps its full ring (no sector drawn, a tap behind stays behind)', async () => {
    const onChange = vi.fn();
    render(
      <SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={onChange} />,
      { capabilities: { reach_inner_m: 0.10, reach_outer_m: 0.25 } },
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.queryByTestId('reach-sector')).toBeNull();
    const svg = giveBox();
    // x = −0.03, y = 0.15 (r ≈ 0.153): inside the ring, behind the base.
    fireEvent.pointerDown(svg, { clientX: 75, clientY: 175 });
    const [placed] = onChange.mock.calls[0][0].objects;
    expect([placed.x, placed.y]).toEqual([-0.03, 0.15]);
  });

  test('a_saved_object_outside_the_shape_is_kept_and_marked', async () => {
    const onChange = vi.fn();
    const scene = {
      objects: [
        { type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 },
        { type: 'wuerfel', tag_id: 1, x: -0.03, y: 0.15, yaw: 0 },
      ],
      zones: [],
    };
    const { container } = render(
      <SimScene scene={scene} catalog={CATALOG} onChange={onChange} />,
      { capabilities: V1_REACH },
    );
    await screen.findByTestId('urdf-twin');
    // Kept where it was saved: loading never moves an object.
    expect(onChange).not.toHaveBeenCalled();
    // SVG groups have no ARIA role, so a DOM query is the only way to assert on them.
    // eslint-disable-next-line testing-library/no-container, testing-library/no-node-access
    const groups = [...container.querySelectorAll('[data-object-tag]')];
    const byTag = Object.fromEntries(groups.map((g) => [g.getAttribute('data-object-tag'), g]));
    expect(byTag['0'].getAttribute('data-unreachable')).toBe('false');
    expect(byTag['1'].getAttribute('data-unreachable')).toBe('true');
    expect(byTag['1'].textContent).toContain('nicht erreichbar');
    expect(byTag['0'].textContent).not.toContain('nicht erreichbar');
    expect(within(byTag['1']).getByTestId('unreachable-ring')).toBeInTheDocument();
    expect(within(byTag['0']).queryByTestId('unreachable-ring')).toBeNull();
  });
});

describe('SimScene — the split stage (WI-1.8)', () => {
  const svgOf = () => screen.getByRole('application');
  let observers;
  let originalRO;
  let originalMatchMedia;
  beforeEach(() => {
    observers = [];
    originalRO = window.ResizeObserver;
    originalMatchMedia = window.matchMedia;
    window.ResizeObserver = class {
      constructor(cb) { this.cb = cb; observers.push(this); }

      observe(el) { this.el = el; }

      disconnect() { this.gone = true; }
    };
  });
  afterEach(() => {
    window.ResizeObserver = originalRO;
    window.matchMedia = originalMatchMedia;
  });
  // eslint-disable-next-line testing-library/no-node-access
  const tableObserver = () => observers.find((o) => o.el === svgOf().parentElement);
  const sizeTable = (width, height) => act(() => {
    tableObserver().cb([{ contentRect: { width, height } }]);
  });

  test('the split layout has a 3D ↔ table divider; the stack layout has none', async () => {
    const { unmount } = render(<SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    const sep = screen.getByRole('separator', { name: 'Aufteilung zwischen 3D-Ansicht und Tisch' });
    expect(sep).toHaveAttribute('aria-orientation', 'horizontal');
    // Hidden below md (the stage scrolls there).
    expect(sep.className).toContain('hidden md:flex');
    unmount();
    render(<SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(screen.queryByRole('separator')).toBeNull();
  });

  test('the split notice has a close button that calls onDismissIntro; dismissed, it is gone', async () => {
    const onDismissIntro = vi.fn();
    const { rerender, store } = render(
      <SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} onDismissIntro={onDismissIntro} />,
    );
    await screen.findByTestId('urdf-twin');
    expect(screen.getByText(/Test im Simulator\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Hinweis ausblenden' }));
    expect(onDismissIntro).toHaveBeenCalledTimes(1);
    rerender(
      <Provider store={store}>
        <SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} onDismissIntro={onDismissIntro} introDismissed />
      </Provider>,
    );
    expect(screen.queryByText(/Test im Simulator\./)).toBeNull();
  });

  // Owner decision Q5 (fix round 2): a Lernpfad needs the calibrated real rig,
  // and the simulator says so in its own „Test im Simulator." notice — part of
  // the same text (one notice for a screen reader), closed with it. The studio
  // header carries no note and no disabled „Lernpfad" key any more.
  test('the notice says Lernpfade need the real robot, and the sentence closes with it', async () => {
    const onDismissIntro = vi.fn();
    const { rerender, store } = render(
      <SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} onDismissIntro={onDismissIntro} />,
    );
    await screen.findByTestId('urdf-twin');
    const sentence = screen.getByText(DE.TUTORIAL_REAL_ONLY);
    expect(DE.TUTORIAL_REAL_ONLY).toBe('Lernpfade gibt es nur am echten Roboter.');
    // The same notice as „Test im Simulator.", read as one piece.
    // eslint-disable-next-line testing-library/no-node-access
    const notice = screen.getByText('Test im Simulator.').parentElement;
    expect(notice).toContainElement(sentence);
    expect(notice).toContainElement(screen.getByRole('button', { name: 'Hinweis ausblenden' }));
    expect(notice.textContent).toContain(
      'lass das Programm den virtuellen Roboter steuern. Lernpfade gibt es nur am echten Roboter.',
    );
    // Visible text, not a title or an aria-only label.
    expect(sentence).toBeVisible();
    expect(sentence.getAttribute('aria-hidden')).toBeNull();
    rerender(
      <Provider store={store}>
        <SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} onDismissIntro={onDismissIntro} introDismissed />
      </Provider>,
    );
    expect(screen.queryByText('Test im Simulator.')).toBeNull();
    expect(screen.queryByText(DE.TUTORIAL_REAL_ONLY)).toBeNull();
  });

  test('the stack layout keeps its notice, with no close button', async () => {
    render(<SimScene scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} onDismissIntro={() => {}} introDismissed />);
    await screen.findByTestId('urdf-twin');
    expect(screen.getByText(/Test im Simulator\./)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Hinweis ausblenden' })).toBeNull();
  });

  test('from md up the table is fitted by height into its area (viewBox ratio kept)', async () => {
    window.matchMedia = vi.fn(() => ({ matches: true }));
    render(<SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    sizeTable(400, 200);
    expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 768px)');
    expect(svgOf().style.width).toBe('324px');
    expect(svgOf().style.height).toBe('200px');
    expect(svgOf().getAttribute('class')).toContain('block');
    expect(svgOf().getAttribute('class')).not.toContain('w-full');
    // A wide-short area fits by width instead.
    sizeTable(150, 400);
    expect(svgOf().style.width).toBe('150px');
    expect(svgOf().style.height).toBe('93px');
  });

  test('below md the table stays full width (no inline size)', async () => {
    window.matchMedia = vi.fn(() => ({ matches: false }));
    render(<SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    sizeTable(400, 200);
    expect(svgOf().getAttribute('class')).toContain('w-full h-auto');
    expect(svgOf().style.width).toBe('');
    expect(svgOf().style.height).toBe('');
  });

  test('without window.matchMedia (jsdom) nothing is fitted and nothing throws', async () => {
    delete window.matchMedia;
    render(<SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    expect(() => sizeTable(400, 200)).not.toThrow();
    expect(svgOf().getAttribute('class')).toContain('w-full h-auto');
    expect(svgOf().style.width).toBe('');
  });

  // Verification round 1, F3: from md up the editor column is short and wide
  // and the fitted table gets only the height its siblings leave (165 × 102 px
  // at 1366 × 768). There the reset button joins the Objekt/Modus row and
  // „Tisch leeren" the help/selection slot under the table, so no control
  // takes a row of its own; below md every piece keeps its own row.
  // eslint-disable-next-line testing-library/no-node-access
  const rowOf = (el) => el.parentElement.closest('.flex-wrap');
  const OBJ = { tag_id: 20, type: 'wuerfel', x: 0.2, y: 0, yaw: 0 };

  test('from md up the reset button shares the Objekt/Modus row, and „Tisch leeren" the help row', async () => {
    window.matchMedia = vi.fn(() => ({ matches: true }));
    render(<SimScene layout="split" scene={{ objects: [OBJ], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    const reset = screen.getByRole('button', { name: 'Simulator zurücksetzen' });
    const controls = rowOf(reset);
    expect(within(controls).getByRole('button', { name: 'Objekte platzieren' })).toBeInTheDocument();
    expect(within(controls).getByText('Objekt:')).toBeInTheDocument();
    const help = screen.getByText(/Objekt-Typ wählen, dann in die grüne Reichweite tippen/);
    // eslint-disable-next-line testing-library/no-node-access
    const slot = help.parentElement;
    expect(within(slot).getByRole('button', { name: 'Tisch leeren' })).toBeInTheDocument();
    expect(within(slot).queryByRole('button', { name: 'Simulator zurücksetzen' })).toBeNull();
    // The table is the only piece between the two rows.
    // eslint-disable-next-line testing-library/no-node-access
    expect(controls.nextElementSibling).toBe(svgOf().parentElement);
    // eslint-disable-next-line testing-library/no-node-access
    expect(svgOf().parentElement.nextElementSibling).toBe(slot);
  });

  test('below md (and without matchMedia) each piece keeps its own row', async () => {
    window.matchMedia = vi.fn(() => ({ matches: false }));
    render(<SimScene layout="split" scene={{ objects: [OBJ], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    const reset = screen.getByRole('button', { name: 'Simulator zurücksetzen' });
    const resetRow = rowOf(reset);
    expect(within(resetRow).getByRole('button', { name: 'Tisch leeren' })).toBeInTheDocument();
    expect(within(resetRow).queryByRole('button', { name: 'Objekte platzieren' })).toBeNull();
    const help = screen.getByText(/Objekt-Typ wählen, dann in die grüne Reichweite tippen/);
    // eslint-disable-next-line testing-library/no-node-access
    expect(help.nextElementSibling).toBe(resetRow);
  });

  // Verification round 2, G1: crossing md re-arranges the editor's pieces, and
  // React reuses DOM nodes by position, so the table's wrap becomes ANOTHER
  // node (child 3 narrow, child 2 wide). An observer that stayed on the old
  // node measured the 31-px help row: the table shrank to 51 × 31 px after
  // 700 → 1366 px. The fitting observer must always watch the node that is the
  // table's wrap NOW, in both directions and however often md is crossed.
  test('crossing md keeps the fitting observer on the table\'s wrap, so the fitted size follows', async () => {
    const media = { matches: false, listeners: new Set() };
    window.matchMedia = vi.fn(() => ({
      get matches() { return media.matches; },
      addEventListener: (type, fn) => { if (type === 'change') media.listeners.add(fn); },
      removeEventListener: (type, fn) => { if (type === 'change') media.listeners.delete(fn); },
    }));
    const crossMd = (matches) => act(() => {
      media.matches = matches;
      [...media.listeners].forEach((fn) => fn({ matches }));
    });
    render(<SimScene layout="split" scene={{ objects: [OBJ], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
    await screen.findByTestId('urdf-twin');
    // eslint-disable-next-line testing-library/no-node-access
    const column = screen.getByRole('separator').parentElement;
    // The live observers other than the divider's own (it watches the column).
    const fitObservers = () => observers.filter((o) => !o.gone && o.el !== column);
    const expectWatchesTheWrap = () => {
      const live = fitObservers();
      expect(live).toHaveLength(1);
      // eslint-disable-next-line testing-library/no-node-access
      expect(live[0].el).toBe(svgOf().parentElement);
      return live[0];
    };
    expectWatchesTheWrap();
    // Narrow → wide: the wrap moved to another node, the observer with it.
    crossMd(true);
    act(() => expectWatchesTheWrap().cb([{ contentRect: { width: 400, height: 200 } }]));
    expect(svgOf().style.width).toBe('324px');
    expect(svgOf().style.height).toBe('200px');
    // Wide → narrow: full width again, and the observer still on the wrap.
    crossMd(false);
    act(() => expectWatchesTheWrap().cb([{ contentRect: { width: 400, height: 200 } }]));
    expect(svgOf().getAttribute('class')).toContain('w-full h-auto');
    expect(svgOf().style.width).toBe('');
    // And back up once more, with a different area.
    crossMd(true);
    act(() => expectWatchesTheWrap().cb([{ contentRect: { width: 150, height: 400 } }]));
    expect(svgOf().style.width).toBe('150px');
    expect(svgOf().style.height).toBe('93px');
  });

  test('a stored 3D share is read back and a committed one is written', async () => {
    window.localStorage.setItem('edubotics_workshop_sim_split', '0.5');
    try {
      render(<SimScene layout="split" scene={{ objects: [], zones: [] }} catalog={CATALOG} onChange={() => {}} />);
      await screen.findByTestId('urdf-twin');
      const sep = screen.getByRole('separator');
      expect(sep).toHaveAttribute('aria-valuenow', '50');
      fireEvent.keyDown(sep, { key: 'ArrowUp' });
      expect(window.localStorage.getItem('edubotics_workshop_sim_split')).toBe('0.48');
    } finally {
      window.localStorage.removeItem('edubotics_workshop_sim_split');
    }
  });
});
