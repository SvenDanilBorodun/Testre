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

"""The OMX camera station's wire codes and German sentences (plan v6.13).

One copy: every station module and the node read them from here, and
``robotis_ai_setup/tests/test_station_lockstep.py`` compares the code sets
with the ``.srv`` comments and the React copy module. Stdlib only.
"""

STATION_PROTOCOL = 6

# The station's frame topic and frame id (written by the arm container's
# camera_ingest_node.py for cam_id 200, plan §4).
STATION_CAMERA = 'station_scene'
STATION_TOPIC = '/station_scene/image_raw/compressed'
STATION_IMAGE_SIZE = (1920, 1080)

# ── §8.1 StationSession ─────────────────────────────────────────────────────
STATES = ('OFF', 'IDLE', 'ENTERING', 'ACTIVE')
SESSION_REASON_CODES = (
    'RESUMED', 'LEADER_ACTIVE', 'OTHER_MODE_ACTIVE', 'RETORQUE_PENDING',
    'BUSY_OTHER_TAB', 'NATIVE_TIMEOUT', 'NOT_STATION_RIG', 'OWNER_MISMATCH',
    'SESSION_ENDED', 'WORKFLOW_ACTIVE',
)
END_REASONS = ('NATIVE_TIMEOUT', 'LEADER_ACTIVE', 'KEEPALIVE_LOST', 'LEFT')

# ── §8.2 StationCheck ───────────────────────────────────────────────────────
CHECK_RESULTS = (
    'OK', 'QUEUED', 'MOVED', 'WRONG_MAT', 'REFS_MISSING', 'NO_FRAME',
    'WRONG_SIZE', 'NOT_CONFIGURED', 'NOT_OWNER', 'BUSY', 'NOT_VERTICAL',
    'NO_DOT', 'ROBOT_MOVED', 'RESOLVE_REFUSED', 'FAILED', 'INVALID',
)

# ── §8.3 StationCommission ──────────────────────────────────────────────────
COMMISSION_RESULTS = (
    'OK', 'QUEUED', 'NO_FRAME', 'NOT_OWNER', 'REFUSED', 'FAILED', 'INVALID',
)
JOB_STATES = ('none', 'queued', 'running', 'retry_wait', 'done', 'failed', 'stale')

# ── Session sentences (§5.2, §5.3, §9.1) ────────────────────────────────────
NOT_STATION_RIG_DE = 'Dieser Roboter ist keine Kamerastation.'
RESUMED_DE = 'Stationssitzung fortgesetzt.'
ENTERED_DE = 'Stationssitzung gestartet.'
LEFT_DE = 'Station verlassen.'
KEEPALIVE_OK_DE = 'Stationssitzung aktiv.'
LEADER_ACTIVE_ENTER_DE = (
    'Bitte zuerst den Leader-Arm ausschalten (Schalter in Roboter Studio).')
BUSY_OTHER_TAB_DE = 'Roboter Studio ist in einem anderen Fenster geöffnet.'
OWNER_MISMATCH_DE = 'Die Kamerastation wird gerade in einem anderen Fenster genutzt.'
SESSION_ENDED_DE = 'Die Stationssitzung ist beendet.'
WORKFLOW_ACTIVE_DE = 'Stoppe zuerst dein Programm.'
RETORQUE_IN_PROGRESS_DE = 'Die Einrichtung wird gerade beendet – bitte kurz warten.'
END_REASON_DE = {
    'NATIVE_TIMEOUT': 'Die Stationskamera liefert kein 1080p-Bild.',
    'LEADER_ACTIVE': 'Die Stationssitzung wurde beendet, weil der Leader-Arm aktiv ist.',
    'KEEPALIVE_LOST': 'Die Verbindung zur Seite war unterbrochen – die Stationssitzung wurde beendet.',
    'LEFT': 'Station verlassen.',
}
# §5.2: recording / inference starts while the claim is held.
CLAIM_REFUSAL_DE = (
    'Roboter Studio nutzt gerade die Stationskamera – bitte Roboter Studio verlassen.')

# ── D4 motion gate (§5.4) ───────────────────────────────────────────────────
GATE_NOT_CONFIGURED_DE = (
    'Die Kamerastation ist noch nicht eingerichtet oder ihre Kalibrierung ist '
    'ungültig – bitte Lehrkraft: Station einrichten.')
GATE_NOT_VERIFIED_DE = 'Bitte zuerst den Stations-Check abschließen (Kamera und Roboter).'
GATE_DIGEST_CHANGED_DE = 'Die Kalibrierung hat sich geändert – bitte das Programm neu starten.'
MANUAL_CALIBRATION_REFUSED_DE = (
    'Auf einer Kamerastation wird über „Station einrichten“ kalibriert.')
