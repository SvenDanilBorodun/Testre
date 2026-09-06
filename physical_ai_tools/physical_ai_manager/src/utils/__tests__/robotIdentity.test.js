// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// How a robot names itself to the student.
//
// The regression being fenced: the Start page printed `robotProfile ||
// robotType` verbatim, so a 13-year-old read `omx_full`. The German name now
// rides `capabilities_json` as `display_de`. Every accessor here must ALSO
// survive an older server image that sends the six booleans and no identity
// keys at all — `isValidCapabilities` adopts such a manifest happily, so a
// missing key is a normal state and not an error.

import {
  robotDisplayName, robotHelpText, expectedCameraRoles, cameraRoleLabel,
  CAMERA_ROLE_LABELS_DE,
} from '../robotIdentity';

const capsWith = (extra) => ({
  recordable: true, editable: true, trainable: true,
  inferable: true, roboter_studio: true, has_leader: true, ...extra,
});

describe('robotDisplayName', () => {
  it('prefers the German name from the wire', () => {
    expect(robotDisplayName(capsWith({ display_de: 'OMX – Voll' }), 'omx_full', 'omx_f'))
      .toBe('OMX – Voll');
  });

  it('falls back to the profile id on an older server image', () => {
    // Not ideal, but it is what the page showed before and it is TRUE.
    expect(robotDisplayName(capsWith({}), 'edu1_studio', 'omx_f')).toBe('edu1_studio');
  });

  it('falls back again to the data robot_type, then to empty', () => {
    expect(robotDisplayName(null, '', 'omx_f')).toBe('omx_f');
    expect(robotDisplayName(null, '', '')).toBe('');
  });

  it('ignores a blank or non-string display_de rather than rendering it', () => {
    expect(robotDisplayName(capsWith({ display_de: '   ' }), 'omx_full')).toBe('omx_full');
    expect(robotDisplayName(capsWith({ display_de: 42 }), 'omx_full')).toBe('omx_full');
  });
});

describe('robotHelpText', () => {
  it('returns the sentence when present', () => {
    expect(robotHelpText(capsWith({ help_de: 'Beide Arme.' }))).toBe('Beide Arme.');
  });

  it('returns empty — never undefined — when the server omitted it', () => {
    // The server omits `help_de` rather than sending '', so absence is the
    // normal case and must render as nothing, not as a gap.
    expect(robotHelpText(capsWith({}))).toBe('');
    expect(robotHelpText(null)).toBe('');
    expect(robotHelpText(capsWith({ help_de: 123 }))).toBe('');
  });
});

describe('expectedCameraRoles', () => {
  it('reads the profile allowlist off the wire', () => {
    expect(expectedCameraRoles(capsWith({ camera_roles: ['gripper', 'scene'] })))
      .toEqual(['gripper', 'scene']);
    expect(expectedCameraRoles(capsWith({ camera_roles: ['scene'] }))).toEqual(['scene']);
  });

  it('returns null — not [] — when the server did not say', () => {
    // The distinction matters downstream: null means "we do not know how many
    // cameras this rig should have", which must NOT render as „1 von 0".
    expect(expectedCameraRoles(capsWith({}))).toBeNull();
    expect(expectedCameraRoles(null)).toBeNull();
    expect(expectedCameraRoles(capsWith({ camera_roles: [] }))).toBeNull();
    expect(expectedCameraRoles(capsWith({ camera_roles: 'scene' }))).toBeNull();
  });

  it('drops non-string entries rather than trusting them into the UI', () => {
    expect(expectedCameraRoles(capsWith({ camera_roles: ['scene', 7, '', null] })))
      .toEqual(['scene']);
  });
});

describe('cameraRoleLabel', () => {
  it('translates the two known roles', () => {
    expect(cameraRoleLabel('gripper')).toBe('Greifer-Kamera');
    expect(cameraRoleLabel('scene')).toBe('Szenen-Kamera');
  });

  it('passes an unknown role through instead of blanking it', () => {
    expect(cameraRoleLabel('phone')).toBe('phone');
  });

  it('every label is German', () => {
    for (const label of Object.values(CAMERA_ROLE_LABELS_DE)) {
      expect(label).toMatch(/Kamera/);
    }
  });
});
