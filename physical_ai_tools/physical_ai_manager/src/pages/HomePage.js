// Copyright 2025 EduBotics
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
//
// Author: Kiwoong Park
//
// ── Startseite ───────────────────────────────────────────────────────────────
//
// Lagebericht und nichts weiter: der Roboter, sein Zustand, die eigene Arbeit.
// Navigation macht die Leiste links — diese Seite berichtet, sie dirigiert
// nicht. (English notes below for the maintainer, per Rule §1.)
//
// WHAT THIS PAGE IS FOR, in one line: answer „ist mein Roboter bereit, und was
// habe ich schon gemacht?" without the student having to open another tab.
//
// THE ONE RULE EVERYTHING ELSE FOLLOWS: every value shown traces to a real
// source, or is not shown. What this replaced claimed three things it could
// not back up — a hardcoded „LIVE" pill that rendered identically while the
// bridge was down, a drawn SVG arm matching none of the four shipped robots,
// and `packageJson.version` (0.9.0) labelled as the EduBotics version when the
// product was at 2.17.0. Unknown now renders „—", never 0 and never a guess.
//
// NOT LIVE, ON PURPOSE. One fetch on mount plus `useRefetchOnFocus`; no
// Realtime channel and no interval anywhere. Start is where every session
// begins and where idle browsers sit for a whole lesson, and the cloud API is
// a single uvicorn worker with an in-process rate limiter. The two exceptions
// are subscriptions the app already runs: the /task/status heartbeat, and one
// 1 Hz /joint_states liveness probe (hooks/useJointLiveness), which is the
// difference between "the arm is silent" and "we never looked".

import React, { useState } from 'react';
import { useSelector } from 'react-redux';

import HeartbeatStatus from '../components/HeartbeatStatus';
import { Pill, SectionHeader } from '../components/EbUI';
import RobotHero from '../components/Home/RobotHero';
import HealthCard from '../components/Home/HealthCard';
import WorkCard from '../components/Home/WorkCard';
import useJointLiveness from '../hooks/useJointLiveness';
import useStudentWork from '../hooks/useStudentWork';
import { isCloudOnlyMode } from '../utils/cloudMode';
import { productVersion, buildId } from '../utils/productVersion';

function getGreeting() {
  const h = new Date().getHours();
  if (h < 11) return 'Guten Morgen';
  if (h < 18) return 'Hallo';
  return 'Guten Abend';
}

export default function HomePage() {
  const fullName = useSelector((state) => state.auth.fullName);
  const username = useSelector((state) => state.auth.username);
  const workgroupName = useSelector((state) => state.auth.workgroupName);
  const robotProfile = useSelector((state) => state.tasks.taskStatus.robotProfile);
  const robotType = useSelector((state) => state.tasks.taskStatus.robotType);

  const cloudOnly = isCloudOnlyMode();
  // Held here rather than in either consumer: the hero's dot and the
  // Health-Check's „Arm antwortet" row must be the same fact. Disabled in
  // cloud mode, where there is no bridge to subscribe to.
  const jointsLive = useJointLiveness({ enabled: !cloudOnly });
  const { work, loading } = useStudentWork();

  // Purely cosmetic, and deliberately not a Redux value: nothing else cares.
  const [firstName] = useState(
    () => (fullName && fullName.split(' ')[0]) || username || 'Schüler',
  );

  const version = productVersion();
  const build = buildId();

  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="eb-shell">
        <SectionHeader
          eyebrow="Startseite"
          title={`${getGreeting()}, ${firstName}.`}
          right={!cloudOnly ? <HeartbeatStatus /> : <Pill tone="neutral">Cloud-Modus</Pill>}
          className="mb-5 md:mb-6"
        />

        {/* Identity chips. Rendered only when the profile actually supplied
            them — an empty chip is furniture, not information. */}
        {workgroupName && (
          <div className="flex flex-wrap items-center gap-2 mb-5 md:mb-6 -mt-2">
            <Pill tone="accent">Gruppe {workgroupName}</Pill>
          </div>
        )}

        <div className="grid grid-cols-12 gap-4 md:gap-6">
          {/* In cloud mode there is no robot to show, so the hero would be a
              picture of nothing. The health card carries the mode instead and
              „Deine Arbeit" — which is cloud data — takes the full width. */}
          {!cloudOnly && (
            <div className="col-span-12 lg:col-span-8">
              <RobotHero jointsLive={jointsLive} />
            </div>
          )}

          <div className="col-span-12 lg:col-span-4">
            <HealthCard jointsLive={jointsLive} cloudOnly={cloudOnly} />
          </div>

          <div className="col-span-12">
            <WorkCard work={work} loading={loading} />
          </div>
        </div>

        {/* The footer names the release and the bundle so a support call can
            quote both. `productVersion()` returns null on a rig that genuinely
            does not know (an unpinned `?_v=latest`, Pi mode, the teacher web
            app) — in which case it says nothing rather than the SPA's own
            package version, which is what it used to print. */}
        <div className="mt-6 md:mt-8 flex flex-wrap gap-x-5 gap-y-1 text-[11px] font-mono text-[var(--ink-4)]">
          {version && <span>EduBotics {version}</span>}
          {(robotProfile || robotType) && <span>{robotProfile || robotType}</span>}
          {build && build !== 'dev' && <span>Build {build}</span>}
        </div>
      </div>
    </div>
  );
}
