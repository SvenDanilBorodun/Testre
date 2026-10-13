// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The cloud target's right panel (spec §6.3–§6.5): the Leader-Schalter, the
// privacy line, the models, the reservation and its summary, today's Cloud-Zeit,
// Start/Stopp and the run's facts. The session itself is the module-level
// controller (features/cloudInference/controller.js); this component only shows
// it and forwards clicks. No ControlPanel here: its Start sends START_INFERENCE
// with a local policy path.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSelector } from 'react-redux';

import TaskPhase from '../../constants/taskPhases';
import LeaderToggle from '../Workshop/LeaderToggle';
import Icon from '../icons/Icon';
import { Key } from '../sucher';
import useRsBridgeStatus from '../../hooks/useRsBridgeStatus';
import useSupabaseTrainings from '../../hooks/useSupabaseTrainings';
import useCloudInferenceSession from '../../hooks/useCloudInferenceSession';
import { getInferenceUsage } from '../../services/cloudInferenceApi';
import * as controller from '../../features/cloudInference/controller';
import CloudModelList, { cloudModelRows } from './CloudModelList';
import CloudRunFacts from './CloudRunFacts';
import CloudWait from './CloudWait';
import { cloudCopy, fill } from './cloudCopy';

// The Cloud-Zeit light bar's segments (Sucher).
const METER_SEGMENTS = 8;
const START_PHASES = new Set(['idle', 'reserved', 'ended']);
const BUSY_PHASES = new Set(['arming', 'starting', 'running', 'stopping']);
const INFERENCE_PHASES = new Set([TaskPhase.INFERENCE_LOADING, TaskPhase.INFERENCING]);

/** The camera names the robot publishes, from /image/get_available_list ('/gripper/image_raw' -> 'gripper'). */
export function robotCameraNames(imageTopicList) {
  const names = new Set();
  (Array.isArray(imageTopicList) ? imageTopicList : []).forEach((topic) => {
    const first = String(topic || '').split('/').filter(Boolean)[0];
    if (first) names.add(first);
  });
  return names;
}

/** The model's cameras the robot does not publish — [] while either side is unknown. */
export function missingCameras(summary, imageTopicList) {
  const expected = Array.isArray(summary?.expected_cameras) ? summary.expected_cameras : [];
  const have = robotCameraNames(imageTopicList);
  if (expected.length === 0 || have.size === 0) return [];
  return expected.filter((name) => !have.has(name));
}

function minutes(seconds) {
  return typeof seconds === 'number' && Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds / 60)) : null;
}

/**
 * @param {object} p
 * @param {boolean} p.isActive
 * @param {{enabled: boolean, reason: ?string}} p.cloud   cloudTargetState's answer
 * @param {?object} p.cloudState   useCloudInferenceState's answer
 */
