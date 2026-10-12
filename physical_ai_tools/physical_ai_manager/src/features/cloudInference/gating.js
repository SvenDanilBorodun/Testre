// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Where the Inferenz page runs the student's model (spec §6.2). Pure.
//
// The target is DERIVED on every render, never stored (R-6, D3): the Jetson
// lifecycle hook stays mounted for both targets, so a classroom-Jetson
// discovery that resolves `resume`/`reclaim` after mount (rosbridge moves to the
// Jetson) wins at once and the cloud view never renders against the Jetson's
// rosbridge.

// rosbridge points at the Jetson (or is about to / still does).
const JETSON_OWNS_ROSBRIDGE = new Set(['connected', 'claiming', 'disconnecting']);

/**
 * Is „In der Cloud" offered, and may it be chosen? First match wins.
 *
 * @param {object} i
 * @param {boolean} i.cloudOnly           `?cloud=1`: no local robot
 * @param {string}  i.jetsonStatus        state.jetson.status
 * @param {?object} i.capabilities        state.tasks.taskStatus.capabilities
 * @param {boolean} i.cloudApiConfigured  services/cloudConfig.isCloudApiConfigured
 * @returns {{offered: boolean, enabled: boolean, reason: ?string}}
 *   reason ∈ null | 'not_configured' | 'no_robot' | 'not_inferable' | 'old_image'
 */
export function cloudTargetState({ cloudOnly, jetsonStatus, capabilities, cloudApiConfigured }) {
  if (cloudOnly) return { offered: false, enabled: false, reason: null };
  if (JETSON_OWNS_ROSBRIDGE.has(jetsonStatus)) return { offered: false, enabled: false, reason: null };
  if (!cloudApiConfigured) return { offered: true, enabled: false, reason: 'not_configured' };
  if (capabilities == null) return { offered: true, enabled: false, reason: 'no_robot' };
  if (capabilities.cloud_inference === true) return { offered: true, enabled: true, reason: null };
  if (capabilities.inferable === false) return { offered: true, enabled: false, reason: 'not_inferable' };
  return { offered: true, enabled: false, reason: 'old_image' };
}

/**
 * The target this render shows, in order:
 *   the Jetson owns rosbridge -> 'jetson';
 *   a live cloud run in the controller -> 'cloud';
 *   a classroom whose Jetson discovery is still pending -> 'pending' (it may
 *     resolve resume/reclaim, i.e. rosbridge moves to the Jetson);
 *   cloud not offered -> 'jetson';
 *   the student's explicit choice -> it;
 *   else (owner A-Q5) cloud when it is enabled, the Jetson otherwise.
 *
 * @param {object} i
 * @param {string}  i.jetsonStatus
 * @param {?string} i.classroomId
 * @param {boolean} i.liveCloudSession
 * @param {{offered: boolean, enabled: boolean}} i.cloud  cloudTargetState's answer
 * @param {?string} i.choice  'cloud' | 'jetson' | null (set only by the TargetSwitch)
 * @returns {'jetson'|'cloud'|'pending'}
 */
export function effectiveTarget({ jetsonStatus, classroomId, liveCloudSession, cloud, choice }) {
  if (JETSON_OWNS_ROSBRIDGE.has(jetsonStatus)) return 'jetson';
  if (liveCloudSession) return 'cloud';
  if (classroomId && jetsonStatus === 'unknown') return 'pending';
  if (!cloud || !cloud.offered) return 'jetson';
  if (choice === 'cloud' || choice === 'jetson') return choice;
  return cloud.enabled ? 'cloud' : 'jetson';
}

/** True while the Jetson owns rosbridge (the TargetSwitch is not shown then). */
export function jetsonOwnsRosbridge(jetsonStatus) {
  return JETSON_OWNS_ROSBRIDGE.has(jetsonStatus);
}
