// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Is the robot this page talks to REMOTE (verification round C, VC-01,
// 2026-11-15)?
//
// A decision that protects a secret on its way to the robot must not hang on
// the Pi marker: `utils/piMode.js::fetchPiMarker` answers „not a Pi" on a
// network error or after 4 s, and before 2026-11-15 the page then stayed non-Pi
// for its lifetime. A Pi page in that state still reached the Pi's rosbridge
// (the robot session cookie of an earlier load), and the token went out the
// Windows way: encrypted for a key any session holder can forge on the topic,
// or in the clear.
//
// So the page decides from ITSELF (utils/pageHost.js): every page whose own host
// is not exactly `localhost`, `127.0.0.1` or `[::1]` is a remote robot's page,
// whatever the marker said, from the first render on (the marker may not have
// answered yet); a page the marker calls a Pi is one too (a kiosk browser on
// the Pi itself opens it at `localhost`). Fails closed: no `window.location`
// reads as remote.

import { isLoopbackPage, LOOPBACK_PAGE_HOSTS, setPageHostnameForTests } from './pageHost';
import { getPiModeSync } from './piMode';

export { isLoopbackPage, LOOPBACK_PAGE_HOSTS, setPageHostnameForTests };

/**
 * True when the robot behind this page is reached over a network others share:
 * the marker says Pi, or the page's own host is not loopback. Every secret on
 * its way to such a robot is encrypted for the key the pi-agent serves behind
 * the robot login, never sent in the clear and never for the topic's key alone.
 */
export function robotIsRemote() {
  return getPiModeSync() === true || !isLoopbackPage();
}
