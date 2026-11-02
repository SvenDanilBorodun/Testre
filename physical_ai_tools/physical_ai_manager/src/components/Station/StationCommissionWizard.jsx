/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Station einrichten (Lehrkraft)" (station spec v6.13, §6, §9.2): the
// commissioning steps in §6's order — begin → lens → board solve → world check
// (cubes on the marks) → robot registration and touch-off (dots 1–4) → commit.
//
// While mounted it polls `StationCommission status` every 2 s; that poll also
// counts as owner activity, so a touch never times out under a teacher who is
// still guiding (§6 rule 6). A command that creates a job (begin, touch_begin,
// touch_solve, commit, cancel, restore_previous) answers QUEUED at once; the
// wizard waits for that job's `job_state` (done / failed / stale) in the poll
// before it offers the next step, and shows its `job_message`. Closing the
// wizard leaves a commission running (a reloaded or reopened wizard resumes it
// from `status`); a session end aborts it on the server (§6).
//
// A capture or a solve refused with „Bitte den Arm loslassen …" (a re-torque
// of the touch failed) keeps that sentence on screen until a new touch begins.

import React, { useCallback, useEffect, useRef, useState } from 'react';

import { Key } from '../sucher';
import Icon from '../icons/Icon';
import CameraFeedOverlay from '../Workshop/CameraFeedOverlay';
import * as station from '../../features/station/controller';
import { useStationSnapshot } from '../../features/station/useStation';
import {
  DEFAULT_LAYOUT_ID,
  JOB_END_STATES,
  JOB_POLL_MS,
  STATION_CAMERA,
} from '../../features/station/stationContract';
import {
  COMMISSION_DE,
  JOB_STATE_DE,
  commissionMessageDe,
} from '../../features/station/stationCopy';

const STEPS = Object.freeze(['lens', 'board', 'marks', 'touch', 'commit']);
const RELEASE_ARM = /loslassen/;

/** Where a resumed commission stands (§8.3 `status`): its `step`, else its results. */
export function stepFromStatus(res) {
  if (!res || !res.commission_id) return 'intro';
  const st = String(res.step || '');
  if (/commit/.test(st)) return 'commit';
  if (/touch/.test(st)) return 'touch';
  if (/mark|world/.test(st)) return 'marks';
  if (/board/.test(st)) return 'board';
  if (/lens/.test(st)) return 'lens';
  if (Number(res.registration_rms_mm) > 0) return 'commit';
  if (Number(res.world_rms_mm) > 0) return 'touch';
  if (Number(res.board_reproj_px) > 0) return 'marks';
  if (Number(res.lens_fit_rms_px) > 0) return 'board';
  return 'lens';
}

/**
 * @param {object} p
 * @param {() => void} p.onClose
 */
