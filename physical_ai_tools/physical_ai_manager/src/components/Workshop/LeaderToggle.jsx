/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/*
 * Roboter Studio leader toggle.
 *
 * The student does Roboter Studio here, but only the GUI (EduBotics.exe) can
 * drive Docker. This control calls the GUI's localhost control bridge
 * (roboter_studio_control.py) to switch the arm between FOLLOWER-ONLY (leader
 * off — autonomous picking, no teleop contention) and BOTH-ARMS (leader on —
 * teleop / recording). Recreating only the open_manipulator container keeps
 * rosbridge + this app connected, so the student just sees the arm blip + a
 * "wird vorbereitet" overlay while it re-homes (~15-20 s); the native camera
 * bridge reconnects on its own.
 *
 * Self-hiding: on Jetson / cloud there is no GUI bridge, so the status probe
 * fails and the component renders nothing.
 */

import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import ROSLIB from 'roslib';
import TaskPhase from '../../constants/taskPhases';
import rosConnectionManager from '../../utils/rosConnectionManager';
import { rsControlBase, usePiMode } from '../../utils/piMode';

// Switching the leader on/off recreates the open_manipulator container, which
// blips /joint_states + the camera topics. Doing that DURING an active
// recording or inference run corrupts the episode (review M2), so the toggle is
// hard-disabled while the task is in any of these phases.
const TASK_BUSY_PHASES = new Set([
  TaskPhase.RECORDING,
  TaskPhase.INFERENCING,
  TaskPhase.INFERENCE_LOADING,
]);

// Where the Roboter-Studio control bridge lives (rsControlBase, utils/piMode):
//   • Windows: the GUI's loopback bridge http://localhost:8769
//     (roboter_studio_control.py) — localhost resolves to the GUI process even
//     though this app is served from the container.
//   • Orange Pi: the SAME contract served SAME-ORIGIN via /api/system (from a
//     remote browser localhost:8769 would resolve to the student's own PC).
// The base is derived from the LIVE `piMode` context value (not the module
// cache) and the first probe waits for `piModeResolved`, so a boot-window probe
// on a Pi never mis-routes to the loopback base before the marker resolves.
// The /status probe is fast; the leader toggle handler runs `docker compose up
// --force-recreate` SYNCHRONOUSLY on the GUI side (~15-180 s) before replying —
// so the POST needs a long timeout, else every successful toggle looks failed.
const STATUS_TIMEOUT_MS = 4000;
const TOGGLE_TIMEOUT_MS = 210000;
// Light background re-probe so the badge tracks the real arm mode even when it
// was flipped from another browser tab / another surface (or a restart is in
// flight on the GUI side). Cheap localhost GET; matches the GUI as source of
// truth. Paused while THIS tab is mid-toggle so it doesn't clobber busyMsg.
const STATUS_POLL_MS = 8000;

// Readiness gate (Option A). `docker compose up -d` returns the moment the arm
// container STARTS — but the follower then re-homes (~15-20 s) and the camera
// topics blip and reconnect. The old code cleared the blocking overlay when the
// POST returned, leaving the student on a frozen camera with clickable controls
// on a not-yet-ready arm. We now HOLD the overlay after the POST until the arm is
// genuinely back: the scene camera is delivering frames AGAIN, /joint_states is
// flowing AGAIN, and the follower has SETTLED (re-home finished). A hard cap ends
// the wait with a manual „Trotzdem fortfahren" so a missed signal never traps the
// student. All browser-observed (rosbridge stays up — physical_ai_server is
// recreated with --no-deps), so no GUI change is needed.
const PREP_MIN_MS = 4000;          // ignore the first few s (old topics dropping)
const PREP_CAM_LIVE_MS = 3000;     // a scene frame within this window = live
const PREP_JOINT_LIVE_MS = 3000;   // a /joint_states within this window = flowing
const PREP_SETTLE_DELTA_RAD = 0.01; // per-joint step below this = "not moving"
const PREP_SETTLE_SAMPLES = 4;     // consecutive still samples → re-home done
// „settled" alone is not enough: /joint_states starts publishing the STATIC
// power-on pose a beat BEFORE the re-home quintic begins, so a naive settle can
// clear the overlay just before the arm lurches. We require the arm to have been
// SEEN MOVING (re-home witnessed) before a settle counts — OR, for the
// already-at-home case where it barely moves, a time floor.
const PREP_MOVE_FLOOR_MS = 12000;
const PREP_CAP_MS = 90000;         // give up waiting → offer manual continue
const PREP_TICK_MS = 500;
const PREP_SCENE_TOPIC = '/scene/image_raw/compressed';
const PREP_JOINTS_TOPIC = '/joint_states';

