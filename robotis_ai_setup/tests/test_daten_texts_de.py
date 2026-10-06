"""Daten 2.0: every German sentence of spec §J.6, verbatim, in the module its
tag names, with typographic quotes and no stale tab name.

[D] sentences live in ``physical_ai_server/daten/texts_de.py`` (the Daten
service, the sidecar, the edit and download workers); [R] sentences in
``physical_ai_server/data_processing/record_texts_de.py`` (the recorder, the
guarded upload, the node, the communicator). The page shows each one verbatim
(Rule §1). Both modules are stdlib-only and loaded by path here (this
directory's other loaders stub the ``physical_ai_server`` package).
"""

import ast
import importlib.util
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
TEXTS_D_PATH = PKG / 'daten' / 'texts_de.py'
TEXTS_R_PATH = PKG / 'data_processing' / 'record_texts_de.py'
SIGNAL_STATUS_PATH = PKG / 'signal_status.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = _load(TEXTS_D_PATH, '_daten_texts_de_under_test')
R = _load(TEXTS_R_PATH, '_record_texts_de_under_test')

# The exact texts of spec §J.6 (the owner-approved copy). Templates keep their
# English placeholders; the builders below fill them.
EXPECTED_D = {
    'BUSY_RECORD_DE': 'Dieser Datensatz wird gerade aufgenommen. Bearbeiten geht erst nach der Aufnahme.',
    'BUSY_UPLOAD_DE': 'Dieser Datensatz wird gerade hochgeladen. Warte, bis das Hochladen fertig ist.',
    'BUSY_DOWNLOAD_DE': 'Dieser Datensatz wird gerade von Hugging Face geladen. Warte, bis das fertig ist.',
    'BUSY_EDIT_DE': 'Es läuft gerade eine andere Bearbeitung. Warte, bis sie fertig ist.',
    'NOT_FOUND_DE': 'Diesen Datensatz gibt es auf dem Roboter nicht mehr. Lade die Liste neu.',
    'EXISTS_DE': 'Einen Datensatz mit diesem Namen gibt es hier schon. Wähle einen anderen Namen.',
    'STALE_DE': 'Der Datensatz hat sich inzwischen geändert. Lade ihn neu und markiere erneut.',
    'STALE_ACTION_DE': 'Der Datensatz auf dem Roboter hat sich inzwischen geändert. Es wurde nichts verändert. Schau dir den neuen Stand an und entscheide dann noch einmal.',
    'DISK_EDIT_DE': 'Für diese Bearbeitung ist zu wenig Speicher frei ({free} frei, etwa {need} nötig). Lösche zuerst alte Datensätze.',
    'NAMESPACE_EDIT_DE': 'Der neue Datensatz kann nur in deinem eigenen Hugging-Face-Konto angelegt werden.',
    'INVALID_EPISODES_DE': 'Die Auswahl der Episoden passt nicht zu diesem Datensatz. Lade die Seite neu und wähle noch einmal.',
    'INCOMPLETE_DE': 'Dieser Datensatz ist unvollständig und kann nicht bearbeitet werden. Du kannst ihn ganz löschen.',
    'IN_SESSION_DE': 'Dieser Datensatz wird gerade aufgenommen oder die Aufnahme wurde unterbrochen. Bearbeiten geht erst, wenn sie sauber beendet ist.',
    'OLD_FORMAT_DE': 'Dieser Datensatz hat ein älteres Format (v2.1) und kann hier nur ganz gelöscht werden.',
    'UNSUPPORTED_DE': 'Dieser Datensatz ist in einem Format gespeichert, das EduBotics nicht bearbeiten kann. Du kannst ihn ganz löschen.',
    'LAYOUT_DE': 'Die Episodenliste dieses Datensatzes passt nicht zu seinen Daten. Er kann hier nicht bearbeitet werden; du kannst ihn ganz löschen.',
    'UNALIGNED_DE': 'Die Videos dieses Datensatzes sind nicht an den Episodengrenzen geschnitten. Bearbeiten geht hier nicht; trainieren kannst du ihn trotzdem.',
    'INCOMPATIBLE_DE': 'Diese Datensätze lassen sich nicht zusammenführen: {names}.',
    'VERIFY_FAILED_DE': 'Das Ergebnis der Bearbeitung war nicht in Ordnung. Der Datensatz wurde nicht verändert.',
    'RUN_EDIT_FAILED_DE': 'Beim Bearbeiten des Datensatzes ist ein unerwarteter Fehler aufgetreten. Der Datensatz wurde nicht verändert.',
    'UNKNOWN_MODE_DE': 'Diese Bearbeitung kennt der Roboter nicht.',
    'UNAVAILABLE_DE': 'Hugging Face ist auf dem Roboter gerade nicht verfügbar. Versuche es gleich noch einmal.',
    'DOWNLOAD_DISK_DE': 'Für diesen Datensatz ist zu wenig Speicher frei ({free} frei, {need} nötig, und für Aufnahmen müssen 3 GB frei bleiben). Lösche zuerst alte Datensätze.',
    'DOWNLOAD_OLD_FORMAT_DE': 'Dieser Datensatz hat ein älteres Format und kann nicht geladen werden.',
    'DOWNLOAD_OTHER_ROBOT_DE': 'Dieser Datensatz stammt von einem anderen Roboter und kann hier nicht geöffnet werden.',
    'DOWNLOAD_UNSUPPORTED_DE': 'Dieser Datensatz ist in einem Format gespeichert, das EduBotics nicht öffnen kann. Auf dem Roboter wurde nichts verändert.',
    'DOWNLOAD_BROKEN_DE': 'Der geladene Datensatz ist unvollständig oder beschädigt. Auf dem Roboter wurde nichts verändert.',
    'DOWNLOAD_EXISTS_DE': 'Diesen Datensatz gibt es hier schon. Lade die neuere Version im Tab Daten.',
    'DOWNLOAD_NOT_FOUND_DE': 'Diesen Datensatz gibt es auf Hugging Face nicht (mehr), oder dein Konto darf ihn nicht lesen. Auf dem Roboter wurde nichts verändert.',
    'DOWNLOAD_TOKEN_CHANGED_DE': 'Das Laden wurde abgebrochen, weil sich ein anderes Konto angemeldet hat. Auf dem Roboter wurde nichts verändert.',
    'DOWNLOAD_FAILED_DE': 'Beim Laden ist ein unerwarteter Fehler aufgetreten. Auf dem Roboter wurde nichts verändert.',
    'KEEP_BOTH_DISK_DE': 'Zum Zusammenführen ist zu wenig Speicher frei ({free} frei, etwa {need} nötig). Lösche zuerst alte Datensätze.',
}
EXPECTED_R = {
    'HUB_CHANGED_SINCE_CHECK_DE': 'Auf Hugging Face hat sich der Datensatz inzwischen geändert. Es wurde nichts hochgeladen. Öffne den Tab Daten und entscheide, welche Version du behalten willst – „Beide behalten“ verliert nichts.',
    'UPLOAD_IN_SESSION_DE': 'Nicht hochgeladen: Die Aufnahme dieses Datensatzes wurde unterbrochen und nicht sauber beendet; hochgeladen würde er die Version auf Hugging Face beschädigen. Lösche ihn im Tab Daten oder lade dort die Online-Version.',
    'UPLOAD_BROKEN_DE': 'Nicht hochgeladen: Der Datensatz auf dem Roboter ist unvollständig oder beschädigt. Die Version auf Hugging Face bleibt, wie sie ist. Lösche ihn im Tab Daten oder lade dort die Online-Version.',
    'UPLOAD_UNCONFIRMED_DE': 'Hochgeladen, aber Hugging Face hat es noch nicht bestätigt. Im Tab Daten siehst du, ob noch etwas zu tun ist.',
    'UPLOAD_HUB_DIFFERS_DE': 'Auf Hugging Face gibt es diesen Datensatz schon in einer anderen Version. Es wurde nichts überschrieben. Öffne den Tab Daten, vergleiche beide Versionen und entscheide dort.',
    'SYNC_CONFLICT_DE': 'Dieser Datensatz wurde hier geändert, und auf Hugging Face gibt es inzwischen eine neuere Version. Entscheide im Tab Daten, welche du behalten willst, und starte dann die Aufnahme neu.',
    'SYNC_UNKNOWN_DE': 'Der Datensatz hier und der auf Hugging Face sind verschieden, und EduBotics kann nicht erkennen, welcher neuer ist. Entscheide im Tab Daten, welche Version du behalten willst, und starte dann die Aufnahme neu.',
    'SYNC_HUB_UNUSABLE_DE': 'Die Version auf Hugging Face kann EduBotics nicht öffnen (älteres Format, anderer Roboter oder unbekanntes Format). Die Aufnahme wurde nicht gestartet. Wähle einen anderen Aufgabennamen.',
    'SYNC_DOWNLOAD_FAILED_DE': 'Die neuere Version von Hugging Face konnte nicht geladen werden. Die Aufnahme wurde nicht gestartet, damit nichts überschrieben wird. Versuche es gleich noch einmal oder schalte unter „Erweitert“ das Hochladen aus.',
    'SYNC_DISK_DE': 'Die neuere Version von Hugging Face braucht {need}, frei sind {free}, und für Aufnahmen müssen 3 GB frei bleiben. Die Aufnahme wurde nicht gestartet. Lösche zuerst alte Datensätze im Tab Daten.',
    'OFFLINE_START_DE': 'Hugging Face war beim Start nicht erreichbar. Die Aufnahme läuft trotzdem; beim Hochladen am Ende prüft EduBotics, dass auf Hugging Face nichts überschrieben wird.',
    'DATASET_BUSY_START_DE': 'Dieser Datensatz wird gerade im Tab Daten bearbeitet oder geladen. Starte die Aufnahme, wenn das fertig ist.',
    'AUTO_UPLOAD_NO_WORKER_DE': 'Automatisches Hochladen fehlgeschlagen: Der Hugging-Face-Dienst des Roboters konnte nicht starten. Lade den Datensatz später im Tab Daten hoch.',
    'AUTO_UPLOAD_BUSY_DE': 'Automatisches Hochladen übersprungen: Gerade läuft ein anderer Hugging-Face-Vorgang. Wenn er beendet ist, lade den Datensatz im Tab Daten hoch.',
    'AUTO_UPLOAD_REFUSED_DE': 'Automatisches Hochladen fehlgeschlagen: Der Hugging-Face-Dienst des Roboters hat die Anfrage abgelehnt. Lade den Datensatz später im Tab Daten hoch.',
    'DATASET_INFO_FAILED_DE': 'Datensatz-Informationen konnten nicht gelesen werden.',
    'BROWSE_FAILED_DE': 'Der Ordner konnte nicht gelesen werden.',
    'AUTO_UPLOAD_FAILED_DE': 'Automatisches Hochladen fehlgeschlagen. Lade den Datensatz später im Tab Daten hoch.',
}

