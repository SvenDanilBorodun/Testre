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
import { render, screen, waitFor, act } from '@testing-library/react';
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
const mockRobot = {
  rotation: { x: 0 },
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
      // Write the test-controlled TCP world position into the target vector so
      // appendPathPoint / emitEndEffector see real numeric coords (the original
      // `(v) => v` returned a coord-less Vector3, which appendPathPoint rejects,
      // so the path never accumulated). Returns the same vector for the callers
      // that read .x/.y/.z off it.
      getWorldPosition: (v) => {
        if (v && typeof v.set === 'function') {
          v.set(mockEEWorld.x, mockEEWorld.y, mockEEWorld.z);
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
// Path-trail internals: a setDrawRange spy + the captured path BufferGeometry let
// the extended tests assert the draw range advances (append) and resets (clear).
const mockSetDrawRange = vi.fn();
let mockPathGeometry = null;
// Test-controlled end-effector world position, written into the target vector by
// the mock robot's getWorldPosition so appendPathPoint sees real numeric coords.
const mockEEWorld = { x: 0, y: 0, z: 0 };
vi.mock('urdf-loader', () => ({
  __esModule: true,
  default: function URDFLoaderMock() {
    this.loadMeshCb = null;
    this.load = (_url, onComplete) => {
      // Invoke the success path with our recording robot (synchronous — no
      // network, no STL parsing in the mock).
      onComplete(mockRobot);
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
    // `attach` is what the held-object release path re-parents through
    // (scene.attach preserves the carried world transform) — a no-op here.
    Scene: function Scene() {
      return noopObj({ background: null, traverse: () => {}, attach: () => {} });
    },
    Color: function Color() {},
    PerspectiveCamera: function PerspectiveCamera() {
      return noopObj({ updateProjectionMatrix: () => {}, aspect: 1, near: 0.1, far: 100 });
    },
    WebGLRenderer: function WebGLRenderer() {
      // Capture the instance + a shadowMap object so the sim-stage shadow test can
      // assert `showShadows` flips renderer.shadowMap.enabled (and the default
      // call leaves it false).
      mockRenderer = {
        setPixelRatio: () => {},
        setSize: () => {},
        render: () => {},
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
      addEventListener: () => {},
      update: () => false,
      dispose: () => {},
    };
  },
}));
vi.mock('three/examples/jsm/loaders/STLLoader', () => ({
  __esModule: true,
  STLLoader: function STLLoader() {
    this.load = () => {};
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
  mockSetDrawRange.mockClear();
  mockPathGeometry = null;
  mockEEWorld.x = 0;
  mockEEWorld.y = 0;
  mockEEWorld.z = 0;
  mockRobot.rotation.x = 0;
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
