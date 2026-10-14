// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The EduBotics mark of the rail (Sucher §4.11, D20): a dark disc with four
// rising bars, the last in signal orange. CSS spans only; rail only (the
// favicon and the manifest keep their own mark).

import React from 'react';
import clsx from 'clsx';

const BAR_HEIGHTS = [30, 45, 62, 80];

export default function BrandMark({ size = 64, className }) {
  return (
    <span
      role="img"
      aria-label="EduBotics"
      className={clsx('su-brand', className)}
      style={{ width: size, height: size, '--s': `${size}px` }}
    >
      <span className="su-brand__bars" aria-hidden="true">
        {BAR_HEIGHTS.map((h, i) => (
          <span key={h} className={`su-brand__bar su-brand__bar--${i}`} style={{ height: `${h}%` }} />
        ))}
      </span>
    </span>
  );
}
