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

"""Every German sentence of the Daten service, the sidecar and the edit and
download workers (spec §J.6, the constants tagged [D]).

The recorder's and the upload's sentences live in
``data_processing/record_texts_de.py`` (tagged [R]). The page shows each one
verbatim (Rule §1); quotes are typographic („…“), never straight
(``robotis_ai_setup/tests/test_daten_texts_de.py``). Stdlib only and free of
package imports, so a worker or a deps-free test can load it by path.
"""

from __future__ import annotations

# ── busy: one sentence per kind (§D3) ─────────────────────────────────────────

BUSY_RECORD_DE = 'Dieser Datensatz wird gerade aufgenommen. Bearbeiten geht erst nach der Aufnahme.'
# T1-3: an upload (the Daten upload, the old page's /huggingface/control upload)
# refused because the dataset records says „Hochladen“, not „Bearbeiten“.
BUSY_RECORD_UPLOAD_DE = 'Dieser Datensatz wird gerade aufgenommen. Hochladen geht erst nach der Aufnahme.'
BUSY_UPLOAD_DE = 'Dieser Datensatz wird gerade hochgeladen. Warte, bis das Hochladen fertig ist.'
BUSY_DOWNLOAD_DE = ('Dieser Datensatz wird gerade von Hugging Face geladen. Warte, bis das '
                    'fertig ist.')
BUSY_EDIT_DE = 'Es läuft gerade eine andere Bearbeitung. Warte, bis sie fertig ist.'

# ── refusals of /daten/command (§J.3) ─────────────────────────────────────────

NOT_FOUND_DE = 'Diesen Datensatz gibt es auf dem Roboter nicht mehr. Lade die Liste neu.'
EXISTS_DE = 'Einen Datensatz mit diesem Namen gibt es hier schon. Wähle einen anderen Namen.'
STALE_DE = 'Der Datensatz hat sich inzwischen geändert. Lade ihn neu und markiere erneut.'
STALE_ACTION_DE = ('Der Datensatz auf dem Roboter hat sich inzwischen geändert. Es wurde nichts '
                   'verändert. Schau dir den neuen Stand an und entscheide dann noch einmal.')
NAMESPACE_EDIT_DE = 'Der neue Datensatz kann nur in deinem eigenen Hugging-Face-Konto angelegt werden.'
INVALID_EPISODES_DE = ('Die Auswahl der Episoden passt nicht zu diesem Datensatz. Lade die Seite neu '
                       'und wähle noch einmal.')
INCOMPLETE_DE = ('Dieser Datensatz ist unvollständig und kann nicht bearbeitet werden. Du kannst '
                 'ihn ganz löschen.')
IN_SESSION_DE = ('Dieser Datensatz wird gerade aufgenommen oder die Aufnahme wurde unterbrochen. '
                 'Bearbeiten geht erst, wenn sie sauber beendet ist.')
OLD_FORMAT_DE = 'Dieser Datensatz hat ein älteres Format (v2.1) und kann hier nur ganz gelöscht werden.'
UNSUPPORTED_DE = ('Dieser Datensatz ist in einem Format gespeichert, das EduBotics nicht bearbeiten '
                  'kann. Du kannst ihn ganz löschen.')
UNAVAILABLE_DE = ('Hugging Face ist auf dem Roboter gerade nicht verfügbar. Versuche es gleich noch '
                  'einmal.')

# ── the edit engine's failures (§D1, SurgeryError codes) ──────────────────────

LAYOUT_DE = ('Die Episodenliste dieses Datensatzes passt nicht zu seinen Daten. Er kann hier nicht '
             'bearbeitet werden; du kannst ihn ganz löschen.')
UNALIGNED_DE = ('Die Videos dieses Datensatzes sind nicht an den Episodengrenzen geschnitten. '
                'Bearbeiten geht hier nicht; trainieren kannst du ihn trotzdem.')
INCOMPATIBLE_DE = 'Diese Datensätze lassen sich nicht zusammenführen: {names}.'
VERIFY_FAILED_DE = ('Das Ergebnis der Bearbeitung war nicht in Ordnung. Der Datensatz wurde nicht '
                    'verändert.')
