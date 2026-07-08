#!/usr/bin/env python3
#
# Regression tests for DataManager._sync_dataset_repo_after_upload — the
# post-upload hub maintenance that makes re-uploads reach training:
#
#   1. Remote-orphan sweep: upload_large_folder only adds/updates files, and
#      LeRobot v3.0 loads data parquet by GLOB — hub files under
#      data/ / meta/ / videos/ that no longer exist locally must be deleted,
#      while hub-managed files (.gitattributes, README.md) are off-limits.
#   2. v3.0 tag re-point: LeRobot 0.5.1 trains at revision CODEBASE_VERSION;
#      a bare create_tag 409s on the second upload of a repo, leaving the
#      tag pinned to the FIRST upload's commit — every re-upload was
#      invisible to training (the 2026-07-04 critical). The sync must
#      delete_tag (suppressing RevisionNotFoundError on first upload) then
#      create_tag, and FAIL the upload (False + German reason) when either
#      step fails.
#
# Same sys.modules stub approach as test_data_manager_finalize.py: the sync
# logic needs none of the ROS/cv2/HF/lerobot import tree.

import importlib.util
import sys
import types
import unittest
from pathlib import Path
import tempfile

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


class _RevisionNotFoundError(Exception):
    pass


class _CommitOperationDelete:
    def __init__(self, path_in_repo):
        self.path_in_repo = path_in_repo


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
          CommitOperationDelete=_CommitOperationDelete,
          DatasetCard=_placeholder, DatasetCardData=_placeholder,
          HfApi=_placeholder, ModelCard=_placeholder, ModelCardData=_placeholder,
          snapshot_download=lambda *a, **k: None,
          upload_large_folder=lambda *a, **k: None)
    _stub('huggingface_hub.errors',
          LocalTokenNotFoundError=type(
              'LocalTokenNotFoundError', (Exception,), {}),
          RevisionNotFoundError=_RevisionNotFoundError)

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
        '_edubotics_dm_upload_sync_test', str(DATA_MANAGER_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeApi:
    """Records the hub calls the sync makes, with per-call fault injection."""

    def __init__(self, remote_files=(), delete_tag_error=None,
                 create_tag_error=None, list_error=None):
        self.remote_files = list(remote_files)
        self.delete_tag_error = delete_tag_error
        self.create_tag_error = create_tag_error
        self.list_error = list_error
        self.calls = []
        self.deleted_paths = None

    def list_repo_files(self, repo_id, repo_type=None):
        self.calls.append('list_repo_files')
        if self.list_error is not None:
            raise self.list_error
        return list(self.remote_files)

    def create_commit(self, repo_id, repo_type=None, operations=(),
                      commit_message=''):
        self.calls.append('create_commit')
        self.deleted_paths = sorted(op.path_in_repo for op in operations)

    def delete_tag(self, repo_id, tag=None, repo_type=None):
        self.calls.append('delete_tag')
        if self.delete_tag_error is not None:
            raise self.delete_tag_error

    def create_tag(self, repo_id, tag=None, repo_type=None):
        self.calls.append('create_tag')
        if self.create_tag_error is not None:
            raise self.create_tag_error


class TestSyncDatasetRepoAfterUpload(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dm_module = _load_data_manager_module()
        cls.DataManager = cls.dm_module.DataManager

    def setUp(self):
        self.DataManager._last_hf_failure_reason_de = None
        self._tmp = tempfile.TemporaryDirectory()
        self.local_dir = self._tmp.name
        # Minimal local v3.0 tree: one data shard, one meta file, one video.
        for rel in (
            'data/chunk-000/file-001.parquet',
            'meta/info.json',
            'meta/episodes/chunk-000/file-000.parquet',
            'videos/observation.images.gripper/chunk-000/file-000.mp4',
            'README.md',
        ):
            p = Path(self.local_dir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'x')

    def tearDown(self):
        self._tmp.cleanup()

    def test_orphans_deleted_and_tag_repointed(self):
        api = _FakeApi(remote_files=[
            '.gitattributes',
            'README.md',
            'data/chunk-000/file-000.parquet',   # deleted locally -> orphan
            'data/chunk-000/file-001.parquet',   # still local -> kept
            'meta/info.json',
            'videos/observation.images.gripper/chunk-000/file-000.mp4',
        ])
        ok = self.DataManager._sync_dataset_repo_after_upload(
            api, 'user/repo', self.local_dir)
        self.assertTrue(ok)
        self.assertEqual(api.deleted_paths, ['data/chunk-000/file-000.parquet'])
        # Tag re-point runs delete THEN create, after the orphan sweep.
        self.assertEqual(
            api.calls,
            ['list_repo_files', 'create_commit', 'delete_tag', 'create_tag'])
        self.assertIsNone(self.DataManager._last_hf_failure_reason_de)

    def test_no_orphans_skips_commit_but_still_repoints_tag(self):
        api = _FakeApi(remote_files=[
            'data/chunk-000/file-001.parquet',
            'meta/info.json',
        ])
        ok = self.DataManager._sync_dataset_repo_after_upload(
            api, 'user/repo', self.local_dir)
        self.assertTrue(ok)
        self.assertIsNone(api.deleted_paths)
        self.assertEqual(api.calls, ['list_repo_files', 'delete_tag', 'create_tag'])

    def test_hub_managed_files_never_deleted(self):
        api = _FakeApi(remote_files=['.gitattributes', 'extra_root_file.txt'])
        ok = self.DataManager._sync_dataset_repo_after_upload(
            api, 'user/repo', self.local_dir)
        self.assertTrue(ok)
        self.assertIsNone(api.deleted_paths)  # nothing under data/meta/videos

    def test_first_upload_missing_tag_is_not_an_error(self):
        api = _FakeApi(delete_tag_error=_RevisionNotFoundError('no tag'))
        ok = self.DataManager._sync_dataset_repo_after_upload(
            api, 'user/repo', self.local_dir)
        self.assertTrue(ok)
        self.assertIn('create_tag', api.calls)

    def test_create_tag_failure_fails_the_upload_with_german_reason(self):
        api = _FakeApi(create_tag_error=RuntimeError('500'))
        ok = self.DataManager._sync_dataset_repo_after_upload(
            api, 'user/repo', self.local_dir)
        self.assertFalse(ok)
        reason = self.DataManager._last_hf_failure_reason_de
        self.assertIsNotNone(reason)
        self.assertIn('Versions-Tag', reason)

    def test_orphan_listing_failure_fails_the_upload_with_german_reason(self):
        api = _FakeApi(list_error=RuntimeError('503'))
        ok = self.DataManager._sync_dataset_repo_after_upload(
            api, 'user/repo', self.local_dir)
        self.assertFalse(ok)
        reason = self.DataManager._last_hf_failure_reason_de
        self.assertIsNotNone(reason)
        self.assertIn('Hugging Face', reason)
        # Tag must NOT have been touched after a failed sweep — the tag may
        # only ever point at a fully synced state.
        self.assertNotIn('delete_tag', api.calls)
        self.assertNotIn('create_tag', api.calls)


if __name__ == '__main__':
    unittest.main()
