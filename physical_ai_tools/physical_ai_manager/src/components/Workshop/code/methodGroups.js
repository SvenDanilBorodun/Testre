/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The „Methoden" column's groups ARE the Blockly toolbox's categories: a
// method sits in the category whose blocks hold its block type, under that
// category's own name and colour, in the toolbox's own order. Nothing is
// restated here — a renamed or recoloured category (or a new heading item
// between them) reaches the column without a change to this file. Pure;
// imported only by the lazy MethodenPanel.jsx.
//
// Overrides, because those blocks live in the Sammlung's dynamic categories
// (no static `contents`): `replay` → the Bewegungen/Aufnahmen group,
// `pin`/`pin_current`/`ziel` → the Ziele group. A code-only row (`zeige`) has
// no block at all: it gets the group „Anzeigen", coloured like the Variablen
// group (it shows a value there), last. The two types sit with `find`.

import { buildToolbox, SAMMLUNG_CATEGORY_KEYS } from '../blocks/toolbox';
import { CODE_DE } from './codeMessagesDe';

const SAMMLUNG_OVERRIDES = Object.freeze({
  replay: 'AUFNAHMEN',
  pin: 'ZIELE',
  pin_current: 'ZIELE',
  ziel: 'ZIELE',
});

// The block whose category the two types join.
const TYPES_BESIDE_BLOCK = 'edubotics_find_object';

/** The id of the group a code-only row joins. */
export const SHOW_GROUP_ID = 'anzeigen';

/**
 * `docs` = `{methods, types}` (methodDocs.METHOD_DOCS) →
 * `[{id, label, colour, methods, types}]`, in the toolbox's category order,
 * empty groups dropped, „Anzeigen" last.
 */
export function methodGroups(docs) {
  const methods = docs && Array.isArray(docs.methods) ? docs.methods : [];
  const types = docs && Array.isArray(docs.types) ? docs.types : [];
  const categories = (buildToolbox().contents || []).filter((item) => item && item.kind === 'category');
  const groups = categories.map((c) => ({
    id: c.custom || c.toolboxitemid || c.name,
    label: c.name,
    colour: c.colour,
    methods: [],
    types: [],
    custom: c.custom || null,
    blocks: new Set((Array.isArray(c.contents) ? c.contents : [])
      .filter((b) => b && b.kind === 'block').map((b) => b.type)),
  }));
  const byCustom = (key) => groups.find((g) => g.custom === key) || null;
  const byBlock = (type) => groups.find((g) => g.blocks.has(type)) || null;
  const variablen = byCustom(SAMMLUNG_CATEGORY_KEYS.VARIABLEN);
  const show = {
    id: SHOW_GROUP_ID,
    label: CODE_DE.METHODS_GROUP_SHOW,
    colour: variablen ? variablen.colour : null,
    methods: [],
    types: [],
  };
  for (const m of methods) {
    let group = null;
    if (m.table === 'code') group = show;
    else if (SAMMLUNG_OVERRIDES[m.name]) group = byCustom(SAMMLUNG_CATEGORY_KEYS[SAMMLUNG_OVERRIDES[m.name]]);
    else group = byBlock(m.block_type);
    if (group) group.methods.push(m);
  }
  const typeGroup = byBlock(TYPES_BESIDE_BLOCK);
  if (typeGroup) typeGroup.types.push(...types);
  const strip = ({
    id, label, colour, methods: ms, types: ts,
  }) => ({
    id, label, colour, methods: ms, types: ts,
  });
  return [...groups, show]
    .filter((g) => g.methods.length > 0 || g.types.length > 0)
    .map(strip);
}
