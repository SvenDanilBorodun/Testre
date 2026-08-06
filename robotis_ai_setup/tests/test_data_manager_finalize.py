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
    # data_manager imports dataset_paths at module level (2026-08-06 path
    # confinement) and the stub package has no __path__, so the submodule must
    # be present as an ATTRIBUTE. Loaded for REAL rather than stubbed: it is
    # stdlib-only, so it costs nothing, and a stub without `confine` would
    # AttributeError the moment anything exercised __init__. Doing this in
    # every installer (not just the first to run) keeps it order-independent —
    # sys.modules is process-global across a `discover` run.
    _dsp_name = 'physical_ai_server.data_processing.dataset_paths'
    if _dsp_name not in sys.modules:
        _dsp_spec = importlib.util.spec_from_file_location(
            _dsp_name, str(DATA_MANAGER_PATH.parent / 'dataset_paths.py'))
        _dsp = importlib.util.module_from_spec(_dsp_spec)
        sys.modules[_dsp_name] = _dsp
        try:
            _dsp_spec.loader.exec_module(_dsp)
        except BaseException:
            sys.modules.pop(_dsp_name, None)
            raise
    sys.modules['physical_ai_server.data_processing'].dataset_paths = (
        sys.modules[_dsp_name])
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
        dm._write_session_marker = lambda: None
        dm._stop_save_completed = False
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

    # ---- 'Stop' must finalize (and upload) exactly like 'finish' ----
    # Pre-fix, the 'stop' branch saved the last episode but returned RECORDING
    # without ever finalizing/uploading -> a Stop-ended dataset was a
    # footer-less, possibly meta/episodes-less corrupt tree on disk.

    def test_stop_state_finalizes_before_upload(self):
        events = []
        fake = _FakeDataset(events)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=True)
        # on_saving=True + not yet completed => the stop-completion tick fires.
        dm = self._make_dm(events, fake, task_info,
                           status='stop', on_saving=True, count=3)
        result = dm.record(None, None, None)
        self.assertTrue(result)  # RECORD_COMPLETED
        self.assertEqual(events, ['finalize', 'upload'])
        self.assertEqual(fake.finalize_calls, 1)
        # The stop completion increments the saved-episode count.
        self.assertEqual(dm._record_episode_count, 4)

    def test_stop_state_finalizes_even_when_not_pushing(self):
        events = []
        fake = _FakeDataset(events)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=False)
        dm = self._make_dm(events, fake, task_info,
                           status='stop', on_saving=True, count=3)
        result = dm.record(None, None, None)
        self.assertTrue(result)
        self.assertEqual(fake.finalize_calls, 1)
        self.assertEqual(events, ['finalize'])  # finalized, no upload

    def test_stop_state_skips_upload_when_finalize_fails(self):
        events = []
        fake = _FakeDataset(events, finalize_raises=True)
        task_info = _FakeTaskInfo(num_episodes=5, push_to_hub=True)
        dm = self._make_dm(events, fake, task_info,
                           status='stop', on_saving=True, count=3)
        result = dm.record(None, None, None)
        self.assertTrue(result)  # recording still completes...
        self.assertEqual(events, [])  # ...but NO upload of an incomplete dataset
        self.assertIn('unvollständig', dm._last_warning_message)


if __name__ == '__main__':
    unittest.main()
