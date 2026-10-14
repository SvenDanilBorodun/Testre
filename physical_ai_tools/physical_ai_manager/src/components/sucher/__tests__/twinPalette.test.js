// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import fs from 'fs';
import path from 'path';
import {
  GRID_CENTER_INK,
  GRID_LINES_INK,
  SCREEN_TOKEN_FALLBACK,
  SUCHER_HEMI_GROUND,
  SUCHER_LINK_COLOR,
  SUCHER_LINK_METALNESS,
  WEB_TWIN_PALETTE,
  mixColor,
  parseHexColor,
  readTwinPalette,
} from '../twinPalette';

function fakeRoot(ui) {
  return { getAttribute: (name) => (name === 'data-ui' ? ui : null) };
}
function styleOf(tokens) {
  return () => ({ getPropertyValue: (name) => (name in tokens ? tokens[name] : '') });
}

describe('twinPalette', () => {
  afterEach(() => { document.documentElement.removeAttribute('data-ui'); });

  it('the web build keeps the colours the twin always had', () => {
    expect(WEB_TWIN_PALETTE).toEqual({
      sucher: false,
      background: 0x1a1d23,
      gridCenter: 0x3a3f47,
      gridLines: 0x2a2e34,
      // UrdfTwin's former LINK_COLOR, metalness, hemisphere ground and SIM_TABLE_COLOR
      link: 0xbfc4cc,
      linkMetalness: 0.25,
      hemiGround: 0x33373d,
      simTable: 0x2a2e34,
    });
    expect(readTwinPalette(fakeRoot(null))).toBe(WEB_TWIN_PALETTE);
    expect(readTwinPalette(fakeRoot('other'))).toBe(WEB_TWIN_PALETTE);
    expect(readTwinPalette(null)).toBe(WEB_TWIN_PALETTE);
    // the real <html> without data-ui (a test DOM, the web build)
    expect(readTwinPalette()).toBe(WEB_TWIN_PALETTE);
  });

  it('under data-ui="sucher" the background IS the screen token and the grid is mixed from its ink', () => {
    const p = readTwinPalette(fakeRoot('sucher'), styleOf({ '--su-scr': ' #102030', '--su-scr-ink-3': '#90a0b0' }));
    expect(p.sucher).toBe(true);
    expect(p.background).toBe(0x102030);
    expect(p.gridCenter).toBe(mixColor(0x102030, 0x90a0b0, GRID_CENTER_INK));
    expect(p.gridLines).toBe(mixColor(0x102030, 0x90a0b0, GRID_LINES_INK));
    // quiet: the lines sit between the screen and the centre line, both well below the ink
    const r = (c) => (c >> 16) & 0xff;
    expect(r(p.background)).toBeLessThan(r(p.gridLines));
    expect(r(p.gridLines)).toBeLessThan(r(p.gridCenter));
    expect(r(p.gridCenter)).toBeLessThan(r(0x90a0b0));
  });

  it('under data-ui="sucher" the arm is cream under a warm ground light and the table slab IS --su-scr-3 (D26/D27)', () => {
    const p = readTwinPalette(fakeRoot('sucher'), styleOf({ '--su-scr-3': '#302010' }));
    expect(p.link).toBe(SUCHER_LINK_COLOR);
    expect(p.linkMetalness).toBe(SUCHER_LINK_METALNESS);
    expect(p.hemiGround).toBe(SUCHER_HEMI_GROUND);
    expect(p.simTable).toBe(0x302010);
    // cream: light, warm (R > G > B), unlike the web build's cool grey (B > R)
    const ch = (c, s) => (c >> s) & 0xff;
    expect(ch(SUCHER_LINK_COLOR, 16)).toBeGreaterThan(ch(SUCHER_LINK_COLOR, 8));
    expect(ch(SUCHER_LINK_COLOR, 8)).toBeGreaterThan(ch(SUCHER_LINK_COLOR, 0));
    expect(ch(SUCHER_LINK_COLOR, 0)).toBeGreaterThan(0xa0);
    expect(ch(SUCHER_HEMI_GROUND, 16)).toBeGreaterThan(ch(SUCHER_HEMI_GROUND, 0));
    // the trail's ghosts (#C9C1B5 at ≤ .35 over the screen) stay far below the lit arm
    expect(ch(SUCHER_LINK_COLOR, 16)).toBeGreaterThan(0xc9);
  });

  it('an unreadable token falls back to index.css\'s value (the shipped tokens give the shipped colours)', () => {
    const p = readTwinPalette(fakeRoot('sucher'), styleOf({}));
    expect(p.background).toBe(0x141210);
    expect(p.gridCenter).toBe(0x2c2926);
    expect(p.gridLines).toBe(0x221f1c);
    expect(p.simTable).toBe(0x24201d);
    expect(readTwinPalette(fakeRoot('sucher'), () => { throw new Error('no'); }).background).toBe(0x141210);
    document.documentElement.setAttribute('data-ui', 'sucher');
    expect(readTwinPalette().background).toBe(0x141210);
  });

  it('the fallback mirrors index.css\'s --su-scr / --su-scr-ink-3 / --su-scr-3 exactly', () => {
    const css = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'index.css'), 'utf8');
    const token = (name) => parseHexColor(new RegExp(`${name}:\\s*(#[0-9A-Fa-f]{3,6})\\s*;`).exec(css)[1]);
    expect(SCREEN_TOKEN_FALLBACK.scr).toBe(token('--su-scr'));
    expect(SCREEN_TOKEN_FALLBACK.ink3).toBe(token('--su-scr-ink-3'));
    expect(SCREEN_TOKEN_FALLBACK.scr3).toBe(token('--su-scr-3'));
  });

  it('parseHexColor and mixColor', () => {
    expect(parseHexColor('#141210')).toBe(0x141210);
    expect(parseHexColor(' #abc ')).toBe(0xaabbcc);
    expect(parseHexColor('rgb(1,2,3)')).toBeNull();
    expect(parseHexColor('')).toBeNull();
    expect(parseHexColor(undefined)).toBeNull();
    expect(mixColor(0x000000, 0xffffff, 0)).toBe(0x000000);
    expect(mixColor(0x000000, 0xffffff, 1)).toBe(0xffffff);
    expect(mixColor(0x102030, 0x304050, 0.5)).toBe(0x203040);
  });
});