export default function CloudInferencePanel({ isActive = true, cloud, cloudState }) {
  const session = useCloudInferenceSession();
  const accessToken = useSelector((s) => s.auth.session?.access_token);
  const taskRunning = useSelector((s) => s.tasks.taskStatus.running);
  const robotPhase = useSelector((s) => s.tasks.taskStatus.phase);
  // The robot sprite on the waiting card (presentation only).
  const robotProfile = useSelector((s) => s.tasks.taskStatus.robotProfile || s.tasks.taskStatus.robotType);
  const taskInfo = useSelector((s) => s.tasks.taskInfo);
  const imageTopicList = useSelector((s) => s.ros.imageTopicList);
  const { jobs, loading } = useSupabaseTrainings();
  const bridge = useRsBridgeStatus({ enabled: isActive });

  const [selectedId, setSelectedId] = useState(null);
  const [usage, setUsage] = useState(null);

  // Refs so the controller always reads the CURRENT values when its timers fire.
  const tokenRef = useRef(accessToken);
  tokenRef.current = accessToken;
  const runningRef = useRef(taskRunning);
  runningRef.current = taskRunning;
  const taskInfoRef = useRef(taskInfo);
  taskInfoRef.current = taskInfo;
  const getAccessToken = useCallback(() => tokenRef.current, []);
  const isTaskRunning = useCallback(() => Boolean(runningRef.current), []);
  const getTaskInfo = useCallback(() => taskInfoRef.current, []);

  // Unmount (target switch, page change) lets a reservation go; never a run.
  useEffect(() => () => controller.release(), []);

  // Every /task/status phase while a run is live (the robot ended it -> /end).
  useEffect(() => {
    controller.onRobotPhase(robotPhase);
  }, [robotPhase]);

  // Today's Cloud-Zeit: on mount and whenever a run or reservation settles.
  const settled = session.phase === 'idle' || session.phase === 'ended' || session.phase === 'reserved';
  useEffect(() => {
    if (!accessToken || !settled) return undefined;
    let cancelled = false;
    getInferenceUsage(accessToken).then(
      (u) => { if (!cancelled) setUsage(u || null); },
      () => { if (!cancelled) setUsage(null); },
    );
    return () => { cancelled = true; };
  }, [accessToken, settled, session.phase]);

  const rows = useMemo(() => cloudModelRows(jobs), [jobs]);
  const selectedJob = rows.find((j) => j.id === selectedId) || null;
  const busy = BUSY_PHASES.has(session.phase);
  const rowsDisabled = busy || Boolean(taskRunning);
  // Owner, round 2: from the reserve until the start ends (success or the final
  // error after the automatic retries) the list is locked; no click reserves again.
  const gpuStarting = controller.isStartingGpu(session);

  const onSelect = useCallback((job) => {
    if (rowsDisabled || gpuStarting) return;
    if (job.id === selectedId) {
      setSelectedId(null);
      controller.release();
      return;
    }
    setSelectedId(job.id);
    controller.selectModel(getAccessToken, job.id, isTaskRunning);
  }, [rowsDisabled, gpuStarting, selectedId, getAccessToken, isTaskRunning]);

  const summary = session.trainingId === selectedId ? session.summary : null;
  const missing = missingCameras(summary, imageTopicList);
  const refused = session.modelRefused && session.trainingId === selectedId;
  const leaderOn = Boolean(bridge.leaderOn);
  const orphanRun = Boolean(taskRunning) && !busy && INFERENCE_PHASES.has(robotPhase);
  const cloudEnabled = cloud ? cloud.enabled !== false : true;

  const startDisabled =
    !cloudEnabled
    || !selectedJob
    || selectedJob.model_type !== 'act'
    || leaderOn
    || missing.length > 0
    || Boolean(taskRunning)
    || !START_PHASES.has(session.phase)
    || refused;

  const onStart = () => {
    if (startDisabled) return;
    controller.start({ getAccessToken, getTaskInfo, trainingId: selectedJob.id });
  };
  const onStop = () => controller.stop();
  const onStopOrphan = () => controller.stopOrphan({ getAccessToken, liveSessionId: usage?.live_session_id || null });

  let hint = null;
  if (!cloudEnabled && cloud?.reason) hint = cloudCopy.disabled[cloud.reason];
  else if (taskRunning && !orphanRun && !busy) hint = cloudCopy.panel.runningElsewhere;
  else if (!selectedJob && START_PHASES.has(session.phase)) hint = cloudCopy.panel.chooseModel;

  const remainingMin = minutes(usage?.remaining_today_s);
  const sessionMin = minutes(usage?.session_cap_s);
  // The Cloud-Zeit light bar (Sucher): drawn only from numbers the panel
  // already has (today's remaining seconds and the daily cap), else nothing.
  const usedSegments = (typeof usage?.remaining_today_s === 'number'
    && typeof usage?.daily_cap_s === 'number' && usage.daily_cap_s > 0)
    ? Math.max(0, Math.min(METER_SEGMENTS, Math.round(
      ((usage.daily_cap_s - usage.remaining_today_s) / usage.daily_cap_s) * METER_SEGMENTS)))
    : null;
  // „Modell in der Cloud ausführen" with „Cloud" in italics, split at render.
  const titleParts = cloudCopy.panel.title.split('Cloud');

  return (
    <div className="su-cip h-full overflow-y-auto text-[var(--ink)] p-5 flex flex-col gap-4">
      <div>
        <h2 className="su-cip__title">
          <Icon name="cloud" size={22} className="su-cip__titleicon" />
          <span className="su-serif">{titleParts.length === 2
            ? <>{titleParts[0]}<em>Cloud</em>{titleParts[1]}</>
            : cloudCopy.panel.title}</span>
        </h2>
        <p className="mt-2 text-[14.5px] text-[var(--ink-2)]">{cloudCopy.panel.intro}</p>
      </div>

      <div className="flex flex-col gap-2">
        <div className="su-cip__leader su-plate su-plate--well"><LeaderToggle isActive={isActive} /></div>
        {leaderOn && (
          <div role="status" className="flex items-start gap-2 text-sm text-[color:var(--amber)]">
            <Icon name="warning" size={16} className="mt-0.5" />
            <span>{cloudCopy.leader.hint}</span>
          </div>
        )}
      </div>

      <p className="text-[13px] leading-snug text-[var(--ink-2)]">{cloudCopy.panel.privacy}</p>

      <CloudModelList
        jobs={jobs}
        loading={loading}
        selectedId={selectedId}
        disabled={rowsDisabled}
        lockedReason={gpuStarting ? cloudCopy.models.gpuStarting : null}
        onSelect={onSelect}
      />

      {session.phase === 'reserving' && session.trainingId === selectedId && (
        <CloudWait
          kind="gpu"
          startedAt={session.reserveStartedAt}
          attempt={session.reserveAttempt}
          attempts={controller.RESERVE_ATTEMPTS}
          robotProfile={robotProfile}
        />
      )}

      {summary && (
        <div className="text-sm flex flex-col gap-1" data-testid="cloud-summary">
          {Array.isArray(summary.expected_cameras) && summary.expected_cameras.length > 0 && (
            <div>{fill(cloudCopy.summary.cameras, { list: summary.expected_cameras.join(', ') })}</div>
          )}
          {typeof summary.trained_fps === 'number' && (
            <div>{fill(cloudCopy.summary.fps, { fps: Math.round(summary.trained_fps) })}</div>
          )}
          {missing.length > 0 && (
            <div role="alert" className="text-[color:var(--danger)]">
              {fill(cloudCopy.summary.camerasMissing, { missing: missing.join(', ') })}
            </div>
          )}
        </div>
      )}

      {session.message && (
        <div role="status" className="text-sm text-[var(--ink-2)]" data-testid="cloud-message">{session.message}</div>
      )}

      <div className="text-[13.5px] text-[var(--ink)]" data-testid="cloud-usage">
        {remainingMin !== null && sessionMin !== null
          ? fill(cloudCopy.usage.line, { min: remainingMin, session: sessionMin })
          : cloudCopy.usage.unknown}
      </div>
      {usedSegments !== null && (
        <div className="su-cip__meter" aria-hidden="true">
          {Array.from({ length: METER_SEGMENTS }, (_, i) => (
            <span key={i} className={i < usedSegments ? 'su-cip__seg su-cip__seg--used' : 'su-cip__seg'} />
          ))}
        </div>
      )}

      <div className="flex flex-col gap-1.5">
        {session.phase === 'running' || session.phase === 'stopping' ? (
          <Key
            variant="danger"
            size="lg"
            onClick={onStop}
            disabled={session.phase === 'stopping'}
            className="w-full"
          >
            <Icon name={session.phase === 'stopping' ? 'loading' : 'stop'} size={16}
              className={session.phase === 'stopping' ? 'animate-spin' : ''} />
            {session.phase === 'stopping' ? cloudCopy.panel.stopping : cloudCopy.panel.stop}
          </Key>
        ) : orphanRun ? (
          <Key variant="danger" size="lg" onClick={onStopOrphan} className="w-full">
            <Icon name="stop" size={16} />
            {cloudCopy.panel.stop}
          </Key>
        ) : (
          <Key variant="signal" size="lg" onClick={onStart} disabled={startDisabled} className="w-full">
            <Icon name={session.phase === 'arming' || session.phase === 'starting' ? 'loading' : 'play'} size={16}
              className={session.phase === 'arming' || session.phase === 'starting' ? 'animate-spin' : ''} />
            {session.phase === 'arming' || session.phase === 'starting' ? cloudCopy.panel.starting : cloudCopy.panel.start}
          </Key>
        )}
        {hint && <div className="text-[13px] text-[var(--ink-2)]" data-testid="cloud-start-hint">{hint}</div>}
      </div>

      <CloudRunFacts state={cloudState} robotPhase={robotPhase} />
    </div>
  );
}
