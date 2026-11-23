// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// Every German string of the V1's camera-tower hint (spec 40 WP-HINT, 52 §5.3,
// §5.4). The three hint sentences are the spec's, word for word; owner
// decision D88: every V1 tower sentence tells the student to lay the arm
// FORWARD, never to stand it up (a V1 standing straight up on the tower side
// is refused too).

// The Startseite, „Roboter aktivieren" (52 §5.3 (a), D88).
export const TOWER_HINT_ACTIVATE_DE =
  "Der Arm liegt so, dass er beim Aktivieren dem Kameraturm zu nahe käme. Lockere ihn, lege ihn nach vorne und setze ihn wieder fest – dann geht's los.";

// Freeing the energised arm (52 §5.4): asked before /workshop/hand_guide(true).
export const TOWER_HINT_HOLD_DE = 'Halte den Arm mit einer Hand – gleich wird er weich.';
// While it is limp.
export const TOWER_HINT_LIMP_DE =
  'Der Arm ist jetzt weich. Lege ihn nach vorne und halte ihn dort – dann drücke „Arm festsetzen".';

export const TOWER_FREE_DE = 'Arm lockern';
export const TOWER_HOLDING_DE = 'Ich halte ihn';
export const TOWER_LOCK_DE = 'Arm festsetzen';
export const TOWER_FREEING_DE = 'Wird gelockert …';
export const TOWER_LOCKING_DE = 'Wird festgesetzt …';
export const TOWER_CANCEL_DE = 'Abbrechen';
// The robot's own answer is shown when it gave one; these when it did not.
export const TOWER_FREE_FAILED_DE = 'Der Arm konnte nicht gelockert werden.';
export const TOWER_LOCK_FAILED_DE = 'Der Arm konnte nicht festgesetzt werden.';
export const TOWER_UNREACHABLE_DE =
  'Der Roboter konnte nicht erreicht werden. Läuft die Umgebung noch?';

// Roboter Studio's home-glide prompt (52 §5.3 (b)).
export const TOWER_HINT_HOME_TITLE_DE = 'Die Grundstellung geht von hier aus nicht';
export const TOWER_HINT_HOME_DE =
  'Von hier würde die Fahrt in die Grundstellung dem Kameraturm zu nahe kommen. Bring den Arm erst mit „Steuern" nach vorne – dann geht die Grundstellung.';

// „Stoppen" on the Pi's System tab (52 §5.3 (c)).
export const TOWER_HINT_STOP_TITLE_DE = 'Achtung: Beim Stoppen wird der Arm weich';
export const TOWER_HINT_STOP_DE =
  'Beim Stoppen wird der Arm weich. Aus dieser Lage würde er nach hinten zum Kameraturm fallen. Fahre ihn zuerst in die Grundstellung (Roboter Studio → Steuern → Grundstellung) oder halte ihn beim Stoppen fest.';
export const TOWER_STOP_ANYWAY_DE = 'Trotzdem stoppen';
