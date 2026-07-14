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
//
// Author: Kiwoong Park

import React, { useCallback, useEffect, useRef } from 'react';
import clsx from 'clsx';
import { MdHome, MdVideocam, MdMemory, MdWidgets, MdConstruction, MdSettings } from 'react-icons/md';
import { GoGraph } from 'react-icons/go';
import toast from 'react-hot-toast';
import './App.css';
import HomePage from './pages/HomePage';
import RecordPage from './pages/RecordPage';
import InferencePage from './pages/InferencePage';
import TrainingPage from './pages/TrainingPage';
import EditDatasetPage from './pages/EditDatasetPage';
import WorkshopPage from './pages/WorkshopPage';
import SystemPage from './pages/SystemPage';
import CollisionModal from './components/CollisionModal';
import PiUpdateGate from './components/PiUpdateGate';
import StartupGate from './components/StartupGate';
import { LogoMark } from './components/EbUI';
import packageJson from '../package.json';
import { useRosTopicSubscription } from './hooks/useRosTopicSubscription';
import { useHfUserList } from './hooks/useHfUserList';
import { useHeartbeatWatchdog } from './hooks/useHeartbeatWatchdog';
import rosConnectionManager from './utils/rosConnectionManager';
import { useDispatch, useSelector } from 'react-redux';
import { setRosHost } from './features/ros/rosSlice';
import { moveToPage } from './features/ui/uiSlice';
import PageType from './constants/pageType';
import { supabase } from './lib/supabaseClient';
import {
  setSession,
  setIsLoading,
  clearSession,
} from './features/auth/authSlice';
import { useMeProfile } from './hooks/useMeProfile';
import { resetJetsonOnLogout } from './hooks/useJetsonConnection';
import { isCloudOnlyMode } from './utils/cloudMode';
import { isCapabilityVisible, robotGateDecision } from './utils/navGating';
import { usePiMode } from './utils/piMode';

