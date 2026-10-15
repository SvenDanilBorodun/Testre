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
import Icon from './icons/Icon';
import RobotGlyph from './icons/RobotGlyph';
import { phasePicture, robotGlyphName } from './icons/robotAssets';

// Show-the-remedy escalation (leLab-comparison PR-3): after repeated failed
// homing attempts a 13-year-old needs a PICTURE of the fix, not another
// paragraph. The picture is a render of the real follower (its URDF, by
// profile id) with an obstacle pressed against the gripper and the obstacle's
// ghost where it belongs; the arrow between them is a registry icon laid over
// the image at anchors measured in the render. Bundled with the app, no
// hot-link (classrooms are offline). A profile with no such picture (or none
// known yet) shows none — never another robot.
function RemedyPicture({ profile }) {
  const pic = phasePicture(profile, 'collision');
  if (!pic) return null;
  const { obstacle, ghost } = pic.anchors;
  const angle = Math.atan2((ghost[1] - obstacle[1]) * pic.height, (ghost[0] - obstacle[0]) * pic.width);
  return (
    <div
      className="relative w-full max-w-xs"
      style={{ aspectRatio: `${pic.width} / ${pic.height}` }}
      role="img"
      aria-label="Hindernis aus dem Bewegungsbereich des Arms entfernen"
      data-robot-picture={pic.file}
    >
      <img src={pic.url} alt="" draggable="false" decoding="async" className="block h-full w-full" />
      <span
        className="absolute text-[#FF5B1F] drop-shadow-[0_1px_2px_rgba(0,0,0,.55)]"
        style={{
          left: `${((obstacle[0] + ghost[0]) / 2) * 100}%`,
          top: `${((obstacle[1] + ghost[1]) / 2) * 100}%`,
          transform: `translate(-50%, -50%) rotate(${angle}rad)`,
        }}
      >
        <Icon name="arrowRight" size="1.9rem" />
      </span>
    </div>
  );
}

export default function CollisionModal() {
  const collision = useSelector((state) => state.tasks.collision);
  const profile = useSelector((state) => (state.tasks && state.tasks.taskStatus && state.tasks.taskStatus.robotProfile) || '');
  const { homeFollower, resumeTeleop, forceResumeTeleop } = useRosServiceCaller();
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
  const showRemedyPicture = stage === 'stopped' && homeAttempts >= 2;
  const handleResume = () => callStep(resumeTeleop, '„Teleoperation fortsetzen" fehlgeschlagen.');
  // Escape hatch (issue #19 finding #4): when the verified safe-home glide keeps failing
  // (e.g. a degraded bus), homing can never reach step 2 and the freeze would otherwise wedge
  // everything until an environment restart. After the same "you're stuck" threshold as the
  // remedy picture, offer „Trotzdem fortsetzen" — the server forces a proximity-gated resync
  // from the follower's CURRENT pose to the leader and clears the freeze. Surfaced only on
  // the step-1 (stopped) screen so the normal two-step flow stays clean.
  const showForceResume = showRemedyPicture;
  const handleForceResume = () =>
    callStep(forceResumeTeleop, '„Trotzdem fortsetzen" fehlgeschlagen.');

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="alertdialog"
      aria-modal="true"
      aria-label="Kollision erkannt"
    >
      <div
        className={`su-plate su-plate--plate su-collision mx-4 max-w-lg p-8 ${
          isHomed ? 'su-collision--homed' : 'su-collision--stop'
        }`}
      >
        <div className="flex flex-col items-center gap-4 text-center">
          {isHomed && robotGlyphName(profile, 'arm') ? (
            <RobotGlyph profile={profile} part="arm" size="3rem" className="text-[color:var(--amber)]" />
          ) : (
            <Icon
              name="warning"
              size="3rem"
              className={isHomed ? 'text-[color:var(--amber)]' : 'text-[color:var(--danger)]'}
            />
          )}
          <span className="su-lab">
            {isHomed ? 'Schritt 2 von 2' : 'Schritt 1 von 2'}
          </span>
          <h2 className={`su-serif text-[34px] leading-none ${isHomed ? 'text-[color:var(--amber)]' : 'text-[color:var(--danger)]'}`}>
            {isHomed ? 'Follower in Grundstellung' : 'STOPP — Kollision erkannt'}
          </h2>
          <p className="text-[15px] text-[var(--ink-2)]">
            {collision.message ||
              (isHomed
                ? 'Bringe den Leader-Arm in die gleiche Stellung und klicke dann auf ' +
                  '„Teleoperation fortsetzen".'
                : 'Der Roboterarm wurde gegen ein Hindernis gedrückt und angehalten. Entferne ' +
                  'zuerst das Hindernis und klicke dann auf „Follower in Grundstellung fahren".')}
          </p>
          {hint && <p className="text-sm font-medium text-[color:var(--amber)]">{hint}</p>}
          {showRemedyPicture && (
            <div className="flex w-full flex-col items-center gap-1 rounded-[14px] bg-[var(--amber-wash)] p-3">
              <RemedyPicture profile={profile} />
              <p className="text-sm font-medium text-[color:var(--amber)]">
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
                        ? 'border-transparent bg-[var(--success-wash)] text-[color:var(--success)]'
                        : 'border-transparent bg-[var(--amber-wash)] text-[color:var(--amber)]'
                    }`}
                    title={`Gelenk ${i + 1}: noch ${deg}° bis zur Grundstellung`}
                  >
                    <span>G{i + 1}</span>
                    <span>{done ? <Icon name="check" /> : `${deg}°`}</span>
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
              className="su-key su-key--signal su-key--lg mt-2"
            >
              {busy ? 'Wird fortgesetzt …' : 'Teleoperation fortsetzen'}
            </button>
          ) : (
            <div className="mt-2 flex w-full flex-col items-center gap-2">
              <button
                type="button"
                onClick={handleHome}
                disabled={busy || isHoming}
                className="su-key su-key--danger su-key--lg"
              >
                {isHoming ? 'Fährt in Grundstellung …' : 'Follower in Grundstellung fahren'}
              </button>
              {showForceResume && (
                <button
                  type="button"
                  onClick={handleForceResume}
                  disabled={busy || isHoming}
                  className="text-sm font-medium text-[var(--ink-2)] underline underline-offset-2 hover:text-[var(--ink)] disabled:opacity-50"
                  title="Bringe den Leader-Arm nah an die aktuelle Stellung des Followers, dann hier klicken."
                >
                  Klappt nicht? Trotzdem fortsetzen (Notentriegelung)
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
