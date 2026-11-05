// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
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
