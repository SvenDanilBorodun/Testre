"""The German sentences of the round-5 recording path (spec §8) are German.

Every template and every builder output is checked with the repo's own
transliteration pattern (``.github/scripts/german_detail_lint.py``, CI's
``german-strings-lint``): literal ä/ö/ü/ß, never ae/oe/ue/ss. The exact texts the
owner decided are pinned verbatim, the builders fill every placeholder, and the
module stays stdlib-only and free of package imports (deps-free loaders load it
by path).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import string
from pathlib import Path

import pytest

from physical_ai_server.data_processing import record_texts_de as t

_REPO = Path(__file__).resolve().parents[3]
_LINT = _REPO / '.github' / 'scripts' / 'german_detail_lint.py'
_DM = (Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'data_processing'
       / 'data_manager.py')


def _lint():
    spec = importlib.util.spec_from_file_location('_german_detail_lint', _LINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRANSLITERATIONS = _lint().TRANSLITERATIONS


def _all_texts():
    out = {}
    for name in dir(t):
        value = getattr(t, name)
        if name.isupper() and isinstance(value, str):
            out[name] = value
    for key, value in t.HF_ERROR_SENTENCES_DE.items():
        out[f'HF_ERROR_SENTENCES_DE[{key}]'] = value
    for kind, name in (('camera', 'gripper'), ('camera', 'scene'), ('camera', 'wrist'),
                       ('leader', None), ('follower', None)):
        for dropped in (True, False):
            out[f'source_stop_de({kind},{name},{dropped})'] = t.source_stop_de(kind, name, dropped)
        out[f'source_gap_de({kind})'] = t.source_gap_de(kind, name, 3)
        out[f'source_gap_kept_de({kind})'] = t.source_gap_kept_de(kind, name, 3)
    out['frame_loss_redo_de'] = t.frame_loss_redo_de(2)
    out['frame_loss_end_de'] = t.frame_loss_end_de(2)
    out['resume_fps_de'] = t.resume_fps_de('Würfel', 25)
    out['resume_features_de'] = t.resume_features_de('Würfel')
    out['resume_robot_de'] = t.resume_robot_de('Würfel')
    out['take_lost_slots_de'] = t.take_lost_slots_de(4, 3, 300, 0.1)
    out['take_excess_repeats_de'] = t.take_excess_repeats_de(4, 'scene', 12.4)
    out['take_arm_late_de'] = t.take_arm_late_de(4, 151.2)
    for kind, name in (('camera', 'scene'), ('leader', None)):
        out[f'source_gap_finish_de({kind})'] = t.source_gap_finish_de(kind, name, 2)
    out['frame_loss_finish_de'] = t.frame_loss_finish_de(2)
    out['saved_length_mismatch_de'] = t.saved_length_mismatch_de(3, ['gripper', 'scene'])
    return out


def test_module_is_stdlib_only_without_package_imports():
    tree = ast.parse(open(t.__file__, encoding='utf-8').read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ''])
            assert all(n.split('.')[0] == '__future__' for n in names), names


@pytest.mark.parametrize('name,text', sorted(_all_texts().items()))
def test_no_transliteration(name, text):
    assert not TRANSLITERATIONS.search(text), (name, text)


@pytest.mark.parametrize('name,text', sorted(_all_texts().items()))
def test_every_placeholder_is_filled_by_the_builders(name, text):
    if name.isupper():
        return
    assert '{' not in text and '}' not in text, (name, text)


def test_the_owner_sentences_verbatim():
    assert t.SOURCE_STOP_PREFIX_DE == 'Aufnahme beendet: '
    assert t.SOURCE_GAP_PREFIX_DE == 'Signalaussetzer: '
    assert t.BUSY_DE == ('Die Aufnahme ist gerade beschäftigt. Bitte versuch es gleich '
                         'noch einmal.')
    assert t.ERROR_STOP_SAVED_DE == ('Die schon gespeicherten Episoden sind gesichert; du '
                                     'kannst sie im Tab Daten hochladen.')
    assert t.HUB_CHECK_REFUSED_DE == (
        'Hugging Face ist gerade nicht erreichbar. Ohne diese Prüfung könnte ein Datensatz '
        'auf Hugging Face überschrieben werden. Schalte unter „Erweitert“ das Hochladen aus '
        'oder versuche es später.')
    assert t.UPLOAD_STALL_DE == (
        'Das Hochladen kommt nicht mehr voran. Prüfe die Internetverbindung des Roboters. '
        'Der Datensatz bleibt auf dem Roboter gespeichert; du kannst ihn später im Tab Daten '
        'hochladen.')
    assert t.frame_loss_redo_de(2) == (
        'Episode 2: Kamera-Bilder gingen beim Speichern verloren (der Rechner war '
        'überlastet). Die Episode wird automatisch neu aufgenommen.')
    # round 7: true after ANY mix under the shared cap of 2 (gap+gap+drop too)
    assert t.frame_loss_end_de(2) == (
        'Aufnahme beendet: Episode 2 konnte auch nach zwei Wiederholungen nicht ohne '
        'Bildverlust gespeichert werden (der Rechner ist überlastet). Gespeicherte Episoden '
        'bleiben erhalten.')
    assert t.resume_fps_de('Würfel', 25) == (
        'Der Datensatz „Würfel“ wurde mit 25 Bildern pro Sekunde aufgenommen. Stell unter '
        '„Erweitert“ 25 Bilder pro Sekunde ein oder wähle einen neuen Aufgabennamen.')
    assert t.resume_features_de('Würfel') == (
        'Der Datensatz „Würfel“ wurde mit anderen Kameras oder Gelenken aufgenommen. Wähle '
        'einen neuen Aufgabennamen.')
    assert t.resume_robot_de('Würfel') == (
        'Der Datensatz „Würfel“ gehört zu einem anderen Roboter. Wähle einen neuen '
        'Aufgabennamen.')


def test_source_stop_sentences_with_and_without_a_running_take():
    assert t.source_stop_de('camera', 'scene') == (
        'Aufnahme beendet: Die Szenen-Kamera sendet keine Bilder mehr. Gespeicherte Episoden '
        'bleiben erhalten, die laufende Episode wurde verworfen. Prüfe das Kabel.')
    assert t.source_stop_de('camera', 'gripper', take_dropped=False) == (
        'Aufnahme beendet: Die Greifer-Kamera sendet keine Bilder mehr. Gespeicherte '
        'Episoden bleiben erhalten. Prüfe das Kabel.')
    assert t.source_stop_de('leader') == (
        'Aufnahme beendet: Der Leader-Arm sendet keine Daten mehr. Gespeicherte Episoden '
        'bleiben erhalten, die laufende Episode wurde verworfen. Ist der Leader-Arm '
        'eingeschaltet und verbunden?')
    assert t.source_stop_de('follower', take_dropped=False) == (
        'Aufnahme beendet: Der Follower-Arm sendet keine Daten mehr. Gespeicherte Episoden '
        'bleiben erhalten. Prüfe Kabel und Stromversorgung des Follower-Arms.')
    assert t.source_stop_de('camera', 'wrist').startswith(
        'Aufnahme beendet: Die Kamera „wrist“ sendet keine Bilder mehr.')
    with pytest.raises(ValueError):
        t.source_stop_de('lidar')


def test_gap_sentences():
    assert t.source_gap_de('camera', 'scene', 3) == (
        'Signalaussetzer: Die Szenen-Kamera hat in Episode 3 kurz keine Daten geliefert. Die '
        'Episode wird neu aufgenommen.')
    assert t.source_gap_de('leader', None, 1) == (
        'Signalaussetzer: Der Leader-Arm hat in Episode 1 kurz keine Daten geliefert. Die '
        'Episode wird neu aufgenommen.')
    # round 7: no „wieder" (drop+drop+gap is the FIRST gap); the cap is two
    # re-records in all
    assert t.source_gap_kept_de('follower', None, 5) == (
        'Signalaussetzer: Der Follower-Arm hat in Episode 5 kurz keine Daten geliefert. Die '
        'Episode konnte auch nach zwei Wiederholungen nicht ohne Signal- oder Bildverlust '
        'aufgenommen werden und wurde trotzdem gespeichert; nimm sie neu auf, wenn sie '
        'wichtig ist.')
    for text in (t.source_gap_de('camera', 'gripper', 1), t.source_gap_kept_de('leader', None, 1)):
        assert text.startswith(t.SOURCE_GAP_PREFIX_DE)
    # the C7 end is a session end: the page reads it as „Abgebrochen, verworfen“
    assert t.frame_loss_end_de(1).startswith(t.SOURCE_STOP_PREFIX_DE)


def test_c5_sentences():
    assert t.take_lost_slots_de(4, 3, 300, 0.1) == (
        'Episode 4: Der Rechner kam nicht hinterher, 3 von 300 Bildern fehlen (0,1 s). Die '
        'Episode wurde gespeichert; nimm sie neu auf, wenn sie wichtig ist.')
    # round 7: singular
    assert t.take_lost_slots_de(4, 1, 301, 0.0333).startswith(
        'Episode 4: Der Rechner kam nicht hinterher, 1 von 301 Bildern fehlt (0,03 s).')
    assert t.take_excess_repeats_de(4, 'gripper', 12.4) == (
        'Episode 4: Die Greifer-Kamera hat zu wenige Bilder geliefert, 12 % der Bilder sind '
        'Wiederholungen. Mehr Licht hilft oft.')
    assert t.take_arm_late_de(4, 151.2) == (
        'Episode 4: Die Armdaten kamen zeitweise verspätet an (bis 151 ms). Die Episode wurde '
        'gespeichert; nimm sie neu auf, wenn sie wichtig ist.')


@pytest.mark.parametrize('seconds,text', [(0.0333, '0,03'), (0.1, '0,1'), (1.5, '1,5'),
                                          (2.0, '2'), (12.345, '12,35'), (-1, '0')])
def test_format_seconds_de(seconds, text):
    assert t.format_seconds_de(seconds) == text


def test_hf_sentences_and_the_auth_sentence_is_the_data_managers():
    assert t.HF_ERROR_SENTENCES_DE == {
        'auth': t.HF_AUTH_ERROR_DE,
        'network': ('Hugging Face ist gerade nicht erreichbar. Prüfe die Internetverbindung '
                    'des Roboters.'),
        'busy': ('Hugging Face meldet gerade zu viele Anfragen. Bitte warte eine Minute und '
                 'versuche es dann erneut.'),
        'server': ('Hugging Face meldet gerade einen Serverfehler. Bitte versuche es später '
                   'erneut.'),
    }
    # the long-standing „Schritt D“ sentence, byte for byte (it points at the
    # GUI token field, never at `hf auth login`)
    src = _DM.read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'HF_AUTH_ERROR_DE'
                and isinstance(node.value, ast.Constant)):
            assert node.value.value == t.HF_AUTH_ERROR_DE
            break
    else:
        # data_manager may take it from this module instead of keeping a copy
        assert 'HF_AUTH_ERROR_DE' in src
    assert 'Schritt D' in t.HF_AUTH_ERROR_DE and 'hf auth login' not in t.HF_AUTH_ERROR_DE


def test_camera_names_match_the_data_managers_vocabulary():
    assert t.camera_name_de('gripper') == 'Greifer-Kamera'
    assert t.camera_name_de('scene') == 'Szenen-Kamera'
    assert t.camera_name_de('wrist') == 'Kamera „wrist“'
    src = _DM.read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'CAMERA_NAME_DE'
                and isinstance(node.value, ast.Dict)):
            assert ast.literal_eval(node.value) == t.CAMERA_NAME_DE
    m = re.search(r"f'Kamera „\{name\}(.)'", src)
    if m:
        assert m.group(1) == '“'


def test_templates_name_only_english_placeholders():
    for name, text in _all_texts().items():
        if not name.isupper():
            continue
        fields = {f for _lit, f, _spec, _conv in string.Formatter().parse(text) if f}
        assert all(re.fullmatch(r'[a-z_]+', f) for f in fields), (name, fields)


# ── round 6 ──────────────────────────────────────────────────────────────────

def test_round6_finalize_and_error_stop_sentences():
    # the long-standing finalize sentence, now in this module; the page keys
    # on its first words (recordSession.js::FINALIZE_FAILED_PREFIX_DE)
    assert t.FINALIZE_FAILED_DE == (
        'Datensatz konnte nicht abgeschlossen werden — die Aufnahme ist unvollständig und '
        'muss neu aufgenommen werden.')
    assert t.FINALIZE_FAILED_DE.startswith(t.FINALIZE_FAILED_PREFIX_DE)
    # D5 + F3: an error stop whose finalize FAILED (the crash marker stays)
    assert t.ERROR_STOP_INCOMPLETE_DE == (
        'Der Datensatz ist unvollständig: Er konnte nicht abgeschlossen werden. Nimm die '
        'Episoden neu auf.')
    assert t.ERROR_STOP_SAVED_DE != t.ERROR_STOP_INCOMPLETE_DE


def test_round6_hub_check_and_upload_off_sentences():
    # F4/D7: the hub refused the rig's token
    assert t.HUB_CHECK_AUTH_DE == (
        'Hugging Face lehnt den Token des Roboters ab (ungültig oder abgelaufen). Speichere '
        'in der EduBotics-App unter „Schritt D: HuggingFace-Token“ einen gültigen Token oder '
        'schalte unter „Erweitert“ das Hochladen aus.')
    # F4: a session that runs WITHOUT upload because the rig has no usable token
    assert t.UPLOAD_OFF_PREFIX_DE == 'Aufnahme ohne Hochladen: '
    assert t.UPLOAD_OFF_NO_TOKEN_DE == (
        'Aufnahme ohne Hochladen: Auf dem Roboter ist kein Hugging-Face-Token gespeichert. '
        'Der Datensatz bleibt auf dem Roboter; speichere einen Token in der EduBotics-App '
        'unter „Schritt D: HuggingFace-Token“ und lade ihn später im Tab Daten hoch.')
    assert t.UPLOAD_OFF_TOKEN_INVALID_DE == (
        'Aufnahme ohne Hochladen: Hugging Face lehnt den Token des Roboters ab (ungültig oder '
        'abgelaufen). Der Datensatz bleibt auf dem Roboter; speichere einen gültigen Token in '
        'der EduBotics-App unter „Schritt D: HuggingFace-Token“ und lade ihn später im Tab '
        'Daten hoch.')
    for text in (t.UPLOAD_OFF_NO_TOKEN_DE, t.UPLOAD_OFF_TOKEN_INVALID_DE):
        assert text.startswith(t.UPLOAD_OFF_PREFIX_DE)
    # F1: a FINISH accepted while the recorder was busy, applied after the discard
    assert t.FINISH_QUEUED_DE == (
        'Die Aufnahme wird beendet, sobald die verworfene Episode aufgeräumt ist.')


def test_round6_sentences_moved_in_from_the_data_manager():
    assert t.source_gap_finish_de('camera', 'scene', 2) == (
        'Signalaussetzer: Die Szenen-Kamera hat in Episode 2 kurz keine Daten geliefert. Die '
        'Episode wurde verworfen, die Aufnahme endet mit den schon gespeicherten Episoden.')
    assert t.source_gap_finish_de('leader', None, 2).startswith(t.SOURCE_GAP_PREFIX_DE)
    assert t.frame_loss_finish_de(2) == (
        'Episode 2: Kamera-Bilder gingen beim Speichern verloren, die Episode wurde verworfen. '
        'Die Aufnahme endet mit den schon gespeicherten Episoden.')
    assert t.saved_length_mismatch_de(3, ['gripper', 'scene']) == (
        'Episode 3: Video und Daten der Kamera(s) Greifer-Kamera, Szenen-Kamera sind nicht '
        'gleich lang. Diese Episode muss neu aufgenommen werden, sonst bricht das Training ab.')


# ── round 7: the last inline sentences of data_manager.py get names ──────────

def test_round7_names_for_the_remaining_inline_sentences():
    assert t.UPLOAD_NOT_STARTED_DE == (
        'Das Hochladen konnte nicht gestartet werden. Du kannst den Datensatz später im Tab '
        'Daten hochladen.')
    assert t.stale_camera_recording_de('scene', 5.0) == (
        'Die Szenen-Kamera zeigt seit über 5 s dasselbe Bild. Die Aufnahme läuft weiter – '
        'prüfe, ob die Kamera hängt.')
    assert t.missing_video_de(3, ['gripper']) == (
        'Episode 3: Für die Greifer-Kamera wurde keine Video-Datei gespeichert. Diese Episode '
        'muss neu aufgenommen werden, sonst ist das Training unbrauchbar.')
    assert t.missing_video_de(3, ['gripper', 'scene']).startswith(
        'Episode 3: Für die Greifer-Kamera, Szenen-Kamera wurde keine Video-Datei gespeichert.')
    assert t.NAMESPACE_REFUSED_DE == (
        'Upload abgelehnt: Der Roboter darf nicht in dieses HuggingFace-Konto hochladen. '
        'Bitte die „Benutzer-ID“ prüfen und erneut anmelden.')
    assert t.HUB_SYNC_FAILED_DE == (
        'Alte Dateien auf Hugging Face konnten nicht entfernt werden. Ohne Bereinigung würde '
        'das Training gelöschte Episoden weiterverwenden — bitte den Upload erneut versuchen.')
    assert t.HUB_TAG_FAILED_DE == (
        'Der Versions-Tag des Datensatzes konnte nicht aktualisiert werden. Ohne aktuellen Tag '
        'trainiert die Cloud auf einem alten Stand — bitte den Upload erneut versuchen.')


def test_round7_the_inline_copies_are_byte_identical_while_they_exist():
    """A moves these into record_texts_de.py; while a copy stays inline in the
    data manager it must say exactly the same (the parser folds implicit
    string concatenation, so the AST constant is the whole sentence)."""
    src = _DM.read_text(encoding='utf-8')
    constants = {node.value for node in ast.walk(ast.parse(src))
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    for name in ('UPLOAD_NOT_STARTED_DE', 'NAMESPACE_REFUSED_DE', 'HUB_SYNC_FAILED_DE',
                 'HUB_TAG_FAILED_DE'):
        text = getattr(t, name)
        assert text in constants or f'record_texts_de.{name}' in src, name
