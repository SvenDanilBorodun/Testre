/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Neu" for a code program (owner decision O4). A Blockly document creates
// Sammlung items from its flyout cards; a code program has no flyout, so the
// Sammlung drawer (per tab) and the code sidebar's „+ Neu" menu (all tabs)
// offer the same creation buttons, as actions the page's provider already
// dispatches (`provider.dispatchAction`), gated by the same capabilities the
// flyout reads (sammlung/toolboxCategories.js).

import { DE } from '../blocks/messages_de';

const canTeach = (c) => !!(c.hardware && c.teach && !c.simMode);

// tab → [label, action, capability test], in flyout order.
const NEW_ACTIONS = Object.freeze({
  aufnahmen: [[DE.FLY_TEACH_RECORDING, { type: 'teach', focus: 'recording' }, canTeach]],
  ziele: [
    [DE.FLY_TEACH_ZIEL, { type: 'teach', focus: 'ziel' }, canTeach],
    [DE.FLY_PIN_CAMERA, { type: 'pinCamera' }, (c) => !!c.pinCamera],
    [DE.FLY_PIN_SIM, { type: 'pinSim' }, (c) => !!c.pinSim],
  ],
  positionen: [[DE.FLY_TEACH_POSE, { type: 'teach', focus: 'pose' }, canTeach]],
});
const TAB_ORDER = ['aufnahmen', 'ziele', 'positionen'];

/**
 * The creation actions the rig can do now: for one drawer `tab`, or every tab
 * when `tab` is omitted. `[{label, action}]`.
 */
export function newActionsFor(capabilities, tab) {
  if (!capabilities || typeof capabilities !== 'object') return [];
  const tabs = tab === undefined ? TAB_ORDER : [tab];
  const out = [];
  for (const t of tabs) {
    for (const [label, action, allowed] of NEW_ACTIONS[t] || []) {
      if (allowed(capabilities)) out.push({ label, action });
    }
  }
  return out;
}
