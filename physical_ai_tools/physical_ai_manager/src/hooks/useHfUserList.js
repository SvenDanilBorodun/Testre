// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { useCallback } from 'react';
import { useDispatch, useSelector, useStore } from 'react-redux';
import { useRosServiceCaller } from './useRosServiceCaller';
import { setHfUserList } from '../features/ui/uiSlice';
import { selectHfListReloadAllowed } from '../features/hfToken/hfTokenSelectors';

/**
 * Shared access to the HuggingFace Benutzer-ID list (the account + orgs the
 * token in the ROBOT's slot can push to). The list comes from the
 * `/get_registered_hf_user` ROS service (server-side `whoami`, which
 * authenticates with whatever token the robot holds — the student's own, put
 * there from their cloud account by useHfTokenSync) and is stored in Redux so
 * it survives tab switches — components must NOT keep it in local useState
 * (that was the "Benutzer-ID gets wiped on tab switch" bug).
 *
 * THE GATE (audit S1). `reload` is the ONE choke point every caller goes
 * through (StudentApp's connect effect, the Aufnahme page, the model-download
 * modal, the dataset upload section), and it does not ask the robot while the
 * slot might hold ANOTHER student's token: the answer would be that student's
 * account and organisations, the recording would upload under them, and the
 * upload namespace guard would agree because both sides name the same account.
 * `selectHfListReloadAllowed` allows it only when the slot is provably this
 * student's, or provably not a personal slot (the Jetson image), or when an old
 * image proved itself silent; a store without the `hfToken` slice is allowed.
 * A blocked call changes nothing and resolves to null, exactly like a failed
 * one, so no caller needed a new branch.
 *
 * Returns:
 *   hfUserList — the cached list (string[])
 *   reload()   — re-fetch from the server and update Redux; resolves to the
 *                new list, or null on failure or while the gate is closed.
 *                Safe to call repeatedly.
 */
export function useHfUserList() {
  const dispatch = useDispatch();
  const store = useStore();
  const { getRegisteredHFUser } = useRosServiceCaller();
  const hfUserList = useSelector((state) => state.ui.hfUserList);

  const reload = useCallback(async () => {
    // Read at call time, never closed over: the gate opens the moment the robot
    // reports the student's own token, and a stale closure would keep it shut.
    if (!selectHfListReloadAllowed(store.getState())) return null;
    try {
      const result = await getRegisteredHFUser();
      if (result && result.success && Array.isArray(result.user_id_list)) {
        // The gate can close while the whoami is on its way (sign-out, a
        // hand-over): an answer for a slot that is no longer provably this
        // student's is not stored.
        if (!selectHfListReloadAllowed(store.getState())) return null;
        dispatch(setHfUserList(result.user_id_list));
        return result.user_id_list;
      }
    } catch (error) {
      // Non-fatal: the token may be unset on a fresh PC, or the network may
      // be slow. The caller shows an empty dropdown; the student can retry
      // via the "Laden" button. Logged for diagnostics only.
      console.warn('[hf-users] reload failed:', error?.message || error);
    }
    return null;
  }, [getRegisteredHFUser, dispatch, store]);

  return { hfUserList, reload };
}
