/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Methoden column's groups are the toolbox's categories. Every expected
// label and colour below is READ from buildToolbox(), never written as a
// literal: the toolbox's names and colours change with the Sammlung round
// („Bewegungen", the static „Arm"), and this file must hold before and after.

import { describe, expect, it } from 'vitest';
import { buildToolbox, SAMMLUNG_CATEGORY_KEYS } from '../../blocks/toolbox';
import { METHOD_DOCS } from '../methodDocs';
import { methodGroups, SHOW_GROUP_ID } from '../methodGroups';
import { CODE_DE } from '../codeMessagesDe';

const categories = () => buildToolbox().contents.filter((c) => c.kind === 'category');
const categoryOfBlock = (type) => categories()
  .find((c) => Array.isArray(c.contents) && c.contents.some((b) => b.kind === 'block' && b.type === type));
const categoryOfCustom = (key) => categories().find((c) => c.custom === key);
const groupOf = (groups, name) => groups.find((g) => g.methods.some((m) => m.name === name));

describe('methodGroups', () => {
  const groups = methodGroups(METHOD_DOCS);

  it('puts every public method in exactly one group', () => {
    const placed = groups.flatMap((g) => g.methods.map((m) => m.name));
    expect(placed.slice().sort()).toEqual(METHOD_DOCS.methods.map((m) => m.name).sort());
    expect(new Set(placed).size).toBe(placed.length);
  });

  it('a method sits under the category that holds its block, with that category’s name and colour', () => {
    for (const name of ['home', 'move_to', 'wait', 'grasp', 'find', 'log', 'counter_add', 'is_holding']) {
      const method = METHOD_DOCS.byName[name];
      const cat = categoryOfBlock(method.block_type);
      expect([name, !!cat]).toEqual([name, true]);
      const group = groupOf(groups, name);
      expect(group.label).toBe(cat.name);
      expect(group.colour).toBe(cat.colour);
    }
  });

  it('replay joins the recordings group, pin / pin_current / ziel the Ziele group', () => {
    const rec = categoryOfCustom(SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN);
    const ziele = categoryOfCustom(SAMMLUNG_CATEGORY_KEYS.ZIELE);
    expect(groupOf(groups, 'replay')).toMatchObject({ label: rec.name, colour: rec.colour });
    for (const name of ['pin', 'pin_current', 'ziel']) {
      expect(groupOf(groups, name)).toMatchObject({ label: ziele.name, colour: ziele.colour });
    }
  });

  it('zeige is „Anzeigen", coloured like the Variablen, last', () => {
    const show = groups[groups.length - 1];
    expect(show).toMatchObject({ id: SHOW_GROUP_ID, label: CODE_DE.METHODS_GROUP_SHOW });
    expect(show.colour).toBe(categoryOfCustom(SAMMLUNG_CATEGORY_KEYS.VARIABLEN).colour);
    expect(show.methods.map((m) => m.name)).toEqual(['zeige']);
  });

  it('the two types follow find’s methods, in its group', () => {
    const group = groupOf(groups, 'find');
    expect(group.label).toBe(categoryOfBlock('edubotics_find_object').name);
    expect(group.types.map((t) => t.name)).toEqual(['Greifziel', 'Greifobjekt']);
    expect(groups.filter((g) => g.types.length > 0)).toHaveLength(1);
  });

  it('keeps the toolbox’s order and drops the groups without a method', () => {
    const order = categories().map((c) => c.name);
    const shown = groups.filter((g) => g.id !== SHOW_GROUP_ID).map((g) => g.label);
    expect(shown).toEqual(order.filter((name) => shown.includes(name)));
    expect(groups.every((g) => g.methods.length + g.types.length > 0)).toBe(true);
    // A category of language blocks (Logik) has no robot method.
    expect(shown).not.toContain(categoryOfBlock('controls_if').name);
  });

  it('within a group, methods keep the table order', () => {
    const table = METHOD_DOCS.methods.map((m) => m.name);
    for (const g of groups) {
      const names = g.methods.map((m) => m.name);
      expect(names).toEqual(table.filter((n) => names.includes(n)));
    }
  });
});