MARK_SCENE_REFUSED_DE = 'Im Stationsbetrieb bitte das Stationsbild verwenden.'
MARK_OUT_OF_IMAGE_DE = 'Der Klick liegt außerhalb des Stationsbilds.'
MARK_PROJECTION_FAILED_DE = 'Klick konnte nicht auf den Tisch projiziert werden.'
FRAME_SIZE_MISMATCH_DE = 'Kamerabild passt nicht zur Kalibrierung.'
CALIBRATION_ACTIVE_DE = 'Kalibrierung läuft gerade — bitte zuerst beenden.'

# ── D16 (manual arbiter) ────────────────────────────────────────────────────
ARM_JUST_LOCKED_DE = 'Der Arm wurde gerade festgesetzt – bitte noch einmal versuchen.'

# ── Camera check (§7.1) ─────────────────────────────────────────────────────
CHECK_OK_DE = 'Kamera-Check bestanden.'
WRONG_MAT_DE = 'Diese Matte gehört nicht zu dieser Kalibrierung.'
REFS_MISSING_DE = (
    'Marker nicht gefunden – bitte die Matte freiräumen. Hilft das nicht, ist '
    'die Kamera stark verstellt (Lehrkraft).')
MOVED_DE = 'Die Kamera hat sich bewegt – bitte Lehrkraft: Kamera neu ausrichten.'
MOVED_FAR_DE = 'Kamera deutlich verstellt – Turm oder Matte verschoben?'
NO_FRAME_DE = 'Kein aktuelles Bild der Stationskamera – bitte kurz warten.'
WRONG_SIZE_DE = 'Das Bild der Stationskamera hat nicht die Größe 1920×1080.'
NOT_CONFIGURED_DE = GATE_NOT_CONFIGURED_DE
NOT_OWNER_DE = 'Nur das Fenster, das die Stationssitzung hält, kann das ausführen.'
CHECK_BUSY_DE = 'Es läuft gerade schon ein Check – bitte kurz warten.'
CHECK_NOT_ACTIVE_DE = 'Die Stationssitzung ist noch nicht bereit – bitte kurz warten.'
CHECK_INVALID_DE = 'Unbekannter Check.'
CAMERA_ACTION_DE = 'Kamera neu ausrichten (Lehrkraft)'

# ── Robot check (§7.2) ──────────────────────────────────────────────────────
ROBOT_OK_DE = 'Roboter-Check bestanden.'
NO_JOINTS_DE = 'Keine aktuellen Gelenkdaten'
NOT_VERTICAL_DE = (
    'Greifer steht schräg – bitte den Greifer senkrecht nach unten halten und '
    'erneut prüfen.')
NO_DOT_DE = 'Kein Tipp-Punkt in der Nähe – bitte die Spitze genau auf einen Punkt setzen.'
ROBOT_MOVED_DE = 'Roboter oder Matte verschoben – bitte Lehrkraft: Station neu einrichten.'

# ── Re-solve (§7.3) ─────────────────────────────────────────────────────────
RESOLVE_REFUSED_DE = 'Kamera zu stark verstellt – vollständige Einrichtung nötig.'
RESOLVE_OK_DE = 'Kamera neu ausgerichtet.'
RESOLVE_QUEUED_DE = 'Kamera wird neu ausgerichtet …'

# ── Commissioning (§6) ──────────────────────────────────────────────────────
JOB_PENDING_DE = 'Eine Einrichtung läuft noch.'
LAYOUT_NOT_RELEASED_DE = 'Diese Matte ist noch nicht freigegeben.'
COMMISSION_QUEUED_DE = 'Wird ausgeführt …'
COMMISSION_STALE_DE = 'Der Auftrag wurde übersprungen, weil sich der Zustand geändert hat.'
COMMISSION_DONE_DE = 'Erledigt.'
COMMISSION_STARTED_DE = 'Einrichtung gestartet.'
NO_COMMISSION_DE = 'Es läuft keine Einrichtung.'
WRONG_COMMISSION_DE = 'Diese Einrichtung ist nicht mehr aktiv.'
UNKNOWN_COMMAND_DE = 'Unbekannter Befehl.'
TOUCH_ALREADY_FREE_DE = 'Der Arm ist bereits frei.'
ABORT_MARKED_DE = 'Die Einrichtung wird gerade beendet.'
ROBOT_BUSY_DE = 'Bitte warten, der Roboter ist noch beschäftigt.'
RELEASE_ARM_DE = 'Bitte den Arm loslassen – er wird gerade wieder verriegelt.'
TOUCH_NOT_ACTIVE_DE = 'Bitte zuerst „Arm freigeben“ drücken.'
TOUCH_NOT_FREE_YET_DE = 'Der Arm wird gerade freigegeben – bitte kurz warten.'
COMMIT_IN_PROGRESS_DE = (
    'Die Einrichtung wird gerade gespeichert – Abbrechen ist nicht mehr möglich.')
