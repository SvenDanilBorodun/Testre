#!/usr/bin/env python3
"""Scene-camera provenance at recording start (D58): the resume refusal, the
stamp of a created dataset, a failed stamp write, and the node's provider.

``DataManager.check_lerobot_dataset`` judges the dataset's scene-camera profile
(``scene_provenance.dataset_profile``) against the robot's live one (the
provider the node injects) AFTER ``_check_dataset_exists`` and BEFORE LeRobot
opens anything: two known, different profiles are refused in German, nothing is
opened (no ``LeRobotDatasetWrapper.__init__``, no ``resume`` — LeRobot 0.6.1's
``resume`` runs no ``__init__`` —, no ``LeRobotDatasetMetadata``) and no file
changes. An unknown profile on either side refuses nothing. A tower session
stamps the dataset it CREATES before ``_write_create_record``; a failed stamp
write is a dataset-init failure that leaves the empty folder, and a second
Start re-creates it through ``_check_dataset_exists``' incomplete-copy path. A
legacy rig creates exactly today's files.

Deps-free (A18): ``data_manager`` is loaded by path with the record-FSM test's
stubs for LeRobot, ROS and the interfaces, so CI's python-tests job runs this.
The node's injection is exec'd from the AST onto a stub node (rclpy is not
importable here).
"""

import ast
import hashlib
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

import test_data_manager_record_fsm as F

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
NODE_PATH = PKG / 'physical_ai_server.py'
PROVENANCE_PATH = PKG / 'scene_provenance.py'
REPO = 'maxmuster/omx_f_Wuerfel-in-die-Schale'
NAME = 'Würfel in die Schale'
STAMP = 'meta/edubotics_scene_camera.json'
TOWER_STAMP = b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":"3efca10f3a05","platform":"windows"}'
MOD = None


def setUpModule():
    global MOD
    MOD = F._load_data_manager_module()          # a fresh copy with the real `time`


def texts():
    return MOD.record_texts_de


def tower():
    return {'profile': 'tower_v1', 'profile_id': '3efca10f3a05', 'platform': 'windows'}


def legacy():
    return {'profile': 'legacy', 'profile_id': None, 'platform': None}


def _raises():
    raise RuntimeError('no statement yet')


_MISSING = object()


# ── LeRobot, counted ─────────────────────────────────────────────────────────

class Calls:
    wrapper_init = 0
    wrapper_resume = 0
    dataset_resume = 0
    metadata_init = 0

    @classmethod
    def reset(cls):
        cls.wrapper_init = cls.wrapper_resume = cls.dataset_resume = cls.metadata_init = 0

    @classmethod
    def total(cls):
        return cls.wrapper_init + cls.wrapper_resume + cls.dataset_resume + cls.metadata_init


class CountingMetadata:
    def __init__(self, *a, **k):
        Calls.metadata_init += 1


class CountingDataset:
    """Stands in for LeRobotDataset: opening it builds its metadata (as LeRobot does)."""

    def __init__(self, repo_id=None, root=None, **k):
        self.meta = CountingMetadata(repo_id, root)
        self.fps = 30
        self.robot_type = None

    @classmethod
    def resume(cls, *a, **k):
        Calls.dataset_resume += 1
        obj = cls.__new__(cls)               # 0.6.1: no __init__ on resume
        obj.fps, obj.robot_type = 30, None
        return obj

    def set_robot_type(self, robot_type):
        self.robot_type = robot_type

    def start_image_writer(self, **kw):
        pass


class CountingWrapper(CountingDataset):

    def __init__(self, repo_id=None, root=None, **k):
        Calls.wrapper_init += 1
        super().__init__(repo_id, root)

    @classmethod
    def resume(cls, *a, **k):
        Calls.wrapper_resume += 1
        return super().resume(*a, **k)


# ── fixtures ─────────────────────────────────────────────────────────────────

