#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Every sentence of the automatic calibration (spec 40 S4's table and the
sentences defined beside their codes), verbatim and held to Rule §1.

german-strings-lint cannot see ``autocal/texts_de.py``, so this file applies
its ``TRANSLITERATIONS`` and ``GERMAN_CHARS``/``GERMAN_WORDS`` rules to every
constant, and the shipped-Python glyph detector. The sentences are written
here a second time on purpose: the spec's table, with its straight closing
quote typeset as the repo's typographic „…“.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from physical_ai_server.workflow.autocal import texts_de as t

_REPO = Path(__file__).resolve().parents[3]
_SRC = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'workflow' / 'autocal' / 'texts_de.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LINT = _load(_REPO / '.github' / 'scripts' / 'german_detail_lint.py', '_german_detail_lint_autocal')
_GLYPHS = _load(_REPO / 'robotis_ai_setup' / 'tests' / 'test_no_icon_glyphs.py', '_no_icon_glyphs_autocal')

# The spec's table (S4), row by row.
SPEC_TABLE = {
    'ok': 'Kalibrierung in Ordnung.',
    'profile_unknown': 'Das Roboter-Abbild meldet nicht, welche Szenen-Kamera eingerichtet ist. Bitte die Umgebung '
                       'neu starten.',
    'camera_not_tower': 'Roboter Studio am echten Roboter braucht die Turm-Kamera (Turm-Kit). Bitte in der '
                        'Einrichtung „Turm-Kamera“ auswählen.',
    'droop_above_design': 'Dieser Arm hängt etwas stärker durch als vorgesehen. Direkt neben der ausgesparten Ecke '
                          'der Matte kann der Roboter deshalb ein Greifen ablehnen.',
    'camera_missing': 'Die Turm-Kamera liefert kein Bild. Bitte das USB-Kabel der Kamera prüfen.',
    'image_size': 'Das Kamerabild passt nicht zur Kalibrierung (Bildgröße). Bitte die Umgebung neu starten.',
    'too_dark': 'Das Kamerabild ist zu dunkel. Bitte das Licht einschalten.',
    'wrong_mat': 'Auf dem Tisch liegt die Matte des anderen Roboters. Bitte die Matte für diesen Roboter auflegen.',
    'target_attached': 'Am Greifer steckt noch das Kalibrier-Ziel. Bitte abnehmen.',
    'fixture_hidden': 'Die beiden Marker neben dem Turm sind nicht zu sehen. Bitte nichts darauf abstellen.',
    'tags_hidden': 'Zu viele Marker der Matte sind verdeckt. Bitte die Matte frei räumen.',
    'needs_arm_check/none': 'Dieser Roboter ist noch nicht eingemessen. Bitte den Arm-Check starten.',
    'needs_arm_check/contract_changed': 'Die gespeicherte Kalibrierung passt nicht mehr zu dieser Version. Bitte den '
                                        'Arm-Check starten.',
    'needs_arm_check/camera_moved': 'Die Kamera wurde verdreht. Bitte den Arm-Check starten.',
    'needs_arm_check/fixture_changed': 'Die Marker neben dem Turm sitzen anders als beim letzten Arm-Check. Bitte den '
                                       'Arm-Check starten.',
    'mat_not_flat': 'Die Matte liegt nicht flach. Bitte glatt auflegen und an den Ecken fixieren.',
    'mat_misplaced': 'Die Matte liegt nicht an der Grundplatte an. Bitte bis zum Anschlag an die Grundplatte schieben.',
    'mat_moved': 'Matte verschoben – automatisch nachgeführt.',
    'camera_followed': 'Kamera leicht verschoben – automatisch nachgeführt.',
    'tag_dropped': 'Ein Marker der Matte liegt nicht flach und wurde nicht verwendet.',
    'lens': 'Die Kamera konnte nicht genau genug vermessen werden. Bitte „Kamera von Hand vermessen“ starten.',
    'tower': 'Die Kamera sitzt nicht an ihrem Platz am Turm oder schaut in die falsche Richtung. Bitte prüfen, ob der '
             'Turm gerade steht und alle Teile ganz zusammengesteckt sind, und den Arm-Check wiederholen.',
    'joint': 'Ein Gelenk des Arms weicht zu stark von seiner Bauform ab. Bitte die Lehrkraft informieren.',
    'mount': 'Das Kalibrier-Ziel sitzt nicht richtig am Greifer. Bitte fest aufklipsen und den Arm-Check wiederholen.',
    'mat': 'Die Matte liegt nicht an ihrem Platz oder nicht flach. Bitte an die Grundplatte schieben, glatt auflegen '
           'und den Arm-Check wiederholen.',
    'stopped': 'Arm-Check abgebrochen. Es wurde nichts gespeichert.',
}
# Sentences defined beside their codes in S4's text.
SPEC_INLINE = {
    'target_not_confirmed': 'Bitte das Kalibrier-Ziel vom Greifer abnehmen und bestätigen.',
    'inconsistent': 'Das Kamerabild passt nicht zur gespeicherten Kalibrierung. Bitte prüfen, ob die Matte flach '
                    'liegt; sonst den Arm-Check starten.',
    'fixture_layout': 'Die beiden Marker neben dem Kameraturm liegen nicht wie vorgesehen. Bitte prüfen, ob die '
                      'Marker-Aufkleber in ihren Mulden kleben, und den Arm-Check wiederholen.',
    'guard_margin': 'Der Arm weicht zu stark von seiner Bauform ab – seine Beobachtungshaltung käme dem Kameraturm zu '
                    'nahe. Bitte den Zusammenbau des Arms prüfen.',
    'target_not_seen': 'Das Kalibrier-Ziel ist nicht zu sehen. Bitte das Ziel auf den Greifer klipsen und den '
                       'Arm-Check neu starten.',
    'foreign_target': 'Am Greifer steckt das Kalibrier-Ziel des anderen Roboters.',
}


