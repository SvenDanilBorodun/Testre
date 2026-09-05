// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Wire-contract lock for the 3D follower twin (PR-7). jsdom has no WebGL, so
// three.js + urdf-loader are mocked to no-ops; what we actually assert is the
// rosbridge contract the real arm depends on:
//   * subscribes to the BARE GLOBAL topic /joint_states,
//   * messageType sensor_msgs/msg/JointState (the /msg/ wire form this app uses),
//   * throttle_rate 100 ms / queue_length 1 (monitor-view budget),
//   * a delivered JointState maps msg.name[i] -> msg.position[i] onto the
//     robot via setJointValue for the 6 follower joints, and ignores unknowns.
//
// Mock idiom mirrors ImageGridCell.test.js: vi.mock() is hoisted above imports,
// and the `mock*`-prefixed factory vars are exempt from the TDZ guard.

import React from 'react';
import { render, screen, waitFor, act, cleanup } from '@testing-library/react';
import UrdfTwin from '../UrdfTwin';

// --- react-redux: selector-aware stub over a mutable module-level state. ---
let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

// --- rosbridge connection manager: resolve a dummy ros handle. ---
vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ rosHandle: true })) },
}));

// --- roslib Topic: capture constructor opts + the subscribe callback. ---
const mockTopicCtor = vi.fn();
const mockSubscribe = vi.fn();
const mockUnsubscribe = vi.fn();
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      mockTopicCtor(opts);
      this.subscribe = mockSubscribe;
      this.unsubscribe = mockUnsubscribe;
    },
  },
}));

// --- urdf-loader: a fake URDFLoader that synchronously "loads" a robot whose
//     setJointValue records the joints the component applies. ---
const mockSetJointValue = vi.fn();
// URDF asset urls the loader was asked for (asserts urdf_asset_id → asset row).
const mockLoadUrls = [];
// Optional URDF-frame TCP for the world-frame yaw tests. When set, the mock link's
// getWorldPosition MODELS matrixWorld — it applies the robot's LIVE rotation (THREE
// Euler 'XYZ' with y=0 → R = Rx(x)·Rz(z)) to this point, so a yawed asset surfaces
// world-frame geometry through getWorldPosition exactly as the real link does. Left
// null by every other test, which keeps the raw mockEEWorld path-trail behaviour.
let mockTcpUrdf = null;
const mockRobot = {
  rotation: { x: 0, z: 0 },
  setJointValue: mockSetJointValue,
  // Box3.setFromObject(robot) walks .traverse during framing.
  traverse: () => {},
  // Phase-5: the path trail reads links.end_effector_link.getWorldPosition; the
  // frame triads parent an AxesHelper onto links.link0 + links.end_effector_link.
  // Only exercised when showPath/showFrames are true (the default tests never
  // touch this, so they are unaffected).
  links: {
    link0: { add: () => {} },
    end_effector_link: {
      add: () => {},
      // The grab path re-parents the held mesh here. Real three.js `attach`
      // preserves the world transform (no move), so the stub records and does
      // not touch .position -- a test asserting the mesh sits on the jaws can
      // only pass if the component positioned it.
      attach(child) {
        attachLog.push({ parent: 'ee', child });
        if (child) child.__parent = 'ee';
      },
      // Write the test-controlled TCP world position into the target vector so
      // appendPathPoint / emitEndEffector see real numeric coords (the original
      // `(v) => v` returned a coord-less Vector3, which appendPathPoint rejects,
      // so the path never accumulated). Returns the same vector for the callers
      // that read .x/.y/.z off it. When mockTcpUrdf is set, model matrixWorld by
      // rotating that URDF-frame point through the robot's live rotation instead.
      getWorldPosition: (v) => {
        if (v && typeof v.set === 'function') {
          if (mockTcpUrdf) {
            const r = mockRobot.rotation;
            const cx = Math.cos(r.x || 0);
            const sx = Math.sin(r.x || 0);
            const cz = Math.cos(r.z || 0);
            const sz = Math.sin(r.z || 0);
            // Rz first, then Rx (THREE order 'XYZ' with y=0: R = Rx·Rz).
            const rx = cz * mockTcpUrdf.x - sz * mockTcpUrdf.y;
            const ry = sz * mockTcpUrdf.x + cz * mockTcpUrdf.y;
            const rz = mockTcpUrdf.z;
            v.set(rx, cx * ry - sx * rz, sx * ry + cx * rz);
          } else {
            v.set(mockEEWorld.x, mockEEWorld.y, mockEEWorld.z);
          }
        }
        return v;
      },
    },
  },
};

// Phase-5 constructor spies — assert the path line + frame triads are built ONLY
// when their props are enabled (the default tests never construct these).
const mockGroupCtor = vi.fn();
const mockLineCtor = vi.fn();
const mockAxesCtor = vi.fn();
// Sim-stage spies — assert the catalog-sized boxes, the reach ring, and the
// per-type object material colour. All stay uncalled for the default-prop call.
const mockBoxGeometryCtor = vi.fn();
const mockRingGeometryCtor = vi.fn();
const mockStandardMaterialCtor = vi.fn(); // captures the opts (incl. color)
// material.color.set spy shared by every MeshStandardMaterial — the held-release
// tests assert the release recolor value (grab/release recolors go through
// setMeshColor → material.color.set).
const mockColorSet = vi.fn();
// Every constructed Mesh, so the release tests can find the sim object's mesh
// (by userData.simId) and assert the snap-to-table height.
const mockMeshInstances = [];
// The captured WebGLRenderer instance so the shadow-map flag can be asserted.
let mockRenderer = null;
// Every scene.attach / link.attach re-parent, in order. Real three.js `attach`
// PRESERVES the world transform (it does not move the object), so these tests can
// only pass if the component itself positions the mesh — which is exactly the
// property the old no-op stub could not check.
const attachLog = [];
// Path-trail internals: a setDrawRange spy + the captured path BufferGeometry let
// the extended tests assert the draw range advances (append) and resets (clear).
const mockSetDrawRange = vi.fn();
let mockPathGeometry = null;
// Test-controlled end-effector world position, written into the target vector by
// the mock robot's getWorldPosition so appendPathPoint sees real numeric coords.
const mockEEWorld = { x: 0, y: 0, z: 0 };
// Every STLLoader.load() the component issued, each with a finish() that lands
// the geometry — the async-mesh arrival the render-invalidation fix hangs off.
const mockStlLoads = [];
// Whatever loadMeshCb handed back to urdf-loader's `done` (one entry per mesh).
const mockMeshDone = [];
// renderer.render calls — the render-on-demand loop's only observable output.
const mockRender = vi.fn();
// camera.position.set calls — frameRobot's observable output.
const mockCameraPositionSet = vi.fn();
// OrbitControls listeners by event type, so a test can fire 'start' (= the
// student grabbed the camera).
const mockControlListeners = {};
// How many <mesh> elements the fake URDF declares. The real assets have 8 (OMX)
// and 10 (edu6). TWO is the minimum that separates the two invalidation paths:
// with one mesh, finishing it also closes the last LoadingManager item, so
// `manager.onLoad` alone satisfied every assertion and the per-mesh
// `needsRender` could be deleted with the whole suite still green (measured).
const MOCK_URDF_MESH_COUNT = 2;
vi.mock('urdf-loader', () => ({
  __esModule: true,
  default: function URDFLoaderMock(manager) {
    this.manager = manager || null;
    this.loadMeshCb = null;
    this.load = (url, onComplete) => {
      // Record which asset url was requested (urdf_asset_id → URDF_ASSETS row).
      mockLoadUrls.push(url);
      // MODEL THE REAL LOADER'S ORDER, which is the defect this file exists to
      // fence (URDFLoader.js:113-116): onComplete fires the instant parse()
      // returns, and parse() only KICKS OFF an async fetch per mesh. So the
      // robot handed to onComplete has no geometry, and the meshes land later —
      // through loadMeshCb, which the old mock never called at all, leaving the
      // entire async-mesh path untested.
      if (this.manager) this.manager.itemStart(url);
      onComplete(mockRobot);
      for (let i = 0; i < MOCK_URDF_MESH_COUNT; i += 1) {
        if (typeof this.loadMeshCb === 'function') {
          this.loadMeshCb(`mesh${i}.stl`, this.manager, (obj) => {
            mockMeshDone.push(obj);
          });
        }
      }
      if (this.manager) this.manager.itemEnd(url);
    };
  },
}));

