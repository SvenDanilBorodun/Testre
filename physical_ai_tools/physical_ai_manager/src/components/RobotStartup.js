// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// RobotStartup — the animated hero on the Start page. The robot arms launch
// LIMP at boot (no torque, no motion); the student clicks "Roboter starten"
// here to home the follower and sync it to the leader. The arm illustration,
// scanline, progress bar and phase stepper are all driven by the live
// /edubotics/arm_state topic (Redux state.armStartup). The arm only reaches
// the green "bereit" state — and only then do the other tabs unlock — when the
// follower↔leader sync VERIFIES, so a mis-synced arm can never be recorded on.

import React from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { Btn, Pill, Progress } from './EbUI';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { markArmStarting } from '../store/armStartupSlice';

const PHASE_ORDER = [
  'idle',
  'homing',
  'reading_leader',
  'syncing',
  'verifying',
  'ready',
];

// Three student-facing steps mapped from the node's fine-grained phases.
const STEPS = [
  { key: 'home', label: 'Referenzierung', phases: ['homing'] },
  { key: 'sync', label: 'Leader-Sync', phases: ['reading_leader', 'syncing'] },
  { key: 'verify', label: 'Prüfung', phases: ['verifying'] },
];

function Spinner({ className }) {
  return (
    <svg
      className={clsx('animate-spin', className)}
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

// The arm itself. `tone` selects the palette; `pose` (0..1) rotates the
// segments so the illustration visibly "moves" as the real arm homes/syncs.
function ArmIllustration({ tone, active }) {
  const stroke =
    tone === 'ready'
      ? '#3DDC84'
      : tone === 'error'
      ? '#FF6B6B'
      : tone === 'active'
      ? '#16C7BE'
      : '#9CA1A6';
  const jointFill = tone === 'idle' ? '#3B4145' : stroke;
  // Idle arm droops; active/ready arm is raised to its home pose.
  const upperRot = tone === 'idle' ? 10 : -6;
  const foreRot = tone === 'idle' ? 14 : -10;

  return (
    <svg
      viewBox="0 0 600 400"
      className={clsx(
        'absolute inset-0 w-full h-full',
        tone === 'idle' && 'eb-float'
      )}
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <linearGradient id="rs-armg" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#E8EBEC" />
          <stop offset="1" stopColor="#9CA1A6" />
        </linearGradient>
        <filter id="rs-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="6" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* base */}
      <rect x="220" y="320" width="160" height="50" rx="6" fill="#24292B" />
      <rect x="250" y="300" width="100" height="26" rx="4" fill="#3B4145" />

      {/* shoulder → upper arm (rotates about the base joint) */}
      <g
        style={{
          transform: `rotate(${upperRot}deg)`,
          transformBox: 'fill-box',
          transformOrigin: '300px 300px',
          transition: 'transform 1.8s cubic-bezier(.45,0,.55,1)',
        }}
      >
        <rect x="270" y="190" width="60" height="120" rx="6" fill="url(#rs-armg)" />
        <rect x="260" y="165" width="80" height="36" rx="6" fill="#3B4145" />

        {/* forearm (rotates about the elbow) */}
        <g
          style={{
            transform: `rotate(${foreRot}deg)`,
            transformBox: 'fill-box',
            transformOrigin: '300px 180px',
            transition: 'transform 1.8s cubic-bezier(.45,0,.55,1)',
          }}
        >
          <rect x="290" y="108" width="20" height="74" rx="4" fill="url(#rs-armg)" />
          <circle cx="300" cy="105" r="16" fill="#3B4145" />
          {/* gripper */}
          <rect x="288" y="80" width="24" height="22" rx="3" fill="url(#rs-armg)" />
          <rect x="284" y="58" width="12" height="28" rx="2" fill="#9CA1A6" />
          <rect x="304" y="58" width="12" height="28" rx="2" fill="#9CA1A6" />
          {/* end-effector joint dot */}
          <circle
            cx="300"
            cy="105"
            r="4"
            fill={jointFill}
            filter={active ? 'url(#rs-glow)' : undefined}
            className={active ? 'eb-joint-pulse' : undefined}
          />
        </g>
        {/* elbow joint dot */}
        <circle
          cx="300"
          cy="180"
          r="4"
          fill={jointFill}
          filter={active ? 'url(#rs-glow)' : undefined}
          className={active ? 'eb-joint-pulse' : undefined}
          style={active ? { animationDelay: '.2s' } : undefined}
        />
      </g>
      {/* base joint dot */}
      <circle
        cx="300"
        cy="300"
        r="4"
        fill={jointFill}
        filter={active ? 'url(#rs-glow)' : undefined}
        className={active ? 'eb-joint-pulse' : undefined}
        style={active ? { animationDelay: '.4s' } : undefined}
      />
    </svg>
  );
}

export default function RobotStartup({ packageVersion }) {
  const dispatch = useDispatch();
  const { startArm } = useRosServiceCaller();

  const { phase, percent, message, perJointErr, armReady, busy } =
    useSelector((state) => state.armStartup);
  const robotType = useSelector((state) => state.tasks.taskStatus.robotType);
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const bridgeReady = heartbeatStatus === 'connected';

  const isError = phase === 'error';
  const tone = isError ? 'error' : armReady ? 'ready' : busy ? 'active' : 'idle';

  const phaseIdx = Math.max(0, PHASE_ORDER.indexOf(busy ? phase : armReady ? 'ready' : 'idle'));

  const handleStart = async () => {
    if (!bridgeReady || busy) return;
    dispatch(markArmStarting()); // optimistic — topic refines the rest
    try {
      const res = await startArm();
      if (res && res.success === false) {
        // The /edubotics/arm_state 'error' message also drives the UI; the
        // toast just makes the failure immediate.
        toast.error(res.message || 'Arm-Start fehlgeschlagen.');
      } else if (res && res.success) {
        toast.success(res.message || 'Roboter bereit.');
      }
    } catch (e) {
      // A service timeout doesn't necessarily mean failure — the node may
      // still finish and publish 'ready'. Only toast; don't force an error
      // state (the topic is the source of truth for armReady).
      console.warn('start_arm call error:', e?.message || e);
    }
  };

  // ---- status line + headline per tone ----
  const headline = isError
    ? 'Synchronisation fehlgeschlagen'
    : armReady
    ? 'Roboter bereit'
    : busy
    ? 'Roboter wird gestartet…'
    : 'Roboter starten';
  const subline = isError
    ? message ||
      'Bringe beide Arme in eine ähnliche Position und versuche es erneut.'
    : armReady
    ? 'Folger und Leader sind synchronisiert. Du kannst jetzt aufnehmen.'
    : busy
    ? message || 'Bitte warten…'
    : bridgeReady
    ? 'Der Folger-Arm wird referenziert und mit dem Leader synchronisiert.'
    : 'Warte auf die Verbindung zum Roboter…';

  return (
    <section className="bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-soft overflow-hidden">
      {/* Hero panel with the animated arm */}
      <div className="relative h-[280px] sm:h-[340px] md:h-[380px] xl:h-[440px] camera-noise overflow-hidden">
        <ArmIllustration tone={tone} active={busy} />

        {/* scanning line while the arm is in motion */}
        {busy && (
          <div className="pointer-events-none absolute inset-x-0 top-0 h-10 eb-scan">
            <div
              className="h-px w-full"
              style={{
                background:
                  'linear-gradient(90deg, transparent, var(--accent), transparent)',
                boxShadow: '0 0 12px 2px rgba(22,199,190,.45)',
              }}
            />
          </div>
        )}

        {/* top-left status pills */}
        <div className="absolute top-4 left-4 flex items-center gap-2">
          <Pill tone="glass" dot className={busy ? 'eb-blink' : undefined}>
            {armReady ? 'BEREIT' : busy ? 'AKTIV' : isError ? 'FEHLER' : 'LIVE'}
          </Pill>
          <Pill tone="glass">
            <span className="font-mono">ros_bridge</span>
          </Pill>
        </div>

        {/* bottom overlay: current robot + version */}
        <div className="absolute bottom-4 left-4 right-4 flex items-end justify-between gap-4">
          <div className="min-w-0">
            <div className="text-white/60 font-mono text-[10px] uppercase tracking-wider">
              Aktueller Roboter
            </div>
            <div className="text-white font-semibold text-xl tracking-tight leading-tight truncate">
              {robotType || 'Kein Robotertyp gewählt'}
            </div>
          </div>
          {packageVersion && (
            <div className="font-mono text-[11px] text-white/60 shrink-0 whitespace-nowrap">
              EduBotics v{packageVersion}
            </div>
          )}
        </div>
      </div>

      {/* Control + progress footer */}
      <div className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              {armReady && (
                <span className="eb-pop inline-flex items-center justify-center w-6 h-6 rounded-full bg-[var(--success-wash)]">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--success)" strokeWidth="3">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                </span>
              )}
              {isError && (
                <span className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-[var(--danger-wash)] eb-pulse-dot">
                  <span className="w-1.5 h-1.5 rounded-full bg-[var(--danger)]" />
                </span>
              )}
              <h3
                className={clsx(
                  'font-semibold tracking-tight',
                  isError
                    ? 'text-[color:var(--danger)]'
                    : armReady
                    ? 'text-[color:var(--success)]'
                    : 'text-[var(--ink)]'
                )}
              >
                {headline}
              </h3>
            </div>
            <p className="text-sm text-[var(--ink-3)] mt-1">{subline}</p>
          </div>

          <div className="shrink-0 flex items-center gap-2">
            {busy ? (
              <div className="flex items-center gap-2 text-[var(--accent-ink)]">
                <Spinner />
                <span className="font-mono text-sm font-semibold tabular-nums">{percent}%</span>
              </div>
            ) : armReady ? (
              <Btn variant="secondary" size="md" onClick={handleStart} title="Erneut referenzieren">
                Erneut starten
              </Btn>
            ) : (
              <Btn
                variant="primary"
                size="lg"
                onClick={handleStart}
                disabled={!bridgeReady}
              >
                {isError ? 'Erneut versuchen' : 'Roboter starten'}
              </Btn>
            )}
          </div>
        </div>

        {/* progress bar + stepper while active */}
        {busy && (
          <div className="mt-4">
            <Progress pct={percent} tone="accent" />
            <div className="mt-3 flex items-center gap-2 flex-wrap">
              {STEPS.map((step, i) => {
                const done = phaseIdx > PHASE_ORDER.indexOf(step.phases[step.phases.length - 1]);
                const activeStep = step.phases.includes(phase);
                return (
                  <div key={step.key} className="flex items-center gap-2">
                    <span
                      className={clsx(
                        'inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border transition-colors',
                        done
                          ? 'bg-[var(--success-wash)] text-[color:var(--success)] border-transparent'
                          : activeStep
                          ? 'bg-[var(--accent-wash)] text-[var(--accent-ink)] border-transparent'
                          : 'bg-[var(--bg-sunk)] text-[var(--ink-3)] border-[var(--line)]'
                      )}
                    >
                      {done ? (
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                        </svg>
                      ) : activeStep ? (
                        <Spinner className="w-3 h-3" />
                      ) : (
                        <span className="font-mono">{i + 1}</span>
                      )}
                      {step.label}
                    </span>
                    {i < STEPS.length - 1 && (
                      <span className="w-4 h-px bg-[var(--line)] hidden sm:block" />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* per-joint error readout (teacher can see which joints are off) */}
        {isError && perJointErr && perJointErr.length === 6 && (
          <div className="mt-4 rounded-[var(--radius-sm)] bg-[var(--danger-wash)] border border-[color:var(--danger)]/20 p-3">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-[color:var(--danger)] mb-2">
              Abweichung pro Gelenk (rad)
            </div>
            <div className="flex flex-wrap gap-1.5">
              {perJointErr.map((e, i) => {
                const labels = ['J1', 'J2', 'J3', 'J4', 'J5', 'Greifer'];
                const high = i < 5 && e >= 0.3; // gripper (5) is verify-exempt
                return (
                  <span
                    key={i}
                    className={clsx(
                      'font-mono text-[11px] px-2 py-0.5 rounded-md border',
                      high
                        ? 'bg-white text-[color:var(--danger)] border-[color:var(--danger)]/40 font-semibold'
                        : 'bg-white/60 text-[var(--ink-3)] border-[var(--line)]'
                    )}
                  >
                    {labels[i]} {e.toFixed(2)}
                  </span>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
