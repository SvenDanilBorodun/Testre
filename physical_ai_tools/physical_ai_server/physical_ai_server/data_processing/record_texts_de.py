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

# Round 7: the re-record cap (2 per episode) is SHARED by gaps and frame loss,
# so these two sentences must stay true after any mix (gap+gap+drop,
# drop+drop+gap): no count of one kind, no „wieder".
GAP_KEPT_DE = (SOURCE_GAP_PREFIX_DE + '{source} hat in Episode {n} kurz keine Daten geliefert. '
               'Die Episode konnte auch nach zwei Wiederholungen nicht ohne Signal- oder '
               'Bildverlust aufgenommen werden und wurde trotzdem gespeichert; nimm sie neu '
               'auf, wenn sie wichtig ist.')


def source_gap_de(kind: str, name, episode: int) -> str:
    return SOURCE_GAP_DE.format(source=source_subject_de(kind, name), n=int(episode))


def source_gap_kept_de(kind: str, name, episode: int) -> str:
    return GAP_KEPT_DE.format(source=source_subject_de(kind, name), n=int(episode))


# ── frame loss in the encoder (O6 + C7) ───────────────────────────────────────

FRAME_LOSS_REDO_DE = ('Episode {n}: Kamera-Bilder gingen beim Speichern verloren (der '
                      'Rechner war überlastet). Die Episode wird automatisch neu aufgenommen.')

FRAME_LOSS_END_DE = (SOURCE_STOP_PREFIX_DE + 'Episode {n} konnte auch nach zwei Wiederholungen '
                     'nicht ohne Bildverlust gespeichert werden (der Rechner ist überlastet). '
                     'Gespeicherte Episoden bleiben erhalten.')


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


# The post-save video check: a camera's episode has no video file at all.
MISSING_VIDEO_DE = ('Episode {n}: Für die {cameras} wurde keine Video-Datei gespeichert. Diese '
                    'Episode muss neu aufgenommen werden, sonst ist das Training unbrauchbar.')


def missing_video_de(episode: int, cameras) -> str:
    names = ', '.join(camera_name_de(c) for c in cameras)
    return MISSING_VIDEO_DE.format(n=int(episode), cameras=names)


# A camera that shows the same image for a while during a RECORDING (warning
# only; a static scene is legitimate). The inference path keeps HEAD's own
# sentence (F3) and does not use this one.
STALE_CAMERA_RECORDING_DE = ('Die {camera} zeigt seit über {s} s dasselbe Bild. Die Aufnahme '
                             'läuft weiter – prüfe, ob die Kamera hängt.')


def stale_camera_recording_de(camera, seconds: float) -> str:
    return STALE_CAMERA_RECORDING_DE.format(camera=camera_name_de(camera),
                                            s=f'{float(seconds):.0f}')


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

# F4/D7 (round 6): the hub answered, and refused the rig's token. Since 042 the
# token is the student's own and is replaced on the Startseite.
HUB_CHECK_AUTH_DE = ('Hugging Face lehnt den Token des Roboters ab (ungültig oder abgelaufen). '
                     'Ersetze dein Token auf der Startseite oder schalte unter „Erweitert“ das '
                     'Hochladen aus.')

# F4 (round 6): a session started WITHOUT upload because the rig has no usable
# token. The page recognises the notice by its prefix (lockstep-tested).
UPLOAD_OFF_PREFIX_DE = 'Aufnahme ohne Hochladen: '
UPLOAD_OFF_NO_TOKEN_DE = (UPLOAD_OFF_PREFIX_DE + 'Auf dem Roboter ist kein Hugging-Face-Token '
                          'gespeichert. Der Datensatz bleibt auf dem Roboter; hinterlege dein '
                          'Token auf der Startseite und lade ihn später im Tab Daten hoch.')
UPLOAD_OFF_TOKEN_INVALID_DE = (UPLOAD_OFF_PREFIX_DE + 'Hugging Face lehnt den Token des Roboters '
                               'ab (ungültig oder abgelaufen). Der Datensatz bleibt auf dem '
                               'Roboter; ersetze dein Token auf der Startseite und lade ihn '
                               'später im Tab Daten hoch.')

# 042: the answers of /register_hf_user (the per-student token slot, see
# data_processing/hf_token_store.py). The reply is read by the SPA's token
# relay, which turns a refusal into the card's own wording, so these are the
# robot-side explanation (logs, a raw service call). None of them carries a
# token, a fingerprint or an exception text.
HF_TOKEN_SET_OK_DE = 'Dein Hugging-Face-Token ist auf dem Roboter aktiv.'
HF_TOKEN_CLEARED_DE = 'Das Hugging-Face-Token wurde vom Roboter entfernt.'
HF_TOKEN_NONE_DE = 'Auf dem Roboter ist kein Hugging-Face-Token gespeichert.'
# is_busy() is true for downloads, list fetches and uploads alike (audit M4),
# so the sentence names all three beside the recording (review e).
HF_TOKEN_BUSY_DE = ('Während der Roboter aufnimmt, etwas hoch- oder herunterlädt oder eine Liste '
                    'bei Hugging Face abfragt, kann das Token nicht geändert werden. Bitte versuche '
                    'es danach noch einmal.')
