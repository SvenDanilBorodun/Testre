// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Roboter aktivieren" — the one control on the Startseite, and the only thing
// in the product that puts a physical arm in motion without a student having
// opened a task tab first.
//
// WHY A BUTTON EXISTS ON A PAGE THAT DOCUMENTS HAVING NO CTA. HomePage's own
// header says it reports and does not dispatch, and HomePage.test.js pins the
// absence of a recording entry point across four capability fixtures. That
// invariant is about NAVIGATION and about `capabilities.recordable`: the old
// „Aufnahme starten" hero button sent the student to a tab that is hidden on
// three of the four shipped profiles, which made the page dead on the whole
// Roboter-Studio line. This button navigates nowhere and is gated on NO
// capability — every rig, on every profile, has to be activated before it
// moves, so it is exactly as useful on an edu6 kit as on a full OMX. The
// „Aufnahme starten" assertion stays green and stays in the suite.
//
// THE THREE-STATE RULE THIS PAGE LIVES BY APPLIES HERE TOO. `status === null`
// means the agent has not answered — an older server image has no agent at all
// — and renders as „unbekannt" with the button offered anyway. Rendering
// „nicht aktiviert" there would be a claim about the rig that nobody checked,
// and greying the button out would strand a student on an image whose agent is
// simply slow to publish its first tick.

import React, { useCallback } from 'react';
import { useSelector } from 'react-redux';

import { Btn, Card, Pill } from '../EbUI';
import useRobotActivation, { ACTIVATING, ACTIVE, FAILED } from '../../hooks/useRobotActivation';
import { selectWorkflowRunning } from '../../features/workshop/workshopSlice';
import { stripSeverityPrefix, isHardError } from '../../utils/homeHealth';

const TITLE = 'Roboter aktivieren';

/**
 * The German body text for the idle state. Says what WILL happen, in the order
 * it happens, because the arm is about to move next to the student.
 */
export function idleBody(hasLeader) {
  return hasLeader
    ? 'Beim Start bewegt sich nichts. Wenn du hier drückst, fährt der '
      + 'Follower-Arm zuerst in die Grundstellung, gleicht sich dann an den '
      + 'Leader-Arm an — und erst danach folgt er deinen Bewegungen.'
    : 'Beim Start bewegt sich nichts. Wenn du hier drückst, fährt der '
      + 'Roboterarm in die Grundstellung. Halte den Bereich um den Arm frei.';
}

/**
 * Why the button is refused right now, or null when it is not.
 *
 * Pure so the reason and the disabled state cannot disagree. The running
 * checks are the same position LeaderToggle documents for its own switch: the
 * agent refuses only a CONCURRENT activation, so „not in the middle of a
 * recording" is a decision the client owns.
 */
export function blockReason({ connected, busy, taskRunning, workflowRunning }) {
  if (!connected) return 'Keine Verbindung zum Roboter.';
  if (taskRunning) return 'Läuft gerade eine Aufnahme oder eine Inferenz.';
  if (workflowRunning) return 'Im Roboter Studio läuft gerade ein Programm.';
  // NOT gated on selectCalibrationHasUnsolvedCaptures, and that is a decision,
  // not an omission. `framesCaptured` LATCHES — its own selector's docstring
  // says the caller must ALSO establish that the wizard is on screen, which by
  // construction it is not when the student is standing on the Startseite.
  // Wiring it here would leave the button permanently dead after one abandoned
  // wizard, which is a worse failure than the one it would prevent. See
  // docs/KNOWN-ISSUES.md for the residual (a re-home during a live hand-guide)
  // and why the existing wizard-unmount cancel plus the 30 s idle re-torque
  // watchdog are what stand in for it.
  if (busy) return 'Der Roboter wird gerade aktiviert.';
  return null;
}

/**
 * Does this rig have a leader arm?
 *
 * The agent's own answer wins when it gave one. When it did not (an older
 * image omits the key), ask the CAPABILITY MANIFEST — `has_leader` is on
 * /task/status and is a checked fact, not a guess. Only with neither do we
 * assume `true`, which is the safe direction: it hides the re-home button
 * rather than offering a home into a rail the leader broadcaster owns.
 */
export function resolveHasLeader(status, caps) {
  if (status && typeof status.hasLeader === 'boolean') return status.hasLeader;
  if (caps && typeof caps.has_leader === 'boolean') return caps.has_leader;
  return true;
}

/**
 * The header pill. UNKNOWN is `neutral`, never amber: amber on this page means
 * „we looked and found something", and an agent that has not published yet is
 * not a finding. Same rule utils/homeHealth enforces for its rows.
 */
export function pillTone(state, busy, warned = false) {
  if (state === ACTIVE) return warned ? 'amber' : 'success';
  if (state === FAILED) return 'danger';
  if (busy || state === ACTIVATING) return 'amber';
  if (!state) return 'neutral';
  return 'amber';
}

