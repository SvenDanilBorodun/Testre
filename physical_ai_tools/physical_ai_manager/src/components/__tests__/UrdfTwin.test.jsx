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
};
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
    set() { return this; }
    copy() { return this; }
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
});
