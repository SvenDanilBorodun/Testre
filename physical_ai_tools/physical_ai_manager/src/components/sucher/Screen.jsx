// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A dark „Dunkelkammer" screen (Sucher §4.2), wherever something is watched:
// cameras, 3D, charts, logs. `bezel` frames it (full / thin / none); callers
// leave at least 8 px around a full bezel. `power` plays M1 („Sucher geht an")
// once when the screen mounts — a later change of the prop does not replay it.

import React, { useRef } from 'react';
import clsx from 'clsx';

const BEZELS = new Set(['full', 'thin', 'none']);

export default function Screen({
  as: Tag = 'div', bezel = 'full', power = false, className, children, ...rest
}) {
  const poweredOnMount = useRef(!!power);
  const b = BEZELS.has(bezel) ? bezel : 'full';
  return (
    <Tag
      {...rest}
      className={clsx('su-screen', `su-screen--${b}`, poweredOnMount.current && 'su-screen--power', className)}
    >
      {children}
    </Tag>
  );
}