// --- three: only the symbols UrdfTwin constructs. All are inert no-ops; the
//     test never renders WebGL. ---
vi.mock('three', () => {
  class Vector3 {
    // Additive: real .x/.y/.z storage + set/copy/sub/length so the path-trail
    // code path (getWorldPosition(target) -> distance gate -> buffer write) runs.
    // Box3.getCenter/getSize (used by frameRobot) only read these through no-op
    // setters, so the existing tests are unaffected.
    constructor(x = 0, y = 0, z = 0) {
      this.x = x; this.y = y; this.z = z;
    }
    set(x = 0, y = 0, z = 0) {
      this.x = x; this.y = y; this.z = z; return this;
    }
    copy(o) {
      if (o) { this.x = o.x; this.y = o.y; this.z = o.z; }
      return this;
    }
    sub(o) {
      if (o) { this.x -= o.x; this.y -= o.y; this.z -= o.z; }
      return this;
    }
    length() {
      return Math.sqrt(this.x * this.x + this.y * this.y + this.z * this.z);
    }
  }
  class Box3 {
    setFromObject() { return this; }
    isEmpty() { return false; }
    getCenter() { return new Vector3(); }
    getSize() { return { x: 0.3, y: 0.3, z: 0.3 }; }
  }
  const noopObj = (extra = {}) => ({
    add: () => {},
    position: { set: () => {} },
    dispose: () => {},
    addEventListener: () => {},
    ...extra,
  });
  return {
    __esModule: true,
    // `attach` is what the held-object grab/release paths re-parent through.
    // It used to be a NO-OP here, which is precisely why the release bug hid: the
    // only thing a test could observe was mesh.position.y (snapMeshToTable), so a
    // mesh left at the carry's x/z looked identical to one landed correctly. The
    // stub now records the re-parent and, like real three.js, PRESERVES the world
    // transform — i.e. it deliberately does NOT move the mesh — so a test that
    // wants the mesh in the right place has to prove the component put it there.
    Scene: function Scene() {
      return noopObj({
        background: null,
        traverse: () => {},
        attach(child) {
          attachLog.push({ parent: 'scene', child });
          if (child) child.__parent = 'scene';
        },
      });
    },
    Color: function Color() {},
    PerspectiveCamera: function PerspectiveCamera() {
      // position.set is spied so the async-mesh tests can assert that frameRobot
      // runs AGAIN once real geometry exists (in production the onComplete call
      // hits an empty Box3 and early-returns, so it is the manager.onLoad repeat
      // that actually frames the arm).
      return noopObj({
        updateProjectionMatrix: () => {},
        aspect: 1,
        near: 0.1,
        far: 100,
        position: { set: mockCameraPositionSet },
      });
    },
    WebGLRenderer: function WebGLRenderer() {
      // Capture the instance + a shadowMap object so the sim-stage shadow test can
      // assert `showShadows` flips renderer.shadowMap.enabled (and the default
      // call leaves it false).
      mockRenderer = {
        setPixelRatio: () => {},
        setSize: () => {},
        render: mockRender,
        dispose: () => {},
        shadowMap: { enabled: false, type: null },
        domElement: document.createElement('canvas'),
      };
      return mockRenderer;
    },
    HemisphereLight: function HemisphereLight() { return noopObj(); },
    // The key light: a settable castShadow + a shadow object (mapSize.set + a
    // shadow camera) so the sim-stage shadow config path runs. Only touched when
    // showShadows is enabled.
    DirectionalLight: function DirectionalLight() {
      return noopObj({
        castShadow: false,
        shadow: {
          bias: 0,
          mapSize: { set: () => {} },
          camera: { updateProjectionMatrix: () => {} },
        },
      });
    },
    GridHelper: function GridHelper() { return noopObj(); },
    MeshStandardMaterial: function MeshStandardMaterial(opts) {
      // Capture the opts (incl. the per-type `color`) so the sim-stage tests can
      // assert an object used its catalog colour; color.set is the shared
      // mockColorSet spy so the grasp/release recolor values are assertable.
      mockStandardMaterialCtor(opts);
      return { color: { set: mockColorSet }, dispose: () => {} };
    },
    MeshPhongMaterial: function MeshPhongMaterial() { return { dispose: () => {} }; },
    MeshBasicMaterial: function MeshBasicMaterial() { return { color: { set: () => {} }, dispose: () => {} }; },
    Mesh: function Mesh(geometry, material) {
      // Rich enough for the sim-object/table/ring paths: userData bookkeeping, a
      // settable position (positionSimObject/snapMeshToTable write .y), rotation,
      // and shadow flags. Never constructed by the default-prop tests (objects/
      // table/zones/ring are all off there). Each instance is recorded so the
      // held-release tests can find a sim object's mesh via userData.simId.
      const mesh = {
        geometry,
        material,
        userData: {},
        position: {
          set(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; return this; },
          x: 0, y: 0, z: 0,
        },
        rotation: { set: () => {}, x: 0 },
        castShadow: false,
        receiveShadow: false,
        visible: true,
        parent: null,
        add: () => {},
        dispose: () => {},
      };
      mockMeshInstances.push(mesh);
      return mesh;
    },
    // Sim-object / zone / table / ring geometries. BoxGeometry + RingGeometry are
    // spied so the sim-stage tests can assert per-type sizes + the reach ring
    // bounds; all are inert no-ops otherwise.
    BoxGeometry: function BoxGeometry(w, h, d) {
      mockBoxGeometryCtor(w, h, d);
      return { dispose: () => {} };
    },
    PlaneGeometry: function PlaneGeometry() { return { dispose: () => {} }; },
    EdgesGeometry: function EdgesGeometry() { return { dispose: () => {} }; },
    RingGeometry: function RingGeometry(inner, outer, seg) {
      mockRingGeometryCtor(inner, outer, seg);
      return { dispose: () => {} };
    },
    LineSegments: function LineSegments(geometry, material) {
      return { geometry, material, position: { set: () => {} }, dispose: () => {} };
    },
    // three enum-ish constants the sim-stage paths reference.
    PCFSoftShadowMap: 1,
    DoubleSide: 2,
    // Phase-5 path-trail + frame-triad primitives (inert; constructed only when
    // showPath/showFrames are enabled).
    Group: function Group() { mockGroupCtor(); return noopObj({ visible: true }); },
    BufferGeometry: function BufferGeometry() {
      // Capture the path geometry + spy its setDrawRange so the path-trail tests
      // can assert the draw range advances (append) and resets (clear). Only the
      // path layer constructs a BufferGeometry in UrdfTwin, so this is the path
      // line's geometry.
      const geo = {
        setAttribute: () => {},
        setDrawRange: mockSetDrawRange,
        attributes: { position: { needsUpdate: false } },
        dispose: () => {},
      };
      mockPathGeometry = geo;
      return geo;
    },
    BufferAttribute: function BufferAttribute() { return {}; },
    LineBasicMaterial: function LineBasicMaterial() { return { dispose: () => {} }; },
    Line: function Line(geometry, material) {
      mockLineCtor();
      return { geometry, material, frustumCulled: true, visible: true };
    },
    AxesHelper: function AxesHelper() { mockAxesCtor(); return noopObj(); },
    // Faithful enough to be meaningful: real item counting, so onLoad fires at
    // the same moment three's does — when every tracked item (the URDF text AND
    // every STL) has ended. A no-op stub here would have let the fix's
    // frameRobot/shadow re-run go untested.
    LoadingManager: function LoadingManager() {
      this.itemsTotal = 0;
      this.itemsLoaded = 0;
      this.onLoad = null;
      this.itemStart = () => { this.itemsTotal += 1; };
      this.itemEnd = () => {
        this.itemsLoaded += 1;
        if (this.itemsLoaded === this.itemsTotal && typeof this.onLoad === 'function') {
          this.onLoad();
        }
      };
      this.itemError = () => {};
      this.resolveURL = (u) => u;
    },
    Box3,
    Vector3,
  };
});
vi.mock('three/examples/jsm/controls/OrbitControls', () => ({
  __esModule: true,
  OrbitControls: function OrbitControls() {
    return {
      enableDamping: false,
      dampingFactor: 0,
      target: { set: () => {}, copy: () => {} },
      // Records handlers so a test can fire the real 'start' event (pointer-down
      // on the canvas) and assert the camera latch. `update: () => false` is the
      // at-rest return of the real control, which is what makes the render loop
      // idle and therefore what makes these tests mean anything.
      addEventListener: (type, fn) => {
        (mockControlListeners[type] || (mockControlListeners[type] = [])).push(fn);
      },
      update: () => false,
      dispose: () => {},
    };
  },
}));
vi.mock('three/examples/jsm/loaders/STLLoader', () => ({
  __esModule: true,
  STLLoader: function STLLoader(manager) {
    this.manager = manager || null;
    // Record instead of resolving. `finish()` replays what three's FileLoader
    // really does: run the onLoad callback, then close the manager item — which
    // is what makes LoadingManager.onLoad fire. Nothing resolves on its own, so
    // every test controls exactly when (and whether) geometry exists.
    this.load = (path, onLoad, onProgress, onError) => {
      if (this.manager) this.manager.itemStart(path);
      const entry = {
        path,
        onLoad,
        onError,
        finish: (geometry = { dispose: () => {} }) => {
          if (typeof onLoad === 'function') onLoad(geometry);
          if (this.manager) this.manager.itemEnd(path);
        },
      };
      mockStlLoads.push(entry);
    };
  },
}));

