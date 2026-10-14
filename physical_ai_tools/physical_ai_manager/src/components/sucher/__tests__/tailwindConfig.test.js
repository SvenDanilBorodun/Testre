// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// tailwind.config.js is branched on the build mode (spec §3.3, D2). The
// teacher web build (REACT_APP_MODE=web) must get EXACTLY the config it had
// before the „Sucher" redesign — the literal below is main@4e5a60ef's file,
// verbatim — so its generated CSS cannot change. The student build re-points
// gray/slate and teal and switches the font families.

import path from 'path';
import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const CONFIG = path.resolve(__dirname, '../../../../tailwind.config.js');

function loadConfig(mode) {
  const prev = process.env.REACT_APP_MODE;
  if (mode === undefined) delete process.env.REACT_APP_MODE;
  else process.env.REACT_APP_MODE = mode;
  try {
    delete require.cache[require.resolve(CONFIG)];
    return require(CONFIG);
  } finally {
    if (prev === undefined) delete process.env.REACT_APP_MODE;
    else process.env.REACT_APP_MODE = prev;
    delete require.cache[require.resolve(CONFIG)];
  }
}

// main@4e5a60ef tailwind.config.js, the exported object verbatim.
const TODAY = {
  content: ['./src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Inter Tight"', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        ebg: 'var(--bg)',
        ebgSunk: 'var(--bg-sunk)',
        eink: 'var(--ink)',
        eink2: 'var(--ink-2)',
        eink3: 'var(--ink-3)',
        eink4: 'var(--ink-4)',
        eline: 'var(--line)',
        eaccent: 'var(--accent)',
        eaccentInk: 'var(--accent-ink)',
        eaccentWash: 'var(--accent-wash)',
        edanger: 'var(--danger)',
        edangerWash: 'var(--danger-wash)',
        esuccess: 'var(--success)',
        esuccessWash: 'var(--success-wash)',
        eamber: 'var(--amber)',
        eamberWash: 'var(--amber-wash)',
        ebgDark: 'var(--dark-bg)',
        eraisedDark: 'var(--dark-raised)',
      },
      borderRadius: {
        eb: 'var(--radius)',
        ebLg: 'var(--radius-lg)',
        ebSm: 'var(--radius-sm)',
      },
      spacing: {
        4: '1rem',
        8: '2rem',
        22: '5.5rem',
        25: '6.25rem',
        30: '7.5rem',
        35: '8.75rem',
        38: '9.5rem',
        65: '16.25rem',
        75: '18.75rem',
        80: '20rem',
        100: '25rem',
      },
      maxHeight: {
        25: '6.25rem',
        30: '7.5rem',
        50: '12.5rem',
        75: '18.75rem',
        100: '25rem',
      },
      minHeight: {
        4: '1rem',
        8: '2rem',
      },
      minWidth: {
        20: '5rem',
        30: '7.5rem',
        65: '16.25rem',
      },
      width: {
        22: '5.5rem',
        35: '8.75rem',
        80: '20rem',
      },
      flex: {
        6: '6 6 0%',
      },
    },
  },
  plugins: [],
};

describe('tailwind.config.js', () => {
  it('the web build gets exactly the config it had before', () => {
    expect(loadConfig('web')).toStrictEqual(TODAY);
  });

  it.each([[undefined], ['student'], ['']])('the student build (REACT_APP_MODE=%j) gets the Sucher palette', (mode) => {
    const c = loadConfig(mode);
    const { extend } = c.theme;
    expect(extend.colors.gray[500]).toBe('#5F5B55');
    expect(extend.colors.gray[100]).toBe('#E9E6E0');
    expect(extend.colors.slate).toEqual(extend.colors.gray);
    expect(Object.keys(extend.colors.gray)).toEqual(['50', '100', '200', '300', '400', '500', '600', '700', '800', '900', '950']);
    expect(extend.colors.teal[600]).toBe('#B83E0C');
    expect(extend.colors.teal[500]).toBe('#C2410C');
    expect(extend.backgroundColor).toEqual({ white: '#F5F3EF' });
    expect(extend.fontFamily.sans[0]).toBe('"Host Grotesk"');
    expect(extend.fontFamily.mono[0]).toBe('ui-monospace');
    // Everything else is today's config.
    const { colors, backgroundColor, fontFamily, ...rest } = extend;
    const { fontFamily: f0, colors: c0, ...todayRest } = TODAY.theme.extend;
    expect(rest).toStrictEqual(todayRest);
    expect({ ...colors, gray: undefined, slate: undefined, teal: undefined })
      .toStrictEqual({ ...c0, gray: undefined, slate: undefined, teal: undefined });
    expect(c.content).toStrictEqual(TODAY.content);
    expect(c.plugins).toStrictEqual([]);
  });
});
