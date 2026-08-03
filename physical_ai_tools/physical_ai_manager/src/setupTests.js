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
//
// ONE object per storage name, installed on BOTH targets. Under vitest 4's
// jsdom environment `globalThis === globalThis.window`, so today the second
// target is the same object and the distinction is moot — but the previous form
// called the factory once PER TARGET inside the loop, so on any environment
// where the two ever diverge (or where one of them already carries a real
// Storage and the other does not) the bare `localStorage` and
// `window.localStorage` a test and its subject reach for would be two
// independent Maps that never see each other's writes. Building the object
// first removes that possibility instead of relying on an equality that is not
// ours to guarantee.
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
const _targets = [...new Set([globalThis, globalThis.window].filter(Boolean))];
for (const name of ['localStorage', 'sessionStorage']) {
  const needsMock = _targets.some(
    (t) => !t[name] || typeof t[name].getItem !== 'function'
  );
  if (!needsMock) continue;
  const shared = _createStorageMock();
  for (const target of _targets) {
    try {
      Object.defineProperty(target, name, {
        value: shared, writable: true, configurable: true,
      });
    } catch { /* non-configurable getter already present — leave it */ }
  }
}
