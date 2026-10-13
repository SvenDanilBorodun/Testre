// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A plate of the device body (Sucher §4.1): `plate` (raised, the default),
// `paper` (a white receipt) or `well` (sunk into the body).

import React from 'react';
import clsx from 'clsx';

const TONES = new Set(['plate', 'paper', 'well']);

export default function Plate({ as: Tag = 'section', tone = 'plate', className, children, ...rest }) {
  const t = TONES.has(tone) ? tone : 'plate';
  return (
    <Tag {...rest} className={clsx('su-plate', `su-plate--${t}`, className)}>
      {children}
    </Tag>
  );
}
