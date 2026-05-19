// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React, { useState, useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import {
  MdKeyboardDoubleArrowLeft,
  MdKeyboardDoubleArrowRight,
  MdLogout,
  MdMemory,
  MdLink,
  MdLinkOff,
  MdSchool,
  MdRefresh,
} from 'react-icons/md';
import ControlPanel from '../components/ControlPanel';
import HeartbeatStatus from '../components/HeartbeatStatus';
import ImageGrid from '../components/ImageGrid';
import InferencePanel from '../components/InferencePanel';
import LoginForm from '../components/LoginForm';
import { addTag } from '../features/tasks/taskSlice';
import { setIsFirstLoadFalse } from '../features/ui/uiSlice';
import { clearSession } from '../features/auth/authSlice';
import { supabase } from '../lib/supabaseClient';
import { useJetsonConnection, resetJetsonOnLogout } from '../hooks/useJetsonConnection';
import { useCameraLiveness } from '../hooks/useCameraLiveness';

const TOAST_LIMIT = 3;

export default function InferencePage({ isActive = true }) {
  const dispatch = useDispatch();

  // Hooks must always run in the same order, so every selector + every
  // hook below runs regardless of which screen we end up rendering. The
  // gating happens entirely in the JSX return below.
  const { toasts } = useToasterStore();
  const taskStatus = useSelector((state) => state.tasks.taskStatus);
  const taskInfo = useSelector((state) => state.tasks.taskInfo);
  const imageTopicList = useSelector((state) => state.ros.imageTopicList);
  const rosbridgeUrl = useSelector((state) => state.ros.rosbridgeUrl);
  const isFirstLoad = useSelector((state) => state.ui.isFirstLoad.inference);

  const isAuthenticated = useSelector((s) => s.auth.isAuthenticated);
  const isAuthLoading = useSelector((s) => s.auth.isLoading);
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);
  const session = useSelector((s) => s.auth.session);
  const username = useSelector((s) => s.auth.username);
  const fullName = useSelector((s) => s.auth.fullName);
  const classroomId = useSelector((s) => s.auth.classroomId);

  const jetsonStatus = useSelector((s) => s.jetson.status);
  const jetsonId = useSelector((s) => s.jetson.jetsonId);
  const jetsonOnline = useSelector((s) => s.jetson.online);
  const jetsonOwnerFullName = useSelector((s) => s.jetson.ownerFullName);
  const jetsonOwnerUsername = useSelector((s) => s.jetson.ownerUsername);
  const jetsonError = useSelector((s) => s.jetson.error);
  const heartbeatTransient = useSelector((s) => s.jetson.heartbeatTransient);

  // Always mount the Jetson lifecycle hook — discovery / claim / heartbeat /
  // release. Returns connect+disconnect callbacks for the gating buttons.
  const { connect, disconnect } = useJetsonConnection();

  // Camera liveness — only runs while connected to the Jetson (the only
  // configuration in which an inference UI makes sense per v2.3.0 product
  // direction). When not connected, returns zeros.
  const isConnected = jetsonStatus === 'connected';
  const { liveCount, allConfiguredAlive } = useCameraLiveness({
    topics: imageTopicList,
    rosbridgeUrl,
    enabled: isConnected && isActive,
  });
  const configuredCount = imageTopicList?.length || 0;

  // Right-panel collapse state — only relevant when the full UI shows but
  // cheap to keep at top to preserve hook order.
  const getInitialCollapsed = () =>
    typeof window !== 'undefined' && window.innerWidth < 900;
  const [isRightPanelCollapsed, setIsRightPanelCollapsed] = useState(getInitialCollapsed);
  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth < 900) setIsRightPanelCollapsed(true);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // Toast cap — applies to every gating screen.
  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  // Seed default tags once the robot type lands. Harmless when prerequisites
  // aren't met yet — the conditions inside guard it.
  useEffect(() => {
    if (
      isFirstLoad &&
      taskStatus.robotType !== '' &&
      taskInfo.tags.length === 0 &&
      isConnected
    ) {
      dispatch(addTag(taskStatus.robotType));
      dispatch(addTag('edubotics'));
    }
    dispatch(setIsFirstLoadFalse('inference'));
  }, [taskInfo.tags, taskStatus.robotType, dispatch, isFirstLoad, isConnected]);

  const handleLogout = async () => {
    // Mirror the TrainingPage / StudentApp sign-out path: release the
    // Jetson lock with the still-valid JWT BEFORE invalidating the
    // session, otherwise the beacon-style release fails server-side and
    // the lock leaks for the full 5-min sweeper window.
    resetJetsonOnLogout(dispatch, session?.access_token, jetsonId);
    await supabase.auth.signOut();
    dispatch(clearSession());
    toast.success('Abgemeldet');
  };

  // ────────────────────────────────────────────────────────────────────
  // GATING — decide which screen to render
  // ────────────────────────────────────────────────────────────────────

  // 1) Auth bootstrap — Supabase session lookup still pending.
  if (isAuthLoading) {
    return <CenteredSpinner label="Laden…" />;
  }

  // 2) Not signed in — show the LoginForm (mirrors the Training tab).
  if (!isAuthenticated) {
    return <LoginForm subtitle="Anmelden für Inferenz mit dem Klassen-Jetson" />;
  }

  // 3) Session is live but /me hasn't responded yet — show a slim spinner.
  if (!profileLoaded) {
    return <CenteredSpinner label="Profil wird geladen…" />;
  }

  // 4) No classroom — message + logout. Per product spec the Inferenz UI
  //    only exists for classroom-assigned students.
  if (!classroomId) {
    return (
      <StateScreen
        icon={<MdSchool size={44} className="text-amber-500" />}
        title="Du bist keiner Klasse zugeordnet"
        body={
          <>
            Inferenz läuft auf einem geteilten Klassen-Jetson. Solange du
            keiner Klasse zugewiesen bist, kannst du den Inferenz-Bereich
            nicht nutzen.
            <br />
            <span className="text-[var(--ink-3)] mt-3 block">
              Bitte deinen Lehrer, dich in eine Klasse aufzunehmen — danach
              erscheint hier dein Klassen-Jetson.
            </span>
          </>
        }
        identity={{ username, fullName }}
        onLogout={handleLogout}
      />
    );
  }

  // 5) Classroom discovery still pending (status === 'unknown' means the
  //    GET /classrooms/{id}/jetson promise hasn't resolved yet).
  if (jetsonStatus === 'unknown') {
    return <CenteredSpinner label="Klassen-Jetson wird gesucht…" />;
  }

  // 6) No Jetson paired with this classroom.
  if (jetsonStatus === 'no_jetson') {
    return (
      <StateScreen
        icon={<MdMemory size={44} className="text-gray-400" />}
        title="Kein Klassen-Jetson eingerichtet"
        body={
          <>
            Für deine Klasse wurde noch kein Jetson gekoppelt. Inferenz
            ist erst möglich, wenn dein Lehrer den Klassen-Jetson
            eingerichtet hat.
            <br />
            <span className="text-[var(--ink-3)] mt-3 block">
              Sobald der Jetson gekoppelt ist, erscheint hier automatisch
              die Verbindungs-Schaltfläche — du musst die Seite nicht neu
              laden.
            </span>
          </>
        }
        identity={{ username, fullName }}
        onLogout={handleLogout}
      />
    );
  }

  // 7) Error state.
  if (jetsonStatus === 'error') {
    return (
      <StateScreen
        icon={<MdLinkOff size={44} className="text-red-500" />}
        title="Verbindung zum Klassen-Jetson fehlgeschlagen"
        body={
          <>
            {jetsonError || 'Unbekannter Fehler.'}
            <br />
            <span className="text-[var(--ink-3)] mt-3 block">
              Bitte versuche es gleich noch einmal. Wenn der Fehler bleibt,
              wende dich an deinen Lehrer.
            </span>
          </>
        }
        identity={{ username, fullName }}
        onLogout={handleLogout}
        primaryAction={{ label: 'Erneut versuchen', onClick: () => window.location.reload(), icon: <MdRefresh /> }}
      />
    );
  }

  // 8) Busy — someone else holds the lock.
  if (jetsonStatus === 'busy') {
    const who = jetsonOwnerFullName || jetsonOwnerUsername || 'einem anderen Schüler';
    return (
      <StateScreen
        icon={<MdLink size={44} className="text-amber-500" />}
        title="Klassen-Jetson belegt"
        body={
          <>
            Der Klassen-Jetson wird gerade von <strong>{who}</strong> benutzt.
            <br />
            <span className="text-[var(--ink-3)] mt-3 block">
              Sobald die andere Person die Verbindung trennt, kannst du den
              Jetson übernehmen. Diese Seite aktualisiert sich automatisch.
            </span>
          </>
        }
        identity={{ username, fullName }}
        onLogout={handleLogout}
      />
    );
  }

  // 9) Available — show the connect CTA.
  if (jetsonStatus === 'available') {
    return (
      <StateScreen
        icon={<MdMemory size={44} className="text-emerald-500" />}
        title={jetsonOnline ? 'Klassen-Jetson bereit' : 'Klassen-Jetson offline'}
        body={
          jetsonOnline ? (
            <>
              Der Klassen-Jetson ist frei und wartet auf eine Verbindung.
              Wenn du verbindest, wird der Jetson für dich reserviert, bis
              du die Verbindung wieder trennst.
              <br />
              <span className="text-[var(--ink-3)] mt-3 block">
                Das Verbinden kann etwa 30–60 Sekunden dauern — der Jetson
                startet seinen ROS-Stack neu, damit alles sauber für dich
                vorbereitet ist.
              </span>
            </>
          ) : (
            <>
              Der Klassen-Jetson wurde länger nicht erreicht und gilt als
              offline. Verbindung ist erst möglich, sobald der Jetson wieder
              ein Lebenszeichen sendet.
              <br />
              <span className="text-[var(--ink-3)] mt-3 block">
                Bitte deinen Lehrer zu prüfen, ob der Jetson eingeschaltet
                ist und Netzwerk hat.
              </span>
            </>
          )
        }
        identity={{ username, fullName }}
        onLogout={handleLogout}
        primaryAction={
          jetsonOnline
            ? {
                label: 'Verbinde mit Klassen-Jetson',
                onClick: connect,
                icon: <MdLink />,
                tone: 'accent',
              }
            : null
        }
      />
    );
  }

  // 10) Claiming — spinner.
  if (jetsonStatus === 'claiming') {
    return (
      <CenteredSpinner
        label="Verbinde mit Klassen-Jetson…"
        sublabel="Der Jetson startet seinen ROS-Stack. Bitte einen Moment Geduld."
      />
    );
  }

  // 11) Disconnecting — spinner.
  if (jetsonStatus === 'disconnecting') {
    return (
      <CenteredSpinner
        label="Verbindung wird beendet…"
        sublabel="Der Jetson wird für die nächste Schülerin/den nächsten Schüler vorbereitet."
      />
    );
  }

  // ────────────────────────────────────────────────────────────────────
  // 12) CONNECTED — the actual Inferenz workspace.
  // ────────────────────────────────────────────────────────────────────

  const cameraChipLabel = (() => {
    if (configuredCount === 0) {
      return 'Keine Kamera-Topics auf dem Jetson';
    }
    if (liveCount === 0) {
      return `0 / ${configuredCount} Kameras liefern Bilder`;
    }
    if (!allConfiguredAlive) {
      return `${liveCount} / ${configuredCount} Kameras aktiv`;
    }
    return liveCount === 1 ? '1 Kamera aktiv' : `${liveCount} Kameras aktiv`;
  })();

  const cameraChipDotColor = (() => {
    if (configuredCount === 0 || liveCount === 0) return '#ef4444'; // red — no real cameras
    if (!allConfiguredAlive) return '#f59e0b';                       // amber — partial
    return 'var(--accent)';                                          // green/teal — all good
  })();

  return (
    <div
      className="relative h-full w-full flex flex-col overflow-hidden"
      style={{ background: 'var(--dark-bg)', color: 'var(--dark-ink)' }}
    >
      <div className="absolute top-3 left-3 right-3 z-30 flex items-center gap-2 flex-wrap">
        <div className="h-8 px-3 rounded-full bg-white/[0.08] border border-white/15 backdrop-blur-md flex items-center gap-2 text-[11px] text-white/80">
          <span className="font-mono uppercase tracking-wider opacity-70">Roboter</span>
          <span className="font-mono px-1.5 py-0.5 rounded bg-white/10 max-w-[160px] truncate">
            {taskStatus?.robotType || '—'}
          </span>
        </div>
        <HeartbeatStatus dark />
        <div
          className="h-8 px-3 rounded-full bg-white/[0.08] border border-white/15 backdrop-blur-md flex items-center gap-2 text-[11px] text-white/80 font-mono whitespace-nowrap"
          title={
            configuredCount === 0
              ? 'Kein /image/get_available_list konfiguriert'
              : `${liveCount}/${configuredCount} Topics liefern aktuell Bilder`
          }
        >
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: cameraChipDotColor }}
          />
          {cameraChipLabel}
        </div>
        <div className="h-8 px-3 rounded-full bg-emerald-500/15 border border-emerald-300/30 backdrop-blur-md flex items-center gap-2 text-[11px] text-emerald-100">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          <span>{heartbeatTransient ? 'Jetson — wird wiederhergestellt' : 'Verbunden mit Klassen-Jetson'}</span>
          <button
            type="button"
            onClick={disconnect}
            className="ml-1 px-2 py-0.5 rounded bg-red-600/80 hover:bg-red-600 text-white text-[10px]"
          >
            Trennen
          </button>
        </div>
        <div className="flex-1" />
        {isRightPanelCollapsed && (
          <button
            onClick={() => setIsRightPanelCollapsed(false)}
            className="w-10 h-10 bg-white/[0.08] border border-white/15 rounded-full flex items-center justify-center text-white/80 backdrop-blur-md hover:bg-white/15"
            title="Panel öffnen"
          >
            <MdKeyboardDoubleArrowLeft size={22} />
          </button>
        )}
      </div>

      <div className="flex-1 flex items-start min-h-0 pt-[56px] pb-2 px-2 sm:px-3 lg:px-4 gap-2 sm:gap-3 lg:gap-4">
        <div className="flex-1 self-stretch min-w-0 relative rounded-[var(--radius-lg)] overflow-hidden">
          <ImageGrid isActive={isActive} />
        </div>
        <div
          className={clsx(
            'relative transition-all duration-300 ease-in-out',
            isRightPanelCollapsed
              ? 'w-0 opacity-0 pointer-events-none overflow-hidden'
              : 'w-[min(420px,38vw)] min-w-[300px] max-w-[480px] md:min-w-[320px] lg:min-w-[360px] 2xl:max-w-[520px] opacity-100'
          )}
          style={{ maxHeight: 'calc(100vh - 220px)' }}
        >
          <button
            onClick={() => setIsRightPanelCollapsed(!isRightPanelCollapsed)}
            className="absolute -left-4 top-2 w-9 h-9 bg-white/95 border border-[var(--line)] rounded-full flex items-center justify-center shadow-pop text-[var(--ink-2)] hover:text-[var(--ink)] z-30 backdrop-blur"
            title="Einklappen"
          >
            <MdKeyboardDoubleArrowRight size={20} />
          </button>
          <InferencePanel />
        </div>
      </div>

      <div className="shrink-0">
        <ControlPanel />
      </div>
    </div>
  );
}