export default function StationCommissionWizard({ onClose }) {
  const snap = useStationSnapshot();
  const [status, setStatus] = useState(null);
  const [step, setStep] = useState('intro');
  const [pendingJob, setPendingJob] = useState(null); // {jobId, kind}
  const [message, setMessage] = useState('');
  const [sticky, setSticky] = useState('');
  const [busy, setBusy] = useState(false);
  const [committed, setCommitted] = useState(false);
  const pendingRef = useRef(null);
  pendingRef.current = pendingJob;
  const stepRef = useRef(step);
  stepRef.current = step;
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const commissionId = (status && status.commission_id) || '';

  const onJobEnd = useCallback((job, res) => {
    const ok = res.job_state === 'done';
    setMessage(res.job_message || JOB_STATE_DE[res.job_state] || '');
    if (job.kind === 'claim' && ok) setStep('lens');
    if (job.kind === 'touch_off' && ok) setSticky('');
    if (job.kind === 'touch_solve' && ok) setStep('commit');
    if (job.kind === 'commit' && ok) {
      setCommitted(true);
      setStep('done');
      setMessage(COMMISSION_DE.committed);
    }
    if (job.kind === 'abort' && ok) setStep('intro');
  }, []);

  const poll = useCallback(async () => {
    const r = await station.commission('status');
    if (!aliveRef.current || !r.ok) return;
    const res = r.res;
    setStatus(res);
    const job = pendingRef.current;
    if (job && Number(res.job_id) === job.jobId && JOB_END_STATES.includes(res.job_state)) {
      setPendingJob(null);
      onJobEnd(job, res);
    }
    // A reloaded or reopened wizard resumes the running commission.
    if (stepRef.current === 'intro' && res.commission_id && !job) setStep(stepFromStatus(res));
  }, [onJobEnd]);

  useEffect(() => {
    poll();
    const id = setInterval(poll, JOB_POLL_MS);
    return () => clearInterval(id);
  }, [poll]);

  const run = useCallback(async (command, fields = {}, { jobKind = null, onOk = null } = {}) => {
    setBusy(true);
    try {
      const r = await station.commission(command, fields);
      if (!aliveRef.current) return null;
      if (!r.ok) {
        setMessage(COMMISSION_DE.waitJob);
        return null;
      }
      const res = r.res;
      const text = commissionMessageDe(res);
      if (res.result === 'QUEUED' && jobKind) {
        setPendingJob({ jobId: Number(res.job_id), kind: jobKind });
        setMessage(COMMISSION_DE.waitJob);
      } else {
        setMessage(text);
        if (res.result === 'REFUSED' && RELEASE_ARM.test(text)) setSticky(text);
        if (res.result === 'OK' && onOk) onOk(res);
      }
      setStatus((prev) => ({ ...(prev || {}), ...pickNumbers(res) }));
      return res;
    } finally {
      if (aliveRef.current) {
        setBusy(false);
        // The next dot / mark / cube turn are `status` fields: ask at once.
        poll();
      }
    }
  }, [poll]);

  const waiting = Boolean(pendingJob) || busy;
  const live = snap.held && snap.phase === 'held';
  const st = status || {};

  const onCancel = () => {
    if (typeof window !== 'undefined' && !window.confirm(COMMISSION_DE.cancelConfirm)) return;
    run('cancel', { commission_id: commissionId }, { jobKind: 'abort' });
  };

  const stepIndex = STEPS.indexOf(step);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={COMMISSION_DE.title}
      className="fixed inset-0 z-[9997] flex items-stretch justify-center bg-black/50 p-4"
      data-testid="station-commission-wizard"
    >
      <div className="flex w-full max-w-5xl flex-col overflow-hidden rounded-lg bg-white shadow-xl">
        <header className="flex items-center gap-3 border-b border-[var(--line)] px-4 py-3">
          <Icon name="settings" size={18} className="text-[var(--ink-3)]" />
          <h2 className="text-base font-semibold text-[var(--ink)]">{COMMISSION_DE.title}</h2>
          <div className="ml-auto flex items-center gap-2">
            {commissionId && step !== 'done' && (
              <Key size="sm" variant="danger" onClick={onCancel} disabled={waiting || !live}>
                {COMMISSION_DE.cancel}
              </Key>
            )}
            <Key size="sm" icon="close" onClick={onClose}>{COMMISSION_DE.close}</Key>
          </div>
        </header>
        <div className="flex min-h-0 flex-1">
          <aside className="w-52 shrink-0 border-r border-[var(--line)] p-3">
            <ol className="space-y-1.5">
              {STEPS.map((k, i) => (
                <li
                  key={k}
                  className={
                    'flex items-center gap-2 rounded px-2 py-1 text-sm '
                    + (k === step ? 'bg-[var(--accent-wash)] text-[var(--accent-ink)] font-medium' : 'text-[var(--ink-3)]')
                  }
                >
                  <span className="inline-flex w-5 justify-center">
                    {committed || (stepIndex > i) ? <Icon name="check" size={14} /> : i + 1}
                  </span>
                  {COMMISSION_DE.steps[k]}
                </li>
              ))}
            </ol>
          </aside>
          <section className="flex min-w-0 flex-1 flex-col gap-3 overflow-auto p-4">
            {(step === 'lens' || step === 'board' || step === 'marks') && (
              <div className="h-[min(48vh,420px)] w-full">
                <CameraFeedOverlay camera={STATION_CAMERA} fill sourceOk={snap.sourceOk} />
              </div>
            )}

            {step === 'intro' && (
              <div className="flex flex-col gap-3">
                <p className="text-sm text-[var(--ink-2)]">{COMMISSION_DE.intro}</p>
                <div className="flex flex-wrap gap-2">
                  <Key
                    variant="signal"
                    icon="play"
                    disabled={waiting || !live}
                    onClick={() => run('begin', { layout_id: snap.layoutId || DEFAULT_LAYOUT_ID }, { jobKind: 'claim' })}
                  >
                    {COMMISSION_DE.begin}
                  </Key>
                  <Key
                    icon="history"
                    disabled={waiting || !live}
                    onClick={() => run('restore_previous', {}, { jobKind: 'restore' })}
                  >
                    {COMMISSION_DE.restore}
                  </Key>
                </div>
              </div>
            )}

            {step === 'lens' && (
              <div className="flex flex-col gap-2">
                <p className="text-sm text-[var(--ink-2)]">{COMMISSION_DE.lensIntro}</p>
                <p className="text-xs text-[var(--ink-3)]" data-testid="lens-counts">
                  {COMMISSION_DE.lensCounts(Number(st.lens_views_fit) || 0, Number(st.lens_views_heldout) || 0, Number(st.coverage_cells) || 0)}
                </p>
                <div className="flex flex-wrap gap-2">
                  <Key icon="camera" disabled={waiting || !live} onClick={() => run('lens_capture', { commission_id: commissionId })}>
                    {COMMISSION_DE.lensCapture}
                  </Key>
                  <Key
                    variant="signal"
                    disabled={waiting || !live}
                    onClick={() => run('lens_solve', { commission_id: commissionId }, { onOk: () => setStep('board') })}
                  >
                    {COMMISSION_DE.lensSolve}
                  </Key>
                </div>
              </div>
            )}

            {step === 'board' && (
              <div className="flex flex-col gap-2">
                {Number(st.lens_fit_rms_px) > 0 && (
                  <p className="text-xs text-[var(--ink-3)]" data-testid="lens-result">
                    {COMMISSION_DE.lensResult(
                      st.lens_fit_rms_px, st.lens_heldout_rms_px, st.lens_heldout_max_px, st.lens_fx_px, st.lens_fy_px,
                    )}
                  </p>
                )}
                <p className="text-sm text-[var(--ink-2)]">{COMMISSION_DE.boardIntro}</p>
                <div>
                  <Key
                    variant="signal"
                    disabled={waiting || !live}
                    onClick={() => run('board_solve', { commission_id: commissionId }, { onOk: () => setStep('marks') })}
                  >
                    {COMMISSION_DE.boardSolve}
                  </Key>
                </div>
              </div>
            )}

            {step === 'marks' && (
              <div className="flex flex-col gap-2">
                {Number(st.board_reproj_px) > 0 && (
                  <p className="text-xs text-[var(--ink-3)]">{COMMISSION_DE.boardResult(st.board_reproj_px)}</p>
                )}
                <p className="text-sm text-[var(--ink-2)]">{COMMISSION_DE.marksIntro}</p>
                {st.next_mark_id ? (
                  <>
                    <p className="text-sm font-semibold text-[var(--ink)]" data-testid="next-mark">
                      {COMMISSION_DE.markNext(st.next_mark_id, Number(st.next_cube_turn) === 1)}
                    </p>
                    <div>
                      <Key
                        icon="camera"
                        disabled={waiting || !live}
                        onClick={() => run('mark_capture', {
                          commission_id: commissionId,
                          mark_id: String(st.next_mark_id),
                          cube_turn: Number(st.next_cube_turn) === 1 ? 1 : 0,
                        })}
                      >
                        {COMMISSION_DE.markCapture}
                      </Key>
                    </div>
                  </>
                ) : (
                  <>
                    <p className="text-sm text-[var(--ink-2)]">{COMMISSION_DE.marksDone}</p>
                    <div>
                      <Key
                        variant="signal"
                        disabled={waiting || !live}
                        onClick={() => run('mark_solve', { commission_id: commissionId }, { onOk: () => setStep('touch') })}
                      >
                        {COMMISSION_DE.markSolve}
                      </Key>
                    </div>
                  </>
                )}
              </div>
            )}

            {step === 'touch' && (
              <div className="flex flex-col gap-2">
                {Number(st.world_points) > 0 && (
                  <p className="text-xs text-[var(--ink-3)]">
                    {COMMISSION_DE.marksResult(st.world_points, st.world_rms_mm, st.world_max_mm)}
                  </p>
                )}
                <p className="text-sm text-[var(--ink-2)]">{COMMISSION_DE.touchIntro}</p>
                {Number(st.next_dot_id) > 0 && (
                  <p className="text-sm font-semibold text-[var(--ink)]" data-testid="next-dot">
                    {COMMISSION_DE.touchNext(st.next_dot_id)}
                  </p>
                )}
                <div className="flex flex-wrap gap-2">
                  <Key icon="hand" disabled={waiting || !live} onClick={() => run('touch_begin', { commission_id: commissionId }, { jobKind: 'touch_off' })}>
                    {COMMISSION_DE.touchBegin}
                  </Key>
                  <Key
                    icon="ziel"
                    disabled={waiting || !live || !(Number(st.next_dot_id) > 0)}
                    onClick={() => run('touch_capture', { commission_id: commissionId, dot_id: Number(st.next_dot_id) })}
                  >
                    {COMMISSION_DE.touchCapture}
                  </Key>
                  <Key
                    variant="signal"
                    disabled={waiting || !live}
                    onClick={() => run('touch_solve', { commission_id: commissionId }, { jobKind: 'touch_solve' })}
                  >
                    {COMMISSION_DE.touchSolve}
                  </Key>
                </div>
              </div>
            )}

            {step === 'commit' && (
              <div className="flex flex-col gap-2">
                {Number(st.registration_rms_mm) > 0 && (
                  <p className="text-xs text-[var(--ink-3)]">
                    {COMMISSION_DE.touchResult(st.registration_rms_mm, st.registration_shift_mm, st.registration_rot_deg)}
                  </p>
                )}
                <div>
                  <Key variant="signal" icon="save" disabled={waiting || !live} onClick={() => run('commit', { commission_id: commissionId }, { jobKind: 'commit' })}>
                    {COMMISSION_DE.commit}
                  </Key>
                </div>
              </div>
            )}

            {step === 'done' && (
              <p className="text-sm font-semibold text-[var(--success)]">{COMMISSION_DE.committed}</p>
            )}

            {sticky && (
              <p className="text-sm font-semibold text-[color:var(--danger)]" role="alert" data-testid="commission-sticky">{sticky}</p>
            )}
            {message && message !== sticky && (
              <p className="text-sm text-[var(--ink-2)]" role="status" data-testid="commission-message">{message}</p>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

// The numeric fields a command answer carries (§8.3), merged into the view so
// a capture shows its new counts before the next poll.
const NUMBER_FIELDS = [
  'lens_views_fit', 'lens_views_heldout', 'coverage_cells', 'lens_fit_rms_px', 'lens_heldout_rms_px',
  'lens_heldout_max_px', 'lens_fx_px', 'lens_fy_px', 'board_reproj_px', 'registration_rms_mm', 'registration_max_mm',
  'registration_shift_mm', 'registration_rot_deg', 'world_rms_mm', 'world_max_mm', 'world_points',
];

function pickNumbers(res) {
  const out = {};
  for (const k of NUMBER_FIELDS) {
    if (res && res[k] !== undefined && res[k] !== null && Number(res[k]) > 0) out[k] = res[k];
  }
  if (res && typeof res.commission_id === 'string' && res.commission_id) out.commission_id = res.commission_id;
  if (res && typeof res.next_mark_id === 'string' && res.next_mark_id) out.next_mark_id = res.next_mark_id;
  if (res && Number(res.next_dot_id) > 0) out.next_dot_id = res.next_dot_id;
  return out;
}
