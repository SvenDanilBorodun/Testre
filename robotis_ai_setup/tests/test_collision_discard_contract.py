#!/usr/bin/env python3
#
# Contract test for the teleop collision e-stop's episode-discard behavior.
#
# When a collision fires mid-recording, the orchestration (safety/collision_monitor.py) calls
# data_manager.re_record() to DISCARD ONLY the in-progress (contaminated) episode — prior saved
# episodes are kept, the current one is not counted, and the safe-home glide is never recorded
# (Rule §2: the recorded action distribution must not be reshaped). This locks that contract at
# the unit level so a future refactor of re_record() can't silently break the guarantee the
# feature depends on.
#
# data_manager.py imports a large ROS/cv2/HF/lerobot tree at module level that the discard logic
# does not need, so we stub those in sys.modules before loading via importlib — same approach as
# test_data_manager_finalize.py / test_data_manager_video_verify.py.

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
    _stub('physical_ai_server.data_processing.data_converter', DataConverter=_placeholder)
    _stub('physical_ai_server.data_processing.lerobot_dataset_wrapper',
          LeRobotDatasetWrapper=_placeholder)
    _stub('physical_ai_server.data_processing.progress_tracker',
          HuggingFaceProgressTqdm=_placeholder, HuggingFaceLogCapture=_placeholder)
    _stub('physical_ai_server.device_manager')
    _stub('physical_ai_server.device_manager.cpu_checker', CPUChecker=_placeholder)
    _stub('physical_ai_server.device_manager.ram_checker', RAMChecker=_placeholder)
    _stub('physical_ai_server.device_manager.storage_checker', StorageChecker=_placeholder)


def _load_data_manager_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_dm_discard_test', str(DATA_MANAGER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDataset:
    def __init__(self):
        # A non-None episode buffer represents an in-progress (partially recorded) episode.
        self.episode_buffer = {'frames': [1, 2, 3], 'meta': ['a']}


class CollisionDiscardContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.DataManager = _load_data_manager_module().DataManager

    def _make_dm(self):
        dm = self.DataManager.__new__(self.DataManager)
        dm._lerobot_dataset = _FakeDataset()
        dm._record_episode_count = 2          # two episodes already saved
        dm._status = 'run'
        dm._current_task = 0
        dm._stop_save_completed = True
        dm._on_saving = True                  # latched as if a save was mid-flight
        dm._start_time_s = 5.0
        dm._last_image_hashes = {'gripper': 'h1'}
        dm._last_image_change_time = {'gripper': 1.0}
        return dm

    def test_re_record_discards_only_in_progress_episode(self):
        dm = self._make_dm()
        dm.re_record()

        # The in-progress episode buffer is dropped (episode discarded)...
        self.assertIsNone(dm._lerobot_dataset.episode_buffer)
        # ...but previously saved episodes are KEPT (count not decremented or zeroed)...
        self.assertEqual(dm._record_episode_count, 2)
        # ...the FSM rewinds to 'reset' (not counted as a completed episode)...
        self.assertEqual(dm._status, 'reset')
        # ...and the save-completion latch is cleared so the next episode records cleanly.
        self.assertFalse(dm._stop_save_completed)
        # ...the in-flight save latch is cleared too, so the NEXT 'save' tick actually calls
        # save() (a collision mid-SAVING must not let the re-recorded episode be counted
        # without its frames ever being written).
        self.assertFalse(dm._on_saving)
        # Stale-frame hashes are dropped so the re-recorded episode starts fresh.
        self.assertEqual(dm._last_image_hashes, {})
        self.assertEqual(dm._last_image_change_time, {})


if __name__ == '__main__':
    unittest.main()
