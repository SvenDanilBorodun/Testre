"""Scene-camera provenance in Daten (D58): the edits carry the stamp, one
contract in three places, the German words.

``data_editor_v3._build`` copies, after ``assemble`` and before
``verify_or_raise``, ONE source's stamp file byte for byte (a later ``v``
survives a delete or a split) and several sources' common stamp (a merge of
stampless datasets writes none); several sources that do not share one profile
never produce an output (the engine refuses them first; the copy fails closed).
Driven through the fake engine of ``test_data_editor_v3_gate.py`` (deps-free).

``MERGE_CHECKS`` ends in ``scene_camera`` in ``v3_surgery.py``,
``daten/contract.py`` and the page's ``datenContract.js``; its German label is
„Szenen-Kamera“. Every new sentence of the recorder and the merge panel passes
the German lint's rules (``.github/scripts/german_detail_lint.py``: no
transliteration, never English without a German marker).
"""

import ast
import importlib.util
import json
import pathlib
import re
import sys
import unittest

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

import test_data_editor_v3_gate as G

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
EDIT = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'features' / 'editDataset'
LINT = REPO_ROOT / '.github' / 'scripts' / 'german_detail_lint.py'
STAMP = 'meta/edubotics_scene_camera.json'
TOWER = b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":"3efca10f3a05","platform":"windows"}'
TOWER_PI = b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":"8b7f72cb250c","platform":"pi"}'
LATER = b'{"v": 2, "scene_camera_profile": "tower_v2", "lens": "wide"}\n'
EIGHT = ('version', 'robot', 'fps', 'cameras', 'joints', 'video', 'stats', 'scene_camera')


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assigned(path, name):
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not assigned in {path.name}')


class _Daten(G._Base):

    def tree(self, name, n, stamp=None):
        root = G._make_v3_tree(self.tmpdir / 'student' / name, n)
        if stamp is not None:
            (root / STAMP).write_bytes(stamp)
        return root

    def stamp_of(self, root):
        p = root / STAMP
        return p.read_bytes() if p.exists() else None

    def assert_no_temp_file(self):
        self.assertEqual(sorted(self.tmpdir.rglob('.edubotics_scene_camera.json.tmp')), [])


class OneSourceKeepsItsStampBytes(_Daten):

    def test_a_delete_keeps_a_later_stamp_byte_for_byte(self):
        src = self.tree('ds', 4, LATER)
        self.assertEqual(self.v3.delete_episodes_v3(str(src), [1]), 3)
        self.assertEqual(G._ids(src), ['ds:0', 'ds:2', 'ds:3'], 'the delete happened')
        self.assertEqual(self.stamp_of(src), LATER)
        self.assert_no_temp_file()

    def test_a_split_keeps_it_in_both_halves(self):
        src = self.tree('ds', 4, TOWER)
        new = self.tmpdir / 'student' / 'ds_teil'
        self.assertEqual(self.v3.split_episodes_v3(str(src), [0, 1], str(new)), (2, 2))
        self.assertEqual((self.stamp_of(src), self.stamp_of(new)), (TOWER, TOWER))
        self.assert_no_temp_file()

    def test_a_stampless_delete_stays_stampless(self):
        src = self.tree('ds', 3)
        self.v3.delete_episodes_v3(str(src), [0])
        self.assertIsNone(self.stamp_of(src))
        self.assert_no_temp_file()

    def test_the_stamp_is_in_the_output_before_verify(self):
        src = self.tree('ds', 3, TOWER)
        seen = []
        real = G._fake_verify_or_raise

        def verify(out, parts):
            seen.append((pathlib.Path(out) / STAMP).read_bytes())
            real(out, parts)
        sys.modules[G.ENGINE_NAME].verify_or_raise = verify
        self.v3.delete_episodes_v3(str(src), [2])
        self.assertEqual(seen, [TOWER])


class SeveralSourcesCarryTheirCommonStamp(_Daten):

    def test_a_merge_of_stampless_datasets_writes_none(self):
        a, b = self.tree('a', 2), self.tree('b', 2)
        out = self.tmpdir / 'student' / 'ab'
        self.assertEqual(self.v3.merge_datasets_v3([str(a), str(b)], str(out)), 4)
        self.assertIsNone(self.stamp_of(out))
        self.assert_no_temp_file()

    def test_a_merge_of_tower_datasets_is_a_tower_dataset(self):
        a, b, c = self.tree('a', 2, TOWER), self.tree('b', 2, TOWER), self.tree('c', 2, TOWER_PI)
        out = self.tmpdir / 'student' / 'ab'
        self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertEqual(self.stamp_of(out), TOWER)
        out2 = self.tmpdir / 'student' / 'ac'
        self.v3.merge_datasets_v3([str(a), str(c)], str(out2))
        self.assertEqual(json.loads(self.stamp_of(out2)),
                         {'v': 1, 'scene_camera_profile': 'tower_v1', 'profile_id': None, 'platform': None})

    def test_two_scene_cameras_never_make_one_output(self):
        # the real engine refuses them first (check_compatible); this fake engine
        # does not, so the copy itself must fail closed
        a, b = self.tree('a', 2, TOWER), self.tree('b', 2)
        out = self.tmpdir / 'student' / 'ab'
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertEqual(str(ctx.exception), G.T.RUN_EDIT_FAILED_DE)
        self.assertFalse(out.exists())
        self.assertFalse((self.tmpdir / 'student' / 'ab.tmp_edit').exists())