async function rsFetch(base, path, options = {}, timeoutMs = STATUS_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base}${path}`, { ...options, signal: ctrl.signal });
    const body = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, body };
  } finally {
    clearTimeout(timer);
  }
}

const overlayStyle = {
  position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
  display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999,
};
const boxStyle = {
  background: '#fff', borderRadius: 10, padding: '28px 36px', maxWidth: 480,
  textAlign: 'center', boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
};
const modalStyle = { ...boxStyle, textAlign: 'left' };

export default function LeaderToggle({ isActive }) {
  // Pi mode from context (never the synchronous module cache): `piMode` selects
  // the control base and `piModeResolved` gates the FIRST probe so it can't fire
  // with the loopback default during the boot window on a Pi.
  const { piMode, piModeResolved } = usePiMode();
  // available: null = probing, false = no bridge (Jetson/cloud → hidden), true = present
  const [available, setAvailable] = useState(null);
  const [followerOnly, setFollowerOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState('');
  const [showReconnect, setShowReconnect] = useState(false);
  // Post-toggle readiness gate (Option A). `preparing` keeps the blocking overlay
  // up after the POST returns until the arm is really back; `prepTimedOut` offers
  // a manual continue once the hard cap is hit.
  const [preparing, setPreparing] = useState(false);
  const [prepTimedOut, setPrepTimedOut] = useState(false);
  const prepStartRef = useRef(0);
  const lastCamRef = useRef(0);
  const lastJointRef = useRef(0);
  const settledCountRef = useRef(0);
  const prevJointPosRef = useRef(null);
  // True once the follower has actually MOVED during this prep (re-home seen), so
  // a settle can't clear the overlay during the static pre-re-home window.
  const movedRef = useRef(false);

  // Block the container-recreating toggle during an active recording/inference
  // run — restarting open_manipulator mid-run blips /joint_states + the camera
  // topics and corrupts the episode (review M2).
  const taskPhase = useSelector((s) => s.tasks?.taskStatus?.phase);
  const taskBusy = TASK_BUSY_PHASES.has(taskPhase);

  // Type-aware hide: an omx_follower rig has NO leader, so the toggle is
  // meaningless there. `has_leader === false` hides it; omx_full / undefined
  // (pre-capability image, pre-first-tick) still render (`=== false` only).
  const caps = useSelector((s) => s.tasks?.taskStatus?.capabilities);

  // Mirror local busy into a ref so the background poll can skip ticks while
  // THIS tab is mid-toggle (the poll must not clear our "wird vorbereitet"
  // overlay early on a status race).
  const busyRef = useRef(false);
  useEffect(() => { busyRef.current = busy; }, [busy]);
  // Mirror `preparing` so the background status poll skips ticks while we hold
  // the readiness overlay (a refreshStatus mid-prep could flip `available` and
  // unmount the overlay).
  const preparingRef = useRef(false);
  useEffect(() => { preparingRef.current = preparing; }, [preparing]);

  const refreshStatus = useCallback(async () => {
    try {
      const { ok, body } = await rsFetch(rsControlBase(piMode), '/roboter-studio/status');
      if (ok) {
        setAvailable(true);
        setFollowerOnly(!!body.follower_only);
        setBusy(!!body.busy);
      } else {
        setAvailable(false);
      }
    } catch (e) {
      setAvailable(false);
    }
  }, [piMode]);

  useEffect(() => {
    // Hold the first probe until Pi mode has resolved (piModeResolved), so
    // rsControlBase(piMode) never routes to the loopback base during the Pi boot
    // window. A missing provider resolves immediately (default context), so
    // Windows behaviour is unchanged.
    if (!isActive || !piModeResolved) return undefined;
    refreshStatus();
    // Light background re-probe so a flip from another tab/surface is reflected
    // here. Skipped while this tab is mid-toggle (busyRef) to protect the
    // overlay.
    const intervalId = setInterval(() => {
      if (!busyRef.current && !preparingRef.current) refreshStatus();
    }, STATUS_POLL_MS);
    return () => clearInterval(intervalId);
  }, [isActive, piModeResolved, refreshStatus]);

  const doToggle = useCallback(async (disable) => {
    setBusy(true);
    setBusyMsg(disable
      ? 'Leader-Arm wird abgeschaltet — Roboter Studio wird vorbereitet …'
      : 'Leader-Arm wird verbunden — Teleoperation wird vorbereitet …');
    let enteredPrep = false;
    try {
      const path = disable ? '/roboter-studio/leader-disable' : '/roboter-studio/leader-enable';
      const { ok, body } = await rsFetch(rsControlBase(piMode), path, { method: 'POST' }, TOGGLE_TIMEOUT_MS);
      if (ok && body.ok) {
        setFollowerOnly(disable);
        toast.success(body.message || (disable ? 'Roboter Studio bereit.' : 'Leader verbunden.'));
        // The POST returns when the arm container STARTS, not when it is ready
        // (it still re-homes + the camera reconnects). Enter the „preparing"
        // phase and HOLD the blocking overlay until the arm is genuinely back
        // (readiness effect below), so the student can't click a frozen camera /
        // a still-homing arm.
        prepStartRef.current = Date.now();
        lastCamRef.current = 0;
        lastJointRef.current = 0;
        settledCountRef.current = 0;
        prevJointPosRef.current = null;
        movedRef.current = false;
        setPrepTimedOut(false);
        setPreparing(true);
        enteredPrep = true;
      } else {
        toast.error(body.message || 'Moduswechsel fehlgeschlagen.');
      }
    } catch (e) {
      toast.error('Die Roboter-Studio-Steuerung ist nicht erreichbar.');
    } finally {
      setBusy(false);
      setBusyMsg('');
      // Failed toggle (no prep phase) → re-sync the badge shortly after; a
      // successful one refreshes the badge when the readiness effect clears.
      if (!enteredPrep) setTimeout(refreshStatus, 2000);
    }
  }, [refreshStatus, piMode]);

  // Readiness effect (Option A): while „preparing", watch the browser-observed
  // signals that the arm container is back — the scene camera delivering frames
  // AGAIN, /joint_states flowing AGAIN, and the follower SETTLED (re-home done) —
  // and clear the overlay only when all hold past a minimum settle. rosbridge
  // stays up during the arm-only restart, so these subscriptions persist across
  // the publisher blip and start receiving again when the arm returns. A hard cap
  // flips `prepTimedOut` so the student can always continue manually.
  useEffect(() => {
    if (!preparing) return undefined;
    const ros = rosConnectionManager?.ros;
    let camSub = null;
    let jointSub = null;
    if (ros && ros.isConnected) {
      try {
        camSub = new ROSLIB.Topic({
          ros, name: PREP_SCENE_TOPIC,
          messageType: 'sensor_msgs/CompressedImage',
          throttle_rate: 500, queue_length: 1,
        });
        camSub.subscribe(() => { lastCamRef.current = Date.now(); });
      } catch (_) { /* subscribe failure → the cap fallback still applies */ }
      try {
        jointSub = new ROSLIB.Topic({
          ros, name: PREP_JOINTS_TOPIC,
          messageType: 'sensor_msgs/msg/JointState',
          throttle_rate: 150, queue_length: 1,
        });
        jointSub.subscribe((msg) => {
          lastJointRef.current = Date.now();
          const pos = Array.isArray(msg?.position) ? msg.position : null;
          const prev = prevJointPosRef.current;
          if (pos && prev && pos.length === prev.length) {
            let maxD = 0;
            for (let i = 0; i < pos.length; i += 1) {
              const d = Math.abs(pos[i] - prev[i]);
              if (d > maxD) maxD = d;
            }
            if (maxD < PREP_SETTLE_DELTA_RAD) {
              settledCountRef.current += 1;
            } else {
              settledCountRef.current = 0;
              movedRef.current = true; // re-home motion witnessed
            }
          }
          if (pos) prevJointPosRef.current = pos.slice();
        });
      } catch (_) { /* subscribe failure → the cap fallback still applies */ }
    }
    const id = setInterval(() => {
      const now = Date.now();
      const elapsed = now - prepStartRef.current;
      const camLive = lastCamRef.current > 0 && (now - lastCamRef.current) < PREP_CAM_LIVE_MS;
      const jointLive = lastJointRef.current > 0 && (now - lastJointRef.current) < PREP_JOINT_LIVE_MS;
      // Settled counts only once the arm has moved (re-home witnessed), else
      // after a time floor (covers the arm that was already at HOME and barely
      // moves) — so the overlay never clears during the static pre-re-home window.
      const settled = settledCountRef.current >= PREP_SETTLE_SAMPLES
        && (movedRef.current || elapsed >= PREP_MOVE_FLOOR_MS);
      if (elapsed >= PREP_MIN_MS && camLive && jointLive && settled) {
        setPreparing(false);
        setPrepTimedOut(false);
        refreshStatus();
      } else if (elapsed >= PREP_CAP_MS) {
        setPrepTimedOut(true);
      }
    }, PREP_TICK_MS);
    return () => {
      clearInterval(id);
      try { if (camSub) camSub.unsubscribe(); } catch (_) { /* torn down */ }
      try { if (jointSub) jointSub.unsubscribe(); } catch (_) { /* torn down */ }
    };
  }, [preparing, refreshStatus]);

  // probing, or no GUI bridge (Jetson/cloud), or a leader-less profile (omx_follower)
  if (available !== true || caps?.has_leader === false) return null;

  return (
    <div className="leader-toggle" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      {!followerOnly ? (
        <button
          type="button"
          disabled={busy || taskBusy}
          onClick={() => doToggle(true)}
          title={taskBusy
            ? 'Während Aufnahme/Inferenz nicht verfügbar.'
            : 'Schaltet den Leader-Arm ab, damit Roboter Studio den Follower allein steuert.'}
        >
          Leader abschalten (Roboter Studio)
        </button>
      ) : (
        <>
          <span style={{ color: '#1a7f37', fontWeight: 600 }}>
            ● Leader abgeschaltet — Roboter Studio aktiv
          </span>
          <button
            type="button"
            disabled={busy || taskBusy}
            onClick={() => setShowReconnect(true)}
            title={taskBusy ? 'Während Aufnahme/Inferenz nicht verfügbar.' : undefined}
          >
            Leader verbinden
          </button>
        </>
      )}

      {taskBusy && (
        <span style={{ color: '#9a6700', fontSize: 13 }}>
          Während Aufnahme/Inferenz nicht verfügbar
        </span>
      )}

      {(busy || preparing) && (
        <div style={overlayStyle}>
          <div style={boxStyle}>
            <div className="eb-pulse-dot" style={{
              width: 14, height: 14, borderRadius: '50%', background: '#2563eb',
              margin: '0 auto 12px',
            }} />
            <p style={{ margin: 0, fontSize: 15 }}>
              {preparing
                ? (prepTimedOut
                    ? 'Der Roboter braucht länger als erwartet. Du kannst weiter warten oder trotzdem fortfahren.'
                    : 'Roboter Studio wird vorbereitet — der Arm fährt in die Grundstellung und die '
                      + 'Kamera verbindet sich neu. Bitte warten …')
                : (busyMsg || 'Bitte warten …')}
            </p>
            {preparing && prepTimedOut && (
              <button
                type="button"
                style={{ marginTop: 16 }}
                onClick={() => { setPreparing(false); setPrepTimedOut(false); refreshStatus(); }}
              >
                Trotzdem fortfahren
              </button>
            )}
          </div>
        </div>
      )}

      {showReconnect && (
        <div style={overlayStyle}>
          <div style={modalStyle}>
            <h3 style={{ marginTop: 0 }}>Leader-Arm wieder verbinden</h3>
            <p>
              Bevor du den Leader-Arm verbindest: Bringe den Follower-Arm in eine
              sichere, aufrechte Grundstellung (z.&nbsp;B. mit einem
              {' '}„Heimposition"-Block) und bewege den schlaffen Leader-Arm von
              Hand in dieselbe Stellung. Wenn beide Arme gleich stehen, klicke „Verbinden".
            </p>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 16 }}>
              <button type="button" onClick={() => setShowReconnect(false)}>Abbrechen</button>
              <button
                type="button"
                disabled={busy || taskBusy}
                title={taskBusy ? 'Während Aufnahme/Inferenz nicht verfügbar.' : undefined}
                onClick={() => { setShowReconnect(false); doToggle(false); }}
              >
                Verbinden
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
