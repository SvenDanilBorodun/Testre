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

import React, { useCallback, useEffect, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { Card, Btn, Pill, SectionHeader } from '../components/EbUI';
import { usePiMode } from '../utils/piMode';
import { useAgentUpdate } from '../hooks/useAgentUpdate';

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

// A labelled step card so the wizard reads top-to-bottom like the .exe.
function Step({ n, title, children, right }) {
  return (
    <Card
      title={n ? `Schritt ${n} — ${title}` : title}
      right={right}
      className="mb-4"
    >
      {children}
    </Card>
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

  const [hfToken, setHfToken] = useState('');
  const [hfJustSaved, setHfJustSaved] = useState(false);
  const [savingToken, setSavingToken] = useState(false);

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
        if (data.cameras.length === 0) toast('Keine Kamera gefunden.', { icon: 'ℹ️' });
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
    const cameras = Object.entries(roles)
      .filter(([, r]) => r === 'gripper' || r === 'scene')
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
  }, [roles, stopPreview, refreshAgentStatus]);

  // ── Schritt D — HF-Token ───────────────────────────────────────────────────
  const handleSaveToken = useCallback(async () => {
    setSavingToken(true);
    try {
      const { ok, data } = await sysFetch('/hf-token', { method: 'POST', body: { token: hfToken } });
      if (ok && data.saved) {
        setHfJustSaved(true);
        setHfToken('');
        toast.success('Token gespeichert.');
      } else if (ok) {
        setHfJustSaved(false);
        toast.success(data.message || 'Token entfernt.');
      } else {
        toast.error(data.message || 'Token konnte nicht gespeichert werden.');
      }
    } catch {
      toast.error('Der Agent ist nicht erreichbar.');
    } finally {
      setSavingToken(false);
      refreshAgentStatus();
    }
  }, [hfToken, refreshAgentStatus]);

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
  const tokenSaved = hfJustSaved || !!agentStatus?.hf_token_saved;
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
          eyebrow="Orange Pi"
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

        {/* Pi-IP-Anzeige + Verbindungszustand */}
        <Card className="mb-4">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="min-w-0">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
                Adresse dieses Roboters
              </div>
              <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-mono text-[24px] font-semibold text-[var(--ink)] leading-none">
                  {lanIp || '—'}
                </span>
                {hostname && (
                  <span className="font-mono text-sm text-[var(--ink-3)]">
                    {hostname}.local
                  </span>
                )}
              </div>
              <p className="mt-2 text-xs text-[var(--ink-3)] max-w-md">
                Diese Adresse steht auf dem Etikett am Gerät. Ruf sie im Browser auf,
                um von einem anderen PC auf den Roboter zuzugreifen.
              </p>
            </div>
            <div className="flex flex-col items-end gap-2">
              {agentReachable ? (
                <Pill tone="success" dot>Agent bereit</Pill>
              ) : (
                <Pill tone="amber" dot>Agent wird gestartet …</Pill>
              )}
              <Pill tone={managerUp ? 'success' : 'neutral'}>
                Weboberfläche {managerUp ? 'läuft' : '—'}
              </Pill>
              <Pill tone={robotTierUp ? 'success' : 'neutral'}>
                Roboter {robotTierUp ? 'läuft' : 'gestoppt'}
              </Pill>
            </div>
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
                className="block text-sm font-medium text-[var(--ink)]"
              >
                Robotertyp
              </label>
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
                className="mt-1 h-10 w-full max-w-md rounded-[var(--radius-sm)] border border-[var(--line)] bg-white px-2 text-sm disabled:opacity-60"
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
              <p className="mt-2 text-xs text-[var(--ink-3)]">
                Wähle, welcher Roboter an diesem Pi angeschlossen ist.{' '}
                {scanRequiresLeader
                  ? 'Dieser Typ braucht beide Arme — Leader und Follower.'
                  : 'Dieser Typ braucht nur einen Arm — ein Leader-Arm wird nicht benötigt.'}{' '}
                Der Typ lässt sich nur ändern, solange die Roboter-Umgebung
                gestoppt ist.
              </p>
            </div>
          )}
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={cloudOnly}
              onChange={(e) => setCloudOnly(e.target.checked)}
              className="mt-1 h-4 w-4"
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
              <div className={`grid grid-cols-1 ${scanRequiresLeader ? 'sm:grid-cols-2' : ''} gap-3 mb-3`}>
                {scanRequiresLeader && (
                  <div className="rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] p-3">
                    <div className="text-[11px] uppercase tracking-wide text-[var(--ink-3)]">Leader</div>
                    <div className="font-mono text-sm text-[var(--ink)] break-all">
                      {arms.leader || '—'}
                    </div>
                  </div>
                )}
                <div className="rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] p-3">
                  <div className="text-[11px] uppercase tracking-wide text-[var(--ink-3)]">
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
              {scanMsg && (
                <p className={`mt-3 text-sm ${scanFailed ? 'text-[color:var(--danger)]' : 'text-[var(--ink-2)]'}`}>
                  {scanMsg}
                </p>
              )}
              {scanFailed && (
                <div className="mt-3 rounded-[var(--radius-sm)] border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
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
                <p className="text-sm text-[var(--ink-3)]">
                  Noch keine Kameras gesucht. „Kameras suchen" klicken.
                </p>
              ) : (
                <div className="space-y-3">
                  {scanCameras.map((cam) => (
                    <div
                      key={cam.path}
                      className="flex flex-wrap items-center gap-3 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] p-3"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium text-[var(--ink)] truncate">
                          {cam.name || cam.path}
                        </div>
                        <div className="font-mono text-[11px] text-[var(--ink-3)] break-all">
                          {cam.path}
                        </div>
                      </div>
                      <select
                        aria-label={`Rolle für ${cam.name || cam.path}`}
                        value={roles[cam.path] || ''}
                        onChange={(e) =>
                          setRoles((prev) => ({ ...prev, [cam.path]: e.target.value }))
                        }
                        className="h-9 rounded-[var(--radius-sm)] border border-[var(--line)] bg-white px-2 text-sm"
                      >
                        <option value="">— Rolle —</option>
                        <option value="gripper">Greifer</option>
                        <option value="scene">Szene</option>
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
                  <div className="text-[11px] uppercase tracking-wide text-[var(--ink-3)] mb-1">
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

        {/* Schritt D — HF-Token */}
        <Step
          n="D"
          title="Hugging Face Token"
          right={tokenSaved ? <Pill tone="success" dot>✓ Token gespeichert</Pill> : null}
        >
          <p className="text-sm text-[var(--ink-3)] mb-2">
            Nötig zum Hochladen von Datensätzen und für das Training.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="password"
              value={hfToken}
              onChange={(e) => setHfToken(e.target.value)}
              placeholder="hf_..."
              aria-label="Hugging Face Token"
              className="h-10 flex-1 min-w-[200px] rounded-[var(--radius-sm)] border border-[var(--line)] bg-white px-3 font-mono text-sm"
            />
            <Btn variant="primary" onClick={handleSaveToken} disabled={savingToken || !hfToken.trim()}>
              {savingToken ? 'Wird gespeichert …' : 'Token speichern'}
            </Btn>
          </div>
        </Step>

        {/* Umgebung starten / stoppen */}
        <Card title="Roboter-Umgebung" className="mb-4">
          <p className="text-sm text-[var(--ink-3)] mb-3">
            {cloudOnly
              ? 'Im Cloud-Modus läuft die Weboberfläche bereits — Roboter-Start nicht nötig.'
              : robotTierUp
              ? 'Die Roboter-Umgebung läuft. Zum Aufnehmen wechsle zur Aufnahme.'
              : scanRequiresLeader
              ? 'Startet Arme und Kameras. Voraussetzung: beide Arme erkannt.'
              : 'Startet den Roboterarm und die Kameras. Voraussetzung: Arm erkannt.'}
          </p>
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
            <p className="mt-2 text-xs text-[var(--ink-3)]">
              {scanRequiresLeader
                ? 'Bitte zuerst beide Arme scannen (Schritt A) oder oben „Cloud-Modus" wählen.'
                : 'Bitte zuerst den Roboterarm scannen (Schritt A) oder oben „Cloud-Modus" wählen.'}
            </p>
          )}
        </Card>

        {/* Update */}
        <Card
          title="Aktualisierung"
          right={
            <span className="text-xs text-[var(--ink-3)]">{fmtAge(agentStatus?.images)}</span>
          }
          className="mb-4"
        >
          <p className="text-sm text-[var(--ink-3)] mb-3">
            Lädt die neuesten Container-Images. Die Weboberfläche wird zuletzt neu
            gestartet — die Seite lädt sich danach kurz selbst neu.
          </p>
          <Btn variant="secondary" onClick={handleUpdate} disabled={updating}>
            {updating ? 'Aktualisierung läuft …' : 'Jetzt aktualisieren'}
          </Btn>
          {updateJob && (
            <div className="mt-3 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] p-3 text-sm">
              <div className="text-[var(--ink-2)]">{updateJob.message}</div>
              {typeof updateJob.progress === 'number' && updateJob.status === 'running' && (
                <div className="mt-2 h-1.5 w-full bg-[var(--line)] rounded-full overflow-hidden">
                  <div
                    className="h-full bg-[var(--accent)] transition-[width] duration-500"
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
          right={
            <Btn size="sm" variant="secondary" onClick={handleNetCheck} disabled={netChecking}>
              {netChecking ? 'Prüfe …' : 'Netzwerk prüfen'}
            </Btn>
          }
          className="mb-4"
        >
          <p className="text-sm text-[var(--ink-3)] mb-3">
            Prüft, ob Cloud, Registry und Hugging Face erreichbar sind und ob die
            Uhrzeit stimmt — die häufigsten Ursachen für Fehler im Schulnetz.
          </p>
          {netCheck && Array.isArray(netCheck.checks) ? (
            <ul className="space-y-2">
              {netCheck.checks.map((c) => (
                <li key={c.key} className="flex items-start gap-2">
                  <span
                    className={`mt-0.5 inline-block h-2.5 w-2.5 rounded-full ${
                      c.ok ? 'bg-[color:var(--success)]' : 'bg-[color:var(--danger)]'
                    }`}
                  />
                  <div className="min-w-0">
                    <div
                      className={`text-sm font-medium ${
                        c.ok ? 'text-[color:var(--success)]' : 'text-[color:var(--danger)]'
                      }`}
                    >
                      {c.label}
                    </div>
                    {!c.ok && c.hint && (
                      <div className="text-xs text-[var(--ink-3)]">{c.hint}</div>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-[var(--ink-3)]">Noch nicht geprüft.</p>
          )}
        </Card>

        {/* Protokoll */}
        <Card
          title="Protokoll"
          right={
            <Btn size="sm" variant="secondary" onClick={() => setProtokollOpen((v) => !v)}>
              {protokollOpen ? 'Ausblenden' : 'Anzeigen'}
            </Btn>
          }
          className="mb-4"
        >
          {protokollOpen ? (
            <div className="max-h-64 overflow-y-auto rounded-[var(--radius-sm)] bg-[var(--ink)] p-3 font-mono text-[11px] text-white/85">
              {logLines.length === 0 ? (
                <p className="text-white/40">Warte auf Meldungen …</p>
              ) : (
                logLines.map((line, i) => (
                  <div key={i} className="whitespace-pre-wrap break-words">
                    {line}
                  </div>
                ))
              )}
            </div>
          ) : (
            <p className="text-sm text-[var(--ink-3)]">
              Live-Meldungen des Roboters (mit ausgeblendeten Passwörtern).
            </p>
          )}
        </Card>

        {/* Daten zurücksetzen */}
        <Card title="Daten zurücksetzen" className="mb-8">
          <p className="text-sm text-[var(--ink-3)] mb-3">
            Löscht Datensätze, den Hugging-Face-Zwischenspeicher und die
            Roboter-Studio-Kalibrierung. Das kann nicht rückgängig gemacht werden.
          </p>
          {resetStep === 0 && (
            <Btn variant="danger" onClick={() => setResetStep(1)}>
              Daten zurücksetzen
            </Btn>
          )}
          {resetStep === 1 && (
            <div className="rounded-[var(--radius-sm)] border border-red-200 bg-red-50 p-3">
              <p className="text-sm text-red-800 mb-2">
                Wirklich alle Daten löschen? Datensätze und Kalibrierung gehen verloren.
              </p>
              <div className="flex items-center gap-2">
                <Btn variant="secondary" onClick={() => setResetStep(0)}>Abbrechen</Btn>
                <Btn variant="danger" onClick={() => setResetStep(2)}>Fortfahren</Btn>
              </div>
            </div>
          )}
          {resetStep === 2 && (
            <div className="rounded-[var(--radius-sm)] border border-red-300 bg-red-50 p-3">
              <p className="text-sm font-medium text-red-800 mb-2">
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
  );
}
