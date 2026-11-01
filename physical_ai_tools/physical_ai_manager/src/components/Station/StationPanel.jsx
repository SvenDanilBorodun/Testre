/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The camera station's card and strip (station spec v6.13, §9.2): the state
// with its German reason, „Station verlassen" (or „Station starten" after
// leaving, §9.1), and three steps — „Kamera prüfen" (runs by itself, the button
// repeats it), „Roboter prüfen" (hand-guide onto a dot) and, after a MOVED
// result, „Kamera neu ausrichten (Lehrkraft)" (a station worker job, polled).
// On an unconfigured station only „Station einrichten (Lehrkraft)", which opens
// the commissioning wizard.
//
// `compact`: the strip under the Roboter-Studio header once the station is
// configured (a stable, keyed sibling, so the editor never remounts); without
// it, the full card in the slot the manual CalibrationWizard uses. Same
// content either way. Everything it shows comes from the station controller.

import React, { useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';

import { Key, Led } from '../sucher';
import Icon from '../icons/Icon';
import StationRobotCheck from './StationRobotCheck';
import * as station from '../../features/station/controller';
import { useStationSnapshot } from '../../features/station/useStation';
import { stationLeaveDisabled } from '../../features/station/gating';
import { JOB_STATE_DE, STATION_DE } from '../../features/station/stationCopy';

// A camera check clearly misaligned (§7.1: max > 25 mm).
const CLEARLY_MOVED_MM = 25;

function stateLine(s) {
  if (s.phase === 'entering') return { tone: 'warn', text: STATION_DE.entering, pulse: 'blink' };
  if (s.phase === 'leaving') return { tone: 'warn', text: STATION_DE.leaving, pulse: 'blink' };
  if (!s.held) return { tone: 'off', text: STATION_DE.stateIdle };
  if (s.verified) return { tone: 'ok', text: STATION_DE.stateVerified };
  if (s.serverState === 'ENTERING' || !s.sourceOk) return { tone: 'warn', text: STATION_DE.sourceWaiting, pulse: 'blink' };
  return { tone: 'sig', text: STATION_DE.stateActive };
}

const hasMetric = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));

function cameraLine(s) {
  if (s.checking === 'camera') return { tone: 'warn', text: STATION_DE.cameraRunning };
  const c = s.cameraCheck;
  const metrics = c && (c.result === 'OK' || c.result === 'MOVED') && hasMetric(c.rmsMm)
    ? STATION_DE.metrics(c.rmsMm, c.maxMm) : '';
  if (s.cameraOk) return { tone: 'ok', text: STATION_DE.cameraOk, detail: metrics };
  if (c && c.result && c.result !== 'OK') {
    const clearly = c.result === 'MOVED' && Number(c.maxMm) > CLEARLY_MOVED_MM;
    return { tone: 'rec', text: clearly ? STATION_DE.cameraClearlyMoved : c.message, detail: c.actionDe || metrics };
  }
  return { tone: 'off', text: STATION_DE.cameraOpen };
}

function robotLine(s) {
  if (s.checking === 'robot') return { tone: 'warn', text: STATION_DE.robotRunning };
  const r = s.robotCheck;
  const metrics = r && r.dotId > 0 && hasMetric(r.dotDxyMm)
    ? STATION_DE.robotMetrics(r.dotId, r.dotDxyMm, r.dotDzMm) : '';
  if (s.robotOk) return { tone: 'ok', text: STATION_DE.robotOk, detail: metrics };
  if (r && r.result && r.result !== 'OK') return { tone: 'rec', text: r.message, detail: r.actionDe || metrics };
  return { tone: 'off', text: STATION_DE.robotOpen };
}

/**
 * @param {object} p
 * @param {boolean} [p.compact]
 * @param {() => void} p.onOpenCommission  opens the commissioning wizard (an overlay)
 * @param {boolean} [p.armBusy]  another surface holds the arm (a JogPanel hand-guide)
 * @param {(on: boolean) => void} [p.onHandGuideChange]  the robot check freed the arm
 */
