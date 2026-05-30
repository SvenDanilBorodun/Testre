#!/usr/bin/env python3
#
# Regression test for the LeRobot v0.5.1 recording-completion contract.
#
# v0.5.1 keeps the data ParquetWriter open and buffers per-episode metadata
# (DatasetMetadata._metadata_buffer, default size 10) until dataset.finalize()
# runs. The canonical record lifecycle is:
#   create() -> (add_frame -> save_episode)* -> finalize() -> push_to_hub()
# The EduBotics state machine drove create/add_frame/save_episode but originally
# NEVER called finalize(), so a recording of <10 episodes shipped a data parquet
# with no footer (unreadable) and no meta/episodes/*.parquet at all — while
# info.json and the mp4s still looked valid (so _verify_saved_video_files and
# _check_dataset_exists both passed). Modal training / a local re-read then
# failed on the corrupt dataset.
#
# These tests lock in: (1) _finalize_dataset() flushes the writers and reports
# success/failure correctly, and (2) record() finalizes BEFORE upload at BOTH
# terminal paths ('finish' and the count-cap fallback), and skips the upload
# when finalize fails (never ships an incomplete dataset).
#
# data_manager.py imports a large ROS/cv2/HF/lerobot tree at module level, none
# of which the recording-completion logic needs, so we stub those in sys.modules
# before loading the overlay via importlib (same approach as
# test_data_manager_video_verify.py).

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# v2.5.3: physical-ai-server is now COPY-wholesale — the single source of truth
# is the package tree, not the (deleted) docker overlays/ dir.
DATA_MANAGER_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'data_manager.py'
)


def _stub(name, **attrs):
    if name in sys.modules:
        mod = sys.modules[name]
    else:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_stubs():
    _placeholder = type('_Placeholder', (), {})

    _stub('cv2')
    _stub('numpy')
    _stub('requests')

    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=_placeholder)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=_placeholder)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', JointState=_placeholder)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=_placeholder)

    _stub('huggingface_hub',
          DatasetCard=_placeholder, DatasetCardData=_placeholder,
          HfApi=_placeholder, ModelCard=_placeholder, ModelCardData=_placeholder,
          snapshot_download=lambda *a, **k: None,
          upload_large_folder=lambda *a, **k: None)
    _stub('huggingface_hub.errors', LocalTokenNotFoundError=type(
        'LocalTokenNotFoundError', (Exception,), {}))

    _stub('lerobot')
    _stub('lerobot.datasets')
    _stub('lerobot.datasets.utils', DEFAULT_FEATURES={})

    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', TaskStatus=_placeholder)

    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    _stub('physical_ai_server.data_processing.data_converter',
          DataConverter=_placeholder)
    _stub('physical_ai_server.data_processing.lerobot_dataset_wrapper',
          LeRobotDatasetWrapper=_placeholder)
    _stub('physical_ai_server.data_processing.progress_tracker',
          HuggingFaceProgressTqdm=_placeholder,
          HuggingFaceLogCapture=_placeholder)
    _stub('physical_ai_server.device_manager')
    _stub('physical_ai_server.device_manager.cpu_checker', CPUChecker=_placeholder)
    _stub('physical_ai_server.device_manager.ram_checker', RAMChecker=_placeholder)
    _stub('physical_ai_server.device_manager.storage_checker',
          StorageChecker=_placeholder)


def _load_data_manager_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_dm_finalize_test', str(DATA_MANAGER_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeMeta:
    def __init__(self, video_keys):
        self.video_keys = list(video_keys)


class _FakeDataset:
    """Minimal stand-in mirroring the v0.5.1 finalize/encoding contract."""

    def __init__(self, events, video_keys=(), finalize_raises=False):
        self._events = events
        self.root = '/nonexistent'
        self.meta = _FakeMeta(video_keys)
        self.episode_buffer = None
        self._is_finalized = False
        self._finalize_raises = finalize_raises
        self.finalize_calls = 0

    def check_video_encoding_completed(self):
        return True

    def check_append_buffer_completed(self):
        return True

    def finalize(self):
        self.finalize_calls += 1
        if self._finalize_raises:
            raise RuntimeError('simulated parquet close failure')
        self._is_finalized = True
        self._events.append('finalize')


class _FakeTaskInfo:
    def __init__(self, num_episodes=5, push_to_hub=True):
        self.push_to_hub = push_to_hub
        self.num_episodes = num_episodes
        self.tags = []
        self.private_mode = True