beforeEach(() => {
  mockTopicCtor.mockClear();
  mockSubscribe.mockClear();
  mockUnsubscribe.mockClear();
  mockSetJointValue.mockClear();
  mockGroupCtor.mockClear();
  mockLineCtor.mockClear();
  mockAxesCtor.mockClear();
  mockBoxGeometryCtor.mockClear();
  mockRingGeometryCtor.mockClear();
  mockStandardMaterialCtor.mockClear();
  mockColorSet.mockClear();
  mockMeshInstances.length = 0;
  mockRenderer = null;
  attachLog.length = 0;
  mockSetDrawRange.mockClear();
  mockPathGeometry = null;
  mockEEWorld.x = 0;
  mockEEWorld.y = 0;
  mockEEWorld.z = 0;
  mockRobot.rotation.x = 0;
  mockRobot.rotation.z = 0;
  mockLoadUrls.length = 0;
  mockStlLoads.length = 0;
  mockMeshDone.length = 0;
  mockRender.mockClear();
  mockCameraPositionSet.mockClear();
  Object.keys(mockControlListeners).forEach((k) => delete mockControlListeners[k]);
  mockTcpUrdf = null;
  mockState = { ros: { rosbridgeUrl: 'ws://student-pc:9090', rosHost: 'student-pc' } };
});

