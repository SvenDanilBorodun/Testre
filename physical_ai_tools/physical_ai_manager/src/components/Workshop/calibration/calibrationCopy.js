/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Every German string of Roboter Studio's calibration panel (spec 40 S8).
//
// CALIBRATION_SENTENCES_DE is the page's copy of the robot's own sentences —
// autocal/texts_de.py's BY_CODE plus the calibration service's codes — byte for
// byte, format fields ({mm}, {k}, {n}, {i}) included
// (robotis_ai_setup/tests/test_calibration_state_lockstep.py). The robot sends
// its sentence with every state (`message_de`); the copy speaks only when a
// state arrives without one. CALIBRATION_DE is the panel's own wording.

export const CALIBRATION_SENTENCES_DE = Object.freeze({
  ok: 'Kalibrierung in Ordnung.',
  profile_unknown:
    'Das Roboter-Abbild meldet nicht, welche Szenen-Kamera eingerichtet ist. Bitte die Umgebung '
    + 'neu starten.',
  camera_not_tower:
    'Roboter Studio am echten Roboter braucht die Turm-Kamera (Turm-Kit). Bitte in der '
    + 'Einrichtung „Turm-Kamera“ auswählen.',
  camera_missing: 'Die Turm-Kamera liefert kein Bild. Bitte das USB-Kabel der Kamera prüfen.',
  image_size: 'Das Kamerabild passt nicht zur Kalibrierung (Bildgröße). Bitte die Umgebung neu starten.',
  too_dark: 'Das Kamerabild ist zu dunkel. Bitte das Licht einschalten.',
  wrong_mat:
    'Auf dem Tisch liegt die Matte des anderen Roboters. Bitte die Matte für diesen Roboter '
    + 'auflegen.',
  target_attached: 'Am Greifer steckt noch das Kalibrier-Ziel. Bitte abnehmen.',
  fixture_hidden: 'Die beiden Marker neben dem Turm sind nicht zu sehen. Bitte nichts darauf abstellen.',
  tags_hidden: 'Zu viele Marker der Matte sind verdeckt. Bitte die Matte frei räumen.',
  'needs_arm_check/none': 'Dieser Roboter ist noch nicht eingemessen. Bitte den Arm-Check starten.',
  'needs_arm_check/contract_changed':
    'Die gespeicherte Kalibrierung passt nicht mehr zu dieser Version. Bitte den Arm-Check '
    + 'starten.',
  'needs_arm_check/camera_moved': 'Die Kamera wurde verdreht. Bitte den Arm-Check starten.',
  'needs_arm_check/fixture_changed':
    'Die Marker neben dem Turm sitzen anders als beim letzten Arm-Check. Bitte den Arm-Check '
    + 'starten.',
  target_not_confirmed: 'Bitte das Kalibrier-Ziel vom Greifer abnehmen und bestätigen.',
  mat_not_flat:
    'Die Matte liegt nicht flach. Bitte glatt auflegen und an den Ecken fixieren. Hilft das '
    + 'nicht, bitte den Arm-Check starten.',
  mat_misplaced:
    'Die Matte liegt nicht an der Grundplatte an. Bitte bis zum Anschlag an die Grundplatte '
    + 'schieben.',
  inconsistent:
    'Das Kamerabild passt nicht zur gespeicherten Kalibrierung. Bitte prüfen, ob die Matte flach '
    + 'liegt; sonst den Arm-Check starten.',
  tower_displaced:
    'Die Kamera sitzt nicht mehr an ihrem Platz am Turm oder ihr Objektiv wurde verdreht '
    + '(Abweichung {mm} mm). Bitte Turm und Objektiv prüfen und danach den Arm-Check starten.',
  mat_moved: 'Matte verschoben – automatisch nachgeführt.',
  camera_followed: 'Kamera leicht verschoben – automatisch nachgeführt.',
  tag_dropped: 'Ein Marker der Matte liegt nicht flach und wurde nicht verwendet.',
  droop_above_design:
    'Dieser Arm hängt etwas stärker durch als vorgesehen. Direkt neben der ausgesparten Ecke der '
    + 'Matte kann der Roboter deshalb ein Greifen ablehnen.',
  too_few_poses:
    'Der Arm-Check konnte zu wenige Posen auswerten ({k} von {n}). Bitte für gleichmäßiges Licht '
    + 'sorgen, den Tisch frei räumen und den Arm-Check wiederholen.',
  lens:
    'Die Kamera konnte nicht genau genug vermessen werden. Bitte „Kamera von Hand vermessen“ '
    + 'starten.',
  rms:
    'Die Kamera konnte nicht genau genug vermessen werden. Bitte „Kamera von Hand vermessen“ '
    + 'starten.',
  tower:
    'Die Kamera sitzt nicht an ihrem Platz am Turm oder schaut in die falsche Richtung. Bitte '
    + 'prüfen, ob der Turm gerade steht und alle Teile ganz zusammengesteckt sind, und den '
    + 'Arm-Check wiederholen.',
  joint: 'Ein Gelenk des Arms weicht zu stark von seiner Bauform ab. Bitte die Lehrkraft informieren.',
  mount:
    'Das Kalibrier-Ziel sitzt nicht richtig am Greifer. Bitte fest aufklipsen und den Arm-Check '
    + 'wiederholen.',
  mat:
    'Die Matte liegt nicht an ihrem Platz oder nicht flach. Bitte an die Grundplatte schieben, '
    + 'glatt auflegen und den Arm-Check wiederholen.',
  fixture_layout:
    'Die beiden Marker neben dem Kameraturm liegen nicht wie vorgesehen. Bitte prüfen, ob die '
    + 'Marker-Aufkleber in ihren Mulden kleben, und den Arm-Check wiederholen.',
  guard_margin:
    'Der Arm weicht zu stark von seiner Bauform ab – seine Beobachtungshaltung käme dem '
    + 'Kameraturm zu nahe. Bitte den Zusammenbau des Arms prüfen.',
  target_not_seen:
    'Das Kalibrier-Ziel ist nicht zu sehen. Bitte das Ziel auf den Greifer klipsen und den '
    + 'Arm-Check neu starten.',
  foreign_target: 'Am Greifer steckt das Kalibrier-Ziel des anderen Roboters.',
  stopped: 'Arm-Check abgebrochen. Es wurde nichts gespeichert.',
  arm_check_running: 'Arm-Check läuft: Pose {i} von {n}.',
  confirm_target_removed: 'Bitte das Kalibrier-Ziel jetzt vom Greifer abnehmen.',
  check_failed: 'Die Kalibrierung konnte gerade nicht geprüft werden. Bitte noch einmal versuchen.',
  handheld_running:
    'Kamera von Hand vermessen: {k} von 18 Ansichten. Bitte das Kalibrier-Ziel langsam durch das '
    + 'ganze Bild bewegen.',
  handheld_done: 'Kamera vermessen. Bitte jetzt den Arm-Check starten.',
  handheld_failed:
    'Die Kamera konnte nicht vermessen werden. Bitte das Kalibrier-Ziel näher und weiter weg '
    + 'zeigen und wiederholen.',
});

