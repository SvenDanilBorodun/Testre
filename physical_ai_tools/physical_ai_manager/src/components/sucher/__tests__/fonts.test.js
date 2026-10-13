// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Fonts (spec §3.5, D18): the student app self-hosts its three OFL faces and
// never contacts Google; the teacher web build adds its Google stylesheet at
// runtime, once (WebApp.js).

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

const APP = path.resolve(__dirname, '../../../..');
const INDEX_CSS = fs.readFileSync(path.join(APP, 'src/index.css'), 'utf8');

const FONTS = {
  'instrument-serif-latin-400-normal.woff2': [21032, '5eb09b5ac0e28b67c2f041c8ba6d244604ca0c0980d65912ab2d47fed84ddc31'],
  'instrument-serif-latin-400-italic.woff2': [22128, '5a51946dfffa82972bc98745359c46761515641fda557c25116459a9f83da4a7'],
  'instrument-serif-latin-ext-400-normal.woff2': [11604, '290e6267dd833bf5f899eba4c29ad0a9b09dbe53f6075b18af38057159e1ff20'],
  'instrument-serif-latin-ext-400-italic.woff2': [12384, 'd0014821252a8a770bf7551641572f239a8ede83d75652d84fef0c3627751baf'],
  'host-grotesk-latin-wght-normal.woff2': [20296, 'ce9c9db8a488852eb7e4a52f51fd8ce0c93ef61a8d3bfa054b509ec92958dcfe'],
  'host-grotesk-latin-ext-wght-normal.woff2': [11828, '194782c85ee857df4373f6c246e4da9c0ba3cccf85756f6d26e1e78de3479495'],
  'doto-latin-wght-normal.woff2': [5364, '0e1a84240f323bafc2db7540d303ba17ace1daa8bde2936a76be82d24a7a271b'],
  'doto-latin-ext-wght-normal.woff2': [3900, '6519c8aec5debdeae71fc6549b69012c9d0edd37f22f6ec93c8c5770ff7551d1'],
};

describe('self-hosted fonts', () => {
  it.each(Object.entries(FONTS))('public/fonts/%s is the fontsource 5.3.0 file', (name, [bytes, sha]) => {
    const buf = fs.readFileSync(path.join(APP, 'public/fonts', name));
    expect(buf.length).toBe(bytes);
    expect(crypto.createHash('sha256').update(buf).digest('hex')).toBe(sha);
  });

  it.each(['OFL-InstrumentSerif.txt', 'OFL-HostGrotesk.txt', 'OFL-Doto.txt'])('the licence %s ships beside them', (name) => {
    expect(fs.readFileSync(path.join(APP, 'public/fonts', name), 'utf8')).toMatch(/SIL OPEN FONT LICENSE Version 1\.1/);
  });

  it('index.css: one @font-face per file, swap, absolute /fonts/ URLs; no Google @import', () => {
    expect(INDEX_CSS).not.toMatch(/fonts\.googleapis/);
    expect(INDEX_CSS).not.toMatch(/@import/);
    const faces = [...INDEX_CSS.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(faces).toHaveLength(8);
    const urls = faces.map((f) => (f.match(/url\('([^']+)'\)/) || [])[1]).sort();
    expect(urls).toEqual(Object.keys(FONTS).map((n) => `/fonts/${n}`).sort());
    faces.forEach((f) => {
      expect(f).toMatch(/font-display:\s*swap;/);
      expect(f).toMatch(/unicode-range:/);
    });
    const weightOf = (file) => faces.find((f) => f.includes(file)).match(/font-weight:\s*([^;]+);/)[1];
    expect(weightOf('host-grotesk-latin-wght')).toBe('300 800');
    expect(weightOf('doto-latin-wght')).toBe('100 900');
    expect(weightOf('instrument-serif-latin-400-italic')).toBe('400');
  });

  it('index.css: the fonts go through --font-ui / --font-mono; web defaults stay Inter Tight + JetBrains Mono', () => {
    expect(INDEX_CSS).toMatch(/body \{[^}]*font-family: var\(--font-ui\);/);
    expect(INDEX_CSS).toMatch(/\.font-mono \{\s*font-family: var\(--font-mono\);/);
    const root = INDEX_CSS.match(/\n:root \{([^}]*)\}/)[1];
    expect(root).toContain("--font-ui: 'Inter Tight', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;");
    expect(root).toContain("--font-mono: 'JetBrains Mono', source-code-pro, Menlo, Monaco, Consolas, 'Courier New', monospace;");
    const sucher = INDEX_CSS.match(/:root\[data-ui='sucher'\] \{([^}]*)\}/)[1];
    expect(sucher).toContain("--font-ui: 'Host Grotesk'");
    expect(sucher).toContain('--accent: #B83E0C;');
  });
});

describe('WebApp: the web build keeps its Google fonts, added once at runtime', () => {
  afterEach(() => {
    document.getElementById('eb-web-fonts')?.remove();
  });

  it('adds one stylesheet link however often it is asked', async () => {
    const { addWebFontsOnce } = await import('../../../WebApp');
    addWebFontsOnce();
    addWebFontsOnce();
    // eslint-disable-next-line testing-library/no-node-access
    const links = document.head.querySelectorAll('link#eb-web-fonts');
    expect(links).toHaveLength(1);
    expect(links[0].rel).toBe('stylesheet');
    expect(links[0].href).toBe('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
  });
});
