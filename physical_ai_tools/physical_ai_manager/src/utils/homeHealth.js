// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The Start page's Health-Check, as a pure function.
//
// Five rows and one verdict. The verdict is DERIVED from the rows and is never
// passed in, so the pill in the card header cannot disagree with the list
// under it — the failure mode of every hand-maintained status summary.
//
// THE RULE THIS FILE EXISTS TO ENFORCE: unknown is not "ok" and it is not
// "broken". A row whose source has not answered reports UNKNOWN and renders as
// „—" with a grey dot. The tempting `|| 0` / `|| false` would turn "we did not
// ask" into "we asked and the answer was none", which on this page is a claim
// about the student's rig that nobody checked.

export const OK = 'ok';
export const WARN = 'warn';
export const BAD = 'bad';
export const UNKNOWN = 'unknown';

export const READY = 'ready';
export const LIMITED = 'limited';
export const NOT_READY = 'notready';

export const VERDICT_LABEL_DE = Object.freeze({
  [READY]: 'Alles bereit',
  [LIMITED]: 'Eingeschränkt',
  [NOT_READY]: 'Nicht bereit',
});

// The server prefixes its student-facing status strings itself; `[WARNUNG]` is
// a quality notice the lesson survives, `[FEHLER]` is a stop. Anything else
// (an unprefixed message) is treated as a warning: it is information the
// student should see, but it is not evidence the rig is unusable.
const ERROR_PREFIX = '[FEHLER]';
const WARNING_PREFIX = '[WARNUNG]';

/** Strip the server's severity prefix for display; the dot carries severity. */
export function stripSeverityPrefix(text) {
  const s = (text || '').trim();
  if (s.startsWith(ERROR_PREFIX)) return s.slice(ERROR_PREFIX.length).trim();
  if (s.startsWith(WARNING_PREFIX)) return s.slice(WARNING_PREFIX.length).trim();
  return s;
}

/** true when the server marked this message as a hard error. */
export function isHardError(text) {
  return (text || '').trim().startsWith(ERROR_PREFIX);
}

/**
 * Build the health rows + verdict.
 *
 * @param {object}  i
 * @param {boolean} i.connected        rosbridge heartbeat is 'connected'
 * @param {boolean|null} i.jointsLive  three-state, from useJointLiveness:
 *                                     true = a JointState arrived inside the
 *                                     3 s window, false = subscribed and
 *                                     silent, null = not observing. `null` is
 *                                     UNKNOWN and must never render as "no".
 * @param {?number} i.cameraCount      image topics seen, or null if unknown
 * @param {?number} i.expectedCameras  how many this profile uses, or null
 * @param {?object} i.calibration      {intrinsic, handeye, table} booleans, or
 *                                     null when /calibration/status has not
 *                                     answered — NOT an object of falses.
 * @param {boolean} i.showCalibration  false hides the row entirely (a profile
 *                                     without Roboter Studio never calibrates)
 * @param {string}  i.errorText        taskStatus.error, verbatim
 * @param {boolean} i.cloudOnly        `?cloud=1` — there is no robot to judge
 */
export function deriveHealth({
  connected = false,
  jointsLive = null,
  cameraCount = null,
  expectedCameras = null,
  calibration = null,
  showCalibration = true,
  errorText = '',
  cloudOnly = false,
} = {}) {
  const rows = [];
  const message = (errorText || '').trim();
  const hardError = isHardError(message);

  // In cloud-only mode there is no robot behind this card at all. Reporting
  // „keine Verbindung" would be true but useless — nothing is meant to be
  // connected — so the card says so once and stops making claims.
  if (cloudOnly) {
    return {
      verdict: READY,
      cloudOnly: true,
      rows: [{ key: 'cloud', label: 'Betriebsart', state: OK, value: 'Cloud-Modus' }],
      hint: null,
    };
  }

  rows.push({
    key: 'connection',
    label: 'Verbindung',
    state: connected ? OK : BAD,
    value: connected ? 'verbunden' : 'keine',
  });

  // Every remaining row describes something only reachable THROUGH the bridge.
  // With the bridge down they are unknown, not failing — saying „keine Kameras"
  // while disconnected would blame the cameras for the connection.
  const armState = !connected || jointsLive === null
    ? UNKNOWN
    : (jointsLive ? OK : WARN);
  rows.push({
    key: 'arm',
    label: 'Arm antwortet',
    state: armState,
    value: armState === UNKNOWN ? '—' : (jointsLive ? 'Gelenkdaten' : 'keine Gelenkdaten'),
  });

  let cameraState = UNKNOWN;
  let cameraValue = '—';
  if (connected && typeof cameraCount === 'number') {
    if (cameraCount <= 0) {
      cameraState = WARN;
      cameraValue = 'keine erkannt';
    } else if (typeof expectedCameras === 'number' && expectedCameras > 0) {
      cameraState = cameraCount >= expectedCameras ? OK : WARN;
      cameraValue = `${cameraCount} von ${expectedCameras}`;
    } else {
      // The profile did not say how many it wants (older server image). Report
      // what is there and claim nothing about whether it is enough.
      cameraState = OK;
      cameraValue = cameraCount === 1 ? '1 erkannt' : `${cameraCount} erkannt`;
    }
  }
  rows.push({ key: 'cameras', label: 'Kameras', state: cameraState, value: cameraValue });

  if (showCalibration) {
    let calState = UNKNOWN;
    let calValue = '—';
    if (connected && calibration) {
      const done = [calibration.intrinsic, calibration.handeye, calibration.table]
        .filter(Boolean).length;
      if (done === 3) {
        calState = OK;
        calValue = 'vollständig';
      } else {
        calState = WARN;
        calValue = `${done} von 3`;
      }
    }
    rows.push({ key: 'calibration', label: 'Kalibrierung', state: calState, value: calValue });
  }

  rows.push({
    key: 'message',
    label: 'Meldungen',
    state: !message ? OK : (hardError ? BAD : WARN),
    value: !message ? 'keine' : (hardError ? 'Fehler' : 'Hinweis'),
    detail: message ? stripSeverityPrefix(message) : '',
  });

  const verdict = rows.some((r) => r.state === BAD)
    ? NOT_READY
    : rows.some((r) => r.state === WARN)
      ? LIMITED
      : rows.every((r) => r.state === OK)
        ? READY
        // Nothing is wrong but nothing has answered either (first paint, or a
        // bridge that is up with no ticks yet). That is not „Alles bereit".
        : LIMITED;

  return { verdict, cloudOnly: false, rows, hint: buildHint({ connected, message, hardError }) };
}

/**
 * The one actionable sentence under the rows, or null when there is nothing to
 * act on. Platform-specific by necessity: the same dead bridge means „start the
 * environment" on Windows and „check the network" on a Pi, and the Windows
 * wording on a Pi sends a teacher looking for a Docker install that is not
 * theirs to fix. `piHint` is passed in by the component from the existing
 * `PI_PORT_BLOCKED_HINT`, so the two surfaces cannot drift.
 */
function buildHint({ connected, message, hardError }) {
  if (!connected) {
    return {
      tone: BAD,
      title: 'Kein Kontakt zum Roboter',
      // Filled in per platform by the component — see HealthCard.
      body: '',
    };
  }
  if (message) {
    return {
      tone: hardError ? BAD : WARN,
      title: hardError ? 'Der Roboter meldet einen Fehler' : 'Hinweis vom Roboter',
      body: stripSeverityPrefix(message),
    };
  }
  return null;
}

export default deriveHealth;