function StudentApp() {
  const dispatch = useDispatch();
  const taskStatus = useSelector((state) => state.tasks.taskStatus);
  const taskInfo = useSelector((state) => state.tasks.taskInfo);
  const trainingTopicReceived = useSelector((state) => state.training.topicReceived);
  const session = useSelector((state) => state.auth.session);
  const role = useSelector((state) => state.auth.role);
  const profileLoaded = useSelector((state) => state.auth.profileLoaded);
  // True iff the student currently holds the lock on the classroom Jetson
  // (state.jetson.status === 'connected'). When set, Aufnahme + Roboter
  // Studio are filtered out of the sidebar (see navItems below) because
  // those tabs need the LOCAL rosbridge — which the Jetson connection has
  // overridden.
  const jetsonConnected = useSelector((state) => state.jetson.status === 'connected');
  // v2.3.0: needed so the signOut handlers below can fire a beacon
  // release with the still-valid JWT before the session goes away.
  // Mirrored into a ref so the profile-fetch effect (keyed on the access
  // token only) reads the CURRENT jetsonId at fire time without listing it
  // as a dep — re-running getMe on every Jetson connect/disconnect would
  // spuriously re-fetch the profile and re-toast. Same latest-value-by-ref
  // pattern as useJetsonConnection's beacon refs.
  const jetsonId = useSelector((state) => state.jetson.jetsonId);
  const jetsonIdRef = useRef(jetsonId);
  useEffect(() => {
    jetsonIdRef.current = jetsonId;
  }, [jetsonId]);
  const cloudOnly = isCloudOnlyMode();
  // Orange Pi: the System tab (the in-browser setup wizard) is revealed once the
  // baked /pi-mode.json marker resolves — progressive reveal, exactly like the
  // Jetson-gated tabs.
  const { piMode, piModeResolved } = usePiMode();
  const currentRosHost = useSelector((state) => state.ros.rosHost);

  // Initialise the local rosbridge host ONCE, not on every render. The
  // previous body-level `dispatch(setRosHost(...))` ran every render and
  // setRosHost overwrites both rosHost AND rosbridgeUrl (rosSlice:31-34),
  // which clobbered the Jetson URL back to localhost the instant any
  // Redux state changed. Net result: Jetson connection couldn't survive
  // a single re-render. Now we only seed the local host when it isn't
  // yet set and the student isn't currently routing to a Jetson.
  //
  // Gated on piModeResolved: setRosHost derives the rosbridge URL
  // Pi-aware (same-origin /rosbridge proxy vs direct :9090, rosSlice),
  // so seeding before the /pi-mode.json marker resolves would connect a
  // Pi browser to the direct port once, then tear down and reconnect
  // when the marker flips — the LeaderToggle first-probe rule, applied
  // to the rosbridge seed. The marker resolves in ms (same-origin
  // static response).
  const defaultRosHost = window.location.hostname;
  useEffect(() => {
    if (cloudOnly) return;
    if (!piModeResolved) return;
    if (jetsonConnected) return;
    if (currentRosHost === defaultRosHost) return;
    dispatch(setRosHost(defaultRosHost));
  }, [cloudOnly, piModeResolved, jetsonConnected, currentRosHost, defaultRosHost, dispatch]);

  const page = useSelector((state) => state.ui.currentPage);
  const robotType = useSelector((state) => state.tasks.taskStatus.robotType);
  // Server-authored capability manifest (null until the first /task/status tick
  // carrying it). Drives the sidebar capability filter below. Never seeded from
  // the `?robot=` URL param — caps are server-authoritative (D4).
  const caps = useSelector((state) => state.tasks.taskStatus.capabilities);

  const isFirstLoad = useRef(true);

  const rosSubscriptionControls = useRosTopicSubscription();
  if (!cloudOnly) {
    rosConnectionManager.setOnConnected(rosSubscriptionControls.initializeSubscriptions);
  }

  // Fetch the HuggingFace Benutzer-ID list ONCE when the local ROS connection
  // comes up, and cache it in Redux so it survives tab switches. The token now
  // comes from $HF_TOKEN in the container env (set once in the GUI), so this
  // succeeds with no in-app token entry. Re-fires only if the list is still
  // empty on a later (re)connect.
  const { reload: reloadHfUsers } = useHfUserList();

  // App-global liveness watchdog: drive the heartbeat 'connected'->'timeout'->
  // 'disconnected' transitions on EVERY page, not only those that render the
  // <HeartbeatStatus> pill (Roboter Studio / Daten have none). Without it, a
  // node restart on those pages produced no 'disconnected'->'connected' edge, so
  // the rehydrate below never fired and the student had to re-select the robot.
  useHeartbeatWatchdog({ enabled: !cloudOnly });

  // The robot type is now GUI-hardset into the container env and boot-set on the
  // server (respawn self-heals it), then re-published on the idle identity tick
  // ≤2-3 s after connect — so the old client-side /set_robot_type rehydrate is
  // gone (useRobotTypeRehydrate deleted with the robot-type picker).

  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const hfUserListLen = useSelector((state) => state.ui.hfUserList.length);
  useEffect(() => {
    if (cloudOnly) return;
    if (heartbeatStatus === 'connected' && hfUserListLen === 0) {
      reloadHfUsers();
    }
  }, [cloudOnly, heartbeatStatus, hfUserListLen, reloadHfUsers]);

  useEffect(() => {
    return () => {
      if (!cloudOnly) {
        console.log('App unmounting, cleaning up global ROS connection');
        rosConnectionManager.disconnect();
      }
    };
  }, [cloudOnly]);

  useEffect(() => {
    const handleBeforeUnload = (e) => {
      if (taskStatus.running) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [taskStatus.running]);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      dispatch(setSession(session));
      dispatch(setIsLoading(false));
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      dispatch(setSession(session));
    });

    return () => subscription.unsubscribe();
  }, [dispatch]);

  // Robust /me load (retry/backoff + 401/403 sign-out + 404/5xx error state)
  // and the HF-identity auto-link live in useMeProfile. The only student-app-
  // specific piece is the wrong-role bounce below; everything else (incl. the
  // refetch the Training tab's "Erneut versuchen" button calls) is shared.
  const handleProfile = useCallback(
    (me) => {
      if (me.role !== 'student') {
        toast.error(
          'Dieses Konto ist für die Web-App. Bitte nutze die Lehrer-URL.',
          { duration: 6000 }
        );
        // v2.3.0: release the Jetson lock BEFORE signOut so the JWT is still
        // valid for the beacon-style release call. Without this, the lock
        // leaks for the full 5-min sweeper window every time a wrong-role
        // account hits the student app.
        resetJetsonOnLogout(dispatch, session?.access_token, jetsonIdRef.current);
        supabase.auth.signOut();
        dispatch(clearSession());
      }
    },
    [dispatch, session?.access_token]
  );
  useMeProfile({ onProfile: handleProfile, enableHfLink: true });

  useEffect(() => {
    if (isFirstLoad.current && page === PageType.HOME && taskStatus.topicReceived) {
      // Auto-rejoin a task that was in flight when the browser (re)loaded — but
      // respect the capability manifest: a stale in-flight task_type on a
      // type-switched rig must not jump into a page that type can't do.
      if (taskInfo?.taskType === PageType.RECORD && caps?.recordable !== false) {
        dispatch(moveToPage(PageType.RECORD));
      } else if (taskInfo?.taskType === PageType.INFERENCE && caps?.inferable !== false) {
        dispatch(moveToPage(PageType.INFERENCE));
      }
      isFirstLoad.current = false;
    } else if (isFirstLoad.current && page === PageType.HOME && trainingTopicReceived) {
      dispatch(moveToPage(PageType.TRAINING));
      isFirstLoad.current = false;
    }
  }, [page, taskInfo?.taskType, taskStatus.topicReceived, trainingTopicReceived, caps, dispatch]);

  const requireRobotOrRedirect = (targetPage) => {
    const decision = robotGateDecision({
      debug: process.env.REACT_APP_DEBUG === 'true',
      cloudOnly,
      robotType,
    });
    if (decision === 'navigate') {
      isFirstLoad.current = false;
      dispatch(moveToPage(targetPage));
      return;
    }
    // 'wait' — identity hasn't arrived yet. It is boot-set on the server and
    // re-published on the idle identity tick within ~2-3 s of connecting, so
    // this is a brief wait, not a dead-end. (The old „Robotertyp auf der
    // Startseite wählen" toast pointed at a picker that no longer exists.)
    toast.error('Verbindung zum Roboter wird hergestellt – bitte einen Moment warten.', {
      duration: 4000,
    });
  };

  const handleHomePageNavigation = () => {
    isFirstLoad.current = false;
    dispatch(moveToPage(PageType.HOME));
  };

  const handleRecordPageNavigation = () => requireRobotOrRedirect(PageType.RECORD);
  const handleInferencePageNavigation = () => requireRobotOrRedirect(PageType.INFERENCE);
  const handleEditDatasetPageNavigation = () => requireRobotOrRedirect(PageType.EDIT_DATASET);

  const handleTrainingPageNavigation = () => {
    isFirstLoad.current = false;
    dispatch(moveToPage(PageType.TRAINING));
  };

  const handleWorkshopPageNavigation = () => requireRobotOrRedirect(PageType.WORKSHOP);

  // The System window is the control panel — it must be reachable even with no
  // robot scanned/started (that's exactly where the student scans + starts), so
  // it deliberately does NOT go through requireRobotOrRedirect.
  const handleSystemPageNavigation = () => {
    isFirstLoad.current = false;
    dispatch(moveToPage(PageType.SYSTEM));
  };

  // Audit F27: the previous blunt-force teardown
  // (document.querySelectorAll('img[src*="/stream"]')) was a safety
  // net for ImageGridCell.js's effect race. F26 fixes that race
  // with effect-scoped cancel tokens, so the per-component cleanup
  // now reliably tears down each stream. Keeping the global sweep
  // here would tear down freshly-mounted stream components in
  // sibling subtrees (the bug §F27 calls out).

  // When the student is connected to a classroom Jetson, the rosbridge
  // URL points at the Jetson — so Aufnahme (needs leader-arm teleop) and
  // Roboter Studio (needs Workshop services not present on Jetson) would
  // both fail. We hide them with the same mechanism cloud-only mode uses:
  // mark them as `jetsonIncompatible` and filter out when the Jetson lock
  // is held.
  //
  // Inferenz is intentionally NOT `hardwareOnly` — v2.3.0 added the
  // classroom Jetson as a remote inference target, so a cloud-only
  // student (no local follower) still has a viable Inferenz path via
  // claim → Jetson runs the policy. The JetsonAvailabilityChip on the
  // page gracefully renders the "Kein Klassen-Jetson in diesem Raum"
  // state when no Jetson is paired, so it's safe to leave the tab
  // visible even in environments with no robot at all.
  // Each tab carries its `capabilityKey`; the third filter below hides a tab
  // only when the server capability manifest sets that key to an explicit
  // `false` (omx_follower hides Aufnahme/Daten/Training). Unknown/null caps
  // hide NOTHING, and the whole capability filter is SKIPPED in Jetson mode
  // (D9) — caps describe the LOCAL rig, so Training stays visible on a Jetson.
  const navItems = [
    { key: PageType.HOME, label: 'Start', Icon: MdHome, onClick: handleHomePageNavigation },
    { key: PageType.RECORD, label: 'Aufnahme', Icon: MdVideocam, onClick: handleRecordPageNavigation, hardwareOnly: true, jetsonIncompatible: true, capabilityKey: 'recordable' },
    { key: PageType.TRAINING, label: 'Training', Icon: GoGraph, onClick: handleTrainingPageNavigation, capabilityKey: 'trainable' },
    { key: PageType.INFERENCE, label: 'Inferenz', Icon: MdMemory, onClick: handleInferencePageNavigation, capabilityKey: 'inferable' },
    { key: PageType.EDIT_DATASET, label: 'Daten', Icon: MdWidgets, onClick: handleEditDatasetPageNavigation, sep: true, jetsonIncompatible: true, capabilityKey: 'editable' },
    { key: PageType.WORKSHOP, label: 'Roboter Studio', Icon: MdConstruction, onClick: handleWorkshopPageNavigation, hardwareOnly: true, jetsonIncompatible: true, capabilityKey: 'roboter_studio' },
    // Pi-only: the in-browser setup wizard (arms/cameras/token, Umgebung
    // starten, Update, Reset, Protokoll, Netzwerk-Check). Not hardwareOnly
    // (its own „Cloud-Modus" checkbox handles that) and not jetsonIncompatible
    // (it controls the Pi itself, independent of a Jetson claim). It carries no
    // capabilityKey, so isCapabilityVisible always keeps it — the piMode filter
    // is its sole gate.
    { key: PageType.SYSTEM, label: 'System', Icon: MdSettings, onClick: handleSystemPageNavigation, sep: true, piOnly: true },
  ]
    .filter((n) => !cloudOnly || !n.hardwareOnly)
    .filter((n) => !jetsonConnected || !n.jetsonIncompatible)
    .filter((n) => isCapabilityVisible(n, { jetsonConnected, caps }))
    .filter((n) => piMode || !n.piOnly);

  const isDarkPage = page === PageType.RECORD || page === PageType.INFERENCE;

  const blockRoleMismatch = profileLoaded && role && role !== 'student';

  return (
    <StartupGate>
      <div
        className={clsx(
          'flex h-screen w-screen overflow-hidden',
          'flex-col sm:flex-row',
          isDarkPage && 'dark-surface'
        )}
        style={isDarkPage ? { background: 'var(--dark-bg)' } : {}}
      >
        {/* Desktop / tablet rail */}
        <aside
          className={clsx(
            'hidden sm:flex shrink-0 flex-col items-center py-4 md:py-5 gap-1',
            'w-[64px] md:w-[88px]',
            isDarkPage
              ? 'border-r border-[color:var(--dark-line)]'
              : 'bg-white border-r border-[var(--line)]'
          )}
        >
          <div className="mb-3 md:mb-4">
            <LogoMark size={22} />
          </div>
          {navItems.map((n) => {
            const Icon = n.Icon;
            const active = page === n.key;
            return (
              <React.Fragment key={n.key}>
                {n.sep && (
                  <div
                    className={clsx(
                      'w-8 h-px my-2',
                      isDarkPage ? 'bg-[color:var(--dark-line)]' : 'bg-[var(--line)]'
                    )}
                  />
                )}
                <button
                  onClick={n.onClick}
                  title={n.label}
                  className={clsx(
                    'group w-12 md:w-[68px] py-2.5 md:py-3 rounded-[var(--radius)] flex flex-col items-center gap-1 md:gap-1.5 transition',
                    active
                      ? isDarkPage
                        ? 'bg-white/[0.08] text-white'
                        : 'bg-[var(--accent-wash)] text-[var(--accent-ink)]'
                      : isDarkPage
                      ? 'text-white/60 hover:bg-white/[0.05]'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]'
                  )}
                >
                  <span
                    className={clsx(
                      'w-9 h-9 md:w-10 md:h-10 flex items-center justify-center rounded-[10px]',
                      active && (isDarkPage ? 'bg-white/10' : 'bg-white/60')
                    )}
                  >
                    <Icon size={20} />
                  </span>
                  <span className="hidden md:block text-[11px] font-medium">{n.label}</span>
                </button>
              </React.Fragment>
            );
          })}
          <div className="flex-1" />
          <div
            className={clsx(
              'text-[10px] font-mono',
              isDarkPage ? 'text-white/40' : 'text-[var(--ink-4)]'
            )}
          >
            v{packageJson.version}
          </div>
        </aside>

        {/* Mobile top bar */}
        <header
          className={clsx(
            'sm:hidden shrink-0 h-12 px-3 flex items-center justify-between border-b',
            isDarkPage
              ? 'border-[color:var(--dark-line)] bg-black/30 backdrop-blur'
              : 'bg-white border-[var(--line)]'
          )}
        >
          <div className="flex items-center gap-2">
            <LogoMark size={20} />
            <span
              className={clsx(
                'text-sm font-semibold tracking-tight',
                isDarkPage ? 'text-white' : 'text-[var(--ink)]'
              )}
            >
              EduBotics
            </span>
          </div>
          <div
            className={clsx(
              'text-[10px] font-mono',
              isDarkPage ? 'text-white/40' : 'text-[var(--ink-4)]'
            )}
          >
            v{packageJson.version}
          </div>
        </header>

        <main className="flex-1 flex flex-col min-h-0 min-w-0 relative overflow-hidden">
          {blockRoleMismatch ? (
            <div className="flex flex-col items-center justify-center h-full p-10 text-center">
              <h2 className="text-xl font-bold text-[var(--ink)] mb-2">Falsches Konto</h2>
              <p className="text-[var(--ink-3)] max-w-md">
                Dieses Konto ist für die Web-App gedacht. Bitte melde dich mit einem
                Schüler-Konto auf diesem Gerät an.
              </p>
            </div>
          ) : page === PageType.HOME ? (
            <HomePage />
          ) : page === PageType.RECORD ? (
            <RecordPage isActive={page === PageType.RECORD} />
          ) : page === PageType.INFERENCE ? (
            <InferencePage isActive={page === PageType.INFERENCE} />
          ) : page === PageType.TRAINING ? (
            <TrainingPage isActive={page === PageType.TRAINING} />
          ) : page === PageType.EDIT_DATASET ? (
            <EditDatasetPage isActive={page === PageType.EDIT_DATASET} />
          ) : page === PageType.WORKSHOP ? (
            <WorkshopPage isActive={page === PageType.WORKSHOP} />
          ) : page === PageType.SYSTEM ? (
            <SystemPage isActive={page === PageType.SYSTEM} />
          ) : (
            <HomePage />
          )}
        </main>

        {/* Mobile bottom nav */}
        <nav
          className={clsx(
            'sm:hidden shrink-0 h-14 border-t flex items-stretch',
            isDarkPage
              ? 'bg-black/60 backdrop-blur border-[color:var(--dark-line)]'
              : 'bg-white border-[var(--line)]'
          )}
        >
          {navItems.map((n) => {
            const Icon = n.Icon;
            const active = page === n.key;
            return (
              <button
                key={n.key}
                onClick={n.onClick}
                className={clsx(
                  'flex-1 min-w-0 flex flex-col items-center justify-center gap-0.5 transition',
                  active
                    ? isDarkPage
                      ? 'text-white'
                      : 'text-[var(--accent-ink)]'
                    : isDarkPage
                    ? 'text-white/60'
                    : 'text-[var(--ink-3)]'
                )}
              >
                <Icon size={20} />
                <span className="text-[10px] font-medium truncate px-1">{n.label}</span>
              </button>
            );
          })}
        </nav>
      </div>
      {/* Orange Pi forced-update gate: a non-closable modal shown at startup when
          the Pi is behind the latest release (parity with the Windows GUI's
          _check_prerequisites). Self-gates on piMode — renders nothing off a Pi.
          Mounted before CollisionModal so the safety e-stop always stacks on top. */}
      <PiUpdateGate />
      {/* Teleop force/collision e-stop: blocking overlay shown whenever the server reports a
          collision-stop (phase=COLLISION). Mounted globally so it covers every page. */}
      <CollisionModal />
    </StartupGate>
  );
}

export default StudentApp;
