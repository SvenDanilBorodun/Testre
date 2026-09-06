// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Start-page Health-Check decision.
//
// The property under test throughout is the one the module exists for:
// UNKNOWN is neither OK nor BAD. Every `|| 0` / `|| false` that would collapse
// "we did not ask" into "we asked and the answer was none" is a claim about a
// student's rig that nobody checked, and each test below pins one place that
// collapse would happen.

import {
  deriveHealth, stripSeverityPrefix, isHardError,
  OK, WARN, BAD, UNKNOWN, READY, LIMITED, NOT_READY, VERDICT_LABEL_DE,
} from '../homeHealth';

const row = (h, key) => h.rows.find((r) => r.key === key);

const healthy = {
  connected: true,
  jointsLive: true,
  cameraCount: 2,
  expectedCameras: 2,
  calibration: { intrinsic: true, handeye: true, table: true },
  errorText: '',
};

describe('deriveHealth — the happy path', () => {
  it('reports READY when every row is OK', () => {
    const h = deriveHealth(healthy);
    expect(h.verdict).toBe(READY);
    expect(h.rows.every((r) => r.state === OK)).toBe(true);
    expect(h.hint).toBeNull();
  });

  it('says nothing when there is nothing to say (no hint on a healthy rig)', () => {
    expect(deriveHealth(healthy).hint).toBeNull();
  });
});

describe('deriveHealth — unknown is not a finding', () => {
  it('a disconnected rig reports UNKNOWN for everything behind the bridge', () => {
    const h = deriveHealth({ ...healthy, connected: false });
    expect(row(h, 'connection').state).toBe(BAD);
    // The cameras are not broken; we cannot see them. Blaming them for the
    // connection is the failure this asserts against.
    for (const key of ['arm', 'cameras', 'calibration']) {
      expect(row(h, key).state).toBe(UNKNOWN);
      expect(row(h, key).value).toBe('—');
    }
    expect(h.verdict).toBe(NOT_READY);
  });

  it('jointsLive === null is UNKNOWN, jointsLive === false is a WARNING', () => {
    // null: nothing is observing (cloud mode, or no bridge URL yet).
    const unknown = deriveHealth({ ...healthy, jointsLive: null });
    expect(row(unknown, 'arm').state).toBe(UNKNOWN);
    expect(row(unknown, 'arm').value).toBe('—');
    // false: subscribed, and the arm is silent. That IS a finding.
    const silent = deriveHealth({ ...healthy, jointsLive: false });
    expect(row(silent, 'arm').state).toBe(WARN);
    expect(row(silent, 'arm').value).toBe('keine Gelenkdaten');
  });

  it('a null calibration is UNKNOWN, not "nothing is calibrated"', () => {
    // This is the concrete bug the row was written around: the Redux
    // calibration flags are only hydrated by CalibrationWizard, so on Start
    // they are all false until Roboter Studio has been opened. Rendering that
    // as „0 von 3" would invent a fact about the rig.
    const h = deriveHealth({ ...healthy, calibration: null });
    expect(row(h, 'calibration').state).toBe(UNKNOWN);
    expect(row(h, 'calibration').value).toBe('—');
    const none = deriveHealth({
      ...healthy,
      calibration: { intrinsic: false, handeye: false, table: false },
    });
    expect(row(none, 'calibration').state).toBe(WARN);
    expect(row(none, 'calibration').value).toBe('0 von 3');
  });

  it('a null cameraCount is UNKNOWN, zero cameras is a WARNING', () => {
    expect(row(deriveHealth({ ...healthy, cameraCount: null }), 'cameras').state).toBe(UNKNOWN);
    const zero = deriveHealth({ ...healthy, cameraCount: 0 });
    expect(row(zero, 'cameras').state).toBe(WARN);
    expect(row(zero, 'cameras').value).toBe('keine erkannt');
  });

  it('never reports READY while any row is still UNKNOWN', () => {
    // First paint: connected, nothing has answered. „Alles bereit" there would
    // be a guess that happens to be right most of the time.
    const h = deriveHealth({ connected: true, jointsLive: null, cameraCount: null });
    expect(h.verdict).toBe(LIMITED);
  });
});