// ─── Sub-components for the gating screens ────────────────────────────

function CenteredSpinner({ label, sublabel }) {
  return (
    <div className="flex flex-col items-center justify-center h-full w-full gap-4 px-6 text-center" style={{ background: 'var(--bg)' }}>
      <svg
        className="animate-spin h-8 w-8 text-[color:var(--accent)]"
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
      >
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
      <div className="text-[var(--ink)] text-base font-medium">{label}</div>
      {sublabel && <div className="text-[var(--ink-3)] text-sm max-w-md">{sublabel}</div>}
    </div>
  );
}

function StateScreen({ icon, title, body, identity, onLogout, primaryAction }) {
  return (
    <div className="h-full w-full overflow-y-auto" style={{ background: 'var(--bg)' }}>
      <div className="min-h-full w-full flex items-center justify-center px-4 py-8">
        <div className="w-full max-w-[560px] flex flex-col items-center gap-5 text-center">
          {icon && <div>{icon}</div>}
          <h1 className="text-2xl font-bold text-[var(--ink)] tracking-tight">{title}</h1>
          <div className="text-[var(--ink-2)] text-[15px] leading-relaxed">{body}</div>
          {primaryAction && (
            <button
              type="button"
              onClick={primaryAction.onClick}
              className={clsx(
                'mt-2 inline-flex items-center gap-2 px-5 py-3 rounded-[var(--radius-sm)] font-semibold text-sm transition shadow-pop',
                primaryAction.tone === 'accent'
                  ? 'bg-[color:var(--accent)] text-white hover:brightness-110'
                  : 'bg-white border border-[var(--line)] text-[var(--ink)] hover:bg-[var(--bg-sunk)]'
              )}
            >
              {primaryAction.icon}
              <span>{primaryAction.label}</span>
            </button>
          )}
          {identity && (
            <div className="mt-6 pt-5 border-t border-[var(--line)] w-full flex flex-col sm:flex-row items-center justify-between gap-3 text-sm">
              <div className="text-[var(--ink-3)]">
                Angemeldet als{' '}
                <span className="text-[var(--ink)] font-medium">
                  {identity.fullName || identity.username || '—'}
                </span>
              </div>
              <button
                type="button"
                onClick={onLogout}
                className="inline-flex items-center gap-1.5 text-[var(--ink-3)] hover:text-[var(--ink)] px-3 py-1.5 rounded-[var(--radius-sm)] hover:bg-[var(--bg-sunk)] transition"
              >
                <MdLogout size={16} />
                <span>Abmelden</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
