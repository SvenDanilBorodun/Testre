#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Every German sentence of the recording path added in Aufnahme 2.0 round 5.

One module so the wording lives in one place (spec §8): the recorder
(``data_manager.py``, ``physical_ai_server.py``), the capture integrity
(``capture_timeline.TakeIntegrity``), the Hugging Face classifier
(``hf_errors.py``) and the upload worker (``hf_api_worker.py``) all read it.
Stdlib only and free of package imports, so a deps-free test loader can load it
by path. The two prefixes and ``BUSY_DE`` are byte-equal with the page
(``features/tasks/recordSession.js``), fenced by
``robotis_ai_setup/tests/test_record_r5_lockstep.py``.

Placeholders are named in English (``{n}``, ``{source}``); the builders below
fill them, so a caller never formats a template itself.
"""

from __future__ import annotations

# ── vocabulary (F6a) ──────────────────────────────────────────────────────────

CAMERA_NAME_DE = {'gripper': 'Greifer-Kamera', 'scene': 'Szenen-Kamera'}


def camera_name_de(name) -> str:
    return CAMERA_NAME_DE.get(str(name), f'Kamera „{name}“')


# Subject of a sentence about a source, with its article: „Die Szenen-Kamera“,
# „Der Leader-Arm“, „Der Follower-Arm“.
def source_subject_de(kind: str, name=None) -> str:
    if kind == 'camera':
        return f'Die {camera_name_de(name)}'
    if kind == 'leader':
        return 'Der Leader-Arm'
    if kind == 'follower':
        return 'Der Follower-Arm'
    raise ValueError(f'unknown source kind {kind!r}')


# ── a source that stops ends the session (R5-2, spec-r5 §2.2) ─────────────────

SOURCE_STOP_PREFIX_DE = 'Aufnahme beendet: '
SOURCE_GAP_PREFIX_DE = 'Signalaussetzer: '

_KEPT_SAVED_DE = 'Gespeicherte Episoden bleiben erhalten'
_TAKE_DROPPED_DE = ', die laufende Episode wurde verworfen'

_SOURCE_STOP_REMEDY_DE = {
    'camera': 'Prüfe das Kabel.',
    'leader': 'Ist der Leader-Arm eingeschaltet und verbunden?',
    'follower': 'Prüfe Kabel und Stromversorgung des Follower-Arms.',
}


def source_stop_de(kind: str, name=None, take_dropped: bool = True) -> str:
    """„Aufnahme beendet: …“ for a required source silent for 2 s.

    ``take_dropped`` is False in warm-up/reset (no take was running)."""
    what = 'sendet keine Bilder mehr' if kind == 'camera' else 'sendet keine Daten mehr'
    dropped = _TAKE_DROPPED_DE if take_dropped else ''
    return (f'{SOURCE_STOP_PREFIX_DE}{source_subject_de(kind, name)} {what}. '
            f'{_KEPT_SAVED_DE}{dropped}. {_SOURCE_STOP_REMEDY_DE[kind]}')


# ── short gaps (O2 + C6) ──────────────────────────────────────────────────────

SOURCE_GAP_DE = (SOURCE_GAP_PREFIX_DE + '{source} hat in Episode {n} kurz keine Daten '
                 'geliefert. Die Episode wird neu aufgenommen.')

GAP_KEPT_DE = (SOURCE_GAP_PREFIX_DE + '{source} hat in Episode {n} wieder kurz keine Daten '
               'geliefert. Die Episode wurde trotzdem gespeichert; nimm sie neu auf, wenn '
               'sie wichtig ist.')


def source_gap_de(kind: str, name, episode: int) -> str:
    return SOURCE_GAP_DE.format(source=source_subject_de(kind, name), n=int(episode))


def source_gap_kept_de(kind: str, name, episode: int) -> str:
    return GAP_KEPT_DE.format(source=source_subject_de(kind, name), n=int(episode))


# ── frame loss in the encoder (O6 + C7) ───────────────────────────────────────

FRAME_LOSS_REDO_DE = ('Episode {n}: Kamera-Bilder gingen beim Speichern verloren (der '
                      'Rechner war überlastet). Die Episode wird automatisch neu aufgenommen.')

FRAME_LOSS_END_DE = (SOURCE_STOP_PREFIX_DE + 'Episode {n} hat dreimal Kamera-Bilder verloren, '
                     'der Rechner ist überlastet. Gespeicherte Episoden bleiben erhalten.')


def frame_loss_redo_de(episode: int) -> str:
    return FRAME_LOSS_REDO_DE.format(n=int(episode))


def frame_loss_end_de(episode: int) -> str:
    return FRAME_LOSS_END_DE.format(n=int(episode))


# A take discarded while the session is already finishing (FINISH/STOP): it is
# not re-recorded, the session ends with the episodes already saved.
_ENDS_WITH_SAVED_DE = 'Die Aufnahme endet mit den schon gespeicherten Episoden.'

SOURCE_GAP_FINISH_DE = (SOURCE_GAP_PREFIX_DE + '{source} hat in Episode {n} kurz keine Daten '
                        'geliefert. Die Episode wurde verworfen, die Aufnahme endet mit den '
                        'schon gespeicherten Episoden.')

FRAME_LOSS_FINISH_DE = ('Episode {n}: Kamera-Bilder gingen beim Speichern verloren, die '
                        'Episode wurde verworfen. ' + _ENDS_WITH_SAVED_DE)


def source_gap_finish_de(kind: str, name, episode: int) -> str:
    return SOURCE_GAP_FINISH_DE.format(source=source_subject_de(kind, name), n=int(episode))


def frame_loss_finish_de(episode: int) -> str:
    return FRAME_LOSS_FINISH_DE.format(n=int(episode))


# The post-save length check (detect + inform): LeRobot's train-time
# FrameTimestampError condition.
SAVED_LENGTH_MISMATCH_DE = ('Episode {n}: Video und Daten der Kamera(s) {cameras} sind nicht '
                            'gleich lang. Diese Episode muss neu aufgenommen werden, sonst '
                            'bricht das Training ab.')


def saved_length_mismatch_de(episode: int, cameras) -> str:
    names = ', '.join(camera_name_de(c) for c in cameras)
    return SAVED_LENGTH_MISMATCH_DE.format(n=int(episode), cameras=names)


# ── commands and error stops (O6, D5) ─────────────────────────────────────────

BUSY_DE = 'Die Aufnahme ist gerade beschäftigt. Bitte versuch es gleich noch einmal.'

ERROR_STOP_SAVED_DE = ('Die schon gespeicherten Episoden sind gesichert; du kannst sie im '
                       'Tab Daten hochladen.')

# F3 (round 6): an error stop whose finalize FAILED. The crash marker stays, and
# the page shows an incomplete dataset instead of „gesichert".
ERROR_STOP_INCOMPLETE_DE = ('Der Datensatz ist unvollständig: Er konnte nicht abgeschlossen '
                            'werden. Nimm die Episoden neu auf.')

# The finalize failure at the end of a session (the terminating [WARNUNG]). The
# page recognises it by its first words
# (features/tasks/recordSession.js::FINALIZE_FAILED_PREFIX_DE, lockstep-tested).
FINALIZE_FAILED_PREFIX_DE = 'Datensatz konnte nicht abgeschlossen werden'
FINALIZE_FAILED_DE = (FINALIZE_FAILED_PREFIX_DE + ' — die Aufnahme ist unvollständig und muss '
                      'neu aufgenommen werden.')

# F1 (round 6): a FINISH the server accepted while the recorder was busy; it is
# applied the moment the official discard releases the recorder.
FINISH_QUEUED_DE = 'Die Aufnahme wird beendet, sobald die verworfene Episode aufgeräumt ist.'


# ── dataset existence and resume (D7, D4) ─────────────────────────────────────

HUB_CHECK_REFUSED_DE = ('Hugging Face ist gerade nicht erreichbar. Ohne diese Prüfung könnte '
                        'ein Datensatz auf Hugging Face überschrieben werden. Schalte unter '
                        '„Erweitert“ das Hochladen aus oder versuche es später.')

# F4/D7 (round 6): the hub answered, and refused the rig's token.
HUB_CHECK_AUTH_DE = ('Hugging Face lehnt den Token des Roboters ab (ungültig oder abgelaufen). '
                     'Speichere in der EduBotics-App unter „Schritt D: HuggingFace-Token“ einen '
                     'gültigen Token oder schalte unter „Erweitert“ das Hochladen aus.')

# F4 (round 6): a session started WITHOUT upload because the rig has no usable
# token. The page recognises the notice by its prefix (lockstep-tested).
UPLOAD_OFF_PREFIX_DE = 'Aufnahme ohne Hochladen: '
UPLOAD_OFF_NO_TOKEN_DE = (UPLOAD_OFF_PREFIX_DE + 'Auf dem Roboter ist kein Hugging-Face-Token '
                          'gespeichert. Der Datensatz bleibt auf dem Roboter; speichere einen '
                          'Token in der EduBotics-App unter „Schritt D: HuggingFace-Token“ und '
                          'lade ihn später im Tab Daten hoch.')
UPLOAD_OFF_TOKEN_INVALID_DE = (UPLOAD_OFF_PREFIX_DE + 'Hugging Face lehnt den Token des Roboters '
                               'ab (ungültig oder abgelaufen). Der Datensatz bleibt auf dem '
                               'Roboter; speichere einen gültigen Token in der EduBotics-App '
                               'unter „Schritt D: HuggingFace-Token“ und lade ihn später im Tab '
                               'Daten hoch.')

RESUME_FPS_DE = ('Der Datensatz „{name}“ wurde mit {d} Bildern pro Sekunde aufgenommen. '
                 'Stell unter „Erweitert“ {d} Bilder pro Sekunde ein oder wähle einen neuen '
                 'Aufgabennamen.')

RESUME_FEATURES_DE = ('Der Datensatz „{name}“ wurde mit anderen Kameras oder Gelenken '
                      'aufgenommen. Wähle einen neuen Aufgabennamen.')

RESUME_ROBOT_DE = ('Der Datensatz „{name}“ gehört zu einem anderen Roboter. Wähle einen '
                   'neuen Aufgabennamen.')


def resume_fps_de(name, dataset_fps) -> str:
    return RESUME_FPS_DE.format(name=name, d=int(dataset_fps))


def resume_features_de(name) -> str:
    return RESUME_FEATURES_DE.format(name=name)


def resume_robot_de(name) -> str:
    return RESUME_ROBOT_DE.format(name=name)


# ── upload (F7, R5-4b) ────────────────────────────────────────────────────────

UPLOAD_STALL_DE = ('Das Hochladen kommt nicht mehr voran. Prüfe die Internetverbindung des '
                   'Roboters. Der Datensatz bleibt auf dem Roboter gespeichert; du kannst ihn '
                   'später im Tab Daten hochladen.')

# Byte-identical with DataManager.HF_AUTH_ERROR_DE (it MUST point at the GUI
# token field „Schritt D“, never at `hf auth login`); fenced by
# test_record_texts_de.py.
HF_AUTH_ERROR_DE = (
    'Hugging Face-Token ungültig oder abgelaufen. Bitte in der '
    'EduBotics-App unter „Schritt D: HuggingFace-Token" einen gültigen '
    'Token speichern und die Umgebung neu starten.'
)
HF_NETWORK_ERROR_DE = ('Hugging Face ist gerade nicht erreichbar. Prüfe die '
                       'Internetverbindung des Roboters.')
HF_BUSY_ERROR_DE = ('Hugging Face meldet gerade zu viele Anfragen. Bitte warte eine Minute '
                    'und versuche es dann erneut.')
HF_SERVER_ERROR_DE = ('Hugging Face meldet gerade einen Serverfehler. Bitte versuche es '
                      'später erneut.')

# classify_hf_error(exc) -> sentence; None (unclassified) keeps the caller's own
# generic sentence.
HF_ERROR_SENTENCES_DE = {
    'auth': HF_AUTH_ERROR_DE,
    'network': HF_NETWORK_ERROR_DE,
    'busy': HF_BUSY_ERROR_DE,
    'server': HF_SERVER_ERROR_DE,
}


# ── per-take integrity (C5, warn only) ────────────────────────────────────────

TAKE_LOST_SLOTS_DE = ('Episode {n}: Der Rechner kam nicht hinterher, {x} von {y} Bildern fehlen '
                      '({s} s). Die Episode wurde gespeichert; nimm sie neu auf, wenn sie '
                      'wichtig ist.')

TAKE_EXCESS_REPEATS_DE = ('Episode {n}: Die {camera} hat zu wenige Bilder geliefert, {p} % der '
                          'Bilder sind Wiederholungen. Mehr Licht hilft oft.')

TAKE_ARM_LATE_DE = ('Episode {n}: Die Armdaten kamen zeitweise verspätet an (bis {ms} ms). Die '
                    'Episode wurde gespeichert; nimm sie neu auf, wenn sie wichtig ist.')


def format_seconds_de(seconds: float) -> str:
    """Seconds with a German decimal comma: 0.0333 -> '0,03', 1.5 -> '1,5', 2.0 -> '2'."""
    text = f'{max(0.0, float(seconds)):.2f}'.rstrip('0').rstrip('.')
    return text.replace('.', ',')


def take_lost_slots_de(episode: int, lost: int, planned: int, seconds: float) -> str:
    return TAKE_LOST_SLOTS_DE.format(n=int(episode), x=int(lost), y=int(planned),
                                     s=format_seconds_de(seconds))


def take_excess_repeats_de(episode: int, camera, percent: float) -> str:
    return TAKE_EXCESS_REPEATS_DE.format(n=int(episode), camera=camera_name_de(camera),
                                         p=int(round(percent)))


def take_arm_late_de(episode: int, milliseconds: float) -> str:
    return TAKE_ARM_LATE_DE.format(n=int(episode), ms=int(round(milliseconds)))
