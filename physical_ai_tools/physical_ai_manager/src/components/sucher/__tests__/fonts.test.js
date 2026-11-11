// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Fonts (spec §3.5, D18; F-27): the student app self-hosts its three OFL faces;
// the teacher web build links its own two self-hosted faces at runtime, once
// (public/fonts/web-fonts.css via WebApp.js). No build contacts Google.

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

// The teacher web's faces (F-27, 2026-11-11): Inter Tight 3.004 and JetBrains
// Mono 2.211 from google/fonts 5bb28733, wght-restricted and subset by
// fonttools 4.60.1 (docs/CLAUDE-CHANGELOG.md 2026-11-11).
const WEB_FONTS = {
  'inter-tight-latin-wght-normal.woff2': [42272, '98a96c159ff5f98901c47019fa8c5a1432896ed153b94c8257b884ae7a0e3507'],
  'inter-tight-latin-ext-wght-normal.woff2': [62204, '32e8a0152a3970daa980adc7d4001fedb0c41b6a98f39d0099ca6d87760d000f'],
  'jetbrains-mono-latin-wght-normal.woff2': [30036, 'fd8ad85d70321741423b9518c587fe7259be9c2320c2a10c44e9ddb958bda26c'],
  'jetbrains-mono-latin-ext-wght-normal.woff2': [10144, '5c59986e7572840c10c2d5d3f340e22b3121352a29ff0a2b49de9944b25f80f8'],
};
const WEB_FONTS_CSS = fs.readFileSync(path.join(APP, 'public/fonts/web-fonts.css'), 'utf8');
const GOOGLE_FONT_HOST = /fonts\.(googleapis|gstatic)\.com/;

describe('the teacher web build self-hosts its fonts (F-27)', () => {
  afterEach(() => {
    document.getElementById('eb-web-fonts')?.remove();
  });

  it.each(Object.entries(WEB_FONTS))('public/fonts/%s is the built file', (name, [bytes, sha]) => {
    const buf = fs.readFileSync(path.join(APP, 'public/fonts', name));
    expect(buf.length).toBe(bytes);
    expect(crypto.createHash('sha256').update(buf).digest('hex')).toBe(sha);
  });

  it.each([
    ['OFL-InterTight.txt', 'Copyright 2022 The Inter Project Authors'],
    ['OFL-JetBrainsMono.txt', 'Copyright 2020 The JetBrains Mono Project Authors'],
  ])('the licence %s ships beside them', (name, copyright) => {
    const text = fs.readFileSync(path.join(APP, 'public/fonts', name), 'utf8');
    expect(text).toMatch(/SIL OPEN FONT LICENSE Version 1\.1/);
    expect(text).toContain(copyright);
  });

  it('every woff2 in public/fonts is pinned, and every family has its OFL text', () => {
    const files = fs.readdirSync(path.join(APP, 'public/fonts'));
    const woff2 = files.filter((f) => f.endsWith('.woff2')).sort();
    expect(woff2).toEqual([...Object.keys(FONTS), ...Object.keys(WEB_FONTS)].sort());
    expect(files.filter((f) => f.startsWith('OFL-')).sort()).toEqual([
      'OFL-Doto.txt',
      'OFL-HostGrotesk.txt',
      'OFL-InstrumentSerif.txt',
      'OFL-InterTight.txt',
      'OFL-JetBrainsMono.txt',
    ]);
  });

  it('web-fonts.css: one @font-face per file, swap, the weights the old Google link asked for', () => {
    const faces = [...WEB_FONTS_CSS.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(faces).toHaveLength(4);
    const urls = faces.map((f) => (f.match(/url\('([^']+)'\)/) || [])[1]).sort();
    expect(urls).toEqual(Object.keys(WEB_FONTS).map((n) => `/fonts/${n}`).sort());
    faces.forEach((f) => {
      expect(f).toMatch(/font-display:\s*swap;/);
      expect(f).toMatch(/font-style:\s*normal;/);
      expect(f).toMatch(/unicode-range:/);
    });
    const face = (file) => faces.find((f) => f.includes(file));
    expect(face('inter-tight-latin-wght')).toMatch(/font-family: 'Inter Tight';[\s\S]*font-weight: 400 800;/);
    expect(face('jetbrains-mono-latin-wght')).toMatch(/font-family: 'JetBrains Mono';[\s\S]*font-weight: 400 600;/);
    // The same two subsets the student faces use.
    const ranges = (css) => new Set([...css.matchAll(/unicode-range:\s*([^;]+);/g)].map((m) => m[1]));
    expect(ranges(WEB_FONTS_CSS)).toEqual(ranges(INDEX_CSS));
  });

  it('adds one stylesheet link to the self-hosted CSS, however often it is asked', async () => {
    const { addWebFontsOnce } = await import('../../../WebApp');
    addWebFontsOnce();
    addWebFontsOnce();
    // eslint-disable-next-line testing-library/no-node-access
    const links = document.head.querySelectorAll('link#eb-web-fonts');
    expect(links).toHaveLength(1);
    expect(links[0].rel).toBe('stylesheet');
    expect(links[0].getAttribute('href')).toBe('/fonts/web-fonts.css');
  });

  it('no source file, page, stylesheet or nginx config names a Google font host', () => {
    const hits = [];
    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== '__tests__') walk(p);
        } else if (/\.(js|jsx|mjs|css|html|json|txt)$/.test(entry.name)) {
          if (GOOGLE_FONT_HOST.test(fs.readFileSync(p, 'utf8'))) hits.push(path.relative(APP, p));
        }
      }
    };
    walk(path.join(APP, 'src'));
    walk(path.join(APP, 'public'));
    for (const page of ['index.html', 'nginx.web.conf.template', 'nginx.conf', 'nginx.opi.conf.template']) {
      if (GOOGLE_FONT_HOST.test(fs.readFileSync(path.join(APP, page), 'utf8'))) hits.push(page);
    }
    expect(hits).toEqual([]);
  });
});