export function pillLabel(state, busy, warned = false) {
  if (state === ACTIVE) return warned ? 'aktiv mit Hinweis' : 'aktiv';
  if (state === FAILED) return 'fehlgeschlagen';
  if (busy || state === ACTIVATING) return 'wird aktiviert …';
  if (!state) return 'unbekannt';
  return 'nicht aktiv';
}

export default function ActivationCard() {
  const heartbeatStatus = useSelector((s) => s.tasks.heartbeatStatus);
  // The classroom Jetson OWNS its own lifecycle — its compose runs
  // `restart: unless-stopped` and its agent drives the follower to
  // SAFE_HOME_JOINTS on every CLAIM, which is already a logged-in student's
  // deliberate action. So on a Jetson there is nothing here to activate, and
  // showing „nicht aktiv" over an arm the agent has already homed would be a
  // lie. Don't conflate the two lifecycles — the same rule the compose
  // `restart:` carve-out states.
  const jetsonConnected = useSelector((s) => s.jetson?.status === 'connected');
  const taskRunning = useSelector((s) => s.tasks.taskStatus.running);
  const workflowRunning = useSelector((s) => (s.workshop ? selectWorkflowRunning(s) : false));
  const caps = useSelector((s) => s.tasks.taskStatus.capabilities);
  const connected = heartbeatStatus === 'connected';

  const { status, activate, calling, error } = useRobotActivation({
    enabled: connected && !jetsonConnected,
  });

  const state = status?.state ?? null;
  const busy = state === ACTIVATING || calling;
  const isActive = state === ACTIVE;
  const reason = blockReason({ connected, busy, taskRunning, workflowRunning });
  const hasLeader = resolveHasLeader(status, caps);
  // Once teleop is live the leader OWNS /leader/joint_trajectory at 100 Hz: a
  // home trajectory onto the same rail is overwritten within 10 ms and the
  // follower snaps back to the leader. Homing a teleoperating rig means moving
  // the LEADER arm, by hand. So the re-home offer disappears — the agent
  // refuses it too, in German, for anything that calls the service directly.
  const teleopOwnsTheArm = isActive && hasLeader;

  const onClick = useCallback(() => { activate(); }, [activate]);

  if (jetsonConnected) return null;

  // The agent's own message wins whenever it has one: it is the only party
  // that knows WHY a home glide was refused (a joint on the position-map edge,
  // a table-floor refusal, a dead leader). `error` is this client's own
  // failure to even reach it.
  const rawDetail = error || status?.message || '';
  // The agent's messages carry this codebase's LOG tags ([FEHLER]/[WARNUNG]) —
  // literally what german-strings-lint greps for. No other student-facing
  // string in the SPA shows them, so strip for display and keep the severity,
  // reusing the Health-Check's own helpers so the two surfaces cannot drift.
  const detail = stripSeverityPrefix(rawDetail);
  const detailIsBad = !!error || isHardError(rawDetail);
  // „aktiv" with a warning attached is not „alles gut": a home that did not
  // arrive still ends in STATE_ACTIVE, and a green pill over a blocked arm is
  // the one thing this page's doctrine forbids.
  const warned = !!rawDetail && rawDetail.trim().startsWith('[WARNUNG]');

  return (
    <Card
      title={TITLE}
      right={(
        <Pill tone={pillTone(state, busy, warned)} dot={isActive && !warned}>
          {pillLabel(state, busy, warned)}
        </Pill>
      )}
    >
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="min-w-0 flex-1">
          <p className="text-[13px] leading-snug text-[var(--ink-2)]">
            {isActive
              ? (hasLeader
                ? 'Der Roboter ist aktiv. Der Follower-Arm folgt dem Leader-Arm.'
                // NOT „…und steht in der Grundstellung": arrival is verified
                // softly, and an incomplete home still ends in STATE_ACTIVE.
                // The Hinweis under this line says what actually happened.
                : 'Der Roboter ist aktiv.')
              : idleBody(hasLeader)}
          </p>
          {detail && (
            <p
              className={`text-[12.5px] leading-snug mt-2 ${
                state === FAILED || detailIsBad
                  ? 'text-[color:var(--danger)]'
                  : 'text-[color:var(--amber)]'
              }`}
            >
              {detail}
            </p>
          )}
          {reason && !busy && (
            <p className="text-[12.5px] leading-snug mt-2 text-[var(--ink-3)]">{reason}</p>
          )}
        </div>
        <div className="shrink-0">
          {teleopOwnsTheArm ? (
            <p className="text-[12.5px] leading-snug text-[var(--ink-3)] max-w-[26ch]">
              Zum Zurücksetzen führst du den Leader-Arm von Hand in die
              Grundstellung.
            </p>
          ) : (
          <Btn
            variant={isActive ? 'secondary' : 'primary'}
            size={isActive ? 'md' : 'lg'}
            onClick={onClick}
            disabled={!!reason}
            title={reason || undefined}
          >
            {busy
              ? 'Roboter wird aktiviert …'
              : (isActive ? 'Grundstellung erneut anfahren' : TITLE)}
          </Btn>
          )}
        </div>
      </div>
    </Card>
  );
}
