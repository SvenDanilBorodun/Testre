/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The handheld lens step of the calibration panel (spec 40 S4, decision D9's
// fallback): the student moves the clip-on target through the image by hand;
// the robot captures a view whenever the target rests in an image cell it has
// not filled yet, in two sizes (near and farther away). This view draws the
// robot's progress (`handheld` of the state) and offers „Abbrechen"
// (/calibration/cancel: nothing written).

import React from 'react';
import Icon from '../../icons/Icon';
import { CALIBRATION_DE } from './calibrationCopy';

// The robot's size classes (handheld.py): 0 = tag edge < 45 px (farther away), 1 = near.
const SIZE_CLASSES = [
  { size: 1, label: CALIBRATION_DE.HANDHELD_CELL_NEAR },
  { size: 0, label: CALIBRATION_DE.HANDHELD_CELL_FAR },
];

function HandheldView({ handheld, onCancel, disabled = false }) {
  const filled = new Set((handheld ? handheld.filled : []).map(([c, r, s]) => `${c}:${r}:${s}`));
  const cols = handheld ? handheld.cols : 4;
  const rows = handheld ? handheld.rows : 3;
  const cells = [];
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      cells.push(
        <div key={`${c}:${r}`} className="flex flex-col gap-0.5 rounded border border-[var(--line)] p-0.5">
          {SIZE_CLASSES.map(({ size, label }) => {
            const on = filled.has(`${c}:${r}:${size}`);
            return (
              <span
                key={size}
                data-filled={on ? 'true' : 'false'}
                title={label}
                className={'h-3 rounded-sm ' + (on ? 'bg-[var(--accent)]' : 'bg-[var(--bg-sunk)]')}
              />
            );
          })}
        </div>,
      );
    }
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-[var(--ink-3)]">{CALIBRATION_DE.HANDHELD_HINT}</p>
      <div
        role="group"
        aria-label={CALIBRATION_DE.HANDHELD_GRID}
        className="grid max-w-xs gap-1"
        style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
      >
        {cells}
      </div>
      <div>
        <button
          type="button"
          onClick={onCancel}
          disabled={disabled}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-[var(--line)] bg-white text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] disabled:opacity-50"
        >
          <Icon name="close" />
          {CALIBRATION_DE.HANDHELD_CANCEL}
        </button>
      </div>
    </div>
  );
}

export default HandheldView;
