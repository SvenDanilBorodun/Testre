/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Neu": THE table of Sammlung creation buttons (owner decision D5). The
// Blockly flyout (sammlung/toolboxCategories.js, as icon buttons), the
// Sammlung drawer (both notations) and the code sidebar (its row buttons and
// „+ Neu") all read these rows, so a button, its words, its icon and when it is
// offered are decided once. Every action is dispatched through the page's
// provider (`provider.dispatchAction`).
//
// When a row is offered (owner decision D9): Vormachen and the camera pin need
// the real rig OUTSIDE the simulator; the Sim-Tisch pin exists only INSIDE it.

import { DE } from '../blocks/messages_de';

const onRig = (c) => !!c.hardware && !c.simMode;

/**
 * The camera pin — its creation row below and the generic „Ziel setzen" block
 * of the Ziele flyout (sammlung/toolboxCategories.js) — is set by a click into
 * the SCENE CAMERA, which the simulator has not; there a Ziel is set on the
 * Sim-Tisch (N5 of the 2026-10-18 semantics round). One rule for both.
 */
export function cameraPinOffered(c) {
  return !(c && typeof c === 'object' && c.simMode);
}

export const NEW_ACTION_ROWS = Object.freeze([
  Object.freeze({
    id: 'teachRecording',
    tab: 'aufnahmen',
    label: DE.FLY_TEACH_RECORDING,
    icon: 'record',
    action: Object.freeze({ type: 'teach', kind: 'recording' }),
    allowed: (c) => onRig(c) && !!c.teach,
  }),
  Object.freeze({
    id: 'teachZiel',
    tab: 'ziele',
    label: DE.FLY_TEACH_ZIEL,
    icon: 'ziel',
    action: Object.freeze({ type: 'teach', kind: 'ziel' }),
    allowed: (c) => onRig(c) && !!c.teach,
  }),
  Object.freeze({
    id: 'pinCamera',
    tab: 'ziele',
    label: DE.FLY_PIN_CAMERA,
    icon: 'camera',
    action: Object.freeze({ type: 'pinCamera' }),
    allowed: (c) => onRig(c) && !!c.pinCamera && cameraPinOffered(c),
  }),
  Object.freeze({
    id: 'pinSim',
    tab: 'ziele',
    label: DE.FLY_PIN_SIM,
    icon: 'box',
    action: Object.freeze({ type: 'pinSim' }),
    allowed: (c) => !!c.pinSim && !!c.simMode,
  }),
  Object.freeze({
    id: 'teachPose',
    tab: 'positionen',
    label: DE.FLY_TEACH_POSE,
    icon: 'pose',
    action: Object.freeze({ type: 'teach', kind: 'pose' }),
    allowed: (c) => onRig(c) && !!c.teach,
  }),
]);

/**
 * The creation buttons the page can offer now: for one Sammlung `tab`, or every
 * tab (in flyout order) when `tab` is omitted. `[{id, label, icon, action}]`.
 */
export function newActionsFor(capabilities, tab) {
  if (!capabilities || typeof capabilities !== 'object') return [];
  return NEW_ACTION_ROWS
    .filter((row) => (tab === undefined || row.tab === tab) && row.allowed(capabilities))
    .map(({ id, label, icon, action }) => ({ id, label, icon, action }));
}

// The tabs whose creation rows include Vormachen (the Variablen tab has none).
const TEACH_TABS = Object.freeze(['aufnahmen', 'ziele', 'positionen']);

// In the simulator the Vormachen rows of a tab are gone (D9); the tab then
// says why instead of silently offering less.
function simHidesTeach(c, tab) {
  return !!c && typeof c === 'object' && !!c.hardware && !!c.simMode && !!c.teach
    && TEACH_TABS.includes(tab);
}

/**
 * „Vormachen geht nur am echten Roboter – beende zuerst den Simulator." for a
 * Bewegungen/Ziele/Positionen tab in the simulator on a rig that can teach,
 * else null. The drawer and the code sidebar show it as one sentence.
 */
export function teachHintFor(capabilities, tab) {
  return simHidesTeach(capabilities, tab) ? DE.TEACH_SIM_HINT : null;
}

/**
 * The same hint as the three short lines the Blockly flyout draws (one label
 * wider than a card would widen the whole flyout), else null.
 */
export function teachHintLinesFor(capabilities, tab) {
  return simHidesTeach(capabilities, tab)
    ? [DE.TEACH_SIM_HINT_LINE1, DE.TEACH_SIM_HINT_LINE2, DE.TEACH_SIM_HINT_LINE3]
    : null;
}

/** The one Vormachen button of a tab (a code sidebar row's), or null. */
export function teachActionFor(capabilities, tab) {
  return newActionsFor(capabilities, tab).find((a) => a.action.type === 'teach') || null;
}
