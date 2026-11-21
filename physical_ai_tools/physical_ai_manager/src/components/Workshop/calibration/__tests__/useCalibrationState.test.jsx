/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// useCalibrationState carries two robot facts into the workshop slice while
// Roboter Studio is open: the calibration service's state
// (/edubotics/calibration_state, spec 40 S4) and the scene-camera profile the
// arm container states (/edubotics/scene_camera_profile, D63). A malformed
// message is dropped, never guessed into a state; leaving the page forgets
// both, so the next entry starts from „wird geprüft", never from a stale „ok".

import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import workshopReducer from '../../../../features/workshop/workshopSlice';
import useCalibrationState, { CALIBRATION_STATE_TOPIC } from '../useCalibrationState';
import { SCENE_CAMERA_PROFILE_TOPIC } from '../../../../hooks/useSceneCameraProfile';

vi.mock('../../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));

const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
  },
}));

function makeStore(rosbridgeUrl = 'ws://localhost/rosbridge') {
  return configureStore({
    reducer: { workshop: workshopReducer, ros: () => ({ rosbridgeUrl }) },
  });
}

function mount(store, enabled = true) {
  const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
  return renderHook(({ on }) => useCalibrationState({ enabled: on }), { wrapper, initialProps: { on: enabled } });
}

const topic = (name) => mockTopics.find((t) => t.opts.name === name && !t.unsubscribed);
const stateMsg = (state, code = state) => ({
  data: JSON.stringify({
    v: 1, state, code, message_de: `Satz ${code}`, notes: [], camera: null, arm_check: null,
    handheld: null, quality: null, fixture: { id: 'tower_v1', lift_m: 0.0072 }, droop_warning: null,
    mat_check: null,
  }),
});

beforeEach(() => {
  mockTopics.length = 0;
});

describe('useCalibrationState', () => {
  it('names the robot topic', () => {
    expect(CALIBRATION_STATE_TOPIC).toBe('/edubotics/calibration_state');
  });

  it('subscribes to the state and the profile, latched-friendly, and fills the slice', async () => {
    const store = makeStore();
    mount(store);
    await waitFor(() => expect(topic(CALIBRATION_STATE_TOPIC)).toBeTruthy());
    await waitFor(() => expect(topic(SCENE_CAMERA_PROFILE_TOPIC)).toBeTruthy());
    expect(topic(CALIBRATION_STATE_TOPIC).opts.messageType).toBe('std_msgs/msg/String');
    expect(topic(CALIBRATION_STATE_TOPIC).opts.throttle_rate).toBeLessThan(1000);

    act(() => { topic(CALIBRATION_STATE_TOPIC).cb(stateMsg('needs_arm_check', 'needs_arm_check/none')); });
    expect(store.getState().workshop.calibration.state).toBe('needs_arm_check');
    expect(store.getState().workshop.calibration.message_de).toBe('Satz needs_arm_check/none');

    act(() => { topic(SCENE_CAMERA_PROFILE_TOPIC).cb({ data: '{"v":1,"profile":"tower_v1"}' }); });
    expect(store.getState().workshop.sceneCameraProfile).toBe('tower_v1');
  });

  it('a malformed message is dropped and the last good state stays', async () => {
    const store = makeStore();
    mount(store);
    await waitFor(() => expect(topic(CALIBRATION_STATE_TOPIC)).toBeTruthy());
    act(() => { topic(CALIBRATION_STATE_TOPIC).cb(stateMsg('ok')); });
    act(() => { topic(CALIBRATION_STATE_TOPIC).cb({ data: '{"v":1,"state":"calibrated"}' }); });
    act(() => { topic(CALIBRATION_STATE_TOPIC).cb({ data: 'kaputt' }); });
    expect(store.getState().workshop.calibration.state).toBe('ok');
  });

  it('leaving the page unsubscribes and forgets both facts', async () => {
    const store = makeStore();
    const { rerender } = mount(store);
    await waitFor(() => expect(topic(SCENE_CAMERA_PROFILE_TOPIC)).toBeTruthy());
    const t = topic(CALIBRATION_STATE_TOPIC);
    act(() => { t.cb(stateMsg('ok')); });
    act(() => { topic(SCENE_CAMERA_PROFILE_TOPIC).cb({ data: '{"v":1,"profile":"tower_v1"}' }); });
    rerender({ on: false });
    expect(t.unsubscribed).toBe(true);
    expect(store.getState().workshop.calibration).toBeNull();
    expect(store.getState().workshop.sceneCameraProfile).toBeNull();
  });

  it('subscribes to nothing while disabled or without a robot link', async () => {
    mount(makeStore(), false);
    mount(makeStore(null), true);
    await new Promise((r) => { setTimeout(r, 0); });
    expect(mockTopics).toHaveLength(0);
  });
});