# A clear refused while busy is not forgotten: the node applies it by itself
# once the robot is idle (physical_ai_server.py::_apply_pending_hf_token_clear).
HF_TOKEN_CLEAR_QUEUED_DE = ('Während der Roboter aufnimmt, etwas hoch- oder herunterlädt oder eine '
                            'Liste bei Hugging Face abfragt, kann das Token nicht entfernt werden. Der '
                            'Roboter entfernt es von selbst, sobald er damit fertig ist.')
HF_TOKEN_SHAPE_DE = ('Das ist kein gültiges Hugging-Face-Token. Es beginnt mit „hf_“ und enthält '
                     'keine Leerzeichen.')
HF_TOKEN_UNSUPPORTED_DE = 'Dieser Roboter verwendet ein eigenes Token und nimmt kein persönliches an.'
HF_TOKEN_WRITE_FAILED_DE = 'Das Token konnte nicht auf dem Roboter gespeichert werden.'

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

# The end-of-session upload could not be handed to the worker.
UPLOAD_NOT_STARTED_DE = ('Das Hochladen konnte nicht gestartet werden. Du kannst den Datensatz '
                         'später im Tab Daten hochladen.')

# The namespace guard: the rig's token does not own the target account. Names
# neither namespace nor repo (an arbitrary-text oracle otherwise).
NAMESPACE_REFUSED_DE = ('Upload abgelehnt: Der Roboter darf nicht in dieses HuggingFace-Konto '
                        'hochladen. Bitte die „Benutzer-ID“ prüfen und erneut anmelden.')

# The training pointer (`v3.0`) could not be moved to the uploaded commit
# (Daten 2.0 §E2 step 7: the data is on the hub, the tag is not).
HUB_TAG_FAILED_DE = ('Der Versions-Tag des Datensatzes konnte nicht aktualisiert werden. Ohne '
                     'aktuellen Tag trainiert die Cloud auf einem alten Stand — bitte den Upload '
                     'erneut versuchen.')

# ── Daten 2.0: the guarded upload, the sync at Start, the auto-upload (§J.6 [R]) ─
#
# Every sentence below that sends the student somewhere names the Daten tab, so
# the Aufnahme finish card adds no second „später hochladen" line
# (components/Record/model/finishModel.js::POINTS_TO_DATEN_TAB).

# §E2 step 3/6: the hub moved between the decision and the commit (Hugging Face
# refused the commit's stale parent, or our read-back found other data).
HUB_CHANGED_SINCE_CHECK_DE = ('Auf Hugging Face hat sich der Datensatz inzwischen geändert. Es wurde '
                              'nichts hochgeladen. Öffne den Tab Daten und entscheide, welche Version '
                              'du behalten willst – „Beide behalten“ verliert nichts.')
# §E2 step 0 (audit M3): a crash marker — a running session, or one never finalized.
UPLOAD_IN_SESSION_DE = ('Nicht hochgeladen: Die Aufnahme dieses Datensatzes wurde unterbrochen und '
                        'nicht sauber beendet; hochgeladen würde er die Version auf Hugging Face '
                        'beschädigen. Lösche ihn im Tab Daten oder lade dort die Online-Version.')
# §E2 steps 0/5b (audit M3/m2): the local copy does not load, or a synced file changed.
UPLOAD_BROKEN_DE = ('Nicht hochgeladen: Der Datensatz auf dem Roboter ist unvollständig oder '
                    'beschädigt. Die Version auf Hugging Face bleibt, wie sie ist. Lösche ihn im Tab '
                    'Daten oder lade dort die Online-Version.')
# §E2 step 6 (G-13): the commit landed but could not be read back (status Success).
UPLOAD_UNCONFIRMED_DE = ('Hochgeladen, aber Hugging Face hat es noch nicht bestätigt. Im Tab Daten '
                         'siehst du, ob noch etwas zu tun ist.')
# §E2 steps 3/4: the hub holds another version (decided at upload time, or the exact check).
UPLOAD_HUB_DIFFERS_DE = ('Auf Hugging Face gibt es diesen Datensatz schon in einer anderen Version. '
                         'Es wurde nichts überschrieben. Öffne den Tab Daten, vergleiche beide '
                         'Versionen und entscheide dort.')

# §C4 (D14): the recording Start against the hub.
SYNC_CONFLICT_DE = ('Dieser Datensatz wurde hier geändert, und auf Hugging Face gibt es inzwischen '
                    'eine neuere Version. Entscheide im Tab Daten, welche du behalten willst, und '
                    'starte dann die Aufnahme neu.')
SYNC_UNKNOWN_DE = ('Der Datensatz hier und der auf Hugging Face sind verschieden, und EduBotics kann '
                   'nicht erkennen, welcher neuer ist. Entscheide im Tab Daten, welche Version du '
                   'behalten willst, und starte dann die Aufnahme neu.')
