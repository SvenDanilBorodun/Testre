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
import TaskPhase from '../../constants/taskPhases';

// Switching the leader on/off recreates the open_manipulator container, which
// blips /joint_states + the camera topics. Doing that DURING an active
// recording or inference run corrupts the episode (review M2), so the toggle is
// hard-disabled while the task is in any of these phases.
const TASK_BUSY_PHASES = new Set([
  TaskPhase.RECORDING,
  TaskPhase.INFERENCING,
  TaskPhase.INFERENCE_LOADING,
]);

// Fixed loopback port the GUI control bridge binds (roboter_studio_control.py
// DEFAULT_PORT). The browser runs on the Windows host, so localhost resolves to
// the GUI process even though this app is served from the container.
const RS_CONTROL_BASE = 'http://localhost:8769';
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

async function rsFetch(path, options = {}, timeoutMs = STATUS_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${RS_CONTROL_BASE}${path}`, { ...options, signal: ctrl.signal });
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
  // available: null = probing, false = no bridge (Jetson/cloud → hidden), true = present
  const [available, setAvailable] = useState(null);
  const [followerOnly, setFollowerOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState('');
  const [showReconnect, setShowReconnect] = useState(false);

  // Block the container-recreating toggle during an active recording/inference
  // run — restarting open_manipulator mid-run blips /joint_states + the camera
  // topics and corrupts the episode (review M2).
  const taskPhase = useSelector((s) => s.tasks?.taskStatus?.phase);
  const taskBusy = TASK_BUSY_PHASES.has(taskPhase);

  // Mirror local busy into a ref so the background poll can skip ticks while
  // THIS tab is mid-toggle (the poll must not clear our "wird vorbereitet"
  // overlay early on a status race).
  const busyRef = useRef(false);
  useEffect(() => { busyRef.current = busy; }, [busy]);

  const refreshStatus = useCallback(async () => {
    try {
      const { ok, body } = await rsFetch('/roboter-studio/status');
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
  }, []);

  useEffect(() => {
    if (!isActive) return undefined;
    refreshStatus();
    // Light background re-probe so a flip from another tab/surface is reflected
    // here. Skipped while this tab is mid-toggle (busyRef) to protect the
    // overlay.
    const intervalId = setInterval(() => {
      if (!busyRef.current) refreshStatus();
    }, STATUS_POLL_MS);
    return () => clearInterval(intervalId);
  }, [isActive, refreshStatus]);

  const doToggle = useCallback(async (disable) => {
    setBusy(true);
    setBusyMsg(disable
      ? 'Leader-Arm wird abgeschaltet — Roboter Studio wird vorbereitet …'
      : 'Leader-Arm wird verbunden — Teleoperation wird vorbereitet …');
    try {
      const path = disable ? '/roboter-studio/leader-disable' : '/roboter-studio/leader-enable';
      const { ok, body } = await rsFetch(path, { method: 'POST' }, TOGGLE_TIMEOUT_MS);
      if (ok && body.ok) {
        setFollowerOnly(disable);
        toast.success(body.message || (disable ? 'Roboter Studio bereit.' : 'Leader verbunden.'));
      } else {
        toast.error(body.message || 'Moduswechsel fehlgeschlagen.');
      }
    } catch (e) {
      toast.error('Die Roboter-Studio-Steuerung ist nicht erreichbar.');
    } finally {
      setBusy(false);
      setBusyMsg('');
      // The arm container re-homes for ~15-20 s; re-sync the badge shortly after.
      setTimeout(refreshStatus, 2000);
    }
  }, [refreshStatus]);

  if (available !== true) return null; // probing, or no GUI bridge (Jetson/cloud)

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

      {busy && (
        <div style={overlayStyle}>
          <div style={boxStyle}>
            <div className="eb-pulse-dot" style={{
              width: 14, height: 14, borderRadius: '50%', background: '#2563eb',
              margin: '0 auto 12px',
            }} />
            <p style={{ margin: 0, fontSize: 15 }}>{busyMsg || 'Bitte warten …'}</p>
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
