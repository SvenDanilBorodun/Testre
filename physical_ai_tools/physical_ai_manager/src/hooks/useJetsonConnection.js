// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import { useCallback, useEffect, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';

import {
  setNoJetson,
  setJetsonInfo,
  setJetsonStatus,
  setJetsonError,
  setHeartbeatTransient,
  clearJetson,
} from '../store/jetsonSlice';
import { setRosbridgeUrl, setRosHost, setImageTopicList } from '../features/ros/rosSlice';
import { moveToPage } from '../features/ui/uiSlice';
import PageType from '../constants/pageType';
import {
  getClassroomJetson,
  claimJetson,
  heartbeatJetson,
  releaseJetson,
  releaseJetsonBeacon,
} from '../services/jetsonClient';
import rosConnectionManager from '../utils/rosConnectionManager';

const HEARTBEAT_INTERVAL_MS = 30_000;
// The proxy listens on :9091 (the JWT-gated front), NOT :9090 (loopback
// rosbridge inside the physical_ai_server container). agent.py exposes
// :9091 directly.
const PROXY_PORT = 9091;

// Refresh-vs-close discriminator (M4). On unload while connected we drop
// this sessionStorage marker and then fire the release beacon.
// sessionStorage survives a same-tab refresh but is wiped on a real tab
// close, so on the next mount the discovery effect can tell a refresh
// (→ auto-reclaim the lock the beacon just freed) from a genuine close
// (→ leave the lock freed for the next student). The grace window bounds
// how long a stale marker can trigger an auto-reclaim.
const RECONNECT_MARKER_KEY = 'edubotics.jetson.reconnect';
const RECONNECT_GRACE_MS = 30_000;

/**
 * React hook that drives the entire Jetson connection lifecycle for
 * the Inference tab:
 *   - On mount: GET /classrooms/{id}/jetson, populate Redux.
 *   - On connect(): claim + swap rosbridge URL + start heartbeat.
 *   - On disconnect(): release + revert rosbridge URL.
 *   - Auto-disconnect on heartbeat 410 (lock lost server-side).
 *   - Best-effort release via sendBeacon on tab unload.
 */
