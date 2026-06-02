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
import { useDispatch, useSelector } from 'react-redux';
import { useRosServiceCaller } from './useRosServiceCaller';
import { setHfUserList } from '../features/ui/uiSlice';

/**
 * Shared access to the HuggingFace Benutzer-ID list (the account + orgs the
 * student's token can push to). The list comes from the `/get_registered_hf_user`
 * ROS service (server-side `whoami`, which now authenticates with the $HF_TOKEN
 * the student set once in the GUI) and is stored in Redux so it survives tab
 * switches — components must NOT keep it in local useState (that was the
 * "Benutzer-ID gets wiped on tab switch" bug).
 *
 * Returns:
 *   hfUserList — the cached list (string[])
 *   reload()   — re-fetch from the server and update Redux; resolves to the
 *                new list, or null on failure. Safe to call repeatedly.
 */
export function useHfUserList() {
  const dispatch = useDispatch();
  const { getRegisteredHFUser } = useRosServiceCaller();
  const hfUserList = useSelector((state) => state.ui.hfUserList);

  const reload = useCallback(async () => {
    try {
      const result = await getRegisteredHFUser();
      if (result && result.success && Array.isArray(result.user_id_list)) {
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
  }, [getRegisteredHFUser, dispatch]);

  return { hfUserList, reload };
}
