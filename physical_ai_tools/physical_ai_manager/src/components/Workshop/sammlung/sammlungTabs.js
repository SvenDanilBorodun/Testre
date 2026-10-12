/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * The four Sammlung groups: their ORDER and their COLOURS, decided once.
 *
 * Read by the toolbox (blocks/toolbox.js, category order + colour), the
 * drawer's tab row (SammlungDrawer.jsx), the code sidebar
 * (code/CodeWorkspace.jsx), the asset view models (assetIndex.js), the twin
 * markers (markers.js) and the destination and replay blocks
 * (blocks/destinations.js, blocks/trajectories.js) — so a group can never be
 * blue in one place and rose in another.
 *
 * Blockly-free and three-free: every reader above (the 3D twin's marker list
 * included) may import it without pulling either into its chunk.
 */

import { DE } from '../blocks/messages_de';

/** The group order everywhere: toolbox, drawer tabs, code sidebar. */
export const SAMMLUNG_TABS = Object.freeze(['aufnahmen', 'ziele', 'positionen', 'variablen']);

/**
 * One colour per group. „Bewegungen" is rose so it can never be mistaken for
 * the static „Arm" category's blue.
 */
export const SAMMLUNG_COLOURS = Object.freeze({
  aufnahmen: '#e11d48',
  ziele: '#f59e0b',
  positionen: '#14b8a6',
  variablen: '#a78bfa',
});

/** The group's visible name (the toolbox category name). */
export const SAMMLUNG_TAB_LABEL_DE = Object.freeze({
  aufnahmen: DE.CATEGORY_AUFNAHMEN,
  ziele: DE.CATEGORY_ZIELE,
  positionen: DE.CATEGORY_POSITIONEN,
  variablen: DE.CATEGORY_VARIABLEN,
});
