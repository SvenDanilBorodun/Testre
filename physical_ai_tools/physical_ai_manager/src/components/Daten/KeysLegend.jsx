// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The player's keys, named in words (the mockup's `.keys`; key labels are
// words, never ⇧/⌘ — the Aufnahme rule).

import React from 'react';
import COPY from '../../features/editDataset/datenCopy';

const P = COPY.player;
const Kbd = ({ children }) => <kbd className="dat-kbd">{children}</kbd>;

export default function KeysLegend() {
  return (
    <div className="dat-keys">
      <span><Kbd>{P.keySpace}</Kbd> {P.keySpaceDo}</span>
      <span><Kbd>{P.keyLeft}</Kbd> <Kbd>{P.keyRight}</Kbd> {P.keyArrowsDo}</span>
      <span><Kbd>{P.keyShift}</Kbd>+<Kbd>{P.keyLeft}</Kbd> <Kbd>{P.keyRight}</Kbd> {P.keyShiftDo}</span>
      <span><Kbd>{P.keyUp}</Kbd> <Kbd>{P.keyDown}</Kbd> {P.keyUpDownDo}</span>
      <span><Kbd>{P.keyDelete}</Kbd> {P.keyDeleteDo}</span>
    </div>
  );
}
