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

export default function TrainingProgressBar() {
  const classContainer = clsx('w-full', 'rounded-lg', 'p-2');

  const classStepText = clsx(
    'text-lg',
    'font-semibold',
    'text-gray-700',
    'mb-2',
    'flex',
    'justify-between',
    'items-center'
  );

  return (
    <div className={classContainer}>
      <div className={classStepText}>
        <span>Trainingsfortschritt</span>
        <span className="text-teal-600 text-sm">Cloud-GPU</span>
      </div>

      <div className="text-sm text-gray-500 text-center">
        Siehe Trainingsverlauf unten für Statusaktualisierungen
      </div>
    </div>
  );
}