describe('UrdfTwin — /joint_states wire contract', () => {
  test('subscribes to /joint_states with the JointState type + monitor throttle/queue', async () => {
    render(<UrdfTwin />);

    await waitFor(() => expect(mockTopicCtor).toHaveBeenCalledTimes(1));
    const opts = mockTopicCtor.mock.calls[0][0];
    expect(opts.name).toBe('/joint_states');
    expect(opts.messageType).toBe('sensor_msgs/msg/JointState');
    expect(opts.throttle_rate).toBe(100);
    expect(opts.queue_length).toBe(1);
    expect(mockSubscribe).toHaveBeenCalledTimes(1);
  });

  test('maps msg.name[i] -> msg.position[i] onto robot.setJointValue for the 6 follower joints', async () => {
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));

    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() =>
      onMsg({
        name: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'],
        position: [0.1, -0.2, 0.3, -0.4, 0.5, 0.6],
      })
    );

    expect(mockSetJointValue).toHaveBeenCalledWith('joint1', 0.1);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint2', -0.2);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint3', 0.3);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint4', -0.4);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint5', 0.5);
    expect(mockSetJointValue).toHaveBeenCalledWith('gripper_joint_1', 0.6);
    expect(mockSetJointValue).toHaveBeenCalledTimes(6);
  });

  test('ignores unknown joint names and non-finite positions', async () => {
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));

    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() =>
      onMsg({
        // gripper_joint_2 is a URDF <mimic> (driven automatically) — must NOT be
        // applied directly; 'wrist_unknown' is not a follower joint; NaN guarded.
        name: ['joint1', 'gripper_joint_2', 'wrist_unknown', 'joint2'],
        position: [0.7, 1.0, 2.0, NaN],
      })
    );

    expect(mockSetJointValue).toHaveBeenCalledTimes(1);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint1', 0.7);
  });

  test('shows the German "waiting for joint data" hint until the first message', async () => {
    render(<UrdfTwin />);
    // Before any message: the hint chip is visible.
    expect(screen.getByText('Wartet auf Gelenkdaten …')).toBeInTheDocument();

    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));

    // After the first message the hint disappears.
    await waitFor(() =>
      expect(screen.queryByText('Wartet auf Gelenkdaten …')).not.toBeInTheDocument()
    );
  });

  test('unsubscribes on unmount', async () => {
    const { unmount } = render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    unmount();
    expect(mockUnsubscribe).toHaveBeenCalledTimes(1);
  });

  test('does not subscribe when rosbridgeUrl is empty', async () => {
    mockState = { ros: { rosbridgeUrl: '', rosHost: '' } };
    render(<UrdfTwin />);
    // Give the async effect a tick; it must bail before constructing a Topic.
    await act(async () => { await Promise.resolve(); });
    expect(mockTopicCtor).not.toHaveBeenCalled();
  });

  // Phase-5 (3D-twin upgrade): the path trail + frame triads are OFF by default,
  // so the default tests above construct neither a Line nor an AxesHelper. With
  // both enabled, the new layers build AND the /joint_states wire contract still
  // holds (the new props never regress the core mirror).
  test('default props build no path Line / AxesHelper', async () => {
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockLineCtor).not.toHaveBeenCalled();
    expect(mockAxesCtor).not.toHaveBeenCalled();
  });

  test('showPath + showFrames build the trail/triads and keep the joint mirror', async () => {
    render(<UrdfTwin showPath showFrames />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));

    // The cyan path Line and the base/TCP AxesHelpers were constructed.
    expect(mockLineCtor).toHaveBeenCalled();
    expect(mockAxesCtor).toHaveBeenCalled();

    // Subscription contract is unchanged by the new props.
    const opts = mockTopicCtor.mock.calls[0][0];
    expect(opts.name).toBe('/joint_states');
    expect(opts.messageType).toBe('sensor_msgs/msg/JointState');

    // A delivered message still maps the 6 follower joints (path accumulation
    // runs alongside but never interferes with the core mirror).
    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() =>
      onMsg({
        name: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'],
        position: [0.1, -0.2, 0.3, -0.4, 0.5, 0.6],
      })
    );
    expect(mockSetJointValue).toHaveBeenCalledTimes(6);
  });
});

// Phase-5 path-trail internals. The default UrdfTwin.test path test only asserts a
// Line/AxesHelper is CONSTRUCTED; appendPathPoint early-returned because the old
// Vector3 mock had no usable .x/.y/.z. With the extended Vector3 + the controllable
// getWorldPosition above, we can now drive distinct TCP positions and assert the
// path buffer actually progresses, and that a pathClearToken bump empties it.
describe('UrdfTwin — path trail accumulation (Phase-5)', () => {
  test('showPath: distinct TCP positions advance the draw range and flag needsUpdate', async () => {
    render(<UrdfTwin showPath />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const onMsg = mockSubscribe.mock.calls[0][0];

    // The path Line + its BufferGeometry were built on mount (showPath on).
    expect(mockPathGeometry).not.toBeNull();
    // Ignore the setDrawRange(0,0) calls from the build + initial clear effects.
    mockSetDrawRange.mockClear();
    mockPathGeometry.attributes.position.needsUpdate = false;

    // First TCP world position.
    mockEEWorld.x = 0.10; mockEEWorld.y = 0.20; mockEEWorld.z = 0.05;
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));

    // Second, ~7 cm away (well over PATH_MIN_MOVE_M = 1 mm) -> a second point.
    mockEEWorld.x = 0.15; mockEEWorld.y = 0.25; mockEEWorld.z = 0.05;
    act(() => onMsg({ name: ['joint1'], position: [0.12] }));

    // The draw range grew 0 -> 1 -> 2 as the two distinct points were appended.
    const ends = mockSetDrawRange.mock.calls.map((c) => c[1]);
    expect(ends).toContain(1);
    expect(ends).toContain(2);
    // The geometry's position attribute was flagged for re-upload.
    expect(mockPathGeometry.attributes.position.needsUpdate).toBe(true);
  });

  test('showPath: a sub-millimetre move does NOT append a new point (distance gate)', async () => {
    render(<UrdfTwin showPath />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const onMsg = mockSubscribe.mock.calls[0][0];

    // First point (state.last has no prior, so it is always appended).
    mockEEWorld.x = 0.20; mockEEWorld.y = 0.20; mockEEWorld.z = 0.20;
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));
    mockSetDrawRange.mockClear();

    // Move 0.1 mm (< 1 mm) -> the distance gate rejects it, no draw-range change.
    mockEEWorld.x = 0.2001;
    act(() => onMsg({ name: ['joint1'], position: [0.1001] }));
    expect(mockSetDrawRange).not.toHaveBeenCalled();
  });

  test('bumping pathClearToken empties the trail (setDrawRange(0,0) + needsUpdate)', async () => {
    const { rerender } = render(<UrdfTwin showPath pathClearToken={0} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const onMsg = mockSubscribe.mock.calls[0][0];

    // Accumulate one point so there is something to clear.
    mockEEWorld.x = 0.30; mockEEWorld.y = 0.10; mockEEWorld.z = 0.0;
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));

    mockSetDrawRange.mockClear();
    mockPathGeometry.attributes.position.needsUpdate = false;

    // Bump the clear token -> the clear effect re-runs and resets the buffer.
    rerender(<UrdfTwin showPath pathClearToken={1} />);

    expect(mockSetDrawRange).toHaveBeenCalledWith(0, 0);
    expect(mockPathGeometry.attributes.position.needsUpdate).toBe(true);
    // The mirror subscription is untouched by the clear (no re-subscribe).
    expect(mockSubscribe).toHaveBeenCalledTimes(1);
  });
});

