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
import { getMe, patchMyHfUsername } from '../services/meApi';
import {
  setProfile,
  setProfileError,
  setHfUsername,
} from '../features/auth/authSlice';
import { signOutStudent } from '../utils/signOut';

// Transient-error retry budget. A 5xx / network blip self-heals; the user
// should not be bounced to a dead "Server nicht erreichbar" card on the
// first hiccup. Backoff: 1s, 2s, 4s.
const RETRY_DELAYS_MS = [1000, 2000, 4000];

/**
 * Robust loader for the cloud profile (GET /me) that covers every failure
 * mode, plus the HF-identity auto-link.
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
 * HF auto-link (when `enableHfLink`): once the profile has loaded with a
 * null hf_username AND a Benutzer-ID is known (localStorage 'edubotics_userId'
 * or the sole hfUserList entry), PATCH /me once to link them. The Benutzer-ID
 * can arrive AFTER /me (the ROS whoami resolves later), so the link effect
 * reacts to both. PATCH failures are swallowed (logged) — it self-heals on
 * the next load.
 *
 * @param {object}  opts
 * @param {(me:object)=>void} [opts.onProfile]  app-specific success handler
 *        (role checks etc.). Receives the raw /me body. setProfile is already
 *        dispatched by the time this runs.
 * @param {boolean} [opts.enableHfLink=false]   run the HF auto-link effect.
 *
 * Retrying a failed load is done by dispatching `requestProfileRefetch()`
 * (the Training/Inferenz error-card buttons) — this hook is the single owner
 * and watches that nonce, so there is no second fetch path to keep in sync.
 */
export function useMeProfile({ onProfile, enableHfLink = false } = {}) {
  const dispatch = useDispatch();
  const session = useSelector((s) => s.auth.session);
  const accessToken = session?.access_token;
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);
  const hfUsername = useSelector((s) => s.auth.hfUsername);
  // Bumped by requestProfileRefetch() (the error-card retry buttons). The
  // load effect lists it so a bump re-runs GET /me without a second hook.
  const profileRefetchNonce = useSelector((s) => s.auth.profileRefetchNonce);
  const hfUserList = useSelector((s) => s.ui.hfUserList);
  // Used only to RE-RUN the auto-link effect when the Benutzer-ID arrives
  // after /me. The single entry is read via the helper below.
  const hfUserListLen = hfUserList.length;

  // Latest-value refs so the fetch/link callbacks never list volatile values
  // as deps (the stale-closure bug class this repo has been bitten by:
  // rosbridge empty-URL, Benutzer-ID wipe). onProfile + hfUserList change
  // identity often; we read them through refs at fire time.
  const onProfileRef = useRef(onProfile);
  useEffect(() => {
    onProfileRef.current = onProfile;
  }, [onProfile]);
  const hfUserListRef = useRef(hfUserList);
  useEffect(() => {
    hfUserListRef.current = hfUserList;
  }, [hfUserList]);

  // Generation counter: every (re)fetch bumps this; in-flight retries check
  // it before dispatching so a stale chain (old token / superseded retry)
  // can't write into Redux. Replaces the per-effect `alive` flag and also
  // cancels pending setTimeout retries across a refetch.
  const genRef = useRef(0);
  // Guards the one-shot HF link per token so the effect (which reacts to the
  // Benutzer-ID arriving) can't loop. Reset when the token changes.
  const linkAttemptedRef = useRef(false);

  // Read the currently-selected Benutzer-ID: the persisted choice, else the
  // sole whoami entry (unambiguous), else null.
  const resolveBenutzerId = useCallback(() => {
    let stored = null;
    try {
      stored = localStorage.getItem('edubotics_userId') || null;
    } catch {
      stored = null;
    }
    const list = hfUserListRef.current || [];
    if (stored && list.includes(stored)) return stored;
    if (stored && list.length === 0) return stored; // whoami not loaded yet
    if (list.length === 1) return list[0];
    if (stored) return stored;
    return null;
  }, []);

  const runFetch = useCallback(() => {
    if (!accessToken) return;
    const myGen = ++genRef.current;
    linkAttemptedRef.current = false;
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

  // ── HF identity auto-link ────────────────────────────────────────────
  // Reacts to BOTH the profile loading AND the Benutzer-ID arriving later
  // (hfUserListLen / hfUsername in the dep list). One PATCH per token.
  useEffect(() => {
    if (!enableHfLink) return;
    if (!accessToken) return;
    if (!profileLoaded) return;
    if (hfUsername) return; // already linked
    if (linkAttemptedRef.current) return;
    const benutzerId = resolveBenutzerId();
    if (!benutzerId) return; // nothing to link yet — wait for whoami

    linkAttemptedRef.current = true;
    const myGen = genRef.current;
    patchMyHfUsername(accessToken, benutzerId)
      .then((updated) => {
        if (genRef.current !== myGen) return;
        // Trust the server echo when present; fall back to the value we sent.
        dispatch(setHfUsername(updated?.hf_username ?? benutzerId));
      })
      .catch((err) => {
        // Self-heals on the next load — keep it quiet (no student toast).
        // eslint-disable-next-line no-console
        console.warn('[me] hf_username auto-link failed:', err?.message || err);
        // Allow a later retry within this token (e.g. the Benutzer-ID
        // changes) by clearing the one-shot guard.
        linkAttemptedRef.current = false;
      });
  }, [
    enableHfLink,
    accessToken,
    profileLoaded,
    hfUsername,
    hfUserListLen,
    resolveBenutzerId,
    dispatch,
  ]);
}

export default useMeProfile;
