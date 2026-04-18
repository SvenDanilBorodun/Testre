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

/**
 * ProgressBar used inside the dark glass control dock and elsewhere.
 * Uses CSS variables so the accent color follows the theme.
 */
export default function ProgressBar({ percent = 0, dark = true }) {
  const textColor = 'text-white';

  const classProgressBarText = clsx(
    'absolute',
    'left-0',
    'top-0',
    'w-full',
    'h-full',
    'flex',
    'items-center',
    'justify-center',
    'font-mono',
    'text-[13px]',
    'pointer-events-none',
    'z-10',
    textColor
  );

  return (
    <div
      className={clsx(
        'w-full h-6 rounded-full relative overflow-hidden',
        dark ? 'bg-white/10' : 'bg-[var(--bg-sunk)]'
      )}
    >
      <div
        className="h-full rounded-full transition-all duration-300"
        style={{
          width: `${Math.max(0, Math.min(100, percent))}%`,
          background: 'linear-gradient(90deg, var(--accent), var(--success))',
        }}
      ></div>
      <span className={classProgressBarText}>{percent}%</span>
    </div>
  );
}