RUN_EDIT_FAILED_DE = ('Beim Bearbeiten des Datensatzes ist ein unerwarteter Fehler aufgetreten. Der '
                      'Datensatz wurde nicht verändert.')
UNKNOWN_MODE_DE = 'Diese Bearbeitung kennt der Roboter nicht.'

# The German label of each compatibility check (contract.MERGE_CHECKS order); the
# page's merge panel names the same checks.
MERGE_CHECK_LABELS_DE = {
    'version': 'Datensatz-Format',
    'robot': 'Roboter',
    'fps': 'Bildrate',
    'cameras': 'Kameras',
    'joints': 'Gelenke',
    'video': 'Videoformat',
    'stats': 'Statistiken',
}

# ── disk (§D3, §E3, §E10) ─────────────────────────────────────────────────────

DISK_EDIT_DE = ('Für diese Bearbeitung ist zu wenig Speicher frei ({free} frei, etwa {need} nötig). '
                'Lösche zuerst alte Datensätze.')
DOWNLOAD_DISK_DE = ('Für diesen Datensatz ist zu wenig Speicher frei ({free} frei, {need} nötig, und '
                    'für Aufnahmen müssen 3 GB frei bleiben). Lösche zuerst alte Datensätze.')
KEEP_BOTH_DISK_DE = ('Zum Zusammenführen ist zu wenig Speicher frei ({free} frei, etwa {need} '
                     'nötig). Lösche zuerst alte Datensätze.')

# ── downloads (§E3) ───────────────────────────────────────────────────────────

DOWNLOAD_OLD_FORMAT_DE = 'Dieser Datensatz hat ein älteres Format und kann nicht geladen werden.'
DOWNLOAD_OTHER_ROBOT_DE = ('Dieser Datensatz stammt von einem anderen Roboter und kann hier nicht '
                           'geöffnet werden.')
DOWNLOAD_UNSUPPORTED_DE = ('Dieser Datensatz ist in einem Format gespeichert, das EduBotics nicht '
                           'öffnen kann. Auf dem Roboter wurde nichts verändert.')
DOWNLOAD_BROKEN_DE = ('Der geladene Datensatz ist unvollständig oder beschädigt. Auf dem Roboter '
                      'wurde nichts verändert.')
DOWNLOAD_EXISTS_DE = 'Diesen Datensatz gibt es hier schon. Lade die neuere Version im Tab Daten.'
DOWNLOAD_NOT_FOUND_DE = ('Diesen Datensatz gibt es auf Hugging Face nicht (mehr), oder dein Konto darf '
                         'ihn nicht lesen. Auf dem Roboter wurde nichts verändert.')
DOWNLOAD_TOKEN_CHANGED_DE = ('Das Laden wurde abgebrochen, weil sich ein anderes Konto angemeldet hat. '
                             'Auf dem Roboter wurde nichts verändert.')
DOWNLOAD_FAILED_DE = ('Beim Laden ist ein unerwarteter Fehler aufgetreten. Auf dem Roboter wurde '
                      'nichts verändert.')


def format_gb_de(n) -> str:
    """Decimal gigabytes, one decimal, German comma — the same rule as
    ``signal_status.format_gb_de`` (a test pins the two equal)."""
    return f'{int(n) / 1e9:.1f}'.replace('.', ',') + ' GB'


def disk_edit_de(free, need) -> str:
    return DISK_EDIT_DE.format(free=format_gb_de(free), need=format_gb_de(need))


def download_disk_de(free, need) -> str:
    return DOWNLOAD_DISK_DE.format(free=format_gb_de(free), need=format_gb_de(need))


def keep_both_disk_de(free, need) -> str:
    return KEEP_BOTH_DISK_DE.format(free=format_gb_de(free), need=format_gb_de(need))


def incompatible_de(names) -> str:
    """``names``: the failing check ids (``MERGE_CHECKS``) or German labels."""
    labels = [MERGE_CHECK_LABELS_DE.get(str(n), str(n)) for n in (names or [])]
    return INCOMPATIBLE_DE.format(names=', '.join(labels))
