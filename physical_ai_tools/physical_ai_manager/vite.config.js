/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';
import path from 'path';

// ---------------------------------------------------------------------------
// CRA → Vite migration config. The goal is behavioural parity with the
// react-scripts build it replaces, NOT a "modernised" rewrite:
//   * the bundled env LITERALS must match CRA's (CI greps + the Dockerfile
//     self-checks assert the placeholder strings appear in the JS bundle),
//   * build.outDir stays `build/` so both Dockerfiles' `COPY /app/build` and
//     the nginx html paths are untouched,
//   * source maps stay OFF (was GENERATE_SOURCEMAP=false; now build.sourcemap).
//
// Vite 8 note: Vite 8 bundles with Rolldown (OXC transformer), not esbuild/
// webpack. The CRA convention of JSX inside plain `.js` files therefore has
// to be enabled on TWO surfaces:
//   1. The production BUILD — Rolldown parses each module per its extension
//      and treats `.js` as plain JS (rejecting JSX). `build.rolldownOptions.
//      moduleTypes = { '.js': 'jsx' }` is the decisive lever: it tells the
//      bundler to parse every `.js` as JSX. SAFE here because the source tree
//      is 0 TypeScript files / 132 .js / 17 .jsx — `jsx` is a strict superset
//      of plain JS, so the .jsx files are unaffected and there are no .ts to
//      misparse. (Add a `.ts`/`.tsx` mapping if such files are ever added.)
//   2. The DEV server — @vitejs/plugin-react-swc transforms via SWC, whose
//      default parser table has no `.js` branch, so `npm start` would skip our
//      `.js` files. The plugin's documented `parserConfig` hook (returning
//      `jsx: true` for .js) supplies the missing branch.
// ---------------------------------------------------------------------------

const srcDir = path.resolve(__dirname, 'src');
const NODE_MODULES_RE = /node_modules/;

// The CRA "process.env.REACT_APP_*" inlining contract. Vite normally exposes
// build-time vars via `import.meta.env`, but the whole app — and the Docker
// --build-arg plumbing — reads `process.env.*`. We therefore define each
// referenced key as a static string literal at BUILD time, reading the value
// from THIS Node process's env (exactly what CRA/webpack DefinePlugin did).
//
// The list is the exact set grepped from src/ (9 REACT_APP_* + PUBLIC_URL),
// plus NODE_ENV which some libraries branch on. `?? ''` so a missing var
// inlines as the empty string (CRA's behaviour) rather than `undefined`,
// which would make `process.env.X` a ReferenceError at runtime.
const CRA_ENV_KEYS = [
  'NODE_ENV',
  'PUBLIC_URL',
  'REACT_APP_SUPABASE_URL',
  'REACT_APP_SUPABASE_ANON_KEY',
  'REACT_APP_CLOUD_API_URL',
  'REACT_APP_MODE',
  'REACT_APP_ALLOWED_POLICIES',
  'REACT_APP_BUILD_ID',
  'REACT_APP_DEBUG',
  'REACT_APP_BASE_WORKSPACE_PATH',
  'REACT_APP_LEROBOT_OUTPUTS_PATH',
];

function craEnvDefine(mode) {
  const define = {};
  for (const key of CRA_ENV_KEYS) {
    let value = process.env[key];
    // NODE_ENV defaults to the Vite mode (development/production) the same way
    // CRA derived it from the script (start→development, build→production).
    if (value === undefined && key === 'NODE_ENV') {
      value = mode === 'development' ? 'development' : 'production';
    }
    define[`process.env.${key}`] = JSON.stringify(value ?? '');
  }
  return define;
}

