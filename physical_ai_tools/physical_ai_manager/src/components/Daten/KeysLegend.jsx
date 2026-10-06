// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

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
