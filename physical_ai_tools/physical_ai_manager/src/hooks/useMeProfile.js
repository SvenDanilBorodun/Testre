// Copyright 2026 EduBotics
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

import { useCallback, useEffect, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { getMe } from '../services/meApi';
import { setProfile, setProfileError } from '../features/auth/authSlice';
import { signOutStudent } from '../utils/signOut';

// Transient-error retry budget. A 5xx / network blip self-heals; the user
// should not be bounced to a dead "Server nicht erreichbar" card on the
// first hiccup. Backoff: 1s, 2s, 4s.
const RETRY_DELAYS_MS = [1000, 2000, 4000];

/**
 * Robust loader for the cloud profile (GET /me) that covers every failure
 * mode.
 *
 * Behaviour, keyed on `session.access_token`:
 *   - success            → dispatch(setProfile(me)); onProfile?.(me) for the
 *                          app-specific role handling.
 *   - 401 / 403          → sign out (JWT dead) so the LoginForm returns.
 *   - 404                → profileError "Profil nicht gefunden …" — do NOT
 *                          sign out (the JWT is valid; the public.users row
 *                          is missing — a teacher must fix it).
 *   - other / network    → retry up to 3× with backoff, then profileError
 *                          "Server nicht erreichbar …". Retriable via refetch.
 *
 * There is NO Hugging-Face auto-link here any more. This hook used to PATCH
 * /me with whatever Benutzer-ID the ROBOT's token happened to report, which
 * linked the account to a name nobody had proven — on a shared PC, to the
 * previous student's. `users.hf_username` is now set by the cloud from the
 * token's own whoami when the student stores it on the Startseite
 * (PUT /me/hf-token, components/Home/HfTokenCard), and `PATCH /me` answers 409
 * once a token is stored.
 *
 * @param {object}  opts
 * @param {(me:object)=>void} [opts.onProfile]  app-specific success handler
 *        (role checks etc.). Receives the raw /me body. setProfile is already
 *        dispatched by the time this runs.
 *
 * Retrying a failed load is done by dispatching `requestProfileRefetch()`
 * (the Training/Inferenz error-card buttons) — this hook is the single owner
 * and watches that nonce, so there is no second fetch path to keep in sync.
 */
export function useMeProfile({ onProfile } = {}) {
  const dispatch = useDispatch();
  const session = useSelector((s) => s.auth.session);
  const accessToken = session?.access_token;
  // Bumped by requestProfileRefetch() (the error-card retry buttons). The
  // load effect lists it so a bump re-runs GET /me without a second hook.
  const profileRefetchNonce = useSelector((s) => s.auth.profileRefetchNonce);

  // Latest-value ref so the fetch callback never lists a volatile value as a
  // dep (the stale-closure bug class this repo has been bitten by: rosbridge
  // empty-URL, Benutzer-ID wipe). onProfile changes identity often; we read it
  // through the ref at fire time.
  const onProfileRef = useRef(onProfile);
  useEffect(() => {
    onProfileRef.current = onProfile;
  }, [onProfile]);

  // Generation counter: every (re)fetch bumps this; in-flight retries check
  // it before dispatching so a stale chain (old token / superseded retry)
  // can't write into Redux. Replaces the per-effect `alive` flag and also
  // cancels pending setTimeout retries across a refetch.
  const genRef = useRef(0);

  const runFetch = useCallback(() => {
    if (!accessToken) return;
    const myGen = ++genRef.current;
    dispatch(setProfileError(null));

    const attempt = (tryIndex) => {
      getMe(accessToken)
        .then((me) => {
          if (genRef.current !== myGen) return; // superseded
          dispatch(setProfile(me));
          if (onProfileRef.current) onProfileRef.current(me);
        })
        .catch((err) => {
          if (genRef.current !== myGen) return; // superseded
          // eslint-disable-next-line no-console
          console.error('getMe failed', err);
          const status = err?.status ?? err?.response?.status;
          if (status === 401 || status === 403) {
            // reload:false so the German reason above survives — the login form
            // alone does not say WHY they were bounced. (The thunk still
            // attempts the Jetson beacon with the now-dead JWT; the server
            // rejects it and the lock waits for the 5-min sweeper, exactly as
            // before. A dead token is not worth a second code path.)
            toast.error('Sitzung abgelaufen — bitte erneut anmelden.');
            dispatch(signOutStudent({ reload: false }));
            return;
          }
          if (status === 404) {
            // Valid JWT, missing public.users row — a teacher must add the
            // student. Signing out would just loop them back to a login that
            // succeeds and lands here again.
            dispatch(
              setProfileError(
                'Profil nicht gefunden – bitte wende dich an deine Lehrkraft.'
              )
            );
            return;
          }
          // Network / 5xx → retry with backoff, then a retriable error card.
          if (tryIndex < RETRY_DELAYS_MS.length) {
            setTimeout(() => {
              if (genRef.current !== myGen) return;
              attempt(tryIndex + 1);
            }, RETRY_DELAYS_MS[tryIndex]);
            return;
          }
          dispatch(
            setProfileError(
              'Server nicht erreichbar – bitte Verbindung prüfen und erneut versuchen.'
            )
          );
        });
    };

    attempt(0);
  }, [accessToken, dispatch]);

  // Initial load + reload on token change + on a refetch-nonce bump.
  useEffect(() => {
    if (!accessToken) {
      // No session: invalidate any in-flight chain so a late resolve can't
      // write a stale profile after sign-out.
      genRef.current += 1;
      return;
    }
    runFetch();
    // profileRefetchNonce re-runs the load when the retry button bumps it.
  }, [accessToken, runFetch, profileRefetchNonce]);
}

export default useMeProfile;
