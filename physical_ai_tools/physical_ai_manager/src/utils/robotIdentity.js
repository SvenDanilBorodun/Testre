// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// How a robot NAMES ITSELF to the student.
//
// The Start page used to print `robotProfile || robotType` verbatim, so a
// 13-year-old read `omx_full`. The German name has existed all along in
// robot_profiles.py (`display_name_de`) and in the two setup wizards
// (`gui/app/constants.py::ROBOT_PROFILES` + the Pi twin, as `display_de`); it
// simply never reached React. It now rides `TaskStatus.capabilities_json` —
// the manifest that is already on the wire — as `display_de` / `help_de` /
// `camera_roles`.
//
// WHY NOT A LOCAL TABLE. A fourth copy of the profile registry in JS would be
// the cheapest change today and a lockstep liability forever; the server-side
// registry already exists three times (server, Windows GUI, Pi agent) and
// `test_robot_profiles.py` now fences all three together. Reading the wire
// keeps the count at three.
//
// EVERY READER FALLS BACK. An older server image sends the six booleans and no
// identity keys, and `isValidCapabilities` adopts that manifest happily
// (extras are tolerated, absences are not errors). So each accessor here
// degrades to what the page showed before rather than to a blank.

/** German labels for the two camera roles the profiles use. */
export const CAMERA_ROLE_LABELS_DE = Object.freeze({
  gripper: 'Greifer-Kamera',
  scene: 'Szenen-Kamera',
});

/**
 * The robot's German name, e.g. „OMX – Voll".
 *
 * Falls back through the identifiers the page had before this key existed, so
 * an un-updated server still shows *something* — just the raw id, as it did.
 * Returns '' when nothing is known yet (no ticks), which the hero renders as
 * „Roboter wird erkannt …" rather than as an empty heading.
 */
export function robotDisplayName(caps, profileId, robotType) {
  const fromWire = caps && typeof caps.display_de === 'string' ? caps.display_de.trim() : '';
  if (fromWire) return fromWire;
  return (profileId || robotType || '').trim();
}

/**
 * The one-sentence German explanation of this robot, or '' when the server
 * did not send one. Deliberately omitted rather than empty-stringed on the
 * wire, so '' here means "render nothing", never "render a gap".
 */
export function robotHelpText(caps) {
  return caps && typeof caps.help_de === 'string' ? caps.help_de.trim() : '';
}

/**
 * The camera roles THIS profile uses, e.g. ['gripper', 'scene'] or ['scene'].
 *
 * Returns null when the server did not say — the caller must then treat the
 * expected count as unknown and report only what it actually sees, never
 * „1 von 2". Non-string entries are dropped rather than trusted: this value
 * reaches the UI as text.
 */
export function expectedCameraRoles(caps) {
  const roles = caps && caps.camera_roles;
  if (!Array.isArray(roles)) return null;
  const clean = roles.filter((r) => typeof r === 'string' && r.trim()).map((r) => r.trim());
  return clean.length ? clean : null;
}

/** German label for a camera role id; unknown ids pass through unchanged. */
export function cameraRoleLabel(role) {
  return CAMERA_ROLE_LABELS_DE[role] || role;
}