def write_complete(root):
    """A complete local copy: meta/, data/ and videos/ (a resume, not a re-create)."""
    files = {
        'meta/info.json': b'{"codebase_version": "v3.0", "fps": 30, "total_episodes": 2}',
        'meta/episodes/chunk-000/file-000.parquet': b'episodes',
        'data/chunk-000/file-000.parquet': b'data',
        'videos/observation.images.scene/chunk-000/file-000.mp4': b'\x00video',
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def snapshot(base):
    out = {}
    for p in sorted(pathlib.Path(base).rglob('*')):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(base).as_posix()] = (st.st_size, st.st_mtime_ns,
                                                   hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def lerobot_like_create(root, created):
    """What LeRobot's create leaves before the first episode: meta/ only (it
    refuses a root that exists, which is why the re-create path removes it)."""
    def create(repo, images, joints):
        (root / 'meta').mkdir(parents=True, exist_ok=False)
        (root / 'meta' / 'info.json').write_bytes(b'{"codebase_version": "v3.0", "fps": 30, "total_episodes": 0}')
        created.append(repo)
        return F._OpenedDataset()
    return create


class ProvenanceCase(BoundedTestCase):

    def setUp(self):
        Calls.reset()
        saved = (MOD.LeRobotDatasetWrapper, MOD._resume_compatibility_check)
        self.addCleanup(self._restore, saved)
        MOD.LeRobotDatasetWrapper = CountingWrapper
        MOD._resume_compatibility_check = lambda *a, **k: None
        lerobot_mods = {
            'lerobot.datasets.lerobot_dataset': types.SimpleNamespace(LeRobotDataset=CountingDataset),
            'lerobot.datasets.dataset_metadata': types.SimpleNamespace(
                LeRobotDatasetMetadata=CountingMetadata, CODEBASE_VERSION='v3.0'),
        }
        patcher = mock.patch.dict(sys.modules, lerobot_mods)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = pathlib.Path(tempfile.mkdtemp(prefix='dm_scene_')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.path = self.base / REPO
        self.created = []

    @staticmethod
    def _restore(saved):
        MOD.LeRobotDatasetWrapper, MOD._resume_compatibility_check = saved

    def dm(self, provider=_MISSING):
        """A DataManager built with __new__ like the contract tests: exactly the
        attributes the Start reads; ``_scene_camera_profile`` only when given."""
        dm = MOD.DataManager.__new__(MOD.DataManager)
        dm._task_info = F._TaskInfo(push=False)
        dm._robot_type = 'omx_f'
        dm._save_repo_name = REPO
        dm._save_path = self.path
        dm._lerobot_dataset = None
        dm._last_warning_message = ''
        dm._lease_logged = True                 # no Daten here: its one log line is not the point
        dm._create_dataset = lerobot_like_create(self.path, self.created)
        if provider is not _MISSING:
            dm._scene_camera_profile = provider
        return dm

    def check(self, dm):
        return dm.check_lerobot_dataset({'scene': F._Img()}, ['j1'])


# ── the resume rule ──────────────────────────────────────────────────────────

class TheResumeRule(ProvenanceCase):

    def assert_refused(self, provider, then, now):
        before = snapshot(self.base)
        dm = self.dm(provider)
        self.assertFalse(self.check(dm))
        self.assertEqual(dm._last_warning_message, texts().resume_scene_camera_de(NAME, then, now))
        self.assertEqual((Calls.wrapper_init, Calls.wrapper_resume, Calls.dataset_resume, Calls.metadata_init),
                         (0, 0, 0, 0), 'LeRobot opened the dataset before the refusal')
        self.assertIsNone(dm._lerobot_dataset)
        self.assertEqual(snapshot(self.base), before, 'a refused resume changed a file')

    def assert_resumed(self, provider):
        dm = self.dm(provider)
        self.assertTrue(self.check(dm))
        self.assertEqual(Calls.wrapper_init, 1, 'resumed as today')
        self.assertEqual(Calls.metadata_init, 1)
        self.assertIsInstance(dm._lerobot_dataset, CountingWrapper)
        self.assertEqual(dm._last_warning_message, '')
        self.assertEqual(self.created, [])

    def test_a_stampless_dataset_is_not_continued_with_the_tower(self):
        write_complete(self.path)
        self.assert_refused(tower, 'legacy', 'tower_v1')

    def test_a_tower_dataset_is_not_continued_with_the_old_camera(self):
        write_complete(self.path)
        (self.path / STAMP).write_bytes(TOWER_STAMP)
        self.assert_refused(legacy, 'tower_v1', 'legacy')

    def test_the_sentences(self):
        self.assertEqual(
            texts().resume_scene_camera_de(NAME, 'legacy', 'tower_v1'),
            'Der Datensatz „Würfel in die Schale“ wurde mit der bisherigen Szenen-Kamera aufgenommen, '
            'dieser Roboter nimmt jetzt mit der Turm-Kamera auf. Die beiden Ansichten dürfen nicht in einem '
            'Datensatz gemischt werden. Wähle einen neuen Aufgabennamen.')
        self.assertEqual(
            texts().resume_scene_camera_de(NAME, 'tower_v1', 'legacy'),
            'Der Datensatz „Würfel in die Schale“ wurde mit der Turm-Kamera aufgenommen, dieser Roboter '
            'nimmt jetzt mit der bisherigen Szenen-Kamera auf. Die beiden Ansichten dürfen nicht in einem '
            'Datensatz gemischt werden. Wähle einen neuen Aufgabennamen.')

    def test_equal_profiles_resume_as_today(self):
        write_complete(self.path)
        self.assert_resumed(legacy)
        Calls.reset()
        (self.path / STAMP).write_bytes(TOWER_STAMP)
        self.assert_resumed(tower)

    def test_an_unknown_live_profile_is_never_refused(self):
        write_complete(self.path)
        for provider in (_MISSING, None, _raises, lambda: None, lambda: 'tower_v1',
                         lambda: {'profile': 'tower_v2'}):
            for stamp in (None, TOWER_STAMP):
                with self.subTest(provider=provider, stamp=stamp):
                    Calls.reset()
                    if stamp is None:
                        (self.path / STAMP).unlink(missing_ok=True)
                    else:
                        (self.path / STAMP).write_bytes(stamp)
                    self.assert_resumed(provider)

    def test_an_unreadable_stamp_is_never_refused(self):
        write_complete(self.path)
        (self.path / STAMP).write_bytes(b'{"v": 2, "scene_camera_profile": "tower_v1"}')
        for provider in (tower, legacy):
            with self.subTest(provider=provider):
                Calls.reset()
                self.assert_resumed(provider)


# ── the create rule ──────────────────────────────────────────────────────────

class TheCreateRule(ProvenanceCase):

    def spy_create_record(self, dm):
        seen = []
        real = dm._write_create_record

        def spy():
            seen.append(sorted(p.relative_to(self.path).as_posix()
                               for p in self.path.rglob('*') if p.is_file()))
            real()
        dm._write_create_record = spy
        return seen

    def test_a_tower_session_stamps_the_dataset_it_creates_before_its_record(self):
        dm = self.dm(tower)
        seen = self.spy_create_record(dm)
        self.assertTrue(self.check(dm))
        self.assertEqual(self.created, [REPO])
        self.assertEqual(seen, [[STAMP, 'meta/info.json']], 'the stamp exists when the record is written')
        data = (self.path / STAMP).read_bytes()
        self.assertEqual(data, TOWER_STAMP)
        self.assertEqual(list(json.loads(data)), ['v', 'scene_camera_profile', 'profile_id', 'platform'])
        self.assertFalse((self.path / '.edubotics_scene_camera.json.tmp').exists())
        self.assertIsNotNone(dm._lerobot_dataset)

    def test_a_legacy_or_unknown_session_creates_todays_files(self):
        trees = []
        for provider in (MOD.scene_provenance.LEGACY_PROVIDER, legacy, _MISSING, None, _raises):
            with self.subTest(provider=provider):
                shutil.rmtree(self.base / 'maxmuster', ignore_errors=True)
                dm = self.dm(provider)
                seen = self.spy_create_record(dm)
                self.assertTrue(self.check(dm))
                self.assertEqual(seen, [['meta/info.json']])
                tree = {p.relative_to(self.base).as_posix(): p.read_bytes()
                        for p in sorted(self.base.rglob('*')) if p.is_file()}
                self.assertEqual(sorted(tree), [f'{REPO}.sync.json', f'{REPO}/meta/info.json'])
                trees.append(tree)
        self.assertTrue(all(t == trees[0] for t in trees), 'byte-identical datasets')

    def test_a_failed_stamp_write_ends_the_start_and_a_second_start_retries_it(self):
        dm = self.dm(tower)
        seen = self.spy_create_record(dm)
        with mock.patch.object(MOD.scene_provenance, 'write_stamp', side_effect=OSError('no space left')):
            self.assertFalse(self.check(dm))
        self.assertEqual(dm._last_warning_message, texts().SCENE_STAMP_FAILED_DE.format(name=NAME))
        self.assertEqual(dm._last_warning_message,
                         'Der Datensatz „Würfel in die Schale“ konnte nicht als Aufnahme der Turm-Kamera '
                         'gekennzeichnet werden – es wurde nichts aufgenommen. Bitte einen neuen Aufgabennamen '
                         'wählen und den freien Speicherplatz prüfen.')
        self.assertIsNone(dm._lerobot_dataset, 'nothing is recorded')
        self.assertEqual(seen, [], 'no create record for a dataset that failed its Start')
        self.assertTrue((self.path / 'meta' / 'info.json').is_file(), 'the recorder deletes nothing')
        self.assertFalse((self.path / STAMP).exists())
        self.assertFalse((self.path / 'data').exists() or (self.path / 'videos').exists())

        # The second Start with the same name: the folder holds meta/ only, so
        # _check_dataset_exists removes it and the dataset is created again.
        second = self.dm(tower)
        self.assertTrue(self.check(second))
        self.assertEqual(self.created, [REPO, REPO])
        self.assertEqual((self.path / STAMP).read_bytes(), TOWER_STAMP)
        self.assertEqual(Calls.wrapper_init, 0, 'never opened as a resume')
        self.assertIsNotNone(second._lerobot_dataset)

    def test_the_incomplete_copy_path_is_unchanged(self):
        """The second Start relies on _check_dataset_exists exactly as it is."""
        src = (PKG / 'data_processing' / 'data_manager.py').read_text(encoding='utf-8')
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == '_check_dataset_exists')
        body = ast.get_source_segment(src, fn)
        self.assertIn("missing = [f for f in ('meta', 'videos', 'data') if not os.path.exists(os.path.join(root, f))]",
                      body)
        self.assertIn('shutil.rmtree(root)', body)
        self.assertNotIn('scene', body)


# ── the node's provider ──────────────────────────────────────────────────────

def _node_init():
    src = NODE_PATH.read_text(encoding='utf-8')
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == 'init_robot_control_parameters_from_user_task')
    return fn, textwrap.dedent(ast.get_source_segment(src, fn))


