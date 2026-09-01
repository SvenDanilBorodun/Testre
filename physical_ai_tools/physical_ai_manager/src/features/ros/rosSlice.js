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
    // The URL is SAME-ORIGIN on every local rig — ws://<host>/rosbridge via
    // the nginx proxy — and has been since the 2026-08-06 rosbridge move.
    // There is no longer a non-Pi branch handing out a direct
    // ws://<host>:9090 URL: `localRosbridgeUrl`'s `piMode` parameter is
    // vestigial (see utils/piMode.js) and this call does not even pass it.
    // Two independent reasons converge on the same answer — school networks
    // filter :9090, and on Windows a page served from :80 talking to :9090 was
    // a CROSS-ORIGIN connection to an unauthenticated socket that can drive
    // the arm, with no CORS preflight to stop it.
    //
    // Both dispatch sites (StudentApp's seed effect, gated on piModeResolved,
    // and useJetsonConnection's swap-back, which runs long after boot) still
    // fire only once the marker has resolved. That gate no longer changes the
    // URL this reducer derives; it is kept as ordering, not as a selector.
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