# Builders and the constants they fill.
BUILDERS_D = {'disk_edit_de': 'DISK_EDIT_DE', 'download_disk_de': 'DOWNLOAD_DISK_DE',
              'keep_both_disk_de': 'KEEP_BOTH_DISK_DE'}
BUILDERS_R = {'sync_disk_de': 'SYNC_DISK_DE'}


def _sentences(module):
    return {n: v for n, v in vars(module).items() if n.isupper() and isinstance(v, str)}


class TheTextsAreTheApprovedCopy(unittest.TestCase):

    def test_every_D_sentence_verbatim(self):
        for name, text in EXPECTED_D.items():
            self.assertEqual(getattr(D, name, None), text, name)

    def test_every_R_sentence_verbatim(self):
        for name, text in EXPECTED_R.items():
            self.assertEqual(getattr(R, name, None), text, name)

    def test_each_sentence_lives_in_the_module_its_tag_names(self):
        for name in EXPECTED_D:
            self.assertFalse(hasattr(R, name), f'{name} is [D], not in record_texts_de')
        for name in EXPECTED_R:
            self.assertFalse(hasattr(D, name), f'{name} is [R], not in daten/texts_de')

    def test_the_builders_fill_every_placeholder_with_german_numbers(self):
        for module, builders in ((D, BUILDERS_D), (R, BUILDERS_R)):
            for builder, constant in builders.items():
                text = getattr(module, builder)(2_500_000_000, 12_345_678_901)
                self.assertNotIn('{', text, builder)
                self.assertNotIn('}', text, builder)
                self.assertIn('2,5 GB', text, builder)
                self.assertIn('12,3 GB', text, builder)
                template = getattr(module, constant)
                self.assertEqual(text, template.format(free='2,5 GB', need='12,3 GB'), builder)

    def test_incompatible_names_the_german_check_labels(self):
        self.assertEqual(D.incompatible_de(['stats']),
                         'Diese Datensätze lassen sich nicht zusammenführen: Statistiken.')
        self.assertEqual(D.incompatible_de(['fps', 'cameras']),
                         'Diese Datensätze lassen sich nicht zusammenführen: Bildrate, Kameras.')
        contract = _load(PKG / 'daten' / 'contract.py', '_daten_contract_for_texts')
        self.assertEqual(list(D.MERGE_CHECK_LABELS_DE), list(contract.MERGE_CHECKS))

    def test_the_gb_format_is_signal_status_s(self):
        signal_status = _load(SIGNAL_STATUS_PATH, '_signal_status_for_texts')
        for n in (0, 1, 999_999_999, 1_000_000_000, 3_049_999_999, 12_345_678_901):
            self.assertEqual(D.format_gb_de(n), signal_status.format_gb_de(n), n)
            self.assertEqual(R._format_gb_de(n), signal_status.format_gb_de(n), n)