export default defineConfig(({ mode }) => ({
  plugins: [
    react({
      // Under Vite 8 (Rolldown) the SWC plugin prints a recommendation to
      // switch to @vitejs/plugin-react because no SWC plugins are configured.
      // We deliberately stay on plugin-react-swc (per the migration spec) and
      // silence the advisory so CI logs stay clean.
      disableOxcRecommendation: true,
      // Dev-server (SWC) side of the JSX-in-.js story. The plugin's default
      // parser table has no `.js` entry, so without this the `npm start` dev
      // transform would skip our .js files and leave raw JSX for the bundler.
      parserConfig(id) {
        if (NODE_MODULES_RE.test(id)) return undefined;
        if (id.endsWith('.tsx')) return { syntax: 'typescript', tsx: true };
        if (id.endsWith('.ts') || id.endsWith('.mts')) {
          return { syntax: 'typescript', tsx: false };
        }
        if (id.endsWith('.jsx') || id.endsWith('.js')) {
          return { syntax: 'ecmascript', jsx: true };
        }
        return undefined;
      },
    }),
  ],
  // Build-time env-literal inlining (the white-screen guard property).
  define: craEnvDefine(mode),
  // Dev dependency optimizer / pre-bundle scanner also has to know `.js` may
  // carry JSX, or it fails its scan ("Unexpected JSX expression") and skips
  // pre-bundling — which would leave CJS-only deps (roslib, blockly, recharts)
  // un-prebundled and break the dev server. moduleTypes maps `.js`→jsx for the
  // Rolldown-based optimizer the same way build.rolldownOptions does for the
  // production bundle.
  optimizeDeps: {
    rolldownOptions: {
      moduleTypes: {
        '.js': 'jsx',
      },
    },
  },
  build: {
    // Keep CRA's output directory name so the Dockerfiles' `COPY /app/build`
    // and nginx root paths need no change.
    outDir: 'build',
    // Was `GENERATE_SOURCEMAP=false`: don't ship source maps (DevTools source
    // exposure + ~15 MB bundle bloat).
    sourcemap: false,
    rolldownOptions: {
      // Parse every `.js` as JSX in the production bundle (see surface #1).
      moduleTypes: {
        '.js': 'jsx',
      },
      // Emit hashed bundles under `static/` (CRA's layout) instead of Vite's
      // default `assets/`. This keeps BOTH nginx configs byte-untouched —
      // their `location /static/ { expires 1y; immutable }` long-cache rule
      // (and the 5 security headers on nginx.web.conf.template) need no edit.
      // Files are still content-addressed (`[name]-[hash]`), so the immutable
      // cache stays safe. Note: the env/white-screen CI greps still move off
      // `main.*.js` — Vite names the entry `index-[hash].js`, and the greps
      // are switched to a path-agnostic recursive search anyway.
      output: {
        entryFileNames: 'static/js/[name]-[hash].js',
        chunkFileNames: 'static/js/[name]-[hash].js',
        assetFileNames: (assetInfo) => {
          const name = assetInfo.names?.[0] ?? assetInfo.name ?? '';
          if (name.endsWith('.css')) return 'static/css/[name]-[hash][extname]';
          return 'static/media/[name]-[hash][extname]';
        },
      },
    },
  },
  server: {
    port: 3000,
  },
  test: {
    // jsdom so @testing-library/react renders into a DOM; globals so the
    // suites' bare describe/test/expect (and the jest→vi shim) resolve.
    environment: 'jsdom',
    globals: true,
    setupFiles: path.resolve(srcDir, 'setupTests.js'),
    // The suite is `src/` and NOTHING else. Without an explicit include,
    // vitest's default glob covers the whole PACKAGE, so any stray test file
    // outside src/ — a scratch probe, a vendored fixture, a copied example —
    // silently joins `ci.yml::react-tests`, which is a BLOCKING gate.
    // Measured: one probe file took the suite from 49 files / 488 tests to
    // 50 / 497 and passed, unreviewed. It is also invisible to
    // sessionScope.test.js's storage-key coverage scan, whose walk starts at
    // src/ — so such a file would be EXECUTED by the gate while contributing
    // nothing the gate can see. Covers all 49 current files (`*.test.js` /
    // `*.test.jsx`, including the dotted `SystemPage.cameraRoles.test.jsx`
    // shape); `spec` is carried for symmetry with vitest's own default.
    include: ['src/**/*.{test,spec}.{js,jsx}'],
  },
}));