SYNC_HUB_UNUSABLE_DE = ('Die Version auf Hugging Face kann EduBotics nicht öffnen (älteres Format, '
                        'anderer Roboter oder unbekanntes Format). Die Aufnahme wurde nicht gestartet. '
                        'Wähle einen anderen Aufgabennamen.')
SYNC_DOWNLOAD_FAILED_DE = ('Die neuere Version von Hugging Face konnte nicht geladen werden. Die '
                           'Aufnahme wurde nicht gestartet, damit nichts überschrieben wird. Versuche '
                           'es gleich noch einmal oder schalte unter „Erweitert“ das Hochladen aus.')
SYNC_DISK_DE = ('Die neuere Version von Hugging Face braucht {need}, frei sind {free}, und für '
                'Aufnahmen müssen 3 GB frei bleiben. Die Aufnahme wurde nicht gestartet. Lösche zuerst '
                'alte Datensätze im Tab Daten.')
OFFLINE_START_DE = ('Hugging Face war beim Start nicht erreichbar. Die Aufnahme läuft trotzdem; beim '
                    'Hochladen am Ende prüft EduBotics, dass auf Hugging Face nichts überschrieben '
                    'wird.')
DATASET_BUSY_START_DE = ('Dieser Datensatz wird gerade im Tab Daten bearbeitet oder geladen. Starte '
                         'die Aufnahme, wenn das fertig ist.')

# The end-of-session auto-upload could not be handed to the HF worker (the node's
# _enqueue_dataset_upload); the exception text goes to the log only.
AUTO_UPLOAD_NO_WORKER_DE = ('Automatisches Hochladen fehlgeschlagen: Der Hugging-Face-Dienst des '
                            'Roboters konnte nicht starten. Lade den Datensatz später im Tab Daten '
                            'hoch.')
AUTO_UPLOAD_BUSY_DE = ('Automatisches Hochladen übersprungen: Gerade läuft ein anderer '
                       'Hugging-Face-Vorgang. Wenn er beendet ist, lade den Datensatz im Tab Daten '
                       'hoch.')
AUTO_UPLOAD_REFUSED_DE = ('Automatisches Hochladen fehlgeschlagen: Der Hugging-Face-Dienst des '
                          'Roboters hat die Anfrage abgelehnt. Lade den Datensatz später im Tab Daten '
                          'hoch.')
AUTO_UPLOAD_FAILED_DE = 'Automatisches Hochladen fehlgeschlagen. Lade den Datensatz später im Tab Daten hoch.'

# The old page's read services (communicator.py, R-27).
DATASET_INFO_FAILED_DE = 'Datensatz-Informationen konnten nicht gelesen werden.'
BROWSE_FAILED_DE = 'Der Ordner konnte nicht gelesen werden.'


def _format_gb_de(n) -> str:
    """The same rule as signal_status.format_gb_de (this module imports no
    package; a test pins the two equal)."""
    return f'{int(n) / 1e9:.1f}'.replace('.', ',') + ' GB'


def sync_disk_de(free, need) -> str:
    """D14's sync download does not fit: ``free``/``need`` in bytes (the
    download worker's result)."""
    return SYNC_DISK_DE.format(free=_format_gb_de(free), need=_format_gb_de(need))


UPLOAD_STALL_DE = ('Das Hochladen kommt nicht mehr voran. Prüfe die Internetverbindung des '
                   'Roboters. Der Datensatz bleibt auf dem Roboter gespeichert; du kannst ihn '
                   'später im Tab Daten hochladen.')

# Item g (2026-10-04): a download with no progress, or a list fetch / delete
# that never answers, is ended like an upload stall.
DOWNLOAD_STALL_DE = ('Das Herunterladen von Hugging Face kommt nicht mehr voran. Prüfe die '
                     'Internetverbindung des Roboters und versuche es danach noch einmal.')
HUB_QUERY_STALL_DE = ('Hugging Face hat nicht rechtzeitig geantwortet. Prüfe die '
                      'Internetverbindung des Roboters und versuche es danach noch einmal.')

# Byte-identical with DataManager.HF_AUTH_ERROR_DE (it MUST point at the
# Startseite, where the student replaces the token, never at `hf auth login`);
# fenced by test_record_texts_de.py. No „restart the environment" any more: a
# replaced token applies at once (042).
HF_AUTH_ERROR_DE = (
    'Hugging Face-Token ungültig oder abgelaufen. Ersetze dein Token auf der '
    'Startseite der EduBotics-App.'
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

TAKE_LOST_SLOTS_DE = ('Episode {n}: Der Rechner kam nicht hinterher, {x} von {y} Bildern {verb} '
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
                                     verb='fehlt' if int(lost) == 1 else 'fehlen',
                                     s=format_seconds_de(seconds))


def take_excess_repeats_de(episode: int, camera, percent: float) -> str:
    return TAKE_EXCESS_REPEATS_DE.format(n=int(episode), camera=camera_name_de(camera),
                                         p=int(round(percent)))


def take_arm_late_de(episode: int, milliseconds: float) -> str:
    return TAKE_ARM_LATE_DE.format(n=int(episode), ms=int(round(milliseconds)))