def _constants():
    return {k: v for k, v in vars(t).items() if k.endswith('_DE') and isinstance(v, str)}


def _rendered():
    out = dict(_constants())
    out['tower_displaced_de'] = t.tower_displaced_de(12.34)
    out['too_few_poses_de'] = t.too_few_poses_de(13, 20)
    out['arm_check_running_de'] = t.arm_check_running_de(3, 20)
    out['droop_warning_de'] = t.droop_warning_de(2, 6.88)
    out['refinement_warning_de'] = t.refinement_warning_de('Ablage', 3.4)
    out['handheld_running_de'] = t.handheld_running_de(7)
    return out


@pytest.mark.parametrize('code', sorted(SPEC_TABLE))
def test_every_sentence_of_the_table_verbatim(code):
    assert t.BY_CODE[code] == SPEC_TABLE[code]


@pytest.mark.parametrize('code', sorted(SPEC_INLINE))
def test_every_sentence_defined_beside_its_code_verbatim(code):
    assert t.BY_CODE[code] == SPEC_INLINE[code]


def test_the_sentences_with_fields_and_the_rest_of_the_table():
    assert t.tower_displaced_de(12.34) == (
        'Die Kamera sitzt nicht mehr an ihrem Platz am Turm oder ihr Objektiv wurde verdreht (Abweichung 12,3 mm). '
        'Bitte Turm und Objektiv prüfen und danach den Arm-Check starten.')
    assert t.arm_check_running_de(3, 20) == 'Arm-Check läuft: Pose 3 von 20.'
    assert t.too_few_poses_de(13, 20) == (
        'Der Arm-Check konnte zu wenige Posen auswerten (13 von 20). Bitte für gleichmäßiges Licht sorgen, den Tisch '
        'frei räumen und den Arm-Check wiederholen.')
    assert t.droop_warning_de(2, 6.88) == (
        'Achtung: Der Arm erreicht seine Posen nicht genau (Abweichung 7° an Gelenk 2): Er hängt stark durch oder '
        'stößt irgendwo an. Greifen am äußeren Rand kann mehrere Zentimeter danebengehen. Bitte Netzteil und Kabel '
        'des Arms prüfen, den Platz um den Arm frei räumen und den Arm-Check wiederholen.')
    assert t.refinement_warning_de('Ablage', 3.4) == (
        'Achtung: „Ablage“ liegt am Rand des Arbeitsbereichs – der Arm trifft es nur auf etwa 3 mm genau.')
    assert t.handheld_running_de(7) == (
        'Kamera von Hand vermessen: 7 von 18 Ansichten. Bitte das Kalibrier-Ziel langsam durch das ganze Bild '
        'bewegen.')
    assert t.HANDHELD_DONE_DE == 'Kamera vermessen. Bitte jetzt den Arm-Check starten.'
    assert t.HANDHELD_FAILED_DE == ('Die Kamera konnte nicht vermessen werden. Bitte das Kalibrier-Ziel näher und '
                                    'weiter weg zeigen und wiederholen.')
    assert t.LAST_RESULT_WARNING_DE == '[WARNUNG] Die Marker der Matte sind verdeckt – es gilt die letzte Prüfung.'
    assert t.TRIM_SKIPPED_WARNING_DE == ('[WARNUNG] Der Arm ist nicht genau angekommen – die Feinkorrektur wurde '
                                         'ausgelassen.')
    assert t.CONFIRM_TARGET_REMOVED_DE == 'Bitte das Kalibrier-Ziel jetzt vom Greifer abnehmen.'
    assert t.TARGET_REMOVED_BUTTON_DE == 'Kalibrier-Ziel ist abgenommen'
    assert t.BY_CODE['rms'] == t.BY_CODE['lens']           # a failed RMS gate offers the handheld step too


def test_every_constant_follows_the_german_lint_rules():
    texts = _rendered()
    assert len(texts) >= 45
    unmarked = set()
    for name, text in texts.items():
        assert text and text == text.strip(), name
        assert not _LINT.TRANSLITERATIONS.search(text), (name, text)
        assert '"' not in text and "'" not in text, (name, text)     # typographic quotes only
        if name.islower():                                          # a rendered helper: every field filled
            assert '{' not in text and '}' not in text, (name, text)
        assert not _LINT.ENGLISH_WORDS.search(text) or _LINT.GERMAN_CHARS.search(text), (name, text)
        if not (_LINT.GERMAN_CHARS.search(text) or _LINT.GERMAN_WORDS.search(text)):
            unmarked.add(name)
    # every sentence carries an umlaut or an unambiguous German word — but exactly these (the spec's own wording)
    assert unmarked == {'OK_DE', 'TARGET_REMOVED_BUTTON_DE', 'FOREIGN_TARGET_DE', 'REFINEMENT_WARNING_DE',
                        'refinement_warning_de'}, unmarked


def test_no_icon_glyph_in_any_sentence():
    source = _SRC.read_text(encoding='utf-8')
    assert _GLYPHS.glyph_hits(source) == []
    for name, text in _rendered().items():
        assert _GLYPHS.banned_code_points(text) == [], name


def test_every_code_has_a_sentence():
    for code in list(SPEC_TABLE) + list(SPEC_INLINE) + ['rms', 'tower_displaced', 'too_few_poses']:
        assert code in t.BY_CODE, code
    assert t.sentence_for('tower_displaced', mm=10.5).endswith('den Arm-Check starten.')
    assert '(5 von 20)' in t.sentence_for('too_few_poses', k=5, n=20)
