// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The PRODUCT version, as opposed to the React app's own.
//
// The Start page used to print `packageJson.version` — 0.9.0 — labelled
// „EduBotics v…". That is the manager SPA's version, deliberately decoupled
// from the product VERSION (2.17.0 at time of writing), so a student reading
// it out during support reads the wrong number.
//
// There is no build-time constant for the product version in this bundle, and
// adding one would mean a fifth bump site (VERSION, the .iss, two constants.py
// fallbacks). The number is already on hand at RUNTIME: the Windows GUI spawns
// the WebView with `?_v=<IMAGE_TAG>` as a cache-buster
// (`gui_app.py::open_student_window`), and on a release IMAGE_TAG is the baked
// product version from `docker/versions.env`.
//
// So this reads the cache-buster and refuses to guess otherwise. Three cases:
//   * `?_v=2.17.0`  → "2.17.0"
//   * `?_v=latest`  → null. An unpinned dev rig genuinely does not know its
//                     product version, and "latest" is not one.
//   * no param      → null. Pi mode and the teacher web app never carry it.
// A null renders as nothing at all, which is the honest answer, and never as
// a version that is not this one.

// Anything that is not a dotted release number is not a version we can quote:
// `latest`, a git sha, a `2.17.0-dirty` workstation tag.
const RELEASE_RE = /^\d+\.\d+\.\d+$/;

/**
 * The product version from the WebView cache-buster, or null when unknown.
 * @returns {string|null}
 */
export function productVersion() {
  if (typeof window === 'undefined' || !window.location) return null;
  try {
    const raw = new URLSearchParams(window.location.search).get('_v');
    if (!raw) return null;
    const trimmed = raw.trim();
    return RELEASE_RE.test(trimmed) ? trimmed : null;
  } catch {
    return null;
  }
}

/**
 * The bundle's build id (short git sha in CI, "dev" locally), or null.
 * Baked by the Dockerfile and inlined by the bundler, exactly as
 * `useVersionCheck` reads it — quoted in the Start-page footer so a support
 * request can name the exact bundle as well as the release.
 */
export function buildId() {
  const id = process.env.REACT_APP_BUILD_ID;
  return typeof id === 'string' && id.trim() ? id.trim() : null;
}

export default productVersion;