class _RecordingFinalizeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_data_manager_module()
        cls.DataManager = cls.mod.DataManager

    def _make_dm(self, events, fake, task_info, *, status, on_saving, count):
        dm = self.DataManager.__new__(self.DataManager)
        dm._lerobot_dataset = fake
        dm._task_info = task_info
        dm._status = status
        dm._on_saving = on_saving
        dm._record_episode_count = count
        dm._current_task = 0
        dm._single_task = True
        dm._start_time_s = 1.0  # non-zero so record() doesn't reset the clock
        dm._proceed_time = 0
        dm._last_warning_message = ''
        dm._last_image_hashes = {}
        dm._last_image_change_time = {}
        # _upload_dataset / unrelated helpers replaced with recorders/no-ops so
        # the test isolates the finalize<->upload ordering contract.
        dm._upload_dataset = lambda tags, private: events.append('upload')
        dm._verify_saved_video_files = lambda: None
        dm._get_current_scenario_number = lambda: None
        return dm

    # ---- _finalize_dataset() unit behavior ----

    def test_finalize_dataset_success(self):
        events = []
        fake = _FakeDataset(events)
        dm = self.DataManager.__new__(self.DataManager)
        dm._lerobot_dataset = fake
        dm._last_warning_message = ''
        self.assertTrue(dm._finalize_dataset())
        self.assertEqual(fake.finalize_calls, 1)
        self.assertEqual(dm._last_warning_message, '')

    def test_finalize_dataset_failure_returns_false_and_warns(self):
        events = []
        fake = _FakeDataset(events, finalize_raises=True)
        dm = self.DataManager.__new__(self.DataManager)
        dm._lerobot_dataset = fake
        dm._last_warning_message = ''
        self.assertFalse(dm._finalize_dataset())  # must not raise
        self.assertEqual(fake.finalize_calls, 1)
        # German, student-facing warning surfaced (Rule §1).
        self.assertIn('unvollständig', dm._last_warning_message)

    def test_finalize_dataset_none_dataset(self):
        dm = self.DataManager.__new__(self.DataManager)
        dm._lerobot_dataset = None
        dm._last_warning_message = ''
        self.assertFalse(dm._finalize_dataset())  # must not raise

    # ---- record() finalizes BEFORE upload at both terminal paths ----

    def test_finish_state_finalizes_before_upload(self):
        events = []
        fake = _FakeDataset(events)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=True)
        dm = self._make_dm(events, fake, task_info,
                           status='finish', on_saving=True, count=3)
        result = dm.record(None, None, None)
        self.assertTrue(result)  # RECORD_COMPLETED
        self.assertEqual(events, ['finalize', 'upload'])
        self.assertEqual(fake.finalize_calls, 1)

    def test_count_cap_fallback_finalizes_before_upload(self):
        # The auto-complete path: last episode in 'save' pushes the count to
        # num_episodes, then the bottom fallback uploads in the same tick.
        events = []
        fake = _FakeDataset(events)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=True)
        dm = self._make_dm(events, fake, task_info,
                           status='save', on_saving=True, count=4)
        result = dm.record(None, None, None)
        self.assertTrue(result)  # RECORD_COMPLETED
        self.assertEqual(events, ['finalize', 'upload'])
        self.assertEqual(fake.finalize_calls, 1)

    def test_finish_state_skips_upload_when_finalize_fails(self):
        events = []
        fake = _FakeDataset(events, finalize_raises=True)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=True)
        dm = self._make_dm(events, fake, task_info,
                           status='finish', on_saving=True, count=3)
        result = dm.record(None, None, None)
        self.assertTrue(result)  # recording still completes...
        self.assertEqual(events, [])  # ...but NO upload of an incomplete dataset
        self.assertIn('unvollständig', dm._last_warning_message)

    def test_finish_state_finalizes_even_when_not_pushing(self):
        # push_to_hub=False: no upload, but the LOCAL dataset must still be
        # finalized (otherwise its on-disk parquet is unreadable).
        events = []
        fake = _FakeDataset(events)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=False)
        dm = self._make_dm(events, fake, task_info,
                           status='finish', on_saving=True, count=3)
        result = dm.record(None, None, None)
        self.assertTrue(result)
        self.assertEqual(fake.finalize_calls, 1)
        self.assertEqual(events, ['finalize'])  # finalized, no upload


if __name__ == '__main__':
    unittest.main()