// Sim-stage layer: catalog-sized/coloured objects, the reach ring, and soft
// shadows. All are additive + default-off — the default-prop tests above construct
// none of these primitives and never set a renderer shadow flag.
describe('UrdfTwin — sim-stage catalog objects / reach ring / shadows', () => {
  test('default props build no Box/Ring geometry and leave the shadow map off', async () => {
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockBoxGeometryCtor).not.toHaveBeenCalled();
    expect(mockRingGeometryCtor).not.toHaveBeenCalled();
    expect(mockRenderer.shadowMap.enabled).toBe(false);
  });

  test('sim objects render per-type box dimensions + colour from catalogDims', async () => {
    render(
      <UrdfTwin
        objects={[{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }]}
        catalogDims={{ wuerfel: { height_m: 0.05, width_m: 0.04, color: '#00ff00', max_instances: 2 } }}
      />
    );
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    // Box is width × height × width (footprint square, catalog height).
    expect(mockBoxGeometryCtor).toHaveBeenCalledWith(0.04, 0.05, 0.04);
    // One object material was built with the catalog colour.
    const colors = mockStandardMaterialCtor.mock.calls.map((c) => c[0] && c[0].color);
    expect(colors).toContain('#00ff00');
  });

  test('an object without catalog dims falls back to the amber cube', async () => {
    render(<UrdfTwin objects={[{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }]} catalogDims={{}} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockBoxGeometryCtor).toHaveBeenCalledWith(0.03, 0.03, 0.03);
    const colors = mockStandardMaterialCtor.mock.calls.map((c) => c[0] && c[0].color);
    expect(colors).toContain('#f59e0b');
  });

  test('late-arriving catalogDims disposes+rebuilds an existing mesh at the new size', async () => {
    const objects = [{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }];
    const { rerender } = render(<UrdfTwin objects={objects} catalogDims={{}} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    // Built once with the fallback cube.
    expect(mockBoxGeometryCtor).toHaveBeenCalledWith(0.03, 0.03, 0.03);
    mockBoxGeometryCtor.mockClear();

    // Catalog arrives (async) with real dims -> the stale mesh is rebuilt.
    rerender(
      <UrdfTwin
        objects={objects}
        catalogDims={{ wuerfel: { height_m: 0.05, width_m: 0.04, color: '#00ff00', max_instances: 2 } }}
      />
    );
    expect(mockBoxGeometryCtor).toHaveBeenCalledWith(0.04, 0.05, 0.04);
  });

  test('a type change under a reused tag_id rebuilds the mesh at the new type size', async () => {
    const catalogDims = {
      wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 },
      kugel: { height_m: 0.06, width_m: 0.06, color: '#3b82f6', max_instances: 1 },
    };
    const { rerender } = render(
      <UrdfTwin objects={[{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }]} catalogDims={catalogDims} />
    );
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockBoxGeometryCtor).toHaveBeenCalledWith(0.03, 0.03, 0.03);
    mockBoxGeometryCtor.mockClear();

    // Same tag_id, different type (a re-hydrated workflow) -> dispose + rebuild.
    rerender(
      <UrdfTwin objects={[{ type: 'kugel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }]} catalogDims={catalogDims} />
    );
    expect(mockBoxGeometryCtor).toHaveBeenCalledWith(0.06, 0.06, 0.06);
  });

  test('showReach builds the 0.10/0.28 ring and showShadows enables the renderer shadow map', async () => {
    render(
      <UrdfTwin
        showReach
        showShadows
        showTable
        objects={[{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }]}
      />
    );
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockRingGeometryCtor).toHaveBeenCalledWith(0.1, 0.28, 64);
    expect(mockRenderer.shadowMap.enabled).toBe(true);
  });
});

