/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Neu" for a code program: the flyout's creation buttons, as one table the
// drawer (per tab) and the code sidebar (all tabs) both read, gated by the
// same capabilities the Blockly flyout reads.

import { describe, it, expect } from 'vitest';
import { DE } from '../../blocks/messages_de';
import { newActionsFor } from '../newActions';

const RIG = { hardware: true, teach: true, simMode: false, pinCamera: true, pinSim: true };

describe('newActionsFor', () => {
  it('per tab, on a calibrated rig', () => {
    expect(newActionsFor(RIG, 'aufnahmen').map((a) => a.label)).toEqual([DE.FLY_TEACH_RECORDING]);
    expect(newActionsFor(RIG, 'ziele').map((a) => a.action)).toEqual([
      { type: 'teach', focus: 'ziel' }, { type: 'pinCamera' }, { type: 'pinSim' },
    ]);
    expect(newActionsFor(RIG, 'positionen').map((a) => a.action)).toEqual([{ type: 'teach', focus: 'pose' }]);
    expect(newActionsFor(RIG, 'variablen')).toEqual([]);
  });

  it('all tabs at once, each action once', () => {
    expect(newActionsFor(RIG).map((a) => a.label)).toEqual([
      DE.FLY_TEACH_RECORDING, DE.FLY_TEACH_ZIEL, DE.FLY_PIN_CAMERA, DE.FLY_PIN_SIM, DE.FLY_TEACH_POSE,
    ]);
  });

  it('no Vormachen in the simulator or without hardware, no camera pin without the capability', () => {
    expect(newActionsFor({ ...RIG, simMode: true, pinCamera: false }).map((a) => a.action))
      .toEqual([{ type: 'pinSim' }]);
    expect(newActionsFor({ ...RIG, hardware: false }).some((a) => a.action.type === 'teach')).toBe(false);
    expect(newActionsFor(null)).toEqual([]);
  });
});
