// jest-dom adds custom matchers for asserting on DOM nodes.
// allows you to do things like:
// expect(element).toHaveTextContent(/react/i)
// learn more: https://github.com/testing-library/jest-dom
import '@testing-library/jest-dom';
import { vi } from 'vitest';

// CRA→Vite migration: the test suites were written against Jest and call
// `jest.fn()` / `jest.spyOn()` / `jest.clearAllMocks()` etc. Vitest exposes the
// identical surface as `vi.*`. Alias the runtime helper so those calls resolve
// unchanged — the least-diff path for the 5 ported suites. (NOTE: `vi.mock()`
// hoisting is a STATIC transform keyed on the literal `vi.mock(` token, so the
// two files that mock modules use `vi.mock` directly; this alias only covers
// the runtime helpers, which is all the rest of the suites need.)
globalThis.jest = vi;

// Web Storage polyfill. Under vitest 4 + jsdom the bare `localStorage` /
// `sessionStorage` globals are NOT exposed (accessing them yields `undefined`),
// so any code or test that touches Web Storage throws
// `Cannot read properties of undefined (reading 'getItem')`. Real browsers
// always have these, and the production code is even defensive (taskSlice wraps
// its reads in try/catch), so this is purely a test-env gap — but it silently
// red-lined every suite that exercises a storage path (taskSlice's robotType
// persistence + useRosTopicSubscription's „Ton"-toggle read). Install a minimal
// in-memory Storage with REAL persistence (set-then-get must round-trip) on both
// globalThis and the jsdom window so those suites reach their actual assertions.
function _createStorageMock() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(String(k)) ? store.get(String(k)) : null),
    setItem: (k, v) => { store.set(String(k), String(v)); },
    removeItem: (k) => { store.delete(String(k)); },
    clear: () => { store.clear(); },
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
}
for (const target of [globalThis, globalThis.window].filter(Boolean)) {
  for (const name of ['localStorage', 'sessionStorage']) {
    if (!target[name] || typeof target[name].getItem !== 'function') {
      try {
        Object.defineProperty(target, name, {
          value: _createStorageMock(), writable: true, configurable: true,
        });
      } catch { /* non-configurable getter already present — leave it */ }
    }
  }
}