class _FakeDataManager:
    def __init__(self, **kw):
        self.kw = kw
        self._dataset_lease = None


class _FakeTimerManager:
    def __init__(self, node):
        self.started = []

    def stop_all(self):
        pass

    def set_timer(self, **kw):
        pass

    def start(self, timer_name):
        self.started.append(timer_name)


class _Log:
    def info(self, *a, **k):
        pass


class _Node:
    DEFAULT_SAVE_ROOT_PATH = '/nonexistent'

    def __init__(self, daten):
        self.daten = daten
        self.robot_type = 'omx_f'
        self.operation_mode = 'inference'
        self.timer_manager = None
        self._record_cb_group = object()
        self._enqueue_dataset_upload = None
        self.timer_callback_dict = {'inference': lambda: None}
        self.communicator = types.SimpleNamespace(clear_latest_data=lambda: None, camera_topic_msgs={})
        # R-C (WP-08): the provider is the studio camera's (the arm container's statement).
        self.studio_camera = types.SimpleNamespace(provider=lambda: None)

    def get_logger(self):
        return _Log()


class TheNodeInjectsTheProvider(BoundedTestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('_edubotics_test_scene_provenance', str(PROVENANCE_PATH))
        cls.P = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.P)
        _, source = _node_init()
        ns = {'DataManager': _FakeDataManager, 'TimerManager': _FakeTimerManager,
              'SlotSampler': None, 'TakeIntegrity': None}
        exec(compile(source, str(NODE_PATH), 'exec'), ns)  # noqa: S102
        cls.INIT = staticmethod(ns['init_robot_control_parameters_from_user_task'])

    def run_init(self, daten):
        node = _Node(daten)
        package = types.ModuleType('physical_ai_server')
        package.scene_provenance = self.P
        with mock.patch.dict(sys.modules, {'physical_ai_server': package,
                                           'physical_ai_server.scene_provenance': self.P}):
            self.INIT(node, types.SimpleNamespace(fps=30))
        return node

    def test_a_node_without_daten_still_injects_the_provider(self):
        node = self.run_init(None)          # a DatenService that failed to build
        self.assertIs(node.data_manager._scene_camera_profile, node.studio_camera.provider)
        self.assertIsNone(node.data_manager._dataset_lease)

    def test_a_node_with_daten_injects_it_beside_the_lease(self):
        daten = types.SimpleNamespace(claim_record_lease=object(), start_sync_download=object())
        node = self.run_init(daten)
        self.assertIs(node.data_manager._scene_camera_profile, node.studio_camera.provider)
        self.assertIs(node.data_manager._dataset_lease, daten.claim_record_lease)

    def test_the_provider_answers_legacy(self):
        self.assertEqual(self.P.live_profile(self.P.LEGACY_PROVIDER),
                         {'profile': 'legacy', 'profile_id': None, 'platform': None})


if __name__ == '__main__':
    unittest.main()
