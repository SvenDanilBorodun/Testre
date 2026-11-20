#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Every sentence the automatic calibration shows a student or a teacher
(spec 40 S4, its table and the sentences defined beside their codes, and the
calibration service's few the spec does not word).

Verbatim, German, typographic quotes („…“, Rule §1). ``{…}`` are format
fields, filled by the helpers below. :data:`BY_CODE` maps a result code (a
mat-check refusal, an arm-check gate, a note) to its sentence.
"""

from __future__ import annotations

OK_DE = 'Kalibrierung in Ordnung.'
PROFILE_UNKNOWN_DE = ('Das Roboter-Abbild meldet nicht, welche Szenen-Kamera eingerichtet ist. '
                      'Bitte die Umgebung neu starten.')
CAMERA_NOT_TOWER_DE = ('Roboter Studio am echten Roboter braucht die Turm-Kamera (Turm-Kit). '
                       'Bitte in der Einrichtung „Turm-Kamera“ auswählen.')
DROOP_ABOVE_DESIGN_DE = ('Dieser Arm hängt etwas stärker durch als vorgesehen. Direkt neben der '
                         'ausgesparten Ecke der Matte kann der Roboter deshalb ein Greifen ablehnen.')
CAMERA_MISSING_DE = 'Die Turm-Kamera liefert kein Bild. Bitte das USB-Kabel der Kamera prüfen.'
IMAGE_SIZE_DE = 'Das Kamerabild passt nicht zur Kalibrierung (Bildgröße). Bitte die Umgebung neu starten.'
TOO_DARK_DE = 'Das Kamerabild ist zu dunkel. Bitte das Licht einschalten.'
WRONG_MAT_DE = ('Auf dem Tisch liegt die Matte des anderen Roboters. '
                'Bitte die Matte für diesen Roboter auflegen.')
TARGET_ATTACHED_DE = 'Am Greifer steckt noch das Kalibrier-Ziel. Bitte abnehmen.'
FIXTURE_HIDDEN_DE = 'Die beiden Marker neben dem Turm sind nicht zu sehen. Bitte nichts darauf abstellen.'
TAGS_HIDDEN_DE = 'Zu viele Marker der Matte sind verdeckt. Bitte die Matte frei räumen.'
NEEDS_ARM_CHECK_NONE_DE = 'Dieser Roboter ist noch nicht eingemessen. Bitte den Arm-Check starten.'
NEEDS_ARM_CHECK_CONTRACT_CHANGED_DE = ('Die gespeicherte Kalibrierung passt nicht mehr zu dieser Version. '
                                       'Bitte den Arm-Check starten.')
NEEDS_ARM_CHECK_CAMERA_MOVED_DE = 'Die Kamera wurde verdreht. Bitte den Arm-Check starten.'
NEEDS_ARM_CHECK_FIXTURE_CHANGED_DE = ('Die Marker neben dem Turm sitzen anders als beim letzten Arm-Check. '
                                      'Bitte den Arm-Check starten.')
TARGET_NOT_CONFIRMED_DE = 'Bitte das Kalibrier-Ziel vom Greifer abnehmen und bestätigen.'
# D84: a stored lens that no longer fits lands here too (step 2's drop rule answers first), hence the way out.
MAT_NOT_FLAT_DE = ('Die Matte liegt nicht flach. Bitte glatt auflegen und an den Ecken fixieren. '
                   'Hilft das nicht, bitte den Arm-Check starten.')
MAT_MISPLACED_DE = 'Die Matte liegt nicht an der Grundplatte an. Bitte bis zum Anschlag an die Grundplatte schieben.'
INCONSISTENT_DE = ('Das Kamerabild passt nicht zur gespeicherten Kalibrierung. Bitte prüfen, ob die Matte '
                   'flach liegt; sonst den Arm-Check starten.')
TOWER_DISPLACED_DE = ('Die Kamera sitzt nicht mehr an ihrem Platz am Turm oder ihr Objektiv wurde verdreht '
                      '(Abweichung {mm} mm). Bitte Turm und Objektiv prüfen und danach den Arm-Check starten.')
MAT_MOVED_DE = 'Matte verschoben – automatisch nachgeführt.'
CAMERA_FOLLOWED_DE = 'Kamera leicht verschoben – automatisch nachgeführt.'
TAG_DROPPED_DE = 'Ein Marker der Matte liegt nicht flach und wurde nicht verwendet.'

ARM_CHECK_RUNNING_DE = 'Arm-Check läuft: Pose {i} von {n}.'
TARGET_NOT_SEEN_DE = ('Das Kalibrier-Ziel ist nicht zu sehen. Bitte das Ziel auf den Greifer klipsen und '
                      'den Arm-Check neu starten.')
FOREIGN_TARGET_DE = 'Am Greifer steckt das Kalibrier-Ziel des anderen Roboters.'
TOO_FEW_POSES_DE = ('Der Arm-Check konnte zu wenige Posen auswerten ({k} von {n}). Bitte für gleichmäßiges '
                    'Licht sorgen, den Tisch frei räumen und den Arm-Check wiederholen.')
GATE_LENS_DE = 'Die Kamera konnte nicht genau genug vermessen werden. Bitte „Kamera von Hand vermessen“ starten.'
GATE_TOWER_DE = ('Die Kamera sitzt nicht an ihrem Platz am Turm oder schaut in die falsche Richtung. Bitte '
                 'prüfen, ob der Turm gerade steht und alle Teile ganz zusammengesteckt sind, und den '
                 'Arm-Check wiederholen.')
GATE_JOINT_DE = 'Ein Gelenk des Arms weicht zu stark von seiner Bauform ab. Bitte die Lehrkraft informieren.'
GATE_MOUNT_DE = ('Das Kalibrier-Ziel sitzt nicht richtig am Greifer. Bitte fest aufklipsen und den '
                 'Arm-Check wiederholen.')
GATE_MAT_DE = ('Die Matte liegt nicht an ihrem Platz oder nicht flach. Bitte an die Grundplatte schieben, '
               'glatt auflegen und den Arm-Check wiederholen.')
FIXTURE_LAYOUT_DE = ('Die beiden Marker neben dem Kameraturm liegen nicht wie vorgesehen. Bitte prüfen, ob '
                     'die Marker-Aufkleber in ihren Mulden kleben, und den Arm-Check wiederholen.')
GUARD_MARGIN_DE = ('Der Arm weicht zu stark von seiner Bauform ab – seine Beobachtungshaltung käme dem '
                   'Kameraturm zu nahe. Bitte den Zusammenbau des Arms prüfen.')
DROOP_WARNING_DE = ('Achtung: Der Arm erreicht seine Posen nicht genau (Abweichung {grad}° an Gelenk {n}): '
                    'Er hängt stark durch oder stößt irgendwo an. Greifen am äußeren Rand kann mehrere '
                    'Zentimeter danebengehen. Bitte Netzteil und Kabel des Arms prüfen, den Platz um den Arm '
                    'frei räumen und den Arm-Check wiederholen.')
REFINEMENT_WARNING_DE = ('Achtung: „{ziel}“ liegt am Rand des Arbeitsbereichs – der Arm trifft es nur auf '
                         'etwa {mm} mm genau.')
CONFIRM_TARGET_REMOVED_DE = 'Bitte das Kalibrier-Ziel jetzt vom Greifer abnehmen.'
TARGET_REMOVED_BUTTON_DE = 'Kalibrier-Ziel ist abgenommen'
STOPPED_DE = 'Arm-Check abgebrochen. Es wurde nichts gespeichert.'
HANDHELD_RUNNING_DE = ('Kamera von Hand vermessen: {k} von 18 Ansichten. Bitte das Kalibrier-Ziel langsam '
                       'durch das ganze Bild bewegen.')
HANDHELD_DONE_DE = 'Kamera vermessen. Bitte jetzt den Arm-Check starten.'
HANDHELD_FAILED_DE = ('Die Kamera konnte nicht vermessen werden. Bitte das Kalibrier-Ziel näher und weiter '
                      'weg zeigen und wiederholen.')
LAST_RESULT_WARNING_DE = '[WARNUNG] Die Marker der Matte sind verdeckt – es gilt die letzte Prüfung.'
TRIM_SKIPPED_WARNING_DE = '[WARNUNG] Der Arm ist nicht genau angekommen – die Feinkorrektur wurde ausgelassen.'

# The calibration service's own sentences, which the spec does not word: a second start while an arm check (its
# program, its pose-1 way home or its solve) is under way, a check that failed for a reason of its own (logged
# in English), a start while the handheld step is under way, and the handheld step cancelled.
ARM_CHECK_BUSY_DE = 'Der Arm-Check läuft gerade – bitte warten, bis er fertig ist.'
CHECK_FAILED_DE = 'Die Kalibrierung konnte gerade nicht geprüft werden. Bitte noch einmal versuchen.'
HANDHELD_BUSY_DE = 'Die Kamera wird gerade von Hand vermessen – bitte zuerst fertig vermessen oder abbrechen.'
HANDHELD_STOPPED_DE = 'Vermessen der Kamera abgebrochen. Es wurde nichts gespeichert.'

# Result code (mat check refusal, arm-check gate, note) -> its sentence.
BY_CODE = {
    'ok': OK_DE,
    'profile_unknown': PROFILE_UNKNOWN_DE,
    'camera_not_tower': CAMERA_NOT_TOWER_DE,
    'camera_missing': CAMERA_MISSING_DE,
    'image_size': IMAGE_SIZE_DE,
    'too_dark': TOO_DARK_DE,
    'wrong_mat': WRONG_MAT_DE,
    'target_attached': TARGET_ATTACHED_DE,
    'fixture_hidden': FIXTURE_HIDDEN_DE,
    'tags_hidden': TAGS_HIDDEN_DE,
    'needs_arm_check/none': NEEDS_ARM_CHECK_NONE_DE,
    'needs_arm_check/contract_changed': NEEDS_ARM_CHECK_CONTRACT_CHANGED_DE,
    'needs_arm_check/camera_moved': NEEDS_ARM_CHECK_CAMERA_MOVED_DE,
    'needs_arm_check/fixture_changed': NEEDS_ARM_CHECK_FIXTURE_CHANGED_DE,
    'target_not_confirmed': TARGET_NOT_CONFIRMED_DE,
    'mat_not_flat': MAT_NOT_FLAT_DE,
    'mat_misplaced': MAT_MISPLACED_DE,
    'inconsistent': INCONSISTENT_DE,
    'tower_displaced': TOWER_DISPLACED_DE,
    'mat_moved': MAT_MOVED_DE,
    'camera_followed': CAMERA_FOLLOWED_DE,
    'tag_dropped': TAG_DROPPED_DE,
    'droop_above_design': DROOP_ABOVE_DESIGN_DE,
    'too_few_poses': TOO_FEW_POSES_DE,
    'lens': GATE_LENS_DE,
    'rms': GATE_LENS_DE,
    'tower': GATE_TOWER_DE,
    'joint': GATE_JOINT_DE,
    'mount': GATE_MOUNT_DE,
    'mat': GATE_MAT_DE,
    'fixture_layout': FIXTURE_LAYOUT_DE,
    'guard_margin': GUARD_MARGIN_DE,
    'target_not_seen': TARGET_NOT_SEEN_DE,
    'foreign_target': FOREIGN_TARGET_DE,
    'stopped': STOPPED_DE,
}


def tower_displaced_de(mm: float) -> str:
    """:data:`TOWER_DISPLACED_DE` with the larger reading, one decimal with a
    German decimal comma (whole millimetres would print a refused 10.4 mm as
    „10 mm“, the bound itself)."""
    return TOWER_DISPLACED_DE.format(mm=f'{float(mm):.1f}'.replace('.', ','))


def too_few_poses_de(k: int, n: int) -> str:
    return TOO_FEW_POSES_DE.format(k=int(k), n=int(n))


def arm_check_running_de(i: int, n: int) -> str:
    return ARM_CHECK_RUNNING_DE.format(i=int(i), n=int(n))


def droop_warning_de(joint: int, deg: float) -> str:
    """The droop warning: ``joint`` 1-based, the deviation in whole degrees."""
    return DROOP_WARNING_DE.format(grad=int(round(float(deg))), n=int(joint))


def refinement_warning_de(ziel: str, mm: float) -> str:
    return REFINEMENT_WARNING_DE.format(ziel=ziel, mm=int(round(float(mm))))


def handheld_running_de(k: int) -> str:
    return HANDHELD_RUNNING_DE.format(k=int(k))


def sentence_for(code: str, **fields) -> str:
    """The sentence of a code; a format field it names is filled from ``fields``."""
    text = BY_CODE[code]
    if code == 'tower_displaced':
        return tower_displaced_de(fields.get('mm', 0.0))
    if code == 'too_few_poses':
        return too_few_poses_de(fields.get('k', 0), fields.get('n', 0))
    return text
