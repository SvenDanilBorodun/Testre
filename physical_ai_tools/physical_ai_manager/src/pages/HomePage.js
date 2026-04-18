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

import React from 'react';
import { useSelector, useDispatch } from 'react-redux';
import RobotTypeSelector from '../components/RobotTypeSelector';
import HeartbeatStatus from '../components/HeartbeatStatus';
import { Card, Pill, Btn, SectionHeader } from '../components/EbUI';
import packageJson from '../../package.json';
import PageType from '../constants/pageType';
import { moveToPage } from '../features/ui/uiSlice';

function getGreeting() {
  const h = new Date().getHours();
  if (h < 11) return 'Guten Morgen';
  if (h < 18) return 'Hallo';
  return 'Guten Abend';
}

export default function HomePage() {
  const dispatch = useDispatch();
  const robotType = useSelector((state) => state.tasks.taskStatus.robotType);
  const fullName = useSelector((state) => state.auth.fullName);
  const username = useSelector((state) => state.auth.username);
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const imageTopicList = useSelector((state) => state.ros.imageTopicList);

  const firstName = (fullName && fullName.split(' ')[0]) || username || 'Schüler';

  const bridgeReady = heartbeatStatus === 'connected';
  const camCount = imageTopicList?.length || 0;

  const goRecord = () => {
    if (!robotType || robotType.trim() === '') return;
    dispatch(moveToPage(PageType.RECORD));
  };

  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="max-w-[1100px] mx-auto px-10 py-10">
        <SectionHeader
          eyebrow="Startseite"
          title={`${getGreeting()}, ${firstName}.`}
          description="Wähle deinen Roboter und leg los."
          right={<HeartbeatStatus />}
          className="mb-8"
        />

        <div className="grid grid-cols-12 gap-6">
          {/* Hero robot card */}
          <div className="col-span-12 lg:col-span-7">
            <Card padded={false}>
              <div className="relative h-[360px] camera-noise rounded-t-[var(--radius-lg)] overflow-hidden">
                <svg
                  viewBox="0 0 600 400"
                  className="absolute inset-0 w-full h-full opacity-80"
                  preserveAspectRatio="xMidYMid meet"
                >
                  <defs>
                    <linearGradient id="armg" x1="0" x2="0" y1="0" y2="1">
                      <stop offset="0" stopColor="#E8EBEC" />
                      <stop offset="1" stopColor="#9CA1A6" />
                    </linearGradient>
                  </defs>
                  <rect x="220" y="320" width="160" height="50" rx="6" fill="#24292B" />
                  <rect x="270" y="200" width="60" height="130" rx="4" fill="url(#armg)" />
                  <rect x="260" y="170" width="80" height="40" rx="6" fill="#3B4145" />
                  <rect x="290" y="110" width="20" height="70" rx="4" fill="url(#armg)" />
                  <circle cx="300" cy="105" r="18" fill="#3B4145" />
                  <rect x="288" y="80" width="24" height="22" rx="3" fill="url(#armg)" />
                  <rect x="285" y="60" width="12" height="26" rx="2" fill="#9CA1A6" />
                  <rect x="303" y="60" width="12" height="26" rx="2" fill="#9CA1A6" />
                  <circle cx="300" cy="105" r="3" fill="var(--accent)" />
                </svg>
                <div className="absolute top-4 left-4 flex items-center gap-2">
                  <Pill tone="glass" dot>
                    LIVE
                  </Pill>
                  <Pill tone="glass">
                    <span className="font-mono">ros_bridge</span>
                  </Pill>
                </div>
                <div className="absolute bottom-4 left-4 right-4 flex items-end justify-between gap-4">
                  <div className="min-w-0">
                    <div className="text-white/60 font-mono text-[10px] uppercase tracking-wider">
                      Aktueller Roboter
                    </div>
                    <div className="text-white font-semibold text-xl tracking-tight leading-tight truncate">
                      {robotType || 'Kein Robotertyp gewählt'}
                    </div>
                  </div>
                  <div className="font-mono text-[11px] text-white/60 shrink-0 whitespace-nowrap">
                    EduBotics v{packageJson.version}
                  </div>
                </div>
              </div>
              <div className="p-5 flex flex-wrap items-center justify-between gap-3">
                <div className="text-sm text-[var(--ink-3)]">
                  <span className="text-[var(--ink)] font-semibold">
                    {bridgeReady ? 'Bereit.' : 'Warte auf Roboter.'}
                  </span>{' '}
                  {bridgeReady
                    ? `ROS-Bridge läuft, ${camCount} Kamera${camCount === 1 ? '' : 's'} erkannt.`
                    : 'Prüfe die Verbindung zum Roboter.'}
                </div>
                <div className="flex items-center gap-2">
                  <Btn
                    variant="primary"
                    onClick={goRecord}
                    disabled={!robotType || !bridgeReady}
                  >
                    Aufnahme starten
                  </Btn>
                </div>
              </div>
            </Card>
          </div>

          {/* Robot type selector */}
          <div className="col-span-12 lg:col-span-5">
            <RobotTypeSelector />
          </div>
        </div>

        <div className="mt-8 text-[11px] font-mono text-[var(--ink-4)]">
          {packageJson.description} · v{packageJson.version}
        </div>
      </div>
    </div>
  );
}
