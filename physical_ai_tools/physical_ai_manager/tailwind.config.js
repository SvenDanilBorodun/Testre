// The student build („Sucher", REACT_APP_MODE unset or 'student'; npm start,
// vitest and the harness leave it unset) re-points Tailwind's grays and the old
// teal accent to the warm Sucher palette and switches the font families. The
// teacher/admin web build (REACT_APP_MODE=web, set as ENV before the build by
// Dockerfile.web) gets EXACTLY the config below, so its CSS is unchanged
// (spec §3.3, owner decision D2; pinned by components/sucher/__tests__/tailwindConfig.test.js).
const SUCHER = process.env.REACT_APP_MODE !== 'web';

/** @type {import('tailwindcss').Config} */
const config = {
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

if (SUCHER) {
  const warmGray = {
    50: '#F0EDE7',
    100: '#E9E6E0',
    200: '#DEDAD2',
    300: '#CFCAC0',
    400: '#A9A398',
    500: '#5F5B55',
    600: '#4D4B47',
    700: '#3A3936',
    800: '#232322',
    900: '#1A1918',
    950: '#0F0E0D',
  };
  // The old teal accent becomes the signal orange (two-tone accent, D3).
  const signal = {
    50: '#FFF4EE',
    100: '#FFE6D9',
    200: '#FFCDB4',
    300: '#FFA97F',
    400: '#F26A2E',
    500: '#C2410C',
    600: '#B83E0C',
    700: '#9A3412',
    800: '#7C2D12',
    900: '#5C210C',
    950: '#3B1508',
  };
  const ext = config.theme.extend;
  ext.colors = { ...ext.colors, gray: warmGray, slate: { ...warmGray }, teal: signal };
  // Backgrounds only: text-white, border-white and ring-white stay #FFFFFF.
  ext.backgroundColor = { white: '#F5F3EF' };
  ext.fontFamily = {
    sans: ['"Host Grotesk"', 'system-ui', '-apple-system', '"Segoe UI"', 'sans-serif'],
    mono: ['ui-monospace', '"Cascadia Mono"', 'Consolas', 'SFMono-Regular', 'Menlo', 'monospace'],
  };
}

module.exports = config;
