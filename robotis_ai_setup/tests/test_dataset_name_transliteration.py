#!/usr/bin/env python3
#
# Aufnahme 2.0 (Q2): the dataset repo name a recording is saved under.
#
# A German task name used to reach the Hub mangled: „Würfel in die Schale“ became
# `W-rfel-in-die-Schale`, and a double space, an emoji or an accented letter left
# `--`/`..` runs that huggingface_hub's repo-id validator refuses. The server now
# spells the task part as: NFC -> German pairs (ä→ae … ß→ss, ẞ→SS) -> NFKD accent
# fold (drop every combining mark) -> the old [^a-zA-Z0-9._-] sanitiser -> collapse
# `-`/`.` runs -> strip `-`. The user-id part keeps HEAD's rule unchanged, because
# the upload namespace guard compares it to whoami's names.
#
# The expected values live in ONE fixture shared with the React side
# (physical_ai_manager/src/utils/__tests__/datasetName.cases.json, read by
# datasetName.test.js), so the name the page predicts and the one the server
# writes cannot drift. The server is the reference: the fixture is generated from
# these functions (json.dumps(..., ensure_ascii=False, indent=1) + newline), and
# test_fixture_bytes_are_generated_from_the_server_functions proves it still is.

import importlib.util
import json
import sys
import tempfile
import types
import unicodedata
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_MANAGER_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'data_manager.py'
)
FIXTURE_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'utils'
    / '__tests__' / 'datasetName.cases.json'
)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _stub_unless_importable(name):
    try:
        __import__(name)
    except Exception:  # noqa: BLE001 — deps-free suite: stub what is missing
        _stub(name)


class _DataConverter:
    def set_action_duration_from_fps(self, fps):
        pass


class _Cpu:
    def get_cpu_usage(self):
        return 0.0


def _install_stubs():
    _placeholder = type('_Placeholder', (), {})
    for name in ('cv2', 'numpy', 'requests'):
        _stub_unless_importable(name)
    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=_placeholder)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=_placeholder)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', JointState=_placeholder)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=_placeholder)
    _stub('huggingface_hub',
          CommitOperationDelete=_placeholder,
          DatasetCard=_placeholder, DatasetCardData=_placeholder,
          HfApi=_placeholder, ModelCard=_placeholder, ModelCardData=_placeholder,
          snapshot_download=lambda *a, **k: None,
          upload_large_folder=lambda *a, **k: None)
    _stub('huggingface_hub.errors',
          LocalTokenNotFoundError=type('LocalTokenNotFoundError', (Exception,), {}),
          RevisionNotFoundError=type('RevisionNotFoundError', (Exception,), {}))
    _stub('lerobot')
    _stub('lerobot.datasets')
    _stub('lerobot.datasets.utils', DEFAULT_FEATURES={})
    _stub('lerobot.datasets.dataset_metadata', CODEBASE_VERSION='v3.0')
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', TaskStatus=_placeholder)
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    dsp_name = 'physical_ai_server.data_processing.dataset_paths'
    if dsp_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            dsp_name, str(DATA_MANAGER_PATH.parent / 'dataset_paths.py'))
        dsp = importlib.util.module_from_spec(spec)
        sys.modules[dsp_name] = dsp
        try:
            spec.loader.exec_module(dsp)
        except BaseException:
            sys.modules.pop(dsp_name, None)
            raise
    sys.modules['physical_ai_server.data_processing'].dataset_paths = sys.modules[dsp_name]
    _stub('physical_ai_server.data_processing.data_converter', DataConverter=_DataConverter)
    _stub('physical_ai_server.data_processing.lerobot_dataset_wrapper',
          LeRobotDatasetWrapper=_placeholder)
    _stub('physical_ai_server.data_processing.progress_tracker',
          HuggingFaceProgressTqdm=_placeholder, HuggingFaceLogCapture=_placeholder)
    _stub('physical_ai_server.device_manager')
    _stub('physical_ai_server.device_manager.cpu_checker', CPUChecker=_Cpu)
    _stub('physical_ai_server.device_manager.ram_checker', RAMChecker=_placeholder)
    _stub('physical_ai_server.device_manager.storage_checker', StorageChecker=_placeholder)