// Held-object RELEASE (heldObjectId set → null): the release branch snaps the
// mesh to the table and restores its rest colour. The dims/colour must come
// from the LIVE catalogDims when the map knows the type — the mesh's
// build-time userData is frozen, and a catalog fetch resolving MID-CARRY (a
// held mesh is deliberately skipped by the objects-diff rebuild) previously
// left the release reading stale fallback values.
describe('UrdfTwin — held-object release snap/recolor', () => {
  const OBJECTS = [{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }];
  const GREEN_DIMS = {
    wuerfel: { height_m: 0.05, width_m: 0.04, color: '#00ff00', max_instances: 2 },
  };
  const findObjectMesh = () =>
    mockMeshInstances.find((m) => m.userData && m.userData.simId === 20);

  test('release recolors with the object\'s OWN catalog colour — live catalogDims wins over stale build-time userData', async () => {
    // Build BEFORE the catalog resolves: fallback amber userData.
    const { rerender } = render(<UrdfTwin objects={OBJECTS} catalogDims={{}} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const mesh = findObjectMesh();
    expect(mesh).toBeTruthy();

    // Grab, then the catalog resolves MID-CARRY (the held mesh is skipped by
    // the objects-diff rebuild, so its userData stays amber/fallback-sized).
    rerender(<UrdfTwin objects={OBJECTS} catalogDims={{}} heldObjectId={20} />);
    rerender(<UrdfTwin objects={OBJECTS} catalogDims={GREEN_DIMS} heldObjectId={20} />);
    expect(findObjectMesh()).toBe(mesh); // not rebuilt while held
    mockColorSet.mockClear();

    // Release → the LIVE catalog colour + height, not the frozen userData.
    rerender(<UrdfTwin objects={OBJECTS} catalogDims={GREEN_DIMS} heldObjectId={null} />);
    expect(mockColorSet).toHaveBeenCalledWith('#00ff00');
    // snap-to-table used the live height (0.05 / 2), not the fallback 0.015.
    expect(mesh.position.y).toBeCloseTo(0.025, 6);
  });

  test('release falls back to the amber rest colour for an object without catalogDims', async () => {
    const { rerender } = render(<UrdfTwin objects={OBJECTS} catalogDims={{}} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const mesh = findObjectMesh();
    expect(mesh).toBeTruthy();

    rerender(<UrdfTwin objects={OBJECTS} catalogDims={{}} heldObjectId={20} />);
    mockColorSet.mockClear();

    rerender(<UrdfTwin objects={OBJECTS} catalogDims={{}} heldObjectId={null} />);
    expect(mockColorSet).toHaveBeenCalledWith('#f59e0b');
    // Fallback cube height (0.03 / 2).
    expect(mesh.position.y).toBeCloseTo(0.015, 6);
  });
});

// edu6 profile (§4.5 / plan §9). The capability manifest's urdf_asset_id selects
// the edu6 URDF row; edu6's software WORLD frame is URDF·rotZ(π) (front = +x like
// the OMX, per edu6_ik.py), so the URDF-native model is yawed π to render in the
// world frame the sim objects live in — OMX gets NO yaw. With the yaw baked into
// getWorldPosition, emitEndEffector surfaces WORLD coords by construction: a URDF
// TCP (x, y) reads back as (−x, −y).
describe('UrdfTwin — edu6 profile asset + world-frame yaw', () => {
  const EDU6_CAPS = {
    urdf_asset_id: 'edu6',
    arm_joints: 6,
    joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6', 'end_gear_joint'],
  };
  const edu6State = () => ({
    ros: { rosbridgeUrl: 'ws://student-pc:9090', rosHost: 'student-pc' },
    tasks: { taskStatus: { capabilities: EDU6_CAPS } },
  });

  test('an OMX manifest loads the omx URDF and applies NO yaw (byte-identical)', async () => {
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockLoadUrls.some((u) => u.includes('/omx-urdf/omx_f.urdf'))).toBe(true);
    expect(mockRobot.rotation.z).toBe(0);
    expect(mockRobot.rotation.x).toBeCloseTo(-Math.PI / 2, 12);
  });

  test('an edu6 manifest selects the edu6 URDF asset and yaws the robot π', async () => {
    mockState = edu6State();
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockLoadUrls.some((u) => u.includes('/edu6-urdf/edu6.urdf'))).toBe(true);
    expect(mockRobot.rotation.z).toBeCloseTo(Math.PI, 12);
    // The up-axis fix is unchanged; the yaw is ADDITIVE to it.
    expect(mockRobot.rotation.x).toBeCloseTo(-Math.PI / 2, 12);
  });

  test('emitEndEffector reports WORLD-frame coords on the yawed edu6 asset (URDF (x,y) → (−x,−y))', async () => {
    mockState = edu6State();
    // A known URDF-frame TCP; getWorldPosition models matrixWorld from the live rotation.
    mockTcpUrdf = { x: 0.15, y: 0.05, z: 0.20 };
    const onEE = vi.fn();
    render(<UrdfTwin onEndEffector={onEE} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));

    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() => onMsg({
      name: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6', 'end_gear_joint'],
      position: [0, 0, 0, 0, 0, 0, 1.0],
    }));

    expect(onEE).toHaveBeenCalled();
    const arg = onEE.mock.calls[onEE.mock.calls.length - 1][0];
    // World frame = URDF·rotZ(π): (x, y) surface negated; z (height) unchanged.
    expect(arg.x).toBeCloseTo(-0.15, 6);
    expect(arg.y).toBeCloseTo(-0.05, 6);
    expect(arg.z).toBeCloseTo(0.20, 6);
    // The gripper channel is read from the edu6 end_gear_joint index (mimic driver).
    expect(arg.gripper).toBeCloseTo(1.0, 6);
    expect(mockSetJointValue).toHaveBeenCalledWith('end_gear_joint', 1.0);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint6', 0);
  });

  test('an un-yawed OMX asset emits the URDF/base frame unchanged (control)', async () => {
    mockTcpUrdf = { x: 0.15, y: 0.05, z: 0.20 };
    const onEE = vi.fn();
    render(<UrdfTwin onEndEffector={onEE} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));

    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));

    const arg = onEE.mock.calls[onEE.mock.calls.length - 1][0];
    // No yaw → world == URDF: coords pass through unchanged.
    expect(arg.x).toBeCloseTo(0.15, 6);
    expect(arg.y).toBeCloseTo(0.05, 6);
    expect(arg.z).toBeCloseTo(0.20, 6);
  });
});

// edu1 profile ("Edu:1", the 5-DOF Feetech claw arm). Same seam as edu6, but it
// is the case that proves the seam is keyed on the ASSET ID and not on the joint
// COUNT: edu1 has 5 arm joints exactly like the OMX, so a count-based selector
// would silently load the OMX meshes for it.
describe('UrdfTwin — edu1 profile asset', () => {
  const EDU1_CAPS = {
    urdf_asset_id: 'edu1',
    arm_joints: 5,
    joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'RL_joint'],
  };
  const edu1State = () => ({
    ros: { rosbridgeUrl: 'ws://student-pc:9090', rosHost: 'student-pc' },
    tasks: { taskStatus: { capabilities: EDU1_CAPS } },
  });

  test('an edu1 manifest selects the edu1 URDF asset and yaws the robot π', async () => {
    mockState = edu1State();
    render(<UrdfTwin />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    expect(mockLoadUrls.some((u) => u.includes('/edu1-urdf/edu1.urdf'))).toBe(true);
    // Its reachable half-disc is on URDF −x too, so it is yawed like the edu6.
    expect(mockRobot.rotation.z).toBeCloseTo(Math.PI, 12);
    expect(mockRobot.rotation.x).toBeCloseTo(-Math.PI / 2, 12);
  });

  test('the claw channel is read from RL_joint, not from a hardcoded name', async () => {
    mockState = edu1State();
    const onEE = vi.fn();
    render(<UrdfTwin onEndEffector={onEE} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));

    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() => onMsg({
      name: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'RL_joint'],
      position: [0, 0.64, 1.48, 0.90, 0, 0.9],
    }));

    const arg = onEE.mock.calls[onEE.mock.calls.length - 1][0];
    expect(arg.gripper).toBeCloseTo(0.9, 6);
    expect(mockSetJointValue).toHaveBeenCalledWith('RL_joint', 0.9);
    expect(mockSetJointValue).toHaveBeenCalledWith('joint4', 0.90);
    // …and never the OMX gripper name, which the count-shaped bug would use.
    expect(mockSetJointValue).not.toHaveBeenCalledWith('gripper_joint_1', 0.9);
  });
});

