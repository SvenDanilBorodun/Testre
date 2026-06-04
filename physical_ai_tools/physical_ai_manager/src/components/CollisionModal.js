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

import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';

import { useRosServiceCaller } from '../hooks/useRosServiceCaller';

export default function CollisionModal() {
  const collision = useSelector((state) => state.tasks.collision);
  const { homeFollower, resumeTeleop } = useRosServiceCaller();
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState('');

  if (!collision?.active) {
    return null;
  }

  const stage = collision.stage || 'stopped';
  const isHomed = stage === 'homed';
  const isHoming = stage === 'homing';

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

  const handleHome = () =>
    callStep(homeFollower, '„Follower in Grundstellung fahren" fehlgeschlagen.');
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
