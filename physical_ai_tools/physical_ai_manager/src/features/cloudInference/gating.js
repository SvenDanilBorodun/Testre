// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Whether the Inferenz page offers „In der Cloud", and whether it may be
// started (spec §6.2). Pure. Derived on every render, never stored (R-6, D3).
// The cloud is the page's only target.

/**
 * Is „In der Cloud" offered, and may it be chosen? First match wins.
 *
 * @param {object} i
 * @param {boolean} i.cloudOnly           `?cloud=1`: no local robot
 * @param {?object} i.capabilities        state.tasks.taskStatus.capabilities
 * @param {boolean} i.cloudApiConfigured  services/cloudConfig.isCloudApiConfigured
 * @returns {{offered: boolean, enabled: boolean, reason: ?string}}
 *   reason ∈ null | 'not_configured' | 'no_robot' | 'not_inferable' | 'old_image'
 */
export function cloudTargetState({ cloudOnly, capabilities, cloudApiConfigured }) {
  if (cloudOnly) return { offered: false, enabled: false, reason: null };
  if (!cloudApiConfigured) return { offered: true, enabled: false, reason: 'not_configured' };
  if (capabilities == null) return { offered: true, enabled: false, reason: 'no_robot' };
  if (capabilities.cloud_inference === true) return { offered: true, enabled: true, reason: null };
  if (capabilities.inferable === false) return { offered: true, enabled: false, reason: 'not_inferable' };
  return { offered: true, enabled: false, reason: 'old_image' };
}