export function useJetsonConnection() {
  const dispatch = useDispatch();
  const status = useSelector((s) => s.jetson.status);
  const jetsonId = useSelector((s) => s.jetson.jetsonId);
  const classroomId = useSelector((s) => s.auth.classroomId);
  const accessToken = useSelector((s) => s.auth.session?.access_token);
  const userId = useSelector((s) => s.auth.session?.user?.id);
  const localRosHost = useSelector((s) => s.ros.rosHost);

  // Stash the access token + jetsonId in refs so the unload beacon and
  // heartbeat timer always see the latest values without re-binding the
  // setInterval on every Redux change.
  const tokenRef = useRef(accessToken);
  const jetsonIdRef = useRef(jetsonId);
  const statusRef = useRef(status);
  const classroomIdRef = useRef(classroomId);
  useEffect(() => { tokenRef.current = accessToken; }, [accessToken]);
  useEffect(() => { jetsonIdRef.current = jetsonId; }, [jetsonId]);
  useEffect(() => { statusRef.current = status; }, [status]);
  useEffect(() => { classroomIdRef.current = classroomId; }, [classroomId]);

  // M3: keep the rosbridge proxy's auth token current. supabase-js
  // refreshes the access token roughly hourly during long sessions;
  // without pushing the rotated token to the manager, a WS reconnect
  // after the old token expired would send a dead JWT (the :9091 proxy
  // closes the socket) while the HTTP heartbeat — which reads the fresh
  // token via tokenRef — keeps the chip "connected". Gate on the live
  // status so we never clobber the null token of a local (non-Jetson)
  // session.
  useEffect(() => {
    if (statusRef.current === 'connected' && accessToken) {
      rosConnectionManager.setAuthToken(accessToken);
    }
  }, [accessToken]);

  // 1. Discovery on mount / classroom change.
  useEffect(() => {
    if (!accessToken || !classroomId) {
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const info = await getClassroomJetson(accessToken, classroomId);
        if (cancelled) return;
        if (!info) {
          dispatch(setNoJetson());
          return;
        }
        dispatch(setJetsonInfo(info));
        // Status flows from the owner field, with one refinement: a freed
        // lock that THIS tab just released on a refresh (fresh marker)
        // should be re-claimed rather than shown as merely "available".
        const action = planJetsonDiscoveryAction({
          owner: info.current_owner_user_id,
          userId,
          marker: _readReconnectMarker(),
          classroomId,
          now: Date.now(),
        });
        if (action !== 'reclaim') {
          _clearReconnectMarker();  // one-shot; only the refresh path consumes it
        }
        if (action === 'resume') {
          // We still hold the lock server-side (the unload beacon didn't
          // land, or this is a non-beacon reload). Re-point rosbridge at
          // the Jetson and resume the heartbeat — no extra claim needed.
          _swapRosbridgeToJetson(info, dispatch, accessToken);
          dispatch(setJetsonStatus('connected'));
        } else if (action === 'reclaim') {
          // Page was refreshed and our unload beacon freed the lock.
          // Re-claim it so an F5 doesn't drop the student's session.
          _clearReconnectMarker();
          dispatch(setJetsonStatus('claiming'));
          try {
            const claimed = await claimJetson(accessToken, info.jetson_id);
            if (cancelled) return;
            dispatch(setJetsonInfo(claimed));
            _swapRosbridgeToJetson(claimed, dispatch, accessToken);
            dispatch(setJetsonStatus('connected'));
          } catch (claimErr) {
            if (cancelled) return;
            // A peer grabbed the lock in the ~1 s reload window (409), or
            // the re-claim otherwise failed — fall back to the normal
            // available/busy view.
            dispatch(setJetsonStatus(claimErr?.status === 409 ? 'busy' : 'available'));
          }
        } else if (action === 'busy') {
          dispatch(setJetsonStatus('busy'));
        } else {
          dispatch(setJetsonStatus('available'));
        }
      } catch (err) {
        if (cancelled) return;
        dispatch(setJetsonError(err.message || 'Jetson-Status konnte nicht geladen werden'));
      }
    })();
    return () => { cancelled = true; };
  }, [accessToken, classroomId, dispatch, userId]);

  // 2. connect / disconnect callbacks the chip + buttons call.
  const connect = useCallback(async () => {
    if (!accessToken || !jetsonId) return;
    dispatch(setJetsonStatus('claiming'));
    try {
      const info = await claimJetson(accessToken, jetsonId);
      dispatch(setJetsonInfo(info));
      _swapRosbridgeToJetson(info, dispatch, accessToken);
      dispatch(setJetsonStatus('connected'));
      // Force navigation to the Inference tab. If the student happened
      // to be on Aufnahme or Roboter Studio when they clicked Verbinde,
      // the sidebar filter would have just hidden those tabs but the
      // routing state would still render the now-broken page (its
      // services live on the local rosbridge which is now overridden).
      dispatch(moveToPage(PageType.INFERENCE));
      toast.success('Verbunden mit Klassen-Jetson');
    } catch (err) {
      if (err.status === 409) {
        dispatch(setJetsonStatus('busy'));
        toast.error('Jetson ist bereits belegt');
      } else {
        dispatch(setJetsonError(err.message || 'Verbindung fehlgeschlagen'));
        toast.error(`Verbindung fehlgeschlagen: ${err.message}`);
      }
    }
  }, [accessToken, jetsonId, dispatch]);

  const disconnect = useCallback(async () => {
    if (!accessToken || !jetsonId) return;
    dispatch(setJetsonStatus('disconnecting'));
    try {
      await releaseJetson(accessToken, jetsonId);
    } catch (err) {
      // Idempotent on the server side — log + proceed.
      console.warn('release_jetson failed (proceeding anyway):', err);
    }
    _swapRosbridgeBackToLocal(localRosHost, dispatch);
    dispatch(setJetsonStatus('available'));
    toast.success('Vom Jetson getrennt');
  }, [accessToken, jetsonId, localRosHost, dispatch]);

  // 3. Heartbeat while connected.
  useEffect(() => {
    if (status !== 'connected' || !accessToken || !jetsonId) {
      return undefined;
    }
    // Consecutive non-410 failure counter. After 2 failures (60s of
    // silence assuming 30s interval) we flip the transient flag so the
    // chip shows "Verbindung wird wiederhergestellt…". On the next
    // success, the counter resets and the flag clears. 410 is handled
    // separately as a terminal auto-disconnect.
    let consecutiveFailures = 0;
    const beat = async () => {
      try {
        await heartbeatJetson(tokenRef.current, jetsonIdRef.current);
        if (consecutiveFailures > 0) {
          // Recovered — clear the warning chip variant.
          consecutiveFailures = 0;
          dispatch(setHeartbeatTransient(false));
        }
      } catch (err) {
        if (err.status === 410) {
          // Lock was released server-side (sweeper or teacher force).
          console.warn('Heartbeat 410 — auto-disconnect');
          _swapRosbridgeBackToLocal(localRosHost, dispatch);
          dispatch(setJetsonStatus('available'));
          dispatch(setHeartbeatTransient(false));
          toast.error('Verbindung zum Jetson verloren — bitte erneut verbinden');
        } else {
          consecutiveFailures += 1;
          console.warn(
            `Heartbeat failed (transient ${consecutiveFailures}):`,
            err
          );
          if (consecutiveFailures >= 2) {
            dispatch(setHeartbeatTransient(true));
          }
        }
      }
    };
    // Fire immediately so a slow-loading tab doesn't wait 30s for the
    // first probe.
    beat();
    const interval = setInterval(beat, HEARTBEAT_INTERVAL_MS);
    return () => {
      clearInterval(interval);
      // Clear the warning chip when the effect tears down (disconnect /
      // status change / unmount); avoids the chip staying "wird wieder-
      // hergestellt…" on the next available-state render.
      dispatch(setHeartbeatTransient(false));
    };
  }, [status, accessToken, jetsonId, localRosHost, dispatch]);

  // 4. Best-effort release on tab unload. We listen on `pagehide` (fires
  // on tab close, navigation, AND bfcache eviction; `beforeunload` is
  // unreliable and blocks the back/forward cache). Before releasing we
  // drop a short-lived sessionStorage marker so the next mount can tell a
  // same-tab refresh (marker survives → auto-reclaim) from a real close
  // (marker wiped with the tab → leave the lock freed). This keeps
  // v2.3.0's prompt release-on-close while no longer dropping the
  // student's session on F5.
  useEffect(() => {
    const onUnload = () => {
      if (statusRef.current === 'connected' && tokenRef.current && jetsonIdRef.current) {
        _writeReconnectMarker(classroomIdRef.current);
        releaseJetsonBeacon(tokenRef.current, jetsonIdRef.current);
      }
    };
    window.addEventListener('pagehide', onUnload);
    return () => window.removeEventListener('pagehide', onUnload);
  }, []);

  return { connect, disconnect, status };
}

