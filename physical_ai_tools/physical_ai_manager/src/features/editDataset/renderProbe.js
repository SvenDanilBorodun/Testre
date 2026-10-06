// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// A test-only render counter (spec §G12, R-14): the player's components call
// `countRender(name)` in their render bodies; while the probe is on, each call
// adds one to `window.__datenRenders[name]`. That is how a test — and
// Playwright against a built app — proves the player does NOT re-render per
// video frame. React's Profiler is not used: it never fires in a production
// build.
//
// It ships in every product bundle, INERT: the flag is on only when the build
// set `REACT_APP_DATEN_RENDER_PROBE=1` (the verification harness's build; no
// product build sets it) or a unit test flipped it through the seam. The flag
// is a mutable module variable, so the minifier cannot drop the code; a few
// hundred bytes, and nothing in the product turns it on.

let enabled = process.env.REACT_APP_DATEN_RENDER_PROBE === '1';

/** Test seam: switch the probe on or off. */
export function setRenderProbeForTests(on) {
  enabled = !!on;
  if (enabled && typeof window !== 'undefined') window.__datenRenders = {};
}

/** True while the probe counts. */
export function renderProbeEnabled() {
  return enabled;
}

/** Count one render of component `name` (a no-op unless the probe is on). */
export function countRender(name) {
  if (!enabled || typeof window === 'undefined') return;
  if (!window.__datenRenders) window.__datenRenders = {};
  window.__datenRenders[name] = (window.__datenRenders[name] || 0) + 1;
}
