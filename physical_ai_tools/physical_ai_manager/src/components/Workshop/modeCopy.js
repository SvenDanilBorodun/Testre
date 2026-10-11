/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
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