COMMIT_NOT_READY_DE = 'Es fehlen noch Schritte der Einrichtung.'
COMMIT_DONE_DE = 'Einrichtung gespeichert.'
CANCELLED_DE = 'Einrichtung abgebrochen.'
WRONG_DOT_DE = 'Das ist nicht der gesuchte Punkt – bitte die Spitze auf Punkt {dot} setzen.'
DOT_ORDER_DE = 'Bitte die Punkte der Reihe nach antippen (als Nächstes Punkt {dot}).'
DOT_CAPTURED_DE = 'Punkt {dot} von 4 erfasst.'
TOUCH_OFF_FAILED_DE = 'Arm konnte nicht freigeschaltet werden – bitte erneut versuchen.'
TOUCH_FREE_DE = 'Der Arm ist frei – bitte die Spitze auf Punkt 1 führen.'
TOUCH_SOLVED_DE = 'Roboter registriert.'
REGISTRATION_OUT_OF_BOUNDS_DE = (
    'Roboterfuß sitzt nicht an seiner Markierung – bitte Sitz und Schrauben prüfen.')
COMMISSION_NO_FRAME_DE = 'Kein aktuelles 1080p-Bild der Stationskamera – bitte kurz warten.'
RELOCK_FAILED_DE = (
    'Arm konnte nicht wieder verriegelt werden — der Greifer ist noch frei '
    'beweglich. Bitte die Umgebung neu starten, bevor du fortfährst.')
RESTORE_INVALID_DE = 'Die vorherige Kalibrierung fehlt oder ist ungültig.'
RESTORE_OK_DE = 'Vorherige Kalibrierung wiederhergestellt.'
WRITE_FAILED_DE = 'Die Kalibrierung konnte nicht gespeichert werden.'

# Lens (§6.1)
LENS_VIEW_TOO_FEW_DE = 'Zu wenige Ecken der Linsentafel erkannt – bitte die Tafel ganz zeigen.'
LENS_VIEW_DUPLICATE_DE = 'Diese Ansicht gibt es schon – bitte die Tafel anders halten.'
LENS_VIEW_OK_DE = 'Ansicht {n} erfasst.'
LENS_NOT_ENOUGH_DE = (
    'Es fehlen noch Ansichten: mindestens 25, über das ganze Bild verteilt, '
    'verschieden groß und schräg.')
LENS_REJECTED_DE = 'Die Linsenkalibrierung ist nicht gut genug ({why}) – bitte neu erfassen.'
LENS_OK_DE = 'Linse kalibriert.'
LENS_MISSING_DE = 'Bitte zuerst die Linse kalibrieren.'

# Board (§6.2)
BOARD_FAILED_DE = (
    'Die Tafel auf der Matte wurde nicht sicher erkannt – bitte die Matte '
    'freiräumen und erneut versuchen.')
BOARD_CAMERA_LOW_DE = 'Die Kamera scheint nicht hoch genug über der Matte zu sein.'
BOARD_REFS_BAD_DE = (
    'Die Marker der Matte passen nicht zur Tafel – bitte Druck und Matte prüfen.')
BOARD_OK_DE = 'Kamera ausgerichtet.'
BOARD_MISSING_DE = 'Bitte zuerst die Kamera an der Tafel ausrichten.'

# World check (§6.4)
MARK_UNKNOWN_DE = 'Unbekannte Markierung.'
MARK_ORDER_DE = 'Bitte die Markierungen der Reihe nach prüfen (als Nächstes {mark}, {turn}°).'
MARK_NO_CUBE_DE = 'Kein Würfel auf der Markierung erkannt.'
MARK_CAPTURED_DE = 'Markierung {mark} ({turn}°) erfasst.'
WORLD_FAILED_DE = (
    'Die Würfel liegen nicht genau genug auf den Markierungen (Fehler '
    '{rms:.1f} mm, größter {mx:.1f} mm) – bitte Kalibrierung wiederholen.')
WORLD_OK_DE = 'Weltprüfung bestanden.'
WORLD_NOT_DONE_DE = 'Bitte zuerst alle 20 Würfelprüfungen erfassen.'
