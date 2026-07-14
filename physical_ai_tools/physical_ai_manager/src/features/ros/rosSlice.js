/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Author: Kiwoong Park
 */

import { createSlice } from '@reduxjs/toolkit';
import { localRosbridgeUrl } from '../../utils/piMode';

const initialState = {
  rosHost: '',
  rosbridgeUrl: '',
  imageTopicList: [],
};

const rosSlice = createSlice({
  name: 'ros',
  initialState,
  reducers: {
    // The URL derivation is Pi-aware: on the Orange Pi rosbridge rides the
    // same-origin nginx proxy (ws://<host>/rosbridge, port 80 — school
    // firewalls filter :9090), everywhere else the classic direct
    // ws://<host>:9090. localRosbridgeUrl reads the piMode module cache;
    // both dispatch sites (StudentApp's seed effect, gated on
    // piModeResolved, and useJetsonConnection's swap-back, which runs long
    // after boot) only fire once the marker has resolved.
    setRosHost: (state, action) => {
      state.rosHost = action.payload;
      state.rosbridgeUrl = localRosbridgeUrl(action.payload);
    },
    setRosbridgeUrl: (state, action) => {
      state.rosbridgeUrl = action.payload;
    },
    setImageTopicList: (state, action) => {
      state.imageTopicList = action.payload;
    },
  },
});

export const {
  setRosHost,
  setRosbridgeUrl,
  setImageTopicList,
} = rosSlice.actions;

export default rosSlice.reducer;
