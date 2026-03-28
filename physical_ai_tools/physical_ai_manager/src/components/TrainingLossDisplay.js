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
import { MdCloud } from 'react-icons/md';

export default function TrainingLossDisplay() {
  const classContainer = clsx(
    'bg-white',
    'border',
    'border-gray-200',
    'rounded-2xl',
    'shadow-lg',
    'p-6',
    'w-full',
    'max-w-lg'
  );

  return (
    <div className={classContainer}>
      <div className="text-xl font-semibold text-gray-700 mb-4 flex items-center gap-2">
        <MdCloud size={24} className="text-teal-500" />
        <span>Cloud-Training</span>
      </div>

      <div className="bg-teal-50 border border-teal-200 rounded-lg p-4">
        <p className="text-sm text-teal-700">
          Das Training läuft auf einer Cloud-GPU. Wenn du auf „Training starten" klickst, wird der Auftrag an die Cloud gesendet. Den Fortschritt kannst du in der Trainingsverlauf-Tabelle unten verfolgen.
        </p>
        <p className="text-sm text-teal-600 mt-2">
          Abgeschlossene Modelle werden automatisch auf HuggingFace Hub hochgeladen und können für die Inferenz verwendet werden.
        </p>
      </div>
    </div>
  );
}
