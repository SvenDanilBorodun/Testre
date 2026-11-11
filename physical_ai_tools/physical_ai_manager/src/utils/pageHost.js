// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Is THIS page the Windows GUI's own loopback page (verification round C,
// VC-01, 2026-11-15)? The Windows GUI opens exactly `http://localhost/`, and the
// student nginx closes the connection for every Host but `localhost`,
// `127.0.0.1` and `[::1]` (SA-11), so a student page anywhere else is served by
// an Orange Pi, whatever the Pi marker answered. No imports on purpose: piMode,
// robotLocality and piAuth all read it without an import cycle.

// Exactly what `URL.hostname` yields for the three loopback hosts the student
// nginx serves (an IPv6 literal keeps its brackets; a host is lower-cased).
export const LOOPBACK_PAGE_HOSTS = Object.freeze(['localhost', '127.0.0.1', '[::1]']);

let hostnameForTests = null;

/** Tests only: pretend the page was opened at `hostname` (null: the real one). */
export function setPageHostnameForTests(hostname) {
  hostnameForTests = typeof hostname === 'string' ? hostname : null;
}

function pageHostname() {
  if (hostnameForTests !== null) return hostnameForTests;
  try {
    if (typeof window !== 'undefined' && window.location && typeof window.location.hostname === 'string') {
      return window.location.hostname;
    }
  } catch {
    /* no location: unknown, i.e. not loopback */
  }
  return null;
}

/** True only when this page's own host is exactly a loopback host (fail-closed). */
export function isLoopbackPage() {
  const host = pageHostname();
  return typeof host === 'string' && LOOPBACK_PAGE_HOSTS.includes(host);
}
