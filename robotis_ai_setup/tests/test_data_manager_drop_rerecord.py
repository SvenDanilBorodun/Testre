#!/usr/bin/env python3
#
# Regression test for the streaming frame-drop -> auto re-record contract.
#
# With streaming_encoding=True the per-camera StreamingVideoEncoder drops camera
# frames when its bounded queue stays full (CPU-starved encoder) while add_frame
# still appended a parquet row each tick. A dropped frame therefore leaves the
# encoded video SHORTER than the data parquet for that episode, and at train time
# LeRobot raises FrameTimestampError. The fix: DataManager.save() reads the
# encoder's per-episode drop counter BEFORE save_episode() commits and, when it
# is non-zero, discards the in-flight episode (cancels the streaming temp mp4 +
# clears the buffer) and routes _status='reset' so the SAME episode index is
# re-recorded — nothing desynced ever reaches disk and the episode count is not
# incremented.
#
# data_manager.py imports a large ROS/cv2/HF/lerobot tree at module level, none
# of which save() needs, so we stub those in sys.modules before loading the
# module via importlib (same approach as test_data_manager_finalize.py).

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
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
          CommitOperationDelete=_placeholder,
          DatasetCard=_placeholder, DatasetCardData=_placeholder,
          HfApi=_placeholder, ModelCard=_placeholder, ModelCardData=_placeholder,
          snapshot_download=lambda *a, **k: None,
          upload_large_folder=lambda *a, **k: None)
    _stub('huggingface_hub.errors',
          LocalTokenNotFoundError=type(
              'LocalTokenNotFoundError', (Exception,), {}),
          RevisionNotFoundError=type(
              'RevisionNotFoundError', (Exception,), {}))
    _stub('lerobot')
    _stub('lerobot.datasets')
    _stub('lerobot.datasets.utils', DEFAULT_FEATURES={})
    _stub('lerobot.datasets.dataset_metadata', CODEBASE_VERSION='v3.0')
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
        '_edubotics_dm_drop_test', str(DATA_MANAGER_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDataset:
    """Stand-in exposing the streaming drop + save surface save() uses."""

    def __init__(self, dropped=0, buffer=None):
        self._dropped = dropped
        self.episode_buffer = buffer
        self.cancel_calls = 0
        self.saved = []  # records which save_* method ran

    def streaming_dropped_frame_count(self):
        return self._dropped

    def cancel_streaming_episode(self):
        self.cancel_calls += 1

    def save_episode_without_write_image(self):
        self.saved.append('optimized_single')

    def save_episode_without_video_encoding(self):
        self.saved.append('optimized_multi')

    def save_episode(self):
        self.saved.append('plain')


class _FakeTaskInfo:
    def __init__(self, use_optimized_save_mode=True, fps=30):
        self.use_optimized_save_mode = use_optimized_save_mode
        self.fps = fps


class _DropRerecordTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_data_manager_module()
        cls.DataManager = cls.mod.DataManager

    def _make_dm(self, fake, *, optimized=True, single_task=True, count=2):
        dm = self.DataManager.__new__(self.DataManager)
        dm._lerobot_dataset = fake
        dm._task_info = _FakeTaskInfo(use_optimized_save_mode=optimized)
        dm._single_task = single_task
        dm._record_episode_count = count
        dm._status = 'save'
        dm._on_saving = True
        dm._stop_save_completed = True
        dm._last_warning_message = ''
        dm._start_time_s = 1.0
        dm._last_image_hashes = {}
        dm._last_image_change_time = {}
        return dm

    def test_dropped_frames_discard_and_reroute_to_reset(self):
        fake = _FakeDataset(dropped=2, buffer={'size': 3, 'timestamp': [0.0, 0.03, 0.06]})
        dm = self._make_dm(fake, count=2)
        committed = dm.save()
        self.assertFalse(committed)               # NOT committed
        self.assertEqual(fake.saved, [])          # save_episode* never called
        self.assertEqual(fake.cancel_calls, 1)    # streaming episode discarded
        self.assertEqual(dm._status, 'reset')     # routed to re-record
        self.assertFalse(dm._on_saving)
        self.assertEqual(dm._record_episode_count, 2)  # count NOT incremented
        self.assertIn('neu aufgenommen', dm._last_warning_message)

    def test_no_drops_commits_optimized_single(self):
        fake = _FakeDataset(dropped=0, buffer={'size': 3, 'timestamp': [0.0, 0.03, 0.06]})
        dm = self._make_dm(fake, optimized=True, single_task=True)
        committed = dm.save()
        self.assertTrue(committed)
        self.assertEqual(fake.saved, ['optimized_single'])
        self.assertEqual(fake.cancel_calls, 0)
        self.assertEqual(dm._status, 'save')      # unchanged

    def test_no_drops_commits_plain_non_optimized(self):
        fake = _FakeDataset(dropped=0, buffer={'size': 3, 'timestamp': [0.0, 0.03, 0.06]})
        dm = self._make_dm(fake, optimized=False, single_task=True)
        committed = dm.save()
        self.assertTrue(committed)
        self.assertEqual(fake.saved, ['plain'])

    def test_no_buffer_is_committed_noop(self):
        fake = _FakeDataset(dropped=5, buffer=None)  # drop count irrelevant
        dm = self._make_dm(fake)
        committed = dm.save()
        self.assertTrue(committed)                 # nothing to commit -> True
        self.assertEqual(fake.saved, [])
        self.assertEqual(fake.cancel_calls, 0)     # drop check skipped


if __name__ == '__main__':
    unittest.main()