def _load_data_manager_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_dm_names_test', str(DATA_MANAGER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TaskInfo:
    def __init__(self, task_name, user_id):
        self.task_name = task_name
        self.user_id = user_id
        self.task_instruction = ['Greife den Würfel.']
        self.fps = 30
        self.tags = []
        self.warmup_time_s = 2
        self.episode_time_s = 3
        self.reset_time_s = 2
        self.num_episodes = 3
        self.push_to_hub = False
        self.private_mode = True
        self.use_optimized_save_mode = True
        self.record_rosbag2 = False


class DatasetNameTransliterationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_data_manager_module()
        cls.fixture_bytes = FIXTURE_PATH.read_bytes()
        cls.fixture = json.loads(cls.fixture_bytes.decode('utf-8'))
        cls.cases = cls.fixture['cases']

    def test_every_fixture_row_task_name(self):
        for row in self.cases:
            with self.subTest(inp=row['in']):
                self.assertEqual(self.mod.safe_dataset_task_name(row['in']), row['task'])

    def test_every_fixture_row_user_id_keeps_the_head_rule(self):
        for row in self.cases:
            with self.subTest(inp=row['in']):
                self.assertEqual(self.mod.safe_dataset_user_id(row['in']), row['user'])

    def test_fixture_covers_the_hard_rows(self):
        inputs = [row['in'] for row in self.cases]
        self.assertGreaterEqual(len(inputs), 44)
        # A DECOMPOSED row (u + combining diaeresis) must land where the composed
        # one does — that is what the NFC step is for.
        nfd_rows = [s for s in inputs if s != unicodedata.normalize('NFC', s)]
        self.assertTrue(nfd_rows, 'fixture needs a decomposed (NFD) input')
        # An astral-plane character (emoji) — a UTF-16 surrogate pair in JS.
        self.assertTrue(any(any(ord(ch) > 0xFFFF for ch in s) for s in inputs))
        for needed in ('ẞ', 'Café Crème', '--', '..'):
            self.assertIn(needed, inputs)

    def test_german_task_names_read_as_german(self):
        f = self.mod.safe_dataset_task_name
        self.assertEqual(f('Würfel in die Schale'), 'Wuerfel-in-die-Schale')
        self.assertEqual(f('große Grüße'), 'grosse-Gruesse')
        self.assertEqual(f('GROẞE'), 'GROSSE')
        self.assertEqual(f('Café Crème'), 'Cafe-Creme')

    def test_no_dash_or_dot_runs_survive(self):
        # huggingface_hub's repo-id validator rejects `--` and `..`.
        for row in self.cases:
            with self.subTest(inp=row['in']):
                self.assertNotIn('--', row['task'])
                self.assertNotIn('..', row['task'])
                self.assertFalse(row['task'].startswith('-'))
                self.assertFalse(row['task'].endswith('-'))

    def test_save_repo_name_uses_both_functions(self):
        root = Path(tempfile.mkdtemp(prefix='dm_names_'))
        dm = self.mod.DataManager(
            root, 'omx_f', _TaskInfo('Würfel in die Schale', 'maxmuster'),
            upload_callback=None)
        self.assertEqual(dm._save_repo_name, 'maxmuster/omx_f_Wuerfel-in-die-Schale')
        # The confinement proof is kept: the save path stays under the root.
        self.assertEqual(dm._save_path.parent.parent, root.resolve())

    def test_dot_only_user_id_still_becomes_a_placeholder(self):
        root = Path(tempfile.mkdtemp(prefix='dm_names_'))
        dm = self.mod.DataManager(
            root, 'omx_f', _TaskInfo('Würfel', '..'), upload_callback=None)
        self.assertEqual(dm._save_repo_name, 'unknown-user/omx_f_Wuerfel')

    def test_fixture_bytes_are_generated_from_the_server_functions(self):
        rows = [
            {'in': row['in'],
             'task': self.mod.safe_dataset_task_name(row['in']),
             'user': self.mod.safe_dataset_user_id(row['in'])}
            for row in self.cases
        ]
        regenerated = {'_comment': self.fixture['_comment'], 'cases': rows}
        text = json.dumps(regenerated, ensure_ascii=False, indent=1) + '\n'
        self.assertEqual(text.encode('utf-8'), self.fixture_bytes)


if __name__ == '__main__':
    unittest.main()