// The codes the robot sends as NOTES beside a state (never a state's own code).
export const CALIBRATION_NOTE_CODES = Object.freeze(['mat_moved', 'camera_followed', 'tag_dropped',
  'droop_above_design']);
export const CALIBRATION_NOTES_DE = Object.freeze(Object.fromEntries(
  CALIBRATION_NOTE_CODES.map((code) => [code, CALIBRATION_SENTENCES_DE[code]]),
));

// texts_de.DROOP_WARNING_DE ({grad} in whole degrees, {n} the joint, 1-based).
export const DROOP_WARNING_DE = 'Achtung: Der Arm erreicht seine Posen nicht genau (Abweichung {grad}° an Gelenk {n}): Er '
  + 'hängt stark durch oder stößt irgendwo an. Greifen am äußeren Rand kann mehrere Zentimeter '
  + 'danebengehen. Bitte Netzteil und Kabel des Arms prüfen, den Platz um den Arm frei räumen und '
  + 'den Arm-Check wiederholen.';

// texts_de.TARGET_REMOVED_BUTTON_DE.
export const TARGET_REMOVED_BUTTON_DE = 'Kalibrier-Ziel ist abgenommen';

export const CALIBRATION_DE = Object.freeze({
  TITLE: 'Kalibrierung',
  CHECKING: 'Die Kalibrierung wird geprüft …',
  RECHECK: 'Erneut prüfen',
  RECHECKING: 'Wird geprüft …',
  ARM_CHECK_START: 'Arm-Check starten',
  ARM_CHECK_PREP_TITLE: 'Vor dem Arm-Check:',
  ARM_CHECK_PREP: Object.freeze([
    'Kalibrier-Ziel auf den Greifer aufklipsen.',
    'Tisch frei räumen.',
    '30 cm hinter dem Roboter frei lassen.',
    'Abstand halten – der Arm bewegt sich von selbst.',
  ]),
  COUNTDOWN: 'Der Arm bewegt sich in {s} s …',
  STOP: 'Stopp',
  ARM_CHECK_PROGRESS: 'Fortschritt des Arm-Checks',
  CAPTURED: '{k} Posen erfasst',
  HANDHELD_START: 'Kamera von Hand vermessen',
  HANDHELD_CANCEL: 'Abbrechen',
  HANDHELD_HINT: 'Halte das Kalibrier-Ziel ruhig in jeden Bildbereich – einmal nah und einmal weiter weg.',
  HANDHELD_GRID: 'Erfasste Bildbereiche',
  HANDHELD_CELL_NEAR: 'nah',
  HANDHELD_CELL_FAR: 'weiter weg',
  DETAILS: 'Details',
  DETAIL_POSES: 'Ausgewertete Posen',
  DETAIL_RMS: 'Bildfehler',
  DETAIL_HOLDOUT: 'Bildfehler der Kontroll-Posen',
  DETAIL_P95: 'Genauigkeit (95 %)',
  DETAIL_TAGS: 'Marker der Matte',
  DETAIL_SHARPNESS: 'Schärfe',
  DETAIL_LENS_OFFSET: 'Abweichung der Kamera',
  DETAIL_LENS_ALONG: 'entlang der Blickrichtung',
  DETAIL_LENS_DRIFT: 'seit dem Arm-Check',
  NOTES: 'Hinweise',
  PROTOKOLL: 'Protokoll',
  BACK_TO_EDITOR: 'Weiter zum Editor',
  OFFLINE: 'Keine Verbindung zum Roboter.',
  PROGRAM_RUNNING: 'Stoppe zuerst dein Programm.',
  NO_ANSWER: 'Der Roboter hat nicht geantwortet. Bitte noch einmal versuchen.',
  TOOLBAR: 'Kalibrierung',
  TOOLBAR_TITLE: 'Kalibrierung prüfen oder den Arm-Check neu starten',
});

/** `{name}` → fields[name], formatted German; an unknown field reads „–". */
export function fillDe(template, fields = {}) {
  return template.replace(/\{([a-z]+)\}/g, (_, name) => {
    const value = fields[name];
    return value === undefined || value === null || value === '' ? '–' : String(value);
  });
}
