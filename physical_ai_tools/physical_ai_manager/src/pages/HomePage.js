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
import RobotStartup from '../components/RobotStartup';
import { Btn, SectionHeader } from '../components/EbUI';
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
  const armReady = useSelector((state) => state.armStartup.armReady);

  const firstName = (fullName && fullName.split(' ')[0]) || username || 'Schüler';

  const bridgeReady = heartbeatStatus === 'connected';

  const goRecord = () => {
    if (!robotType || robotType.trim() === '') return;
    if (!armReady) return;
    dispatch(moveToPage(PageType.RECORD));
  };

  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="eb-shell">
        <SectionHeader
          eyebrow="Startseite"
          title={`${getGreeting()}, ${firstName}.`}
          description="Wähle deinen Roboter und leg los."
          right={<HeartbeatStatus />}
          className="mb-6 md:mb-8"
        />

        <div className="grid grid-cols-12 gap-4 md:gap-6">
          {/* Animated robot-startup hero — homing + leader-sync live here */}
          <div className="col-span-12 lg:col-span-7 xl:col-span-8 flex flex-col gap-4">
            <RobotStartup packageVersion={packageJson.version} />

            {/* Aufnahme is gated on a verified arm — disabled until armReady */}
            <div className="flex flex-wrap items-center justify-between gap-3 px-1">
              <div className="text-sm text-[var(--ink-3)]">
                {armReady
                  ? 'Der Roboter ist synchronisiert — du kannst jetzt aufnehmen.'
                  : 'Starte zuerst den Roboter, um aufzunehmen.'}
              </div>
              <Btn
                variant="primary"
                onClick={goRecord}
                disabled={!robotType || !bridgeReady || !armReady}
                title={!armReady ? 'Starte zuerst den Roboter' : undefined}
              >
                Aufnahme starten
              </Btn>
            </div>
          </div>

          {/* Robot type selector */}
          <div className="col-span-12 lg:col-span-5 xl:col-span-4">
            <RobotTypeSelector />
          </div>
        </div>

        <div className="mt-6 md:mt-8 text-[11px] font-mono text-[var(--ink-4)]">
          {packageJson.description} · v{packageJson.version}
        </div>
      </div>
    </div>
  );
}
