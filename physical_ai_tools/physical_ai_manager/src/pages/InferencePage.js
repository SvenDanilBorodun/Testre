// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React, { useCallback, useState, useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import ControlPanel from '../components/ControlPanel';
import TaskPhase from '../constants/taskPhases';
import HeartbeatStatus from '../components/HeartbeatStatus';
import ImageGrid from '../components/ImageGrid';
import InferencePanel from '../components/InferencePanel';
import LoginForm from '../components/LoginForm';
import { addTag } from '../features/tasks/taskSlice';
import { setIsFirstLoadFalse } from '../features/ui/uiSlice';
import { useJetsonConnection } from '../hooks/useJetsonConnection';
import { useCameraLiveness } from '../hooks/useCameraLiveness';
import Icon from '../components/icons/Icon';
import TargetSwitch from '../components/CloudInference/TargetSwitch';
import { Key, Led, Plate, Screen } from '../components/sucher';
import '../components/sucher-pages/inference.css';
import CloudInferenceView from '../components/CloudInference/CloudInferenceView';
import { cloudCopy } from '../components/CloudInference/cloudCopy';
import useCloudInferenceSession from '../hooks/useCloudInferenceSession';
import { cloudTargetState, effectiveTarget, jetsonOwnsRosbridge } from '../features/cloudInference/gating';
import { isLiveRun, release as releaseCloudReservation } from '../features/cloudInference/controller';
import { isCloudApiConfigured } from '../services/cloudConfig';
import { isCloudOnlyMode } from '../utils/cloudMode';

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
  const username = useSelector((s) => s.auth.username);
  const fullName = useSelector((s) => s.auth.fullName);
  const classroomId = useSelector((s) => s.auth.classroomId);

  const jetsonStatus = useSelector((s) => s.jetson.status);
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

  // Seed default tags once the robot type lands. Wait for the identity before
  // consuming first-load — consuming it early (before the robotType tick or the
  // Jetson connection) would permanently skip the robotType + `edubotics` tag
  // seed for the session (the picker's setIsFirstLoadTrue reset path is gone).
  useEffect(() => {
    if (taskStatus.robotType === '') return;
    if (isFirstLoad && taskInfo.tags.length === 0 && isConnected) {
      dispatch(addTag(taskStatus.robotType));
      dispatch(addTag('edubotics'));
    }
    dispatch(setIsFirstLoadFalse('inference'));
  }, [taskInfo.tags, taskStatus.robotType, dispatch, isFirstLoad, isConnected]);

  // Cloud-Inferenz (spec §6.2): WHERE the model runs is derived on every
  // render, never stored (R-6, D3). useJetsonConnection above stays mounted for
  // BOTH targets, so a discovery that resolves resume/reclaim after mount moves
  // the page to the Jetson flow at once. `targetChoice` is set only by the
  // TargetSwitch; without one, „In der Cloud" is the default whenever the robot
  // advertises it and no Jetson owns rosbridge (owner A-Q5).
  const cloudSession = useCloudInferenceSession();
  const [targetChoice, setTargetChoice] = useState(null);
  const cloud = cloudTargetState({
    cloudOnly: isCloudOnlyMode(),
    jetsonStatus,
    capabilities: taskStatus.capabilities,
    cloudApiConfigured: isCloudApiConfigured,
  });
  const liveCloudRun = isLiveRun(cloudSession);
  const target = effectiveTarget({
    jetsonStatus,
    classroomId,
    liveCloudSession: liveCloudRun,
    cloud,
    choice: targetChoice,
  });
  const chooseTarget = useCallback((next) => {
    // Leaving the cloud lets a reservation go (its GPU slot is freed now).
    if (next !== 'cloud') releaseCloudReservation();
    setTargetChoice(next);
  }, []);

  // NO page-level "Abmelden" here any more. StudentApp's own chrome — the
  // desktop rail and the mobile header, both rendered on EVERY page including
  // behind these full-page StateScreens — carries one now. StateScreen keeps
  // its "Angemeldet als <Name>" line: that names the student by full name,
  // which the rail does not, and it is a label rather than a second control.

  // ────────────────────────────────────────────────────────────────────
  // GATING — decide which screen to render
  // ────────────────────────────────────────────────────────────────────

  // 1) Auth bootstrap — Supabase session lookup still pending.
  if (isAuthLoading) {
    return <CenteredSpinner label="Laden…" />;
  }

  // 2) Not signed in — show the LoginForm (mirrors the Training tab).
  //    Since the student login gate (utils/authGate) StudentApp's <main> asks
  //    for a login first, so this is no longer the PRIMARY gate — it is the
  //    second line of defence for the one path that gets past it, „Ohne
  //    Anmeldung fortfahren". KEEP IT: that path is exactly what it now guards,
  //    and the teacher web (WebApp) has no StudentApp gate at all.
  if (!isAuthenticated) {
    return <LoginForm subtitle="Anmelden für Inferenz mit dem Klassen-Jetson" />;
  }

  // 3) Session is live but /me hasn't responded yet — show a slim spinner.
  if (!profileLoaded) {
    return <CenteredSpinner label="Profil wird geladen…" />;
  }

  // 4) The classroom Jetson's discovery may still resolve resume/reclaim
  //    (rosbridge moves to the Jetson): wait for it before showing anything.
  if (target === 'pending') {
    return <CenteredSpinner label="Klassen-Jetson wird gesucht…" />;
  }

  // The Jetson flow: the page body from „No classroom" on, unchanged (a plain
  // render function — no hooks inside).
  function renderJetsonFlow() {
    // 4) No classroom — message + logout. Per product spec the Inferenz UI
    //    only exists for classroom-assigned students.
    if (!classroomId) {
      return (
        <StateScreen
          icon={<Icon name="graduationCap" size={44} className="text-amber-500" />}
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
          icon={<Icon name="cpu" size={44} className="text-gray-400" />}
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
        />
      );
    }

    // 7) Error state.
    if (jetsonStatus === 'error') {
      return (
        <StateScreen
          icon={<Icon name="unlink" size={44} className="text-red-500" />}
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
          primaryAction={{ label: 'Erneut versuchen', onClick: () => window.location.reload(), icon: <Icon name="refresh" /> }}
        />
      );
    }

    // 8) Busy — someone else holds the lock.
    if (jetsonStatus === 'busy') {
      const who = jetsonOwnerFullName || jetsonOwnerUsername || 'einem anderen Schüler';
      return (
        <StateScreen
          icon={<Icon name="link" size={44} className="text-amber-500" />}
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
        />
      );
    }

    // 9) Available — show the connect CTA.
    if (jetsonStatus === 'available') {
      return (
        <StateScreen
          icon={<Icon name="cpu" size={44} className="text-emerald-500" />}
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
          primaryAction={
            jetsonOnline
              ? {
                  label: 'Verbinde mit Klassen-Jetson',
                  onClick: connect,
                  icon: <Icon name="link" />,
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


    const cameraChipLed = (() => {
      if (configuredCount === 0 || liveCount === 0) return 'rec'; // no real cameras
      if (!allConfiguredAlive) return 'warn';                       // partial
      return 'ok';                                                  // all good
    })();

    return (
      <div className="su-inf relative h-full w-full flex flex-col overflow-hidden">
        <div className="su-inf__body">
          <div className="su-inf__left">
            <Screen power className="su-inf__screen">
              <div className="su-inf__chips">
                <div className="su-inf-chip">
                  <span className="su-inf-chip__key">Roboter</span>
                  <span className="max-w-[160px] truncate">
                    {taskStatus?.robotType || '—'}
                  </span>
                </div>
                <HeartbeatStatus dark size="sm" />
                <div
                  className="su-inf-chip whitespace-nowrap"
                  title={
                    configuredCount === 0
                      ? 'Kein /image/get_available_list konfiguriert'
                      : `${liveCount}/${configuredCount} Topics liefern aktuell Bilder`
                  }
                >
                  <Led tone={cameraChipLed} size={7} />
                  {cameraChipLabel}
                </div>
                <div className="su-inf-chip su-inf-chip--text">
                  <Led tone="ok" pulse="breathe" size={7} />
                  <span>{heartbeatTransient ? 'Jetson — wird wiederhergestellt' : 'Verbunden mit Klassen-Jetson'}</span>
                  <button
                    type="button"
                    onClick={disconnect}
                    className="su-inf-chip__btn"
                  >
                    Trennen
                  </button>
                </div>
                <div className="flex-1" />
                {isRightPanelCollapsed && (
                  <button
                    type="button"
                    onClick={() => setIsRightPanelCollapsed(false)}
                    className="su-inf-chip su-inf-chip--round"
                    title="Panel öffnen"
                    aria-label="Panel öffnen"
                  >
                    <Icon name="chevronsLeft" size={20} />
                  </button>
                )}
              </div>
              <div className="su-inf__grid">
                <ImageGrid isActive={isActive} />
                {taskStatus.phase === TaskPhase.INFERENCE_LOADING && (
                  <div
                    className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-black/55 backdrop-blur-sm"
                    role="status"
                    aria-live="polite"
                  >
                    <Icon name="loading" size={40} className="animate-spin text-white" />
                    <span className="su-serif text-[30px] leading-none text-white">
                      Modell wird geladen &amp; Roboter verbindet …
                    </span>
                    <span className="text-[13px] text-[var(--su-scr-ink-2)]">
                      Das kann beim ersten Start bis zu 30 Sekunden dauern.
                    </span>
                  </div>
                )}
              </div>
            </Screen>
            <div className="shrink-0">
              <ControlPanel />
            </div>
          </div>
          <div
            className={clsx(
              'su-inf__side relative transition-all duration-300 ease-in-out',
              isRightPanelCollapsed
                ? 'w-0 opacity-0 pointer-events-none overflow-hidden'
                : 'w-[min(420px,38vw)] min-w-[300px] max-w-[480px] md:min-w-[320px] lg:min-w-[360px] 2xl:max-w-[520px] opacity-100'
            )}
          >
            <button
              type="button"
              onClick={() => setIsRightPanelCollapsed(!isRightPanelCollapsed)}
              className="su-inf__collapse"
              title="Einklappen"
              aria-label="Einklappen"
            >
              <Icon name="chevronsRight" size={18} />
            </button>
            <Plate as="aside" className="su-inf__panel su-inf__panel--jetson">
              <InferencePanel />
            </Plate>
          </div>
        </div>
      </div>
    );
  }

  const body = target === 'cloud'
    ? <CloudInferenceView isActive={isActive} cloud={cloud} />
    : renderJetsonFlow();
  if (!cloud.offered || jetsonOwnsRosbridge(jetsonStatus)) {
    return body;
  }
  return (
    <div className="h-full w-full flex flex-col min-h-0">
      <TargetSwitch
        cloud={cloud}
        target={target}
        onChoose={chooseTarget}
        lockedReason={liveCloudRun ? cloudCopy.status.running : null}
      />
      <div className="flex-1 min-h-0">{body}</div>
    </div>
  );
}

// ─── Sub-components for the gating screens ────────────────────────────

function CenteredSpinner({ label, sublabel }) {
  return (
    <div className="flex flex-col items-center justify-center h-full w-full gap-4 px-6 text-center" style={{ background: 'var(--bg)' }}>
      <Icon name="loading" size={32} className="animate-spin text-[color:var(--accent)]" />
      <div className="su-serif text-[30px] leading-none text-[var(--ink)]">{label}</div>
      {sublabel && <div className="text-[var(--ink-2)] text-sm max-w-md">{sublabel}</div>}
    </div>
  );
}

function StateScreen({ icon, title, body, identity, primaryAction }) {
  return (
    <div className="h-full w-full overflow-y-auto" style={{ background: 'var(--bg)' }}>
      <div className="min-h-full w-full flex items-center justify-center px-4 py-8">
        <Plate className="su-state w-full max-w-[560px] flex flex-col items-center gap-5 text-center">
          {icon && <div className="su-state__disc">{icon}</div>}
          <h1 className="su-serif text-[34px] leading-none text-[var(--ink)]">{title}</h1>
          <div className="text-[var(--ink-2)] text-[15px] leading-relaxed">{body}</div>
          {primaryAction && (
            <Key
              variant={primaryAction.tone === 'accent' ? 'signal' : 'default'}
              size="lg"
              onClick={primaryAction.onClick}
              className="mt-2"
            >
              {primaryAction.icon}
              <span>{primaryAction.label}</span>
            </Key>
          )}
          {identity && (
            <div className="mt-6 pt-5 border-t border-[var(--su-hair)] w-full text-center text-sm">
              <div className="text-[var(--ink-2)]">
                Angemeldet als{' '}
                <span className="text-[var(--ink)] font-medium">
                  {identity.fullName || identity.username || '—'}
                </span>
              </div>
            </div>
          )}
        </Plate>
      </div>
    </div>
  );
}
