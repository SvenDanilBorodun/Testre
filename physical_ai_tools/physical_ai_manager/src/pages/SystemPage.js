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
//
// The Orange Pi „System"-Fenster — the in-browser re-implementation of the
// Windows EduBotics.exe setup wizard. Every action drives the pi-agent through
// the manager's SAME-ORIGIN /api/system reverse proxy (nginx strips the prefix,
// so /api/system/scan-arms reaches the agent's /scan-arms). All student-facing
// strings are German with literal umlauts (Rule §1); code/comments are English.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { Card, Btn, Pill, SectionHeader } from '../components/EbUI';
import { Led, RobotIcon, Screen } from '../components/sucher';
import Icon from '../components/icons/Icon';
import '../components/sucher-pages/system.css';
import { usePiMode } from '../utils/piMode';
import { useAgentUpdate } from '../hooks/useAgentUpdate';
import { toastIcon } from '../components/icons/toast';

// ── same-origin agent fetch helper ───────────────────────────────────────────

async function sysFetch(path, { method = 'GET', body } = {}) {
  const opts = { method, cache: 'no-store' };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api/system${path}`, opts);
  let data = {};
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  return { ok: res.ok, status: res.status, data };
}

function fmtAge(images) {
  if (!images || images.age_days == null) return 'Noch nie aktualisiert';
  const d = images.age_days;
  if (d < 1) return 'Heute aktualisiert';
  const days = Math.round(d);
  return `Vor ${days} Tag${days === 1 ? '' : 'en'} aktualisiert`;
}

// Every camera role the wizard knows how to render, in DISPLAY order. The
// agent's per-profile allowlist decides which of these are offered; it never
// decides their order, because `pi_agent/constants.py::ROBOT_PROFILES` orders
// `camera_roles` for the WINDOWS GUI's lone-camera default (`camera_roles[0]`)
// — `omx_follower` carries ('scene', 'gripper') — and rendering in registry
// order would flip the dropdown between two profiles that offer the same two
// roles. `value` is the wire role; `label` is the student's word for it and
// must stay in step with `pi_agent/constants.py::CAMERA_ROLE_LABELS_DE`, which
// is what the agent's refusal message says back.
const CAMERA_ROLE_OPTIONS = [
  { value: 'gripper', label: 'Greifer' },
  { value: 'scene', label: 'Szene' },
];

// A labelled step card so the wizard reads top-to-bottom like the .exe.
// Sucher: a serif title; a lettered step gets its letter in a dark disc
// (decorative — the title keeps the whole „Schritt A — …" text).
function Step({ n, title, children, right }) {
  return (
    <Card
      title={n ? (
        <span className="su-sys-step">
          <span className="su-sys-step__disc" aria-hidden="true">{n}</span>
          <span>{`Schritt ${n} — ${title}`}</span>
        </span>
      ) : title}
      titleStyle="serif"
      right={right}
      className="su-sys-card"
    >
      {children}
    </Card>
  );
}

// A status chip with a light: green wash when true, the well and an off light otherwise.
function StatusChip({ tone, led, children }) {
  return (
    <Pill tone={tone}>
      <Led tone={led} size={7} />
      {children}
    </Pill>
  );
}

export default function SystemPage() {
  const { agentStatus, agentReachable, refreshAgentStatus } = usePiMode();

  // ── local UI state ─────────────────────────────────────────────────────────
  const [cloudOnly, setCloudOnly] = useState(false);

  // Robot type (ArmProfile). The AGENT's /status is the authority — this pair
  // only holds the optimistic value across the POST so the <select> does not
  // snap back to the old id while the round-trip is in flight, and (crucially)
  // so a REFUSED change (409 „nur wenn die Umgebung gestoppt ist") reverts to
  // what the rig actually is instead of leaving the wizard showing a type the
  // Pi never adopted.
  const [savingRobotType, setSavingRobotType] = useState(false);
  const [pendingRobotType, setPendingRobotType] = useState(null);

  const [scanning, setScanning] = useState(false);
  const [scanMsg, setScanMsg] = useState('');
  const [scanFailed, setScanFailed] = useState(false);

  const [camScanning, setCamScanning] = useState(false);
  const [scanCameras, setScanCameras] = useState([]);
  const [roles, setRoles] = useState({}); // path -> 'gripper'|'scene'|''
  const [savingRoles, setSavingRoles] = useState(false);
  const [previewDevice, setPreviewDevice] = useState(null);

  const [startingEnv, setStartingEnv] = useState(false);
  const [stoppingEnv, setStoppingEnv] = useState(false);

  // Update (ACK-early job + 404-tolerant poll) is driven by a shared hook, so
  // this System card and the forced PiUpdateGate use one identical driver.
  const onUpdateSettled = useCallback(
    (job) => {
      if (job.status === 'succeeded') toast.success(job.message || 'Aktualisierung abgeschlossen.');
      else toast.error(job.message || 'Aktualisierung fehlgeschlagen.');
      refreshAgentStatus();
    },
    [refreshAgentStatus]
  );
  const { updating, updateJob, startUpdate } = useAgentUpdate({ onSettled: onUpdateSettled });

  const [resetStep, setResetStep] = useState(0); // 0 idle · 1 confirm · 2 final
  const [resetting, setResetting] = useState(false);

  const [protokollOpen, setProtokollOpen] = useState(false);
  const [logLines, setLogLines] = useState([]);

  const [netCheck, setNetCheck] = useState(null);
  const [netChecking, setNetChecking] = useState(false);

  const previewRef = useRef(null);

  // ── robot profile (declared HERE, above the callbacks that close over it) ───
  //
  // Every profile-shaped question the wizard asks is answered from the AGENT's
  // /status: the selectable list, which id is live, and whether that id needs a
  // leader arm. Nothing is derived from an id LITERAL — a new profile in
  // `pi_agent/constants.py::ROBOT_PROFILES` reaches this page with no edit here.
  //
  // GRACEFUL DEGRADE is the load-bearing property: an agent from before WP-3
  // sends none of these keys, and that Pi must keep behaving EXACTLY as it did.
  // Hence an absent/malformed `robot_profiles` yields [] (selector hidden, not
  // crashed), an absent `hardware_ready` falls back to `arms.both` (the old
  // gate), and `scan_requires_leader` is read as `!== false` so only an
  // EXPLICIT false hides the Leader tile — the same doctrine the nav's
  // capability gating uses.
  //
  // Rows are filtered rather than trusted: `{row.display_de}` on a non-string
  // throws „Objects are not valid as a React child", which white-screens the
  // Pi's ONLY repair surface.
  const robotProfiles = Array.isArray(agentStatus?.robot_profiles)
    ? agentStatus.robot_profiles.filter(
        (p) =>
          p
          && typeof p.id === 'string'
          && p.id !== ''
          && typeof p.display_de === 'string'
          && p.display_de !== ''
      )
    : [];
  const selectedRobotType =
    pendingRobotType
    ?? (typeof agentStatus?.robot_type === 'string' ? agentStatus.robot_type : '');
  const selectedProfile = robotProfiles.find((p) => p.id === selectedRobotType) || null;
  const scanRequiresLeader = selectedProfile
    ? selectedProfile.scan_requires_leader !== false
    : true;

  // Which camera roles this rig may use. `edu6_studio` allows „Szene" ONLY —
  // its server config declares a single `scene:` topic, so a camera named
  // `gripper` publishes a topic nothing subscribes to while the opi compose
  // healthcheck (which greps the STUDENT-NAMED topic) still goes green. The
  // agent refuses such a role with a German 400; this filter is the other half
  // of that pair, so the wizard stops OFFERING it in the first place.
  //
  // GRACEFUL DEGRADE, same doctrine as `robotProfiles` above: an agent from
  // before this key — or a malformed/unknown/empty list — yields BOTH roles,
  // i.e. exactly the pre-change dropdown. Never an empty <select>: the roles
  // step would become unusable, and on a Pi this page is the only repair
  // surface there is.
  const allowedCameraRoles = useMemo(() => {
    const known = CAMERA_ROLE_OPTIONS.map((o) => o.value);
    const declared = Array.isArray(selectedProfile?.camera_roles)
      ? selectedProfile.camera_roles.filter((r) => known.includes(r))
      : [];
    return declared.length > 0 ? declared : known;
  }, [selectedProfile]);

  useEffect(() => {
    previewRef.current = previewDevice;
  }, [previewDevice]);

  // ── live status: refresh on mount + every 5 s while the page is open ────────
  useEffect(() => {
    refreshAgentStatus();
    const id = setInterval(refreshAgentStatus, 5000);
    return () => clearInterval(id);
  }, [refreshAgentStatus]);

  // Best-effort teardown: release the camera when leaving. (The update poll
  // timer is owned + cleaned up by useAgentUpdate.)
  useEffect(() => {
    return () => {
      if (previewRef.current) {
        try {
          fetch('/api/system/cameras/preview/stop', { method: 'POST' });
        } catch {
          /* leaving the page anyway */
        }
      }
    };
  }, []);

  // ── camera preview control ─────────────────────────────────────────────────
  const stopPreview = useCallback(async () => {
    setPreviewDevice(null);
    try {
      await sysFetch('/cameras/preview/stop', { method: 'POST' });
    } catch {
      /* non-fatal */
    }
  }, []);

  // ── Modus — Robotertyp ─────────────────────────────────────────────────────
  const handleRobotTypeChange = useCallback(
    async (next) => {
      setPendingRobotType(next);
      setSavingRobotType(true);
      try {
        const { ok, data } = await sysFetch('/robot-type', {
          method: 'POST',
          body: { robot_type: next },
        });
        if (ok) toast.success(data.message || 'Robotertyp gespeichert.');
        else toast.error(data.message || 'Robotertyp konnte nicht gespeichert werden.');
      } catch {
        toast.error('Der Agent ist nicht erreichbar.');
      } finally {
        setSavingRobotType(false);
        // Drop the optimistic value only AFTER the refreshed truth has landed:
        // clearing it first would flash the old id on a successful change, and
        // keeping it would leave a refused change showing a type this Pi is
        // not. Either outcome then renders the agent's own answer.
        try {
          await refreshAgentStatus();
        } catch {
          /* the 5 s poll retries — the agent stays the authority */
        }
        setPendingRobotType(null);
      }
    },
    [refreshAgentStatus]
  );

  // ── Schritt A/B — Arme scannen ─────────────────────────────────────────────
  const handleScanArms = useCallback(
    async (force) => {
      setScanning(true);
      setScanMsg('');
      setScanFailed(false);
      await stopPreview();
      const foundMsg = scanRequiresLeader ? 'Beide Arme erkannt.' : 'Roboterarm erkannt.';
      try {
        const { ok, data } = await sysFetch('/scan-arms', {
          method: 'POST',
          body: force ? { force: true } : {},
        });
        setScanMsg(data.message || (ok ? foundMsg : 'Arm-Scan fehlgeschlagen.'));
        setScanFailed(!ok);
        if (ok) toast.success(data.message || foundMsg);
        else toast.error(data.message || 'Arm-Scan fehlgeschlagen.');
      } catch {
        setScanMsg('Der Agent ist nicht erreichbar. Bitte kurz warten und erneut versuchen.');
        setScanFailed(true);
        toast.error('Der Agent ist nicht erreichbar.');
      } finally {
        setScanning(false);
        refreshAgentStatus();
      }
    },
    [stopPreview, refreshAgentStatus, scanRequiresLeader]
  );

  // ── Schritt C — Kameras ────────────────────────────────────────────────────
  const handleScanCameras = useCallback(async () => {
    setCamScanning(true);
    await stopPreview();
    try {
      const { ok, data } = await sysFetch('/cameras/scan');
      if (ok && Array.isArray(data.cameras)) {
        setScanCameras(data.cameras);
        // Seed the role dropdowns from any roles already persisted in the .env.
        const seed = {};
        (agentStatus?.cameras || []).forEach((c) => {
          if (c.path) seed[c.path] = c.role || '';
        });
        setRoles((prev) => ({ ...seed, ...prev }));
        if (data.cameras.length === 0) toast('Keine Kamera gefunden.', { icon: toastIcon('info') });
      } else {
        toast.error('Kameras konnten nicht gesucht werden.');
      }
    } catch {
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setCamScanning(false);
    }
  }, [stopPreview, agentStatus]);

  const handleSaveRoles = useCallback(async () => {
    // Drop anything the profile does not allow, which on a both-role profile is
    // the identical `r === 'gripper' || r === 'scene'` filter this replaced. It
    // matters when the student changes Robotertyp AFTER assigning roles: the
    // now-invalid selection is dropped exactly like an unassigned camera (and
    // the <select> below already shows it as unassigned), rather than being
    // POSTed for the agent to refuse.
    const cameras = Object.entries(roles)
      .filter(([, r]) => allowedCameraRoles.includes(r))
      .map(([path, role]) => ({ path, role }));
    setSavingRoles(true);
    await stopPreview();
    try {
      const { ok, data } = await sysFetch('/cameras/roles', { method: 'POST', body: { cameras } });
      if (ok) toast.success(data.message || 'Kameras zugeordnet.');
      else toast.error(data.message || 'Zuordnung fehlgeschlagen.');
    } catch {
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setSavingRoles(false);
      refreshAgentStatus();
    }
  }, [roles, allowedCameraRoles, stopPreview, refreshAgentStatus]);

  // ── Umgebung starten / stoppen ─────────────────────────────────────────────
  const handleStartEnv = useCallback(async () => {
    setStartingEnv(true);
    await stopPreview();
    try {
      const { ok, data } = await sysFetch('/environment/start', {
        method: 'POST',
        body: { cloud_only: cloudOnly },
      });
      if (ok) toast.success(data.message || 'Roboter-Umgebung gestartet.');
      else toast.error(data.message || 'Start fehlgeschlagen.');
    } catch {
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setStartingEnv(false);
      refreshAgentStatus();
    }
  }, [cloudOnly, stopPreview, refreshAgentStatus]);

  const handleStopEnv = useCallback(async () => {
    setStoppingEnv(true);
    try {
      const { ok, data } = await sysFetch('/environment/stop', { method: 'POST' });
      if (ok) toast.success(data.message || 'Roboter-Umgebung gestoppt.');
      else toast.error(data.message || 'Stopp fehlgeschlagen.');
    } catch {
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setStoppingEnv(false);
      refreshAgentStatus();
    }
  }, [refreshAgentStatus]);

  // ── Update (ACK-early job + poll, via the shared useAgentUpdate hook) ───────
  const handleUpdate = useCallback(async () => {
    const res = await startUpdate();
    if (!res.ok) {
      toast.error(res.message || 'Aktualisierung konnte nicht gestartet werden.');
    }
  }, [startUpdate]);

  // ── Factory reset (double confirm) ─────────────────────────────────────────
  const handleReset = useCallback(async () => {
    setResetting(true);
    try {
      const { ok, data } = await sysFetch('/factory-reset', {
        method: 'POST',
        body: { confirm: true, confirm_again: true },
      });
      if (ok) toast.success(data.message || 'Daten zurückgesetzt.');
      else toast.error(data.message || 'Zurücksetzen fehlgeschlagen.');
    } catch {
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setResetting(false);
      setResetStep(0);
      refreshAgentStatus();
    }
  }, [refreshAgentStatus]);

  // ── Netzwerk-Check ─────────────────────────────────────────────────────────
  const handleNetCheck = useCallback(async () => {
    setNetChecking(true);
    try {
      const { ok, data } = await sysFetch('/netzwerk-check');
      if (ok) setNetCheck(data);
      else {
        setNetCheck(null);
        toast.error('Netzwerk-Check fehlgeschlagen.');
      }
    } catch {
      setNetCheck(null);
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setNetChecking(false);
    }
  }, []);

  // ── Protokoll (SSE) ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!protokollOpen) return undefined;
    if (typeof EventSource === 'undefined') return undefined;
    const es = new EventSource('/api/system/protokoll');
    es.onmessage = (e) => {
      setLogLines((prev) => {
        const next = [...prev, e.data];
        return next.length > 500 ? next.slice(next.length - 500) : next;
      });
    };
    es.onerror = () => {
      /* EventSource auto-reconnects; the proxy blip during an update is expected */
    };
    return () => es.close();
  }, [protokollOpen]);

  // ── derived flags ──────────────────────────────────────────────────────────
  const arms = agentStatus?.arms_identified || {};
  const armsBoth = !!arms.both;
  const robotTierUp = !!agentStatus?.robot_tier_up;
  const managerUp = !!agentStatus?.manager_up;
  // PROFILE-AWARE readiness, computed by the agent against the selected
  // ArmProfile (`agent.py::_hardware_ready`). `arms_identified.both` answers a
  // DIFFERENT question — are two arms present — and a follower-only rig is
  // fully ready with it false, which is why omx_follower and edu6_studio were
  // both unstartable on a Pi. Falls back to the old both-arms gate when the
  // agent predates the key, so an un-updated Pi is byte-unchanged.
  const hardwareReady =
    typeof agentStatus?.hardware_ready === 'boolean' ? agentStatus.hardware_ready : armsBoth;
  const canStart = cloudOnly || (hardwareReady && !!agentStatus?.agent_ready);
  const lanIp = agentStatus?.lan_ip || null;
  const hostname = agentStatus?.hostname || null;

  // Origin version for the system-files drift banner below, or null to omit it.
  // Derived rather than inlined so the JSX cannot leak: `{value && <p/>}` renders
  // a BARE `0` or `NaN` (measured), and a non-string throws „Objects are not
  // valid as a React child", which white-screens the System tab — the Pi's ONLY
  // repair surface, i.e. the one page that must survive a malformed payload.
  // The agent's stamp is `Optional[str]` (agent.py::_read_system_files_stamp
  // degrades blank/unreadable to None), so anything else is a contract
  // violation and is simply not shown. Suppressed when it equals the agent
  // version too: naming the same number twice reads as a contradiction — the
  // same rule agent.py applies to its Protokoll lines.
  //
  // The contract is non-empty-string-or-null, and the consumer below tests
  // `!== null` rather than truthiness ON PURPOSE. Truthiness would make the
  // empty-string clause here redundant — i.e. a clause no mutation can kill,
  // which this repo treats as worse than no clause — and would silently start
  // rendering „… stammen aus Version ." the day someone tightens the JSX.
  const driftOriginVersion =
    typeof agentStatus?.system_files_version === 'string'
    && agentStatus.system_files_version !== ''
    && agentStatus.system_files_version !== agentStatus.agent_version
      ? agentStatus.system_files_version
      : null;

  const previewSrc = previewDevice
    ? `/api/system/cameras/preview?device=${encodeURIComponent(previewDevice)}`
    : null;

  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="eb-shell">
        <SectionHeader
          size="md"
          eyebrow={<span className="su-sys-eyebrow">Orange Pi</span>}
          title="System"
          description="Richte deinen Roboter ein: Arme und Kameras scannen, Umgebung starten, aktualisieren."
          className="mb-6 md:mb-8"
        />

        {/* System-files drift the agent could NOT repair. `system_files_stale`
            is set at exactly ONE place in agent.py — the branch where a repair
            FAILED. At boot the agent byte-compares the installed systemd units,
            udev rules and docker-compose.opi.yml against the copies it ships
            inside pi_agent/, and installs the shipped bytes atomically on
            drift; this banner is therefore the REMAINDER only (a read-only
            filesystem, missing permissions, or a shipped compose the
            `docker compose config` gate refused).

            It is deliberately INFORMATIONAL, not an alarm, and that is a
            correctness point rather than a style preference: a refused or
            failed repair leaves the previous, working file byte-intact, so the
            Pi keeps running on its last-known-good configuration and the agent
            retries on the next boot. Telling a teacher something is broken
            while the rig works is the warning-fatigue pattern this repo has
            been burned by twice (`_APPROACH_WARN_FRAC`,
            `EDUBOTICS_HOME_SELFCOLL_WARN_M`). Hence `role="status"` (polite
            live region) over `role="alert"` (assertive), and the file's own
            neutral --bg-sunk treatment over amber.

            Two sentences that used to be here are GONE on purpose.
            „Aktualisierungen erneuern nur den Agenten und die Images — nicht
            diese Dateien" became FALSE when the units/udev/compose joined the
            self-updated set. And „sudo ./setup.sh aus dem EduBotics-Quellordner"
            is a remedy no classroom can perform (it needs a source checkout) and
            is no longer the primary one — the agent's own retry is. NEVER tell
            anyone here to re-flash the SD card (an earlier draft did, at ~100 %
            false positives).

            The origin version is resolved by `driftOriginVersion` above (null
            = omit): suppressed when it equals `agent_version` or is absent, and
            type-gated so a malformed payload cannot leak or throw. */}
        {agentStatus?.system_files_stale && (
          <div
            role="status"
            className="mb-4 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] p-3 text-sm text-[var(--ink-2)]"
          >
            <div className="font-semibold">
              Eine Systemdatei-Aktualisierung wurde nicht übernommen
            </div>
            <p className="mt-1">
              Der Pi läuft normal weiter — mit der zuletzt funktionierenden
              Konfiguration. Eine Änderung aus einer neueren Version konnte nicht
              übernommen werden; den Grund nennt das Protokoll. Beim nächsten
              Neustart versucht der Pi es automatisch erneut.
            </p>
            {driftOriginVersion !== null && (
              <p className="mt-1 text-xs text-[var(--ink-3)]">
                Die installierten Systemdateien stammen aus Version{' '}
                <span className="font-mono">{driftOriginVersion}</span>.
              </p>
            )}
          </div>
        )}

        <div className="su-sys-grid">
        <div className="su-sys-col">
        {/* Pi-IP-Anzeige + Verbindungszustand */}
        <Card className="su-sys-card su-sys-addr" padded={false}>
          <div className="su-sys-addr__main">
            <div className="su-lab">
              Adresse dieses Roboters
            </div>
            <Screen className="su-sys-addr__screen">
              <span className="su-sys-addr__disc" aria-hidden="true"><Led tone="sig" size={10} /></span>
              <span className="min-w-0">
                <span className="su-sys-addr__ip su-dot">
                  {lanIp || '—'}
                </span>
                {hostname && (
                  <span className="su-sys-addr__host">
                    {hostname}.local
                  </span>
                )}
              </span>
            </Screen>
            <p className="su-sys-addr__desc">
              Diese Adresse steht auf dem Etikett am Gerät. Ruf sie im Browser auf,
              um von einem anderen PC auf den Roboter zuzugreifen.
            </p>
          </div>
          <div className="su-sys-addr__chips">
            {agentReachable ? (
              <StatusChip tone="success" led="ok">Agent bereit</StatusChip>
            ) : (
              <StatusChip tone="amber" led="warn">Agent wird gestartet …</StatusChip>
            )}
            <StatusChip tone={managerUp ? 'success' : 'neutral'} led={managerUp ? 'ok' : 'off'}>
              Weboberfläche {managerUp ? 'läuft' : '—'}
            </StatusChip>
            <StatusChip tone={robotTierUp ? 'success' : 'neutral'} led={robotTierUp ? 'ok' : 'off'}>
              Roboter {robotTierUp ? 'läuft' : 'gestoppt'}
            </StatusChip>
          </div>
        </Card>

        {/* Modus — Robotertyp + Cloud-Modus.
            ONE card, deliberately: the Windows twin puts both in a single
            „Modus" LabelFrame (gui_app.py::mode_frame), and a second card
            called Modus would read as two competing mode switches.

            The selector sits ABOVE the cloud checkbox because it is the
            decision the rest of the wizard depends on — the arm family the scan
            looks for, whether a Leader tile exists at all, and what
            „Umgebung starten" requires. It renders only when the agent offers a
            list (an older agent → no selector, no crash), and is frozen exactly
            where the GUI grays its combobox: while the robot tier RUNS (the
            type is hardset at start and the arm container branches on it) and
            in cloud-only mode (no robot, so the type is irrelevant). */}
        <Step title="Modus">
          {robotProfiles.length > 0 && (
            <div className="mb-4">
              <label
                htmlFor="eb-robot-type"
                className="su-lab block"
              >
                Robotertyp
              </label>
              <div className="su-sys-type">
              <select
                id="eb-robot-type"
                value={selectedRobotType}
                disabled={robotTierUp || cloudOnly || savingRobotType}
                onChange={(e) => handleRobotTypeChange(e.target.value)}
                // Every disabled reason gets its own sentence. The help text
                // below explains the tier case only, so in Cloud-Modus the
                // control was grey while the one explanation on screen named a
                // condition that was already satisfied — a contradiction.
                title={
                  robotTierUp
                    ? 'Zum Wechseln zuerst die Roboter-Umgebung stoppen.'
                    : cloudOnly
                    ? 'Im Cloud-Modus wird kein Roboter verwendet — der Robotertyp spielt keine Rolle.'
                    : undefined
                }
                className="su-field mt-1 h-11 w-full max-w-md px-3"
              >
                {/* Only while the agent's id is not (yet) in the list — a
                    hand-edited .env, or a status that has not landed. Without
                    it the <select> would silently display the FIRST profile
                    while the rig runs another. `disabled` because it is a
                    REPORT of the current state, not an offer: choosing it would
                    POST an id the agent must reject. */}
                {selectedProfile === null && (
                  <option value={selectedRobotType} disabled>
                    {selectedRobotType || '— Robotertyp —'}
                  </option>
                )}
                {robotProfiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.display_de}
                  </option>
                ))}
              </select>
              {/* The selected type's sprite (D8: the dropdown stays; it plays on hover). */}
              <span className="su-sys-type__robot">
                <RobotIcon
                  profile={selectedRobotType}
                  size={72}
                  title={selectedProfile ? selectedProfile.display_de : undefined}
                />
              </span>
              </div>
              <p className="mt-2 text-[13px] text-[var(--ink-2)]">
                Wähle, welcher Roboter an diesem Pi angeschlossen ist.{' '}
                {scanRequiresLeader
                  ? 'Dieser Typ braucht beide Arme — Leader und Follower.'
                  : 'Dieser Typ braucht nur einen Arm — ein Leader-Arm wird nicht benötigt.'}{' '}
                Der Typ lässt sich nur ändern, solange die Roboter-Umgebung
                gestoppt ist.
              </p>
            </div>
          )}
          <label className="su-sys-cloud flex items-start gap-4 cursor-pointer">
            <input
              type="checkbox"
              checked={cloudOnly}
              onChange={(e) => setCloudOnly(e.target.checked)}
              className="su-switch"
            />
            <span className="text-sm text-[var(--ink-2)]">
              <span className="font-medium text-[var(--ink)]">Cloud-Modus (ohne Roboter)</span>
              <br />
              Überspringt Arme und Kameras — nur Training und Datensätze. Die
              Weboberfläche läuft dann trotzdem.
            </span>
          </label>
        </Step>

        {!cloudOnly && (
          <>
            {/* Schritt A/B — Arme.
                DELIBERATE DIVERGENCE from the Windows GUI, whose Schritt-A
                frame says „Arme scannen" on every profile. On the Pi this card
                sits beside labels the Windows card does not have — a
                „Roboterarm" tile and an „Arm erkannt" pill — so on a
                follower-only profile, where there is exactly ONE arm, the
                plural read as a bug. Heading + button go singular there;
                `omx_full` is unchanged. Do not "re-sync" this with the GUI. */}
            <Step
              n="A"
              title={scanRequiresLeader ? 'Arme scannen' : 'Arm scannen'}
              right={
                <Pill tone={hardwareReady ? 'success' : 'neutral'}>
                  <Led tone={hardwareReady ? 'ok' : 'off'} size={7} />
                  {hardwareReady
                    ? (scanRequiresLeader ? 'Beide Arme erkannt' : 'Arm erkannt')
                    : 'Nicht erkannt'}
                </Pill>
              }
            >
              {/* The Leader tile is HIDDEN, not disabled, on a profile that has
                  no leader: showing a permanently empty „Leader —" is what made
                  a Roboter-Studio kit look half-broken. The remaining tile is
                  then named „Roboterarm" (the agent's own German for it), since
                  „Follower" only means anything opposite a leader. */}
              <div className="su-sys-scan">
              {/* The radar (decorative): it sweeps while scanning; a found arm is a blip. */}
              <Screen className={`su-sys-radar${scanning ? ' su-sys-radar--on' : ''}`} aria-hidden="true">
                <span className="su-sys-radar__sweep" />
                {scanRequiresLeader && arms.leader ? <span className="su-sys-radar__blip su-sys-radar__blip--l" /> : null}
                {arms.follower ? <span className="su-sys-radar__blip su-sys-radar__blip--f" /> : null}
              </Screen>
              <div className="su-sys-scan__side">
              <div className={`grid grid-cols-1 ${scanRequiresLeader ? 'lg:grid-cols-1' : ''} gap-3 mb-3`}>
                {scanRequiresLeader && (
                  <div className="su-sys-well">
                    <div className="su-sys-well__lab">Leader</div>
                    <div className="font-mono text-sm text-[var(--ink)] break-all">
                      {arms.leader || '—'}
                    </div>
                  </div>
                )}
                <div className="su-sys-well">
                  <div className="su-sys-well__lab">
                    {scanRequiresLeader ? 'Follower' : 'Roboterarm'}
                  </div>
                  <div className="font-mono text-sm text-[var(--ink)] break-all">
                    {arms.follower || '—'}
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Btn variant="primary" onClick={() => handleScanArms(false)} disabled={scanning}>
                  {scanning
                    ? 'Wird gescannt …'
                    : (scanRequiresLeader ? 'Arme scannen' : 'Arm scannen')}
                </Btn>
                <Btn variant="secondary" onClick={() => handleScanArms(true)} disabled={scanning}>
                  Vollständig neu scannen
                </Btn>
              </div>
              </div>
              </div>
              {scanMsg && (
                <p className={`mt-3 text-sm ${scanFailed ? 'text-[color:var(--danger)]' : 'text-[var(--ink-2)]'}`}>
                  {scanMsg}
                </p>
              )}
              {scanFailed && (
                <div className="mt-3 rounded-[14px] bg-[var(--amber-wash)] p-3 text-sm text-[color:var(--amber)]">
                  <div className="font-medium mb-1">Hilfe bei Problemen</div>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>
                      {scanRequiresLeader
                        ? 'Beide Arme über USB anschließen und einschalten (12-V-Netzteil).'
                        : 'Den Roboterarm über USB anschließen und einschalten (12-V-Netzteil).'}
                    </li>
                    <li>
                      Rechte fehlen? Der Benutzer muss in den Gruppen
                      {' '}<span className="font-mono">dialout</span> und
                      {' '}<span className="font-mono">video</span> sein.
                    </li>
                    <li>USB-Kabel neu einstecken und erneut scannen.</li>
                  </ul>
                </div>
              )}
            </Step>

            {/* Schritt C — Kameras */}
            <Step
              n="C"
              title="Kameras"
              right={
                <Btn size="sm" variant="secondary" onClick={handleScanCameras} disabled={camScanning}>
                  {camScanning ? 'Suche …' : 'Kameras suchen'}
                </Btn>
              }
            >
              {scanCameras.length === 0 ? (
                <p className="text-sm text-[var(--ink-2)]">
                  Noch keine Kameras gesucht. „Kameras suchen" klicken.
                </p>
              ) : (
                <div className="space-y-3">
                  {scanCameras.map((cam) => (
                    <div
                      key={cam.path}
                      className="su-sys-well flex flex-wrap items-center gap-3"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium text-[var(--ink)] truncate">
                          {cam.name || cam.path}
                        </div>
                        <div className="font-mono text-[11.5px] text-[var(--ink-2)] break-all">
                          {cam.path}
                        </div>
                      </div>
                      <select
                        aria-label={`Rolle für ${cam.name || cam.path}`}
                        // Deliberately NOT narrowed to `allowedCameraRoles`. A
                        // role the profile lost (change Robotertyp after
                        // assigning) leaves a value with no matching <option>,
                        // and HTML's own selectedness algorithm then selects
                        // the first one — „— Rolle —" — so the box already
                        // reads UNASSIGNED. MEASURED: a normalising expression
                        // here is indistinguishable from this one (both give
                        // value '' / selectedIndex 0), i.e. it would be an
                        // unfenceable no-op. `handleSaveRoles` is the real
                        // fence, and it drops the stale role.
                        value={roles[cam.path] || ''}
                        onChange={(e) =>
                          setRoles((prev) => ({ ...prev, [cam.path]: e.target.value }))
                        }
                        className="su-field su-sys-rolesel h-10 px-3"
                      >
                        <option value="">— Rolle —</option>
                        {CAMERA_ROLE_OPTIONS.filter((o) =>
                          allowedCameraRoles.includes(o.value)
                        ).map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                      <Btn
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          previewDevice === cam.path ? stopPreview() : setPreviewDevice(cam.path)
                        }
                      >
                        {previewDevice === cam.path ? 'Vorschau stoppen' : 'Vorschau'}
                      </Btn>
                    </div>
                  ))}
                  <div className="flex items-center gap-2">
                    <Btn variant="primary" onClick={handleSaveRoles} disabled={savingRoles}>
                      {savingRoles ? 'Wird gespeichert …' : 'Zuordnung speichern'}
                    </Btn>
                  </div>
                </div>
              )}

              {previewSrc && (
                <div className="mt-4">
                  <div className="su-lab mb-1">
                    Vorschau
                  </div>
                  <img
                    src={previewSrc}
                    alt="Kameravorschau"
                    className="max-w-full rounded-[var(--radius-sm)] border border-[var(--line)] bg-black"
                    style={{ maxHeight: 320 }}
                  />
                </div>
              )}
            </Step>
          </>
        )}

        </div>
        <div className="su-sys-col">
        {/* Umgebung starten / stoppen */}
        <Card title="Roboter-Umgebung" titleStyle="serif" className="su-sys-card">
          <p className="text-sm text-[var(--ink-2)] mb-3">
            {cloudOnly
              ? 'Im Cloud-Modus läuft die Weboberfläche bereits — Roboter-Start nicht nötig.'
              : robotTierUp
              ? 'Die Roboter-Umgebung läuft. Zum Aufnehmen wechsle zur Aufnahme.'
              : scanRequiresLeader
              ? 'Startet Arme und Kameras. Voraussetzung: beide Arme erkannt.'
              : 'Startet den Roboterarm und die Kameras. Voraussetzung: Arm erkannt.'}
          </p>
          {/* The state as a screen bar: a light, the state word, activity dots
              while it starts or stops. Words from the page's own chips. */}
          <Screen className="su-sys-env" aria-hidden="true">
            <Led
              tone={startingEnv || stoppingEnv ? 'sig' : robotTierUp ? 'ok' : 'off'}
              pulse={startingEnv || stoppingEnv || robotTierUp ? 'breathe' : 'none'}
              size={12}
            />
            <span className="su-sys-env__word su-dot">{robotTierUp ? 'läuft' : 'gestoppt'}</span>
            <span className={`su-sys-env__dots${startingEnv || stoppingEnv ? ' su-sys-env__dots--on' : ''}`}>
              {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => <i key={i} style={{ '--i': i }} />)}
            </span>
          </Screen>
          <div className="flex flex-wrap items-center gap-2">
            <Btn
              variant="primary"
              onClick={handleStartEnv}
              disabled={startingEnv || (!cloudOnly && robotTierUp) || !canStart}
              title={
                !canStart
                  ? (scanRequiresLeader
                      ? 'Bitte zuerst beide Arme scannen (oder Cloud-Modus wählen).'
                      : 'Bitte zuerst den Roboterarm scannen (oder Cloud-Modus wählen).')
                  : undefined
              }
            >
              {startingEnv ? 'Wird gestartet …' : 'Umgebung starten'}
            </Btn>
            <Btn
              variant="secondary"
              onClick={handleStopEnv}
              disabled={stoppingEnv || (!robotTierUp && !cloudOnly)}
            >
              {stoppingEnv ? 'Wird gestoppt …' : 'Stoppen'}
            </Btn>
          </div>
          {!cloudOnly && !canStart && (
            <p className="mt-2 text-[13px] text-[var(--ink-2)]">
              {scanRequiresLeader
                ? 'Bitte zuerst beide Arme scannen (Schritt A) oder oben „Cloud-Modus" wählen.'
                : 'Bitte zuerst den Roboterarm scannen (Schritt A) oder oben „Cloud-Modus" wählen.'}
            </p>
          )}
        </Card>

        {/* Update */}
        <Card
          title="Aktualisierung"
          titleStyle="serif"
          right={
            <span className="text-[13px] text-[var(--ink-2)]">{fmtAge(agentStatus?.images)}</span>
          }
          className="su-sys-card"
        >
          <p className="text-sm text-[var(--ink-2)] mb-3">
            Lädt die neuesten Container-Images. Die Weboberfläche wird zuletzt neu
            gestartet — die Seite lädt sich danach kurz selbst neu.
          </p>
          <Btn variant="secondary" onClick={handleUpdate} disabled={updating}>
            {updating ? 'Aktualisierung läuft …' : 'Jetzt aktualisieren'}
          </Btn>
          {updateJob && (
            <div className="su-sys-well mt-3 text-sm">
              <div className="text-[var(--ink)]">{updateJob.message}</div>
              {typeof updateJob.progress === 'number' && updateJob.status === 'running' && (
                <div className="mt-2 h-1.5 w-full bg-[var(--line)] rounded-full overflow-hidden">
                  <div
                    className="h-full bg-[var(--su-sig)] transition-[width] duration-500"
                    style={{ width: `${Math.max(0, Math.min(100, updateJob.progress))}%` }}
                  />
                </div>
              )}
            </div>
          )}
        </Card>

        {/* Netzwerk-Check */}
        <Card
          title="Netzwerk-Check"
          titleStyle="serif"
          right={
            <Btn size="sm" variant="secondary" onClick={handleNetCheck} disabled={netChecking}>
              {netChecking ? 'Prüfe …' : 'Netzwerk prüfen'}
            </Btn>
          }
          className="su-sys-card"
        >
          <p className="text-sm text-[var(--ink-2)] mb-3">
            Prüft, ob Cloud, Registry und Hugging Face erreichbar sind und ob die
            Uhrzeit stimmt — die häufigsten Ursachen für Fehler im Schulnetz.
          </p>
          {netCheck && Array.isArray(netCheck.checks) ? (
            <ul className="space-y-2">
              {netCheck.checks.map((c) => (
                <li key={c.key} className="flex items-start gap-2.5">
                  <Led tone={c.ok ? 'ok' : 'rec'} className="mt-1.5" />
                  <div className="min-w-0">
                    <div
                      className={`text-sm font-medium ${
                        c.ok ? 'text-[color:var(--success)]' : 'text-[color:var(--danger)]'
                      }`}
                    >
                      {c.label}
                    </div>
                    {!c.ok && c.hint && (
                      <div className="text-[13px] text-[var(--ink-2)]">{c.hint}</div>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-[var(--ink-2)]">Noch nicht geprüft.</p>
          )}
        </Card>

        {/* Protokoll */}
        <Card
          title="Protokoll"
          titleStyle="serif"
          right={
            <Btn size="sm" variant="secondary" onClick={() => setProtokollOpen((v) => !v)}>
              {protokollOpen ? 'Ausblenden' : 'Anzeigen'}
            </Btn>
          }
          className="su-sys-card"
        >
          {protokollOpen ? (
            <Screen className="su-sys-log">
              {logLines.length === 0 ? (
                <p className="su-sys-log__line su-sys-log__line--last">Warte auf Meldungen …<span className="su-sys-log__caret" aria-hidden="true" /></p>
              ) : (
                logLines.map((line, i) => (
                  <div
                    key={i}
                    className={`su-sys-log__line whitespace-pre-wrap break-words${i === logLines.length - 1 ? ' su-sys-log__line--last' : ''}`}
                  >
                    {line}
                    {i === logLines.length - 1 && <span className="su-sys-log__caret" aria-hidden="true" />}
                  </div>
                ))
              )}
            </Screen>
          ) : (
            <p className="text-sm text-[var(--ink-2)]">
              Live-Meldungen des Roboters (mit ausgeblendeten Passwörtern).
            </p>
          )}
        </Card>

        {/* Daten zurücksetzen */}
        <Card title="Daten zurücksetzen" titleStyle="serif" className="su-sys-card">
          <p className="text-sm text-[var(--ink-2)] mb-3">
            Löscht Datensätze, den Hugging-Face-Zwischenspeicher und die
            Roboter-Studio-Kalibrierung. Das kann nicht rückgängig gemacht werden.
          </p>
          {resetStep === 0 && (
            <div className="su-sys-hazard">
              <Btn variant="ghost" className="su-sys-hazard__btn" onClick={() => setResetStep(1)}>
                <Icon name="trash" size={16} />
                Daten zurücksetzen
              </Btn>
            </div>
          )}
          {resetStep === 1 && (
            <div className="rounded-[14px] bg-[var(--danger-wash)] p-3">
              <p className="text-sm text-[color:var(--danger)] mb-2">
                Wirklich alle Daten löschen? Datensätze und Kalibrierung gehen verloren.
              </p>
              <div className="flex items-center gap-2">
                <Btn variant="secondary" onClick={() => setResetStep(0)}>Abbrechen</Btn>
                <Btn variant="danger" onClick={() => setResetStep(2)}>Fortfahren</Btn>
              </div>
            </div>
          )}
          {resetStep === 2 && (
            <div className="rounded-[14px] bg-[var(--danger-wash)] p-3 shadow-[inset_0_0_0_1px_#F1C4C4]">
              <p className="text-sm font-medium text-[color:var(--danger)] mb-2">
                Letzte Bestätigung: Dies löscht endgültig alle lokalen Daten.
              </p>
              <div className="flex items-center gap-2">
                <Btn variant="secondary" onClick={() => setResetStep(0)} disabled={resetting}>
                  Abbrechen
                </Btn>
                <Btn variant="danger" onClick={handleReset} disabled={resetting}>
                  {resetting ? 'Wird gelöscht …' : 'Endgültig löschen'}
                </Btn>
              </div>
            </div>
          )}
        </Card>
        </div>
        </div>
      </div>
    </div>
  );
}
