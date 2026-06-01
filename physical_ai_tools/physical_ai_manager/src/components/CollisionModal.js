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

// Teleop force/collision e-stop modal. A big, blocking, non-dismissible overlay shown whenever
// state.tasks.collision.active is true (set by the /task/status phase=COLLISION handler in
// useRosTopicSubscription.js). The student must bring the leader arm near the follower's home
// pose and press "Teleoperation neu starten", which calls the RESUME_TELEOP service. The server
// refuses (success=false) with a German hint if the leader is still too far; it succeeds once
// the leader is close, and the modal then unmounts when the next /task/status carries a
// non-COLLISION phase (which clears collision.active).

import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';

import { useRosServiceCaller } from '../hooks/useRosServiceCaller';

export default function CollisionModal() {
  const collision = useSelector((state) => state.tasks.collision);
  const { resumeTeleop } = useRosServiceCaller();
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState('');

  if (!collision?.active) {
    return null;
  }

  const handleResume = async () => {
    setBusy(true);
    setHint('');
    try {
      const result = await resumeTeleop();
      if (result && result.success === false) {
        // Leader too far / not available — re-prompt inline and keep the modal open.
        setHint(
          result.message ||
            'Bitte den Leader-Arm näher an die Grundstellung bringen und erneut versuchen.'
        );
      } else {
        // Success: the modal closes when the next /task/status (phase != COLLISION) arrives.
        setHint(result?.message || 'Teleoperation wird wiederhergestellt …');
      }
    } catch (err) {
      setHint('Fehler beim Wiederherstellen — bitte erneut versuchen.');
      toast.error('Teleoperation neu starten fehlgeschlagen.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="alertdialog"
      aria-modal="true"
      aria-label="Kollision erkannt"
    >
      <div className="mx-4 max-w-lg rounded-2xl border-2 border-red-500 bg-white p-8 shadow-2xl">
        <div className="flex flex-col items-center gap-4 text-center">
          <div className="text-5xl" aria-hidden="true">
            ⚠️
          </div>
          <h2 className="text-2xl font-bold text-red-600">STOPP — Kollision erkannt</h2>
          <p className="text-gray-700">
            {collision.message ||
              'Der Roboterarm wurde gegen ein Hindernis gedrückt und sicher in die Grundstellung ' +
                'gefahren. Bringe den Leader-Arm in die Nähe der Grundstellung des Followers und ' +
                'klicke dann auf „Teleoperation neu starten".'}
          </p>
          {hint && <p className="text-sm font-medium text-amber-700">{hint}</p>}
          <button
            type="button"
            onClick={handleResume}
            disabled={busy}
            className="mt-2 rounded-xl bg-red-600 px-6 py-3 text-base font-semibold text-white shadow hover:bg-red-700 disabled:opacity-50"
          >
            {busy ? 'Wird wiederhergestellt …' : 'Teleoperation neu starten'}
          </button>
        </div>
      </div>
    </div>
  );
}
