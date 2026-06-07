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

// Teleop force/collision e-stop modal — two-step, student-paced recovery. A big, blocking,
// non-dismissible overlay shown whenever state.tasks.collision.active is true (set by the
// /task/status collision-phase handler in useRosTopicSubscription.js). The server is the
// source of truth for the step via collision.stage:
//   'stopped' (phase=COLLISION)        — the follower was forced against an object and halted
//                                        IN PLACE (it does not auto-home, so the student can
//                                        remove the obstacle first). Step 1: button
//                                        „Follower in Grundstellung fahren" → HOME_FOLLOWER.
//   'homing'  (phase=COLLISION_HOMING) — the verified safe-home glide is running; step 1
//                                        button disabled. On a failed glide the server falls
//                                        back to 'stopped' with a German retry hint as the
//                                        message, re-enabling the button.
//   'homed'   (phase=COLLISION_HOMED)  — follower verified at home. Step 2: the student
//                                        brings the leader near the home pose and clicks
//                                        „Teleoperation fortsetzen" → RESUME_TELEOP. The
//                                        server refuses (success=false) with a hint while the
//                                        leader is too far; on success the modal unmounts when
//                                        the next non-collision /task/status arrives.

import React, { useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';

import { useRosServiceCaller } from '../hooks/useRosServiceCaller';

// Show-the-remedy escalation (leLab-comparison PR-3): after repeated failed
// homing attempts a 13-year-old needs a PICTURE of the fix, not another
// paragraph. Inline SVG — bundled with the app, no hot-link (classrooms are
// offline), no binary asset.
function RemedyDiagram() {
  return (
    <svg
      viewBox="0 0 280 110"
      className="w-full max-w-xs"
      role="img"
      aria-label="Hindernis aus dem Bewegungsbereich des Arms entfernen"
    >
      {/* table */}
      <line x1="10" y1="95" x2="270" y2="95" stroke="#9ca3af" strokeWidth="3" />
      {/* robot base + arm */}
      <rect x="30" y="75" width="34" height="20" rx="3" fill="#6b7280" />
      <line x1="47" y1="75" x2="70" y2="40" stroke="#374151" strokeWidth="7" strokeLinecap="round" />
      <line x1="70" y1="40" x2="105" y2="58" stroke="#374151" strokeWidth="6" strokeLinecap="round" />
      <circle cx="70" cy="40" r="5" fill="#111827" />
      {/* obstacle pressed against the gripper */}
      <rect x="108" y="52" width="26" height="43" rx="4" fill="#f59e0b" />
      {/* arrow moving the obstacle away */}
      <line x1="150" y1="72" x2="215" y2="72" stroke="#dc2626" strokeWidth="5" strokeLinecap="round" />
      <polygon points="215,62 235,72 215,82" fill="#dc2626" />
      {/* obstacle at its new, safe spot (ghost) */}
      <rect x="240" y="52" width="26" height="43" rx="4" fill="#fcd34d" opacity="0.55" />
    </svg>
  );
}

export default function CollisionModal() {
  const collision = useSelector((state) => state.tasks.collision);
  const { homeFollower, resumeTeleop } = useRosServiceCaller();
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState('');
  // Failed step-1 attempts since this collision began — drives the
  // visual-remedy escalation below. Reset whenever the modal closes.
  const [homeAttempts, setHomeAttempts] = useState(0);

  useEffect(() => {
    if (!collision?.active) {
      setHomeAttempts(0);
      setHint('');
    }
  }, [collision?.active]);

  if (!collision?.active) {
    return null;
  }

  const stage = collision.stage || 'stopped';
  const isHomed = stage === 'homed';
  const isHoming = stage === 'homing';
  // Per-joint |pos - home| in rad (joint1..joint5) — published by the
  // collision monitor while a joint_states sample exists. Empty on
  // pre-rebuild server images (the strip simply hides). Read-only
  // telemetry: never feeds the recovery state machine (Rule §2 scope).
  const jointDists = Array.isArray(collision.jointDistToHome)
    ? collision.jointDistToHome
    : [];
  // Mirrors HOME_ARRIVED_TOL_RAD in collision_monitor.py (0.10 rad).
  const HOME_TOL_RAD = 0.1;

  const callStep = async (serviceCall, errorToast) => {
    setBusy(true);
    setHint('');
    try {
      const result = await serviceCall();
      if (result && result.success === false) {
        // Refused (no active collision / not homed yet / leader too far) — re-prompt inline.
        setHint(result.message || 'Bitte erneut versuchen.');
      } else {
        setHint(result?.message || '');
      }
    } catch (err) {
      setHint('Fehler — bitte erneut versuchen.');
      toast.error(errorToast);
    } finally {
      setBusy(false);
    }
  };

  const handleHome = () => {
    setHomeAttempts((n) => n + 1);
    return callStep(homeFollower, '„Follower in Grundstellung fahren" fehlgeschlagen.');
  };
  // Escalate to the picture after the second attempt that did NOT get the
  // arm home (the server falls back to stage 'stopped' with a retry hint
  // when the glide exhausts its re-sends).
  const showRemedyDiagram = stage === 'stopped' && homeAttempts >= 2;
  const handleResume = () => callStep(resumeTeleop, '„Teleoperation fortsetzen" fehlgeschlagen.');

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="alertdialog"
      aria-modal="true"
      aria-label="Kollision erkannt"
    >
      <div
        className={`mx-4 max-w-lg rounded-2xl border-2 bg-white p-8 shadow-2xl ${
          isHomed ? 'border-amber-500' : 'border-red-500'
        }`}
      >
        <div className="flex flex-col items-center gap-4 text-center">
          <div className="text-5xl" aria-hidden="true">
            {isHomed ? '🦾' : '⚠️'}
          </div>
          <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
            {isHomed ? 'Schritt 2 von 2' : 'Schritt 1 von 2'}
          </span>
          <h2 className={`text-2xl font-bold ${isHomed ? 'text-amber-600' : 'text-red-600'}`}>
            {isHomed ? 'Follower in Grundstellung' : 'STOPP — Kollision erkannt'}
          </h2>
          <p className="text-gray-700">
            {collision.message ||
              (isHomed
                ? 'Bringe den Leader-Arm in die gleiche Stellung und klicke dann auf ' +
                  '„Teleoperation fortsetzen".'
                : 'Der Roboterarm wurde gegen ein Hindernis gedrückt und angehalten. Entferne ' +
                  'zuerst das Hindernis und klicke dann auf „Follower in Grundstellung fahren".')}
          </p>
          {hint && <p className="text-sm font-medium text-amber-700">{hint}</p>}
          {showRemedyDiagram && (
            <div className="flex w-full flex-col items-center gap-1 rounded-xl border border-amber-200 bg-amber-50 p-3">
              <RemedyDiagram />
              <p className="text-sm font-medium text-amber-800">
                Hindernis ganz aus dem Bewegungsbereich nehmen — auch unter
                dem Greifer nachsehen. Danach erneut auf „Follower in
                Grundstellung fahren" klicken.
              </p>
            </div>
          )}
          {isHoming && jointDists.length > 0 && (
            <div
              className="mt-1 flex w-full flex-row items-center justify-center gap-2"
              role="status"
              aria-label="Fortschritt je Gelenk"
            >
              {jointDists.map((dist, i) => {
                const done = dist <= HOME_TOL_RAD;
                const deg = Math.round((dist * 180) / Math.PI);
                return (
                  <div
                    key={i}
                    className={`flex min-w-14 flex-col items-center rounded-lg border px-2 py-1 text-xs font-medium ${
                      done
                        ? 'border-green-300 bg-green-50 text-green-700'
                        : 'border-amber-300 bg-amber-50 text-amber-700'
                    }`}
                    title={`Gelenk ${i + 1}: noch ${deg}° bis zur Grundstellung`}
                  >
                    <span>G{i + 1}</span>
                    <span>{done ? '✓' : `${deg}°`}</span>
                  </div>
                );
              })}
            </div>
          )}
          {isHomed ? (
            <button
              type="button"
              onClick={handleResume}
              disabled={busy}
              className="mt-2 rounded-xl bg-amber-600 px-6 py-3 text-base font-semibold text-white shadow hover:bg-amber-700 disabled:opacity-50"
            >
              {busy ? 'Wird fortgesetzt …' : 'Teleoperation fortsetzen'}
            </button>
          ) : (
            <button
              type="button"
              onClick={handleHome}
              disabled={busy || isHoming}
              className="mt-2 rounded-xl bg-red-600 px-6 py-3 text-base font-semibold text-white shadow hover:bg-red-700 disabled:opacity-50"
            >
              {isHoming ? 'Fährt in Grundstellung …' : 'Follower in Grundstellung fahren'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
