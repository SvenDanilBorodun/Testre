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
import clsx from 'clsx';
import RobotTypeSelector from '../components/RobotTypeSelector';
import HeartbeatStatus from '../components/HeartbeatStatus';
import packageJson from '../../package.json';

export default function HomePage() {
  const classContainer = clsx(
    'w-full',
    'h-full',
    'flex',
    'items-center',
    'justify-center',
    'pt-10'
  );

  const classHeartbeatStatus = clsx('absolute', 'top-5', 'left-35', 'z-10');

  const aboutEduBotics = () => {
    return (
      <div className="flex flex-col items-center justify-center m-5 gap-5 min-w-72">
        <p className="text-3xl font-bold">EduBotics</p>
        <div className="flex flex-col items-center justify-center gap-2">
          <div className="flex flex-col items-center justify-center">
            <div>
              <p>{packageJson.description}</p>
            </div>
            <div className="flex flex-row items-center justify-center gap-2">
              <p className="font-semibold">Version</p>
              <p className="bg-teal-400 text-white rounded-2xl font-semibold px-2 py-1 shadow-md">
                {packageJson.version}
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className={classContainer}>
      <div className={classHeartbeatStatus}>
        <HeartbeatStatus />
      </div>
      <div className="flex flex-raw items-center justify-center gap-16">
        {aboutEduBotics()}
        <RobotTypeSelector />
      </div>
    </div>
  );
}