describe('deriveHealth — cameras against the profile', () => {
  it('counts against the profile when it says how many it uses', () => {
    const h = deriveHealth({ ...healthy, cameraCount: 1, expectedCameras: 2 });
    expect(row(h, 'cameras').state).toBe(WARN);
    expect(row(h, 'cameras').value).toBe('1 von 2');
  });

  it('claims nothing about sufficiency when the profile did not say', () => {
    // An older server image sends no camera_roles. Reporting „1 von 2" there
    // would be inventing the denominator.
    const h = deriveHealth({ ...healthy, cameraCount: 1, expectedCameras: null });
    expect(row(h, 'cameras').state).toBe(OK);
    expect(row(h, 'cameras').value).toBe('1 erkannt');
  });

  it('a follower kit with its single scene camera is complete', () => {
    const h = deriveHealth({ ...healthy, cameraCount: 1, expectedCameras: 1 });
    expect(row(h, 'cameras').state).toBe(OK);
    expect(h.verdict).toBe(READY);
  });
});

describe('deriveHealth — server messages', () => {
  it('[FEHLER] is BAD and drives the whole verdict to NOT_READY', () => {
    const h = deriveHealth({ ...healthy, errorText: '[FEHLER] Kamera nicht gefunden' });
    expect(row(h, 'message').state).toBe(BAD);
    expect(row(h, 'message').detail).toBe('Kamera nicht gefunden');
    expect(h.verdict).toBe(NOT_READY);
    expect(h.hint.title).toBe('Der Roboter meldet einen Fehler');
  });

  it('[WARNUNG] is only LIMITED — the lesson survives it', () => {
    const h = deriveHealth({ ...healthy, errorText: '[WARNUNG] Kamera liefert 18 Hz' });
    expect(row(h, 'message').state).toBe(WARN);
    expect(h.verdict).toBe(LIMITED);
    expect(h.hint.title).toBe('Hinweis vom Roboter');
  });

  it('an unprefixed message is a warning, not an error', () => {
    // Information the student should see, but not evidence the rig is unusable
    // — the safe direction when the server did not classify it.
    const h = deriveHealth({ ...healthy, errorText: 'Etwas ist passiert' });
    expect(row(h, 'message').state).toBe(WARN);
  });

  it('strips the prefix for display and keeps it for the decision', () => {
    expect(stripSeverityPrefix('[FEHLER] X')).toBe('X');
    expect(stripSeverityPrefix('[WARNUNG] Y')).toBe('Y');
    expect(stripSeverityPrefix('  Z  ')).toBe('Z');
    expect(stripSeverityPrefix(null)).toBe('');
    expect(isHardError('[FEHLER] X')).toBe(true);
    expect(isHardError('[WARNUNG] X')).toBe(false);
    expect(isHardError(undefined)).toBe(false);
  });
});

describe('deriveHealth — the verdict cannot disagree with the rows', () => {
  it('is BAD-dominant, then WARN-dominant', () => {
    const bad = deriveHealth({ ...healthy, connected: false, cameraCount: 0 });
    expect(bad.verdict).toBe(NOT_READY);
    const warn = deriveHealth({ ...healthy, cameraCount: 1, expectedCameras: 2 });
    expect(warn.verdict).toBe(LIMITED);
  });

  it('every verdict has German wording', () => {
    for (const v of [READY, LIMITED, NOT_READY]) {
      expect(typeof VERDICT_LABEL_DE[v]).toBe('string');
      expect(VERDICT_LABEL_DE[v].length).toBeGreaterThan(0);
    }
  });
});

describe('deriveHealth — special modes', () => {
  it('hides the calibration row for a profile that never calibrates', () => {
    const h = deriveHealth({ ...healthy, showCalibration: false });
    expect(row(h, 'calibration')).toBeUndefined();
    expect(h.verdict).toBe(READY);
  });

  it('cloud mode makes no claims about a robot that is not meant to be there', () => {
    const h = deriveHealth({ connected: false, cloudOnly: true });
    expect(h.cloudOnly).toBe(true);
    expect(h.verdict).toBe(READY);
    expect(h.hint).toBeNull();
    // Crucially: no „keine Verbindung" row. True, but about nothing.
    expect(row(h, 'connection')).toBeUndefined();
  });

  it('a disconnected rig gets a hint whose body the component fills in', () => {
    // The body is left empty here on purpose: the remedy is platform-specific
    // (Windows „Umgebung starten" vs the Pi network hint) and this module has
    // no business knowing which platform it is on.
    const h = deriveHealth({ connected: false });
    expect(h.hint.tone).toBe(BAD);
    expect(h.hint.title).toBe('Kein Kontakt zum Roboter');
    expect(h.hint.body).toBe('');
  });
});

describe('deriveHealth — defaults', () => {
  it('called with nothing, assumes nothing good', () => {
    const h = deriveHealth();
    expect(h.verdict).toBe(NOT_READY);
    expect(row(h, 'connection').state).toBe(BAD);
  });
});
