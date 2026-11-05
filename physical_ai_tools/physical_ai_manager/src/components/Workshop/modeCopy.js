/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Every German string of the studio's mode signal: the badge after the page
// title (ModeBadge.jsx) and the toast on a switch between the real robot and
// the simulator (WorkshopPage). The badge words are LITERAL uppercase — never
// CSS `uppercase`, so a screen reader and a text search read what is shown.

export const MODE_DE = Object.freeze({
  BADGE_SIM: 'SIMULATOR',
  BADGE_REAL: 'ECHTER ROBOTER',
  BADGE_SIM_TITLE: 'Programme laufen nur im Simulator – der echte Arm bewegt sich nicht.',
  BADGE_REAL_TITLE: 'Programme steuern den echten Roboterarm.',
  RUN_LIVE: 'Programm läuft',
  RUN_PAUSED: 'Angehalten',
  TOAST_REAL: 'Echter Roboter: Dein Programm steuert ab jetzt den echten Arm.',
  TOAST_SIM: 'Simulator: Programme laufen nur virtuell – der echte Arm bleibt stehen.',
});
