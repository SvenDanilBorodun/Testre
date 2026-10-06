// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// daten.css's cascade (V2-4): the page-wide reset of buttons and inputs must
// never beat the button classes. Before the fix `.dat-page button` (0,1,1)
// out-ranked `.dat-btn-primary` (0,1,0): every primary button had near-black
// ink on teal (≈ 3.8:1, below WCAG AA), danger buttons lost their red, and
// every label was 14 px / 400 instead of 13 px / 600. jsdom's getComputedStyle
// applies selector specificity (it reports a custom property as its `var(…)`
// text, which says WHICH rule won), so the real stylesheet is checked here;
// the built page is measured in a browser too (Playwright, the fix round).

import fs from 'node:fs';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const CSS = fs.readFileSync(path.resolve(__dirname, '../daten.css'), 'utf8');

let style;
let root;
beforeEach(() => {
  style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);
  root = document.createElement('div');
  root.className = 'dat-page';
  root.innerHTML = `
    <button type="button" class="dat-btn" id="plain">Abbrechen</button>
    <button type="button" class="dat-btn dat-btn-primary" id="primary">Ansehen</button>
    <button type="button" class="dat-btn dat-btn-sm dat-btn-primary" id="primary-sm">Ansehen</button>
    <button type="button" class="dat-btn dat-btn-danger" id="danger">Ganzen Datensatz löschen</button>
    <button type="button" class="dat-btn dat-btn-danger-solid" id="danger-solid">Datensatz löschen</button>
    <button type="button" id="bare">ohne Klasse</button>
    <input id="field" />`;
  document.body.appendChild(root);
});
afterEach(() => {
  style.remove();
  root.remove();
});

const cs = (id) => getComputedStyle(document.getElementById(id));

describe('daten.css: the button classes win over the page reset (V2-4)', () => {
  it('a primary button: white 13 px / 600 on teal', () => {
    const s = cs('primary');
    expect(s.color).toBe('rgb(255, 255, 255)');
    expect(s.fontWeight).toBe('600');
    expect(s.fontSize).toBe('13px');
  });

  it('a small primary button keeps white ink at 12.5 px / 600', () => {
    const s = cs('primary-sm');
    expect(s.color).toBe('rgb(255, 255, 255)');
    expect(s.fontWeight).toBe('600');
    expect(s.fontSize).toBe('12.5px');
  });

  it('a danger button keeps its red ink; a solid one white', () => {
    expect(cs('danger').color).toBe('var(--danger-ink)');
    expect(cs('danger-solid').color).toBe('rgb(255, 255, 255)');
  });

  it('a plain .dat-btn is the page ink at 13 px / 600', () => {
    const s = cs('plain');
    expect(s.color).toBe('var(--ink)');
    expect(s.fontWeight).toBe('600');
    expect(s.fontSize).toBe('13px');
  });

  it('the reset still reaches an unclassed button and an input (inherited font and ink)', () => {
    expect(cs('bare').fontSize).toBe('14px');
    expect(cs('bare').color).toBe('var(--ink)'); // inherited from .dat-page
    expect(cs('field').fontSize).toBe('14px');
  });

  it('V2-13: the card\'s buttons wrap inside their own box; the ⋮ menu never shrinks or wraps', () => {
    const row = document.createElement('div');
    row.className = 'dat-card-actions';
    row.innerHTML = '<div class="dat-card-btns" id="btns"></div><div class="dat-menu-wrap" id="menu"></div><div class="dat-card-actions2" id="a2"></div>';
    root.appendChild(row);
    const btns = cs('btns');
    expect(btns.flexWrap).toBe('wrap');
    expect(btns.flexGrow).toBe('1');
    expect(btns.flexShrink).toBe('1');
    expect(btns.flexBasis).toBe('0px');
    expect(btns.minWidth).toBe('0px');
    const menu = cs('menu');
    expect(menu.flexGrow).toBe('0');
    expect(menu.flexShrink).toBe('0');
    expect(menu.alignSelf).toBe('flex-start');
    expect(cs('a2').flexBasis).toBe('100%');
  });

  it('the reset rule itself carries no class weight (`:where(.dat-page)`)', () => {
    const reset = CSS.split('\n').find((l) => /font:\s*inherit/.test(l));
    expect(reset).toMatch(/^:where\(\.dat-page\) button, :where\(\.dat-page\) input \{/);
  });
});