class TheContractInThreePlaces(unittest.TestCase):

    def test_merge_checks_end_in_scene_camera_everywhere(self):
        contract = _assigned(PKG / 'daten' / 'contract.py', 'MERGE_CHECKS')
        engine = _assigned(PKG / 'data_processing' / 'v3_surgery.py', 'MERGE_CHECKS')
        js = re.search(r"^export const MERGE_CHECKS = Object\.freeze\(\[(.*)\]\);$",
                       (EDIT / 'datenContract.js').read_text(encoding='utf-8'), re.M)
        page = tuple(re.findall(r"'([^']*)'", js.group(1)))
        self.assertEqual((contract, engine, page), (EIGHT, EIGHT, EIGHT))
        self.assertEqual(G.MERGE_CHECKS, EIGHT, "the gate test's fake engine copy")

    def test_the_label(self):
        texts = _load(PKG / 'daten' / 'texts_de.py', '_scene_daten_texts')
        self.assertEqual(texts.MERGE_CHECK_LABELS_DE['scene_camera'], 'Szenen-Kamera')
        self.assertEqual(texts.incompatible_de(['scene_camera']),
                         'Diese Datensätze lassen sich nicht zusammenführen: Szenen-Kamera.')

    def test_the_library_names_every_entrys_profile(self):
        src = (PKG / 'daten' / 'library.py').read_text(encoding='utf-8')
        self.assertIn("'scene_camera_profile': scene_provenance.dataset_profile(path)", src)


class TheGermanWords(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.lint = _load(LINT, '_scene_german_lint')
        cls.R = _load(PKG / 'data_processing' / 'record_texts_de.py', '_scene_record_texts')
        copy = (EDIT / 'datenCopy.js').read_text(encoding='utf-8')
        cls.page = {k: re.search(rf"^\s+{k}: '([^']*)',$", copy, re.M).group(1)
                    for k in ('sceneOk', 'sceneBad', 'sceneUnknown')}

    def sentences(self):
        R = self.R
        return {
            'resume legacy->tower': R.resume_scene_camera_de('Würfel', 'legacy', 'tower_v1'),
            'resume tower->legacy': R.resume_scene_camera_de('Würfel', 'tower_v1', 'legacy'),
            'stamp failed': R.scene_stamp_failed_de('Würfel'),
            'sceneBad': self.page['sceneBad'],
            'sceneUnknown': self.page['sceneUnknown'],
        }

    def test_the_owner_sentences_verbatim(self):
        self.assertEqual(self.R.SCENE_CAMERA_NAMES_DE,
                         {'legacy': 'der bisherigen Szenen-Kamera', 'tower_v1': 'der Turm-Kamera'})
        self.assertEqual(self.R.RESUME_SCENE_CAMERA_DE,
                         'Der Datensatz „{name}“ wurde mit {then} aufgenommen, dieser Roboter nimmt jetzt '
                         'mit {now} auf. Die beiden Ansichten dürfen nicht in einem Datensatz gemischt werden. '
                         'Wähle einen neuen Aufgabennamen.')
        self.assertEqual(self.R.SCENE_STAMP_FAILED_DE,
                         'Der Datensatz „{name}“ konnte nicht als Aufnahme der Turm-Kamera gekennzeichnet '
                         'werden – es wurde nichts aufgenommen. Bitte einen neuen Aufgabennamen wählen und '
                         'den freien Speicherplatz prüfen.')
        self.assertEqual(self.R.scene_stamp_failed_de('X'), self.R.SCENE_STAMP_FAILED_DE.format(name='X'))
        self.assertEqual(self.page, {
            'sceneOk': 'Gleiche Szenen-Kamera: Turm-Kamera',
            'sceneBad': 'Mit verschiedenen Szenen-Kameras aufgenommen (Turm-Kamera und bisherige '
                        'Szenen-Kamera) – lässt sich nicht zusammenführen',
            'sceneUnknown': 'Bei einem Datensatz ist nicht lesbar, mit welcher Szenen-Kamera er '
                            'aufgenommen wurde – lässt sich nicht zusammenführen',
        })

    def test_every_new_sentence_is_german_by_the_lints_rules(self):
        words = {**self.sentences(), 'sceneOk': self.page['sceneOk'], 'label': 'Szenen-Kamera',
                 **{f'name {k}': v for k, v in self.R.SCENE_CAMERA_NAMES_DE.items()}}
        for name, text in words.items():
            with self.subTest(name=name):
                self.assertIsNone(self.lint.TRANSLITERATIONS.search(text), text)
                german = bool(self.lint.GERMAN_CHARS.search(text) or self.lint.GERMAN_WORDS.search(text))
                self.assertFalse(not german and self.lint.ENGLISH_WORDS.search(text), text)
        for name, text in self.sentences().items():
            with self.subTest(sentence=name):
                self.assertTrue(self.lint.GERMAN_CHARS.search(text) or self.lint.GERMAN_WORDS.search(text),
                                f'{name} carries no German marker')

    def test_the_lint_has_teeth(self):
        self.assertIsNotNone(self.lint.TRANSLITERATIONS.search('Waehle einen neuen Aufgabennamen'))


if __name__ == '__main__':
    unittest.main()
