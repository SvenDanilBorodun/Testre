// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Roboter-Zustand" — a health check, not a dashboard.
//
// Explicitly NOT CPU / RAM / disk gauges. Three numbers a student cannot act
// on are three numbers that teach them to ignore this card; what they need to
// know is whether the thing in front of them will work, and if not, what to do
// about it. The decision lives in utils/homeHealth (pure, unit-tested); this
// file only renders it and supplies the one platform-dependent sentence.
//
// TWO rows make this component fetch, and both for the same reason: the Redux
// value they would otherwise read is only ever populated by ANOTHER page, so on
// Start it is an initial value masquerading as a measurement.
//
// `state.ros.imageTopicList` is written by exactly one component —
// `ImageGrid.js`, on the Aufnahme tab — and its initial state is `[]`, which is
// indistinguishable from "asked, and there are none". Reading it here would
// report „keine erkannt" on every fresh session.
//
// `setCalibrationStatus` is dispatched from exactly ONE place in the app —
// CalibrationWizard.jsx — so on the Start page those Redux flags are all
// `false` until the student has opened Roboter Studio in this session.
// Rendering „Kalibrierung fehlt" from that would be an invented claim about
// the rig, so this asks the robot once (the wizard's own comment calls the
// read cheap: a few file-existence checks) and shows „—" until it answers.
//
// Both are ONE shot per connection, never polled. Neither dispatches into
// Redux: these answers belong to this card, and writing `imageTopicList` from
// here would hand the Aufnahme tab a list it did not ask for.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import { useSelector } from 'react-redux';

import { Card, Pill } from '../EbUI';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { usePiMode, PI_PORT_BLOCKED_HINT } from '../../utils/piMode';
import { expectedCameraRoles } from '../../utils/robotIdentity';
import {
  deriveHealth, VERDICT_LABEL_DE, OK, WARN, BAD, READY, LIMITED, NOT_READY,
} from '../../utils/homeHealth';

const DOT_CLASS = {
  [OK]: 'bg-[color:var(--success)]',
  [WARN]: 'bg-[color:var(--amber)]',
  [BAD]: 'bg-[color:var(--danger)]',
};

const VERDICT_TONE = { [READY]: 'success', [LIMITED]: 'amber', [NOT_READY]: 'danger' };

/**
 * The German next step for a dead bridge, per platform.
 *
 * On an Orange Pi the Windows wording („Umgebung starten") names a desktop app
 * the student does not have; `HeartbeatStatus` already makes exactly this
 * distinction, and the wording is shared from utils/piMode so the two surfaces
 * cannot drift apart.
 */
export function disconnectedHintDe({ piMode, robotTierUp }) {
  if (piMode) {
    return robotTierUp
      ? PI_PORT_BLOCKED_HINT
      : 'Die Roboter-Umgebung läuft nicht. Starte sie im Tab „System".';
  }
  return 'Die Umgebung läuft nicht. Öffne EduBotics auf dem Desktop und klicke „Umgebung starten".';
}

export default function HealthCard({ jointsLive = null, cloudOnly = false }) {
  const { piMode, agentStatus } = usePiMode();
  const { getCalibrationStatus, getImageTopicList } = useRosServiceCaller();

  const heartbeatStatus = useSelector((s) => s.tasks.heartbeatStatus);
  const caps = useSelector((s) => s.tasks.taskStatus.capabilities);
  const errorText = useSelector((s) => s.tasks.taskStatus.error);

  const connected = heartbeatStatus === 'connected';
  // Both null until the robot answers. `calibration` is never an object of
  // falses and `cameraCount` is never 0 before the service replies — either
  // would read as "measured, and there is nothing".
  const [calibration, setCalibration] = useState(null);
  const [cameraCount, setCameraCount] = useState(null);
  const askedForRef = useRef(null);

  // One shot per connection. Re-asked when the bridge comes back, because a
  // reconnect usually means the stack restarted and the answer may have
  // changed; NOT polled, in line with the rest of this page.
  useEffect(() => {
    if (cloudOnly || !connected) {
      setCalibration(null);
      setCameraCount(null);
      askedForRef.current = null;
      return undefined;
    }
    if (askedForRef.current === heartbeatStatus) return undefined;
    askedForRef.current = heartbeatStatus;

    let cancelled = false;
    (async () => {
      try {
        const res = await getCalibrationStatus();
        if (cancelled || !res) return;
        setCalibration({
          intrinsic: !!res.has_scene_intrinsics,
          handeye: !!res.has_scene_handeye,
          table: !!res.has_table_plane,
        });
      } catch {
        // Leave it null: „—" is the honest answer to a question that failed.
        if (!cancelled) setCalibration(null);
      }
    })();
    (async () => {
      try {
        const res = await getImageTopicList();
        if (cancelled) return;
        // `success === false` is the service reporting it could not look, which
        // is unknown — NOT zero cameras. Only a successful reply counts.
        if (res && res.success) {
          setCameraCount((res.image_topic_list || []).length);
        } else {
          setCameraCount(null);
        }
      } catch {
        if (!cancelled) setCameraCount(null);
      }
    })();
    return () => { cancelled = true; };
  }, [cloudOnly, connected, heartbeatStatus, getCalibrationStatus, getImageTopicList]);

  const health = useMemo(() => {
    const roles = expectedCameraRoles(caps);
    return deriveHealth({
      connected,
      jointsLive,
      cameraCount,
      expectedCameras: roles ? roles.length : null,
      calibration,
      // A profile without Roboter Studio never runs the calibration wizard, so
      // the row would be permanently „—". Absent caps keep it visible: hiding a
      // row on missing information is itself a claim.
      showCalibration: caps ? caps.roboter_studio !== false : true,
      errorText,
      cloudOnly,
    });
  }, [connected, jointsLive, cameraCount, caps, calibration, errorText, cloudOnly]);

  const hint = health.hint
    ? {
      ...health.hint,
      body: health.hint.body
        || disconnectedHintDe({ piMode, robotTierUp: !!(agentStatus && agentStatus.robot_tier_up) }),
    }
    : null;

  return (
    <Card
      title="Roboter-Zustand"
      right={<Pill tone={VERDICT_TONE[health.verdict]} dot>{VERDICT_LABEL_DE[health.verdict]}</Pill>}
      className="h-full"
    >
      <ul className="-my-2">
        {health.rows.map((row) => (
          <li
            key={row.key}
            className="flex items-center gap-2.5 py-2 border-b border-dashed border-[var(--line)] last:border-b-0"
          >
            <span
              className={clsx('w-[7px] h-[7px] rounded-full shrink-0',
                DOT_CLASS[row.state] || 'bg-[var(--ink-4)]')}
              aria-hidden="true"
            />
            <span className="text-sm text-[var(--ink-2)] flex-1 min-w-0">{row.label}</span>
            <span className="font-mono text-xs text-[var(--ink-3)] text-right">{row.value}</span>
          </li>
        ))}
      </ul>

      {hint && (
        <div
          className={clsx(
            'mt-4 rounded-[var(--radius)] p-3',
            hint.tone === BAD
              ? 'bg-[var(--danger-wash)] text-[color:var(--danger)]'
              : 'bg-[var(--amber-wash)] text-[color:var(--amber)]',
          )}
        >
          <div className="text-[13px] font-semibold leading-snug">{hint.title}</div>
          {hint.body && <p className="text-[12.5px] leading-snug mt-1 opacity-90">{hint.body}</p>}
        </div>
      )}
    </Card>
  );
}
