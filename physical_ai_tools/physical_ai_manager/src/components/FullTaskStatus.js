// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Author: Kiwoong Park

import React from 'react';
import clsx from 'clsx';

import { useSelector } from 'react-redux';

const classBody = clsx(
  'h-full',
  'w-full',
  'max-w-xs',
  'text-center',
  'flex',
  'flex-col',
  'items-center',
  'justify-around',
  'gap-1',
  'rounded-[var(--radius-lg)]',
  'border',
  'border-white/15',
  'py-2',
  'px-3',
  'box-border',
  'bg-white/[0.06]',
  'text-white'
);

const MultiTaskFontSizeTitle = 'clamp(0.8rem, 1vw, 1.1rem)';
const MultiTaskFontSizeNumber = 'clamp(0.9rem, 1.1vw, 1.3rem)';

const SingleTaskFontSizeTitle = 'clamp(0.9rem, 1.1vw, 1.2rem)';
const SingleTaskFontSizeNumber = 'clamp(1.2rem, 1.3vw, 1.6rem)';

export default function FullTaskStatus() {
  const useMultiTaskMode = useSelector((state) => state.tasks.useMultiTaskMode);
  const currentScenarioNumber = useSelector(
    (state) => state.tasks.taskStatus.currentScenarioNumber
  );

  return (
    <div className={classBody}>
      <div
        className="w-full flex justify-center items-center text-[10px] font-mono uppercase tracking-wider text-white/60"
        style={{ fontSize: useMultiTaskMode ? MultiTaskFontSizeTitle : SingleTaskFontSizeTitle }}
      >
        Szenario
      </div>
      <div
        className="w-full h-full flex justify-center items-center bg-white/10 rounded-[var(--radius-sm)] px-3 font-mono font-semibold whitespace-nowrap"
        style={{
          fontSize: useMultiTaskMode ? MultiTaskFontSizeNumber : SingleTaskFontSizeNumber,
        }}
      >
        <span className="font-semibold">{currentScenarioNumber}</span>
      </div>
    </div>
  );
}