export default function StationPanel({
  compact = false, onOpenCommission, armBusy = false, onHandGuideChange = null,
}) {
  const s = useStationSnapshot();
  const [confirmResolve, setConfirmResolve] = useState(false);
  if (!s.active) return null;

  const line = stateLine(s);
  const notice = s.streamError || s.notice;
  const configured = s.stationConfigured === true;
  const unknown = s.stationConfigured === null;
  const canCheck = s.held && s.phase === 'held' && !s.checking;
  const showResolve = configured && s.held && (
    (s.cameraCheck && s.cameraCheck.result === 'MOVED') || s.resolveJob
  );
  const resolveRunning = s.resolveJob && !['done', 'failed', 'stale'].includes(s.resolveJob.jobState);

  const onLeave = async () => {
    const r = await station.leave();
    if (!r.left && r.message) toast.error(r.message);
  };

  const cam = cameraLine(s);
  const rob = robotLine(s);

  return (
    <section
      className={clsx(
        'su-station border-[var(--line)] bg-white',
        compact ? 'st-station-strip px-3 sm:px-4 py-2 border-b shrink-0' : 'm-4 rounded-lg border p-4 max-w-3xl',
      )}
      aria-label={STATION_DE.title}
      data-testid={compact ? 'station-strip' : 'station-card'}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon name="camera" size={16} className="text-[var(--ink-3)]" />
          <span className="text-sm font-semibold text-[var(--ink)]">{STATION_DE.title}</span>
          <Led tone={line.tone} pulse={line.pulse || 'none'} size={8} />
          <span className="text-xs text-[var(--ink-2)]" data-testid="station-state">{line.text}</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {s.held ? (
            <Key
              size="sm"
              icon="logout"
              onClick={onLeave}
              disabled={stationLeaveDisabled(s)}
              title={s.workflowActive ? STATION_DE.stopProgramFirst : undefined}
            >
              {STATION_DE.leave}
            </Key>
          ) : s.startOffered ? (
            <Key size="sm" variant="signal" icon="play" onClick={() => station.startSession()}>
              {STATION_DE.start}
            </Key>
          ) : null}
        </div>
      </div>

      {s.leaderBlocked && !s.held && (
        <p className="mt-2 text-xs text-[color:var(--amber)] flex items-start gap-1.5" role="status">
          <Icon name="warning" size={14} className="mt-0.5" />
          <span>{STATION_DE.leaderFirst}</span>
        </p>
      )}
      {notice && (
        <p className="mt-2 text-xs text-[var(--ink-2)]" role="status" data-testid="station-notice">{notice}</p>
      )}

      {unknown && !notice && (
        <p className="mt-2 text-xs text-[var(--ink-3)]">{STATION_DE.checkingConfig}</p>
      )}

      {s.stationConfigured === false && (
        <div className="mt-3 flex flex-col gap-2">
          <p className="text-sm text-[var(--ink-2)]">{STATION_DE.notConfigured}</p>
          <div>
            <Key size="sm" variant="signal" icon="settings" onClick={onOpenCommission} disabled={!s.held || s.phase !== 'held'}>
              {STATION_DE.commission}
            </Key>
          </div>
        </div>
      )}

      {configured && (
        <ol className={clsx('mt-2 flex gap-3', compact ? 'flex-wrap items-start' : 'flex-col')}>
          <li className="flex flex-col gap-1 min-w-[12rem]" data-testid="station-step-camera">
            <div className="flex items-center gap-2">
              <Led tone={cam.tone} size={8} />
              <span className="text-xs text-[var(--ink-2)]">{cam.text}</span>
            </div>
            {cam.detail ? <span className="text-[11px] text-[var(--ink-3)]">{cam.detail}</span> : null}
            <div>
              <Key size="sm" icon="camera" onClick={() => station.runCameraCheck()} disabled={!canCheck || !s.sourceOk}>
                {STATION_DE.stepCamera}
              </Key>
            </div>
          </li>
          <li className="flex flex-col gap-1 min-w-[12rem]" data-testid="station-step-robot">
            <div className="flex items-center gap-2">
              <Led tone={rob.tone} size={8} />
              <span className="text-xs text-[var(--ink-2)]">{rob.text}</span>
            </div>
            {rob.detail ? <span className="text-[11px] text-[var(--ink-3)]">{rob.detail}</span> : null}
            <StationRobotCheck
              disabled={!canCheck || armBusy || s.workflowActive}
              onHandGuideChange={onHandGuideChange}
            />
          </li>
          {showResolve && (
            <li className="flex flex-col gap-1 min-w-[12rem]" data-testid="station-step-resolve">
              {s.resolveJob && (
                <span className="text-xs text-[var(--ink-2)]">
                  {resolveRunning ? STATION_DE.resolveRunning : (s.resolveJob.message || JOB_STATE_DE[s.resolveJob.jobState] || '')}
                </span>
              )}
              <div>
                <Key size="sm" icon="refresh" onClick={() => setConfirmResolve(true)} disabled={!canCheck || Boolean(resolveRunning)}>
                  {STATION_DE.stepResolve}
                </Key>
              </div>
            </li>
          )}
          <li className="flex items-end">
            <button
              type="button"
              className="text-[11px] underline text-[var(--ink-3)] disabled:opacity-50"
              onClick={onOpenCommission}
              disabled={!s.held || s.phase !== 'held'}
            >
              {STATION_DE.commission}
            </button>
          </li>
        </ol>
      )}

      {confirmResolve && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={STATION_DE.resolveConfirmTitle}
          className="fixed inset-0 z-[9998] flex items-center justify-center bg-black/50"
        >
          <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
            <h3 className="text-base font-semibold text-[var(--ink)]">{STATION_DE.resolveConfirmTitle}</h3>
            <p className="mt-2 text-sm font-semibold text-[color:var(--amber)]">{STATION_DE.resolveConfirm}</p>
            <p className="mt-2 text-sm text-[var(--ink-2)]">{STATION_DE.resolveConfirmBody}</p>
            <div className="mt-4 flex justify-end gap-2">
              <Key size="sm" onClick={() => setConfirmResolve(false)}>{STATION_DE.cancel}</Key>
              <Key
                size="sm"
                variant="signal"
                onClick={() => { setConfirmResolve(false); station.resolveCamera(); }}
              >
                {STATION_DE.resolveGo}
              </Key>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
