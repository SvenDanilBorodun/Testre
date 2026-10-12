/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// ONE colour source for the four Sammlung groups (sammlungTabs.js): every
// reader takes its colour from it, and „Bewegungen" can never be mistaken for
// the static „Arm" category.

import fs from 'fs';
import path from 'path';
import {
  SAMMLUNG_COLOURS, SAMMLUNG_TABS, SAMMLUNG_TAB_LABEL_DE,
} from '../sammlungTabs';
import { ASSET_COLOURS } from '../assetIndex';
import { MARKER_COLORS } from '../markers';
import { DEST_COLOR } from '../../blocks/destinations';
import { TRAJECTORY_COLOR } from '../../blocks/trajectories';
import {
  buildToolbox, SAMMLUNG_CATEGORY_KEYS, SAMMLUNG_TOOLBOX_IDS,
} from '../../blocks/toolbox';
import { DE } from '../../blocks/messages_de';

const categories = () => buildToolbox().contents.filter((c) => c.kind === 'category');

describe('Sammlung colours — one source', () => {
  it('the source: order, colours, labels', () => {
    expect(SAMMLUNG_TABS).toEqual(['aufnahmen', 'ziele', 'positionen', 'variablen']);
    expect(SAMMLUNG_COLOURS).toEqual({
      aufnahmen: '#e11d48', ziele: '#f59e0b', positionen: '#14b8a6', variablen: '#a78bfa',
    });
    expect(SAMMLUNG_TAB_LABEL_DE).toEqual({
      aufnahmen: DE.CATEGORY_AUFNAHMEN,
      ziele: DE.CATEGORY_ZIELE,
      positionen: DE.CATEGORY_POSITIONEN,
      variablen: DE.CATEGORY_VARIABLEN,
    });
  });

  it('the toolbox categories take their order and colours from it', () => {
    const byId = Object.fromEntries(categories()
      .filter((c) => c.toolboxitemid).map((c) => [c.toolboxitemid, c]));
    const keyOf = {
      aufnahmen: 'AUFNAHMEN', ziele: 'ZIELE', positionen: 'POSITIONEN', variablen: 'VARIABLEN',
    };
    for (const tab of SAMMLUNG_TABS) {
      const c = byId[SAMMLUNG_TOOLBOX_IDS[keyOf[tab]]];
      expect(c.colour).toBe(SAMMLUNG_COLOURS[tab]);
      expect(c.name).toBe(SAMMLUNG_TAB_LABEL_DE[tab]);
      expect(c.custom).toBe(SAMMLUNG_CATEGORY_KEYS[keyOf[tab]]);
    }
    expect(categories().filter((c) => c.toolboxitemid).map((c) => c.toolboxitemid))
      .toEqual(SAMMLUNG_TABS.map((t) => SAMMLUNG_TOOLBOX_IDS[keyOf[t]]));
  });

  it('cards, markers and the destination + replay blocks read the same colours', () => {
    expect(ASSET_COLOURS).toEqual({
      variable: SAMMLUNG_COLOURS.variablen,
      recording: SAMMLUNG_COLOURS.aufnahmen,
      pin: SAMMLUNG_COLOURS.ziele,
      pose: SAMMLUNG_COLOURS.positionen,
    });
    expect(MARKER_COLORS).toEqual({
      pin: SAMMLUNG_COLOURS.ziele,
      pose: SAMMLUNG_COLOURS.positionen,
      variable: SAMMLUNG_COLOURS.variablen,
    });
    expect(DEST_COLOR).toBe(SAMMLUNG_COLOURS.ziele);
    expect(TRAJECTORY_COLOR).toBe(SAMMLUNG_COLOURS.aufnahmen);
  });

  it('„Bewegungen" differs from the static „Arm" category', () => {
    const arm = categories().find((c) => c.name === DE.CATEGORY_BEWEGUNG);
    expect(arm).toBeTruthy();
    expect(arm.custom).toBeUndefined();
    expect(SAMMLUNG_COLOURS.aufnahmen.toLowerCase()).not.toBe(String(arm.colour).toLowerCase());
  });

  it('the source imports neither Blockly nor three', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'sammlungTabs.js'), 'utf8');
    const imports = src.split('\n').filter((l) => /^\s*import\b/.test(l) || /require\(/.test(l));
    expect(imports.some((l) => /blockly/i.test(l))).toBe(false);
    expect(imports.some((l) => /['"]three/.test(l))).toBe(false);
    expect(imports).toEqual(["import { DE } from '../blocks/messages_de';"]);
  });
});
