#!/usr/bin/env python3
#
# Regression test for DataManager._verify_saved_video_files against the LeRobot
# v0.5.1 (codebase v3.0) on-disk video layout.
#
# v3.0 writes one concatenated mp4 per video key at
#   <root>/videos/<video_key>/chunk-NNN/file-NNN.mp4
# (the pre-v2.5.0 v2.1 layout videos/chunk-NNN/<key>/episode_NNNNNN.mp4 is gone).
# The migration's verifier still checked the v2.1 paths and false-positived
# "muss neu aufgenommen werden" on every episode; commit 24ac7b9 rewrote it to
# verify, per camera, that the key's video directory holds >=1 non-empty .mp4.
# This locks that behavior in so a future LeRobot bump that changes the layout
# fails loudly here instead of silently re-breaking recording UX.
#
# data_manager.py imports a large ROS/cv2/HF/lerobot dependency tree at module
# level, none of which is needed by the method under test, so we stub those
# modules in sys.modules before loading the overlay file via importlib.

import importlib.util
import sys
import types
import unittest
from pathlib import Path
import tempfile
import shutil

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_MANAGER_PATH = (
    REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'physical_ai_server'
    / 'overlays' / 'data_manager.py'
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
        '_edubotics_data_manager_under_test', str(DATA_MANAGER_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeMeta:
    def __init__(self, video_keys):
        self.video_keys = list(video_keys)


class _FakeDataset:
    def __init__(self, root, video_keys):
        self.root = str(root)
        self.meta = _FakeMeta(video_keys)


class VerifySavedVideoFilesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_data_manager_module()
        cls.DataManager = cls.mod.DataManager

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Build a DataManager without running __init__ (it needs ROS task_info).
        self.dm = self.DataManager.__new__(self.DataManager)
        self.dm._record_episode_count = 0
        self.dm._last_warning_message = ''

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_video(self, key, content=b'\x00\x01\x02'):
        # v3.0 layout: <root>/videos/<key>/chunk-000/file-000.mp4
        d = self.tmp / 'videos' / key / 'chunk-000'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'file-000.mp4').write_bytes(content)

    def test_passes_when_each_key_has_nonempty_mp4(self):
        keys = ['observation.images.gripper', 'observation.images.scene']
        self.dm._lerobot_dataset = _FakeDataset(self.tmp, keys)
        for k in keys:
            self._write_video(k)
        self.dm._verify_saved_video_files()
        self.assertEqual(self.dm._last_warning_message, '')

    def test_flags_missing_camera_video(self):
        keys = ['observation.images.gripper', 'observation.images.scene']
        self.dm._lerobot_dataset = _FakeDataset(self.tmp, keys)
        # Only one camera produced a video.
        self._write_video('observation.images.gripper')
        self.dm._verify_saved_video_files()
        self.assertIn('scene', self.dm._last_warning_message)
        self.assertNotIn('gripper', self.dm._last_warning_message)

    def test_flags_zero_byte_mp4(self):
        keys = ['observation.images.gripper']
        self.dm._lerobot_dataset = _FakeDataset(self.tmp, keys)
        self._write_video('observation.images.gripper', content=b'')
        self.dm._verify_saved_video_files()
        self.assertIn('gripper', self.dm._last_warning_message)

    def test_noop_when_no_dataset(self):
        self.dm._lerobot_dataset = None
        self.dm._verify_saved_video_files()  # must not raise
        self.assertEqual(self.dm._last_warning_message, '')

    def test_noop_when_no_video_keys(self):
        self.dm._lerobot_dataset = _FakeDataset(self.tmp, [])
        self.dm._verify_saved_video_files()  # must not raise
        self.assertEqual(self.dm._last_warning_message, '')


if __name__ == '__main__':
    unittest.main()