// ---- Refresh-vs-close marker helpers (M4) ----

function _readReconnectMarker() {
  try {
    const raw = window.sessionStorage.getItem(RECONNECT_MARKER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;  // sessionStorage unavailable / malformed — treat as no marker
  }
}

function _writeReconnectMarker(classroomId) {
  try {
    window.sessionStorage.setItem(
      RECONNECT_MARKER_KEY,
      JSON.stringify({ classroomId, at: Date.now() })
    );
  } catch (_) {
    /* best-effort: sessionStorage unavailable / full */
  }
}

function _clearReconnectMarker() {
  try {
    window.sessionStorage.removeItem(RECONNECT_MARKER_KEY);
  } catch (_) {
    /* best-effort */
  }
}

/**
 * Pure decision for the discovery effect's owner branch. Exported for
 * unit testing.
 *   'resume'    — server still names us owner → re-attach, no claim call.
 *   'reclaim'   — lock is free AND this tab just released it on a refresh
 *                 (fresh marker for this classroom) → re-claim it.
 *   'busy'      — a different user holds the lock.
 *   'available' — lock is free and no fresh refresh marker → normal idle.
 * @returns {'resume'|'reclaim'|'busy'|'available'}
 */
export function planJetsonDiscoveryAction({ owner, userId, marker, classroomId, now }) {
  if (owner && userId && owner === userId) return 'resume';
  if (owner) return 'busy';
  const fresh =
    marker &&
    marker.classroomId === classroomId &&
    typeof marker.at === 'number' &&
    now - marker.at >= 0 &&
    now - marker.at < RECONNECT_GRACE_MS;
  return fresh ? 'reclaim' : 'available';
}

function _swapRosbridgeToJetson(info, dispatch, accessToken) {
  const host = info.lan_ip || info.mdns_name;
  if (!host) {
    console.warn('Jetson info missing lan_ip and mdns_name — cannot route');
    return;
  }
  // Plain ws:// + JWT auth-op (no TLS in v1). The proxy listens on :9091.
  const url = `ws://${host}:${PROXY_PORT}`;
  rosConnectionManager.setAuthToken(accessToken);
  dispatch(setRosbridgeUrl(url));
  // Drop any cached camera-topic list from the previous (local) session
  // so the top-bar "N Kameras aktiv" chip + ImageGrid don't render
  // pre-Jetson topic names against the new rosbridge. ImageGrid's mount
  // effect re-fetches /image/get_available_list against the Jetson and
  // the liveness hook re-subscribes against the new socket.
  dispatch(setImageTopicList([]));
  // Force a reconnect against the new URL. The singleton handles URL
  // changes automatically via getConnection(newUrl), but we need to
  // disconnect first so the existing local-rosbridge socket goes away
  // before subscribers re-bind.
  rosConnectionManager.disconnect();
  rosConnectionManager.resetReconnectCounter();
  // The actual getConnection() call comes from useRosTopicSubscription /
  // useRosServiceCaller on next render — they read state.ros.rosbridgeUrl.
}

function _swapRosbridgeBackToLocal(localRosHost, dispatch) {
  rosConnectionManager.setAuthToken(null);
  if (localRosHost) {
    dispatch(setRosHost(localRosHost));
  } else {
    // No local rosbridge configured (cloud-only mode?) — just clear.
    dispatch(setRosbridgeUrl(''));
  }
  // Same reasoning as in _swapRosbridgeToJetson — drop the cached list
  // so the Jetson's topic names don't render against a local rosbridge.
  dispatch(setImageTopicList([]));
  rosConnectionManager.disconnect();
  rosConnectionManager.resetReconnectCounter();
}

/**
 * Reset everything on logout. MUST be called from every signOut path
 * BEFORE supabase.auth.signOut() invalidates the JWT, otherwise the
 * release call cannot authenticate and the lock leaks for the full
 * 5-min sweeper window.
 *
 * Optional jetsonId + accessToken let us do a best-effort beacon
 * release on the way out (matches the tab-close path). Callers that
 * don't have those handy can omit them — the slice clear still happens.
 *
 * Always called from React event handlers, not from the heartbeat
 * loop, so we can read state lazily via the closure rather than
 * needing refs.
 */
export function resetJetsonOnLogout(dispatch, accessToken = null, jetsonId = null) {
  // Best-effort beacon release so the lock frees immediately on
  // explicit logout instead of waiting 5 min for the sweeper. The
  // beacon path revalidates the JWT server-side, so a slightly-stale
  // token still works as long as it hasn't expired.
  if (accessToken && jetsonId) {
    try { releaseJetsonBeacon(accessToken, jetsonId); } catch (_) { /* swallow */ }
  }
  // Drop any pending refresh-reconnect marker so a later sign-in to the
  // same classroom doesn't auto-reclaim on a stale marker.
  _clearReconnectMarker();
  rosConnectionManager.setAuthToken(null);
  dispatch(clearJetson());
}
