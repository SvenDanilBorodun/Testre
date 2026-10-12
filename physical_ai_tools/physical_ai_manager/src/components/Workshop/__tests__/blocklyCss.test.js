/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// R0-10: Tailwind's Preflight must not show what Blockly hides. Preflight's
// `svg { display: block }` is a CSS rule, and a CSS rule beats the SVG
// presentation attribute `display="none"` Blockly hides a scrollbar with, so a
// closed flyout's scrollbar stayed drawn on the workspace. index.css carries
// the rule that restores the attribute's meaning; this pins it in jsdom's
// cascade (the same cascade a browser applies).

import fs from 'fs';
import path from 'path';

const INDEX_CSS = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'index.css'), 'utf8');
const HIDE_RULE = ".injectionDiv svg[display='none'] { display: none; }";
// Tailwind 3 Preflight, verbatim.
const PREFLIGHT_RULE = 'img, svg, video, canvas, audio, iframe, embed, object { display: block; vertical-align: middle; }';

let style;
let host;

afterEach(() => {
  if (style) style.remove();
  if (host) host.remove();
  style = null;
  host = null;
});

function mount(css) {
  style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
  host = document.createElement('div');
  host.innerHTML = '<div class="injectionDiv"><svg display="none"></svg><svg></svg></div><svg display="none"></svg>';
  document.body.appendChild(host);
  // eslint-disable-next-line testing-library/no-node-access
  const [hidden, shown] = host.querySelectorAll('.injectionDiv svg');
  // eslint-disable-next-line testing-library/no-node-access
  const outside = host.querySelector(':scope > svg');
  return { hidden, shown, outside };
}

describe('index.css next to Blockly', () => {
  it('carries the rule that keeps a Blockly-hidden SVG hidden', () => {
    expect(INDEX_CSS).toContain(HIDE_RULE);
  });

  it('Preflight alone draws the hidden scrollbar (the bug)', () => {
    const { hidden } = mount(PREFLIGHT_RULE);
    expect(window.getComputedStyle(hidden).display).toBe('block');
  });

  it('with the rule a display="none" SVG inside Blockly computes none; any other SVG stays block', () => {
    const { hidden, shown, outside } = mount(`${PREFLIGHT_RULE}\n${HIDE_RULE}`);
    expect(window.getComputedStyle(hidden).display).toBe('none');
    expect(window.getComputedStyle(shown).display).toBe('block');
    // Outside Blockly nothing changes.
    expect(window.getComputedStyle(outside).display).toBe('block');
  });

  it('carries the heading, teach-button and hint rules the Sammlung flyout uses', () => {
    for (const selector of [
      '.eduToolboxHeading {',
      '.eduToolboxHeadingLabel {',
      '.eduFlyoutTeachButton > .blocklyFlyoutButtonBackground {',
      '.eduFlyoutTeachButton:hover > .blocklyFlyoutButtonBackground {',
      '.eduFlyoutTeachButton .blocklyText {',
      '.blocklyFlyout .eduFlyoutHint .blocklyFlyoutLabelText {',
    ]) {
      expect(INDEX_CSS).toContain(selector);
    }
  });
});
