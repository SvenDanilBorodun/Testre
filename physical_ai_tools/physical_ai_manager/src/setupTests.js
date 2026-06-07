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