class QuotesAndTabNames(unittest.TestCase):

    def _all(self):
        out = {}
        for module, tag in ((D, 'D'), (R, 'R')):
            for name, text in _sentences(module).items():
                out[f'[{tag}] {name}'] = text
        return out

    def test_quotes_are_typographic_never_straight(self):
        for name, text in self._all().items():
            self.assertNotIn('"', text, name)
            self.assertEqual(text.count('„'), text.count('“'), name)

    def test_no_sentence_names_the_old_tab(self):
        for name, text in self._all().items():
            self.assertNotIn('Datensatz bearbeiten', text, name)

    def test_the_D_module_is_stdlib_only_and_imports_no_package(self):
        tree = ast.parse(TEXTS_D_PATH.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ''])
                self.assertTrue(all(n.split('.')[0] == '__future__' for n in names), names)

    def test_every_R_sentence_that_points_somewhere_names_the_daten_tab(self):
        """§G11 POINTS_TO_DATEN_TAB = /\\b(im|den) Tab Daten\\b/: the finish card then
        adds no second „später hochladen" line."""
        points = re.compile(r'\b(im|den) Tab Daten\b')
        for name in ('HUB_CHANGED_SINCE_CHECK_DE', 'UPLOAD_HUB_DIFFERS_DE', 'UPLOAD_IN_SESSION_DE',
                     'UPLOAD_BROKEN_DE', 'AUTO_UPLOAD_NO_WORKER_DE', 'AUTO_UPLOAD_BUSY_DE',
                     'AUTO_UPLOAD_REFUSED_DE', 'AUTO_UPLOAD_FAILED_DE'):
            self.assertRegex(getattr(R, name), points, name)


if __name__ == '__main__':
    unittest.main()
