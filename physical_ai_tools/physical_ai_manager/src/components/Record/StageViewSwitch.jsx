// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
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
