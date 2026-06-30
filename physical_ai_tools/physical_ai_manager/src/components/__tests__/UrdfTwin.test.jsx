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
    Scene: function Scene() { return noopObj({ background: null, traverse: () => {} }); },
    Color: function Color() {},
    PerspectiveCamera: function PerspectiveCamera() {
      return noopObj({ updateProjectionMatrix: () => {}, aspect: 1, near: 0.1, far: 100 });
    },
    WebGLRenderer: function WebGLRenderer() {
      return {
        setPixelRatio: () => {},
        setSize: () => {},
        render: () => {},
        dispose: () => {},
        domElement: document.createElement('canvas'),
      };
    },
    HemisphereLight: function HemisphereLight() { return noopObj(); },
    DirectionalLight: function DirectionalLight() { return noopObj(); },
    GridHelper: function GridHelper() { return noopObj(); },
    MeshStandardMaterial: function MeshStandardMaterial() { return { dispose: () => {} }; },
    MeshPhongMaterial: function MeshPhongMaterial() { return { dispose: () => {} }; },
    Mesh: function Mesh() { return noopObj(); },
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