// ── Server-authoritative sim scene (/sim/objects) ───────────────────────────
// The twin used to run its OWN object model: SimScene guessed the grasp from a
// distance test and UrdfTwin then moved meshes the server never learned about,
// so after one pick-and-place the 3D pane and the 2D editor disagreed and the
// arm followed the editor. `simPositions` / `simEpoch` make the server the
// single source of truth.
//
// These tests are only meaningful because the three mock's `attach` PRESERVES
// the world transform, exactly like the real one — a no-op stub made a mesh
// stranded at the carry position indistinguishable from a correctly landed one.
describe('UrdfTwin — the server owns where sim objects are', () => {
  const OBJECTS = [{ type: 'wuerfel', tag_id: 20, x: 0.15, y: 0, yaw: 0 }];
  const findObjectMesh = () =>
    mockMeshInstances.find((m) => m.userData && m.userData.simId === 20);

  test('a released mesh lands on the SERVER\'s coordinates, not just snapped in Y', async () => {
    const { rerender } = render(<UrdfTwin objects={OBJECTS} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const mesh = findObjectMesh();
    expect(mesh).toBeTruthy();

    // Grab, carry (the server reports the object moving with the gripper), release.
    rerender(<UrdfTwin objects={OBJECTS} heldObjectId={20}
      simPositions={{ 20: { x: 0.15, y: 0, yaw: 0 } }} />);
    rerender(<UrdfTwin objects={OBJECTS} heldObjectId={20}
      simPositions={{ 20: { x: 0.12, y: 0.10, yaw: 0 } }} />);
    rerender(<UrdfTwin objects={OBJECTS} heldObjectId={null}
      simPositions={{ 20: { x: 0.12, y: 0.10, yaw: 0 } }} />);

    // base (x, y) -> viewer (x, ·, -y). The editor still says x=0.15,y=0; if the
    // component read THAT the cube would be back at its placement.
    expect(mesh.position.x).toBeCloseTo(0.12, 6);
    expect(mesh.position.z).toBeCloseTo(-0.10, 6);
    expect(mesh.__parent).toBe('scene');
  });

  test('a re-grasp re-seats the mesh on the jaws instead of inheriting a stale offset', async () => {
    const { rerender } = render(<UrdfTwin objects={OBJECTS} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const mesh = findObjectMesh();

    // First pick-and-place leaves the cube away from its placement.
    rerender(<UrdfTwin objects={OBJECTS} heldObjectId={20}
      simPositions={{ 20: { x: 0.15, y: 0, yaw: 0 } }} />);
    rerender(<UrdfTwin objects={OBJECTS} heldObjectId={null}
      simPositions={{ 20: { x: 0.12, y: 0.10, yaw: 0 } }} />);
    // Second grasp, at the cube's CURRENT place.
    rerender(<UrdfTwin objects={OBJECTS} heldObjectId={20}
      simPositions={{ 20: { x: 0.12, y: 0.10, yaw: 0 } }} />);

    expect(mesh.__parent).toBe('ee');
    expect(mesh.position.x).toBeCloseTo(0.12, 6);
    expect(mesh.position.z).toBeCloseTo(-0.10, 6);
    expect(attachLog.filter((a) => a.parent === 'ee')).toHaveLength(2);
  });

  test('a simEpoch bump repositions every mesh from the server scene', async () => {
    const { rerender } = render(<UrdfTwin objects={OBJECTS} simEpoch={1}
      simPositions={{ 20: { x: 0.12, y: 0.10, yaw: 0 } }} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const mesh = findObjectMesh();
    expect(mesh.position.x).toBeCloseTo(0.12, 6);

    // A new run: the server resets the scene to the placement and bumps the epoch.
    rerender(<UrdfTwin objects={OBJECTS} simEpoch={2}
      simPositions={{ 20: { x: 0.15, y: 0, yaw: 0 } }} />);
    expect(mesh.position.x).toBeCloseTo(0.15, 6);
    expect(mesh.position.z).toBeCloseTo(0, 6);
  });

  test('without simPositions the placement coordinates are used, exactly as before', async () => {
    render(<UrdfTwin objects={OBJECTS} />);
    await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
    const mesh = findObjectMesh();
    expect(mesh.position.x).toBeCloseTo(0.15, 6);
    expect(mesh.position.z).toBeCloseTo(0, 6);
  });
});

// ---------------------------------------------------------------------------
// Async STL arrival — the render-on-demand invalidation contract.
//
// urdf-loader calls its onComplete the instant the URDF XML is parsed, while
// every <mesh> is still an in-flight fetch (URDFLoader.js:113-116). This
// renderer paints ON DEMAND, so the frame that onComplete dirtied paints an
// EMPTY robot, and unless something marks the frame dirty again when the meshes
// land, the pane stays black until the student drags the canvas. On the SIM twin
// there IS nothing else: /sim/joint_states does not exist until the first run.
//
// These tests are the fence. The mocks above deliberately do NOT resolve an STL
// on their own — each test lands the geometry itself, so the ordering is exact.
// ---------------------------------------------------------------------------

const nextFrame = () =>
  new Promise((resolve) => window.requestAnimationFrame(() => resolve()));

// Let the render-on-demand loop run a few frames and go quiet.
async function settleFrames(n = 4) {
  for (let i = 0; i < n; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await nextFrame(); });
  }
}

describe('UrdfTwin — meshes that arrive AFTER the URDF callback', () => {
  async function mountAndWaitForMeshRequests(ui = <UrdfTwin />) {
    const utils = render(ui);
    await waitFor(() => expect(mockStlLoads.length).toBe(MOCK_URDF_MESH_COUNT));
    await waitFor(() => expect(mockRender).toHaveBeenCalled());
    return utils;
  }

  // Prove the loop is genuinely idle, and return the settled draw count. Without
  // this, "a render happened after the mesh landed" would also pass against a
  // loop that paints every frame regardless — i.e. against the bug.
  async function settledDrawCount() {
    await settleFrames();
    const idle = mockRender.mock.calls.length;
    await settleFrames();
    expect(mockRender.mock.calls.length).toBe(idle);
    return idle;
  }

  test('the FIRST mesh landing already re-paints — the arm appears progressively', async () => {
    await mountAndWaitForMeshRequests();
    const idle = await settledDrawCount();

    // Only ONE of the two meshes lands. The LoadingManager still has an open
    // item, so `manager.onLoad` has NOT fired: the only thing that can dirty the
    // frame here is the per-mesh invalidation inside loadMeshCb.
    await act(async () => { mockStlLoads[0].finish(); });
    await waitFor(() =>
      expect(mockRender.mock.calls.length).toBeGreaterThan(idle));
    expect(mockCameraPositionSet.mock.calls.length).toBe(1); // no framing yet
  });

  test('the LAST mesh landing re-paints too — manager.onLoad', async () => {
    await mountAndWaitForMeshRequests();
    await act(async () => { mockStlLoads[0].finish(); });
    const idle = await settledDrawCount();

    await act(async () => { mockStlLoads[1].finish(); });
    await waitFor(() =>
      expect(mockRender.mock.calls.length).toBeGreaterThan(idle));
  });

  test('every mesh reaches urdf-loader (the invalidation did not replace done())', async () => {
    await mountAndWaitForMeshRequests();
    await act(async () => { mockStlLoads.forEach((l) => l.finish()); });

    expect(mockMeshDone).toHaveLength(MOCK_URDF_MESH_COUNT);
    expect(mockMeshDone).toEqual(mockMeshInstances);
  });

  test('the camera is framed ONCE, and only after every mesh has landed', async () => {
    // Framing before the geometry exists is not a harmless no-op: on omx_f it
    // measures the 1 cm end-effector <box> primitive and over-zooms 37.8x, which
    // is why onComplete no longer frames at all. The baseline below is the mount
    // effect's own camera.position.set and nothing else.
    await mountAndWaitForMeshRequests();
    expect(mockCameraPositionSet.mock.calls.length).toBe(1);

    await act(async () => { mockStlLoads[0].finish(); });
    expect(mockCameraPositionSet.mock.calls.length).toBe(1);

    await act(async () => { mockStlLoads[1].finish(); });
    expect(mockCameraPositionSet.mock.calls.length).toBe(2);
  });

  test('a student who grabs the camera mid-load KEEPS their view', async () => {
    // The arm now paints progressively, so there is a real window in which a
    // student on a slow link can orbit before the last STL lands. OrbitControls
    // fires 'start' on pointer-down; the re-frame must yield to it.
    await mountAndWaitForMeshRequests();
    await act(async () => { mockStlLoads[0].finish(); });
    const framedBefore = mockCameraPositionSet.mock.calls.length;

    act(() => { (mockControlListeners.start || []).forEach((fn) => fn()); });
    await act(async () => { mockStlLoads[1].finish(); });

    expect(mockCameraPositionSet.mock.calls.length).toBe(framedBefore);
  });

  test('showShadows marks EVERY link mesh as a caster — none exist at onComplete', async () => {
    await mountAndWaitForMeshRequests(<UrdfTwin showShadows />);
    await act(async () => { mockStlLoads.forEach((l) => l.finish()); });

    expect(mockMeshInstances).toHaveLength(MOCK_URDF_MESH_COUNT);
    mockMeshInstances.forEach((m) => expect(m.castShadow).toBe(true));
  });

  test('without showShadows a link mesh casts nothing (control)', async () => {
    await mountAndWaitForMeshRequests();
    await act(async () => { mockStlLoads.forEach((l) => l.finish()); });

    expect(mockMeshInstances).toHaveLength(MOCK_URDF_MESH_COUNT);
    mockMeshInstances.forEach((m) => expect(m.castShadow).toBe(false));
  });

  test('a mesh that resolves after unmount neither paints nor throws', async () => {
    const { unmount } = await mountAndWaitForMeshRequests();
    unmount();
    const after = mockRender.mock.calls.length;

    await act(async () => { mockStlLoads.forEach((l) => l.finish()); });
    await settleFrames();
    expect(mockRender.mock.calls.length).toBe(after);
  });
});

// ---------------------------------------------------------------------------
// „Wartet auf Gelenkdaten …" must be able to come BACK.
//
// `hasJointData` was a one-way latch — one setter, no false path, no timer — so
// a feed that died (rosbridge drop, node respawn, „Umgebung stoppen", the arm
// container recreated by the LeaderToggle) left the mesh frozen at its last pose
// under a GREEN liveness dot. The pane asserted liveness over a stale picture.
// ---------------------------------------------------------------------------

describe('UrdfTwin — the liveness chip is not a latch', () => {
  const WAITING = 'Wartet auf Gelenkdaten …';

  // The watchdog interval is created by the subscription effect at MOUNT, so the
  // fake clock has to be installed BEFORE render or the real timer is the one
  // that runs. That rules out `waitFor` here — it polls on setInterval, which is
  // faked — so the subscribe handshake is awaited by flushing microtasks
  // instead. `getConnection` resolves immediately (mocked above), and setTimeout
  // is deliberately NOT faked, so nothing else in the harness changes behaviour.
  async function mountWithFakeClock() {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    vi.setSystemTime(new Date('2026-09-01T12:00:00Z'));
    render(<UrdfTwin />);
    for (let i = 0; i < 8 && mockSubscribe.mock.calls.length === 0; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await Promise.resolve(); });
    }
    expect(mockSubscribe).toHaveBeenCalledTimes(1);
    return mockSubscribe.mock.calls[0][0];
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  test('a silent feed returns the twin to „Wartet auf Gelenkdaten …"', async () => {
    const onMsg = await mountWithFakeClock();
    expect(screen.getByText(WAITING)).toBeInTheDocument();

    act(() => onMsg({ name: ['joint1'], position: [0.1] }));
    expect(screen.queryByText(WAITING)).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(4000); });
    expect(screen.getByText(WAITING)).toBeInTheDocument();
  });

  test('a feed that keeps ticking never trips the watchdog', async () => {
    const onMsg = await mountWithFakeClock();
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));

    // 10 s of a 2 Hz feed — the sim idle republish cadence, the slowest real one.
    for (let i = 0; i < 20; i += 1) {
      act(() => { vi.advanceTimersByTime(500); });
      act(() => onMsg({ name: ['joint1'], position: [0.1 + i] }));
    }
    expect(screen.queryByText(WAITING)).not.toBeInTheDocument();
  });

  test('the watchdog is torn down with the component', async () => {
    const onMsg = await mountWithFakeClock();
    act(() => onMsg({ name: ['joint1'], position: [0.1] }));
    const before = vi.getTimerCount();

    act(() => { cleanup(); });

    expect(vi.getTimerCount()).toBeLessThan(before);
  });
});
