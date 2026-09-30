// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A small segmented control on the dark stage: the view switch („Kameras" |
// „3D" | „Beides") and the 3D tile's camera presets use the same one. The
// pressed state is aria-pressed, one group label names the whole control. A
// mouse click gives the focus back to the page (Space starts a recording).

import React from 'react';
import { releasePointerFocus } from './ActionBar';

export default function StageViewSwitch({ groupLabel, options, value, onChange, className = '' }) {
  return (
    <div className={`rec-seg ${className}`.trim()} role="group" aria-label={groupLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          onClick={(e) => {
            releasePointerFocus(e);
            if (o.value !== value) onChange(o.value);
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
