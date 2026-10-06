#!/usr/bin/env python3
#
# Daten 2.0 (spec §E2, §J.1): DataManager.upload_huggingface_repo — the HF
# worker's call for every dataset upload — reaches ONLY hub_sync's guarded
# single commit. This file used to test `_sync_dataset_repo_after_upload`
# (upload_large_folder, then a SEPARATE orphan-sweep commit, then the tag
# re-created without a revision); that function is gone. Rewritten to prove,
# through the data manager:
#
#   * the orphan deletes ride the upload's ONE commit (no sweep commit);
#   * the `v3.0` tag is created with revision = that commit (main's head);
#   * a failed tag move gives the record `tag_ok: false` and HUB_TAG_FAILED_DE;
#   * a failing listing fails the upload in German, nothing committed;
#   * every guarded refusal is its German sentence, `expected_hub_sha`
#     absent / None / a sha means what §E2 step 3 says, the per-file progress
#     and the status extras (repo_type, info_json, the unconfirmed sentence).
#
# Against the Appendix K fake hub in-process (huggingface_hub 1.23's own client
# on a patched transport), the harness of test_hub_sync_upload.py; data_manager
# is loaded with its ROS/cv2/LeRobot imports stubbed inside the harness's
# isolated sys.modules (the real huggingface_hub stays real there). Skipped
# where huggingface_hub is not installed (CI installs it, spec §H8).

import importlib.util
import json
import pathlib
import queue
import sys
import types
import unittest
from unittest import mock

import test_hub_sync_upload as T

DATA_MANAGER_PATH = (
    T.REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'data_manager.py'
)
CARD = '---\ntags:\n- robotis\n---\n# EduBotics dataset\n'


def _fresh(name, **attrs):
    """A fresh stub in the (isolated) sys.modules — never a mutation of a real module."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_data_manager():
    placeholder = type('_Placeholder', (), {})
    _fresh('cv2')
    if importlib.util.find_spec('numpy') is None:
        _fresh('numpy')
    for pkg, cls in (('geometry_msgs', 'Twist'), ('nav_msgs', 'Odometry'), ('sensor_msgs', 'JointState'),
                     ('trajectory_msgs', 'JointTrajectory')):
        _fresh(pkg)
        _fresh(f'{pkg}.msg', **{cls: placeholder})
    _fresh('lerobot.datasets.utils', DEFAULT_FEATURES={})
    _fresh('physical_ai_interfaces')
    _fresh('physical_ai_interfaces.msg', TaskStatus=placeholder)
    _fresh('physical_ai_server')
    package = _fresh('physical_ai_server.data_processing')
    spec = importlib.util.spec_from_file_location(
        'physical_ai_server.data_processing.dataset_paths', str(DATA_MANAGER_PATH.parent / 'dataset_paths.py'))
    paths = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = paths
    spec.loader.exec_module(paths)
    package.dataset_paths = paths
    _fresh('physical_ai_server.data_processing.data_converter', DataConverter=placeholder)
    _fresh('physical_ai_server.data_processing.lerobot_dataset_wrapper', LeRobotDatasetWrapper=placeholder)
    _fresh('physical_ai_server.data_processing.progress_tracker',
           HuggingFaceProgressTqdm=placeholder, HuggingFaceLogCapture=placeholder)
    _fresh('physical_ai_server.device_manager')
    for name, cls in (('cpu_checker', 'CPUChecker'), ('ram_checker', 'RAMChecker'),
                      ('storage_checker', 'StorageChecker')):
        _fresh(f'physical_ai_server.device_manager.{name}', **{cls: placeholder})
    spec = importlib.util.spec_from_file_location('_edubotics_dm_upload_sync_test', str(DATA_MANAGER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardedUploadThroughTheDataManager(T.HubCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.MOD = _load_data_manager()
        cls.DM = cls.MOD.DataManager
        cls.texts = cls.MOD.record_texts_de
        cls.MOD.dataset_card.build_dataset_card = lambda repo_id, info, tags, public: CARD

    def setUp(self):
        super().setUp()
        self.progress = queue.Queue()
        self.DM.set_progress_queue(self.progress)
        self.addCleanup(self.DM.set_progress_queue, None)

    # -- helpers ------------------------------------------------------------------

    def upload(self, repo=T.REPO, **kw):
        return self.DM.upload_huggingface_repo(repo, 'dataset', str(self.root), **kw)

    def reason(self):
        return self.DM._last_hf_failure_reason_de

    def progress_items(self):
        out = []
        while not self.progress.empty():
            out.append(self.progress.get_nowait())
        return out

    # -- the ONE commit, the tag ------------------------------------------------------

    def test_the_orphan_deletes_ride_the_one_commit(self):
        T.write_tree(self.root, T.BASE)
        self.assertTrue(self.upload(private=False, expected_hub_sha=None))
        T.write_tree(self.root, T.SESSION2)
        self.assertTrue(self.upload())
        for p in ('data/chunk-000/file-001.parquet', 'meta/episodes/chunk-000/file-001.parquet',
                  'videos/observation.images.scene/chunk-000/file-001.mp4'):
            (self.root / p).unlink()
        T.write_tree(self.root, {'meta/stats.json': b'{"s": 9}'})
        self.S.update_record(self.root, T.REPO, files=self.S.files_manifest(self.root))   # the engine's delete
        n = len(self.audit('create_commit'))
        self.assertTrue(self.upload())
        commits = self.audit('create_commit')
        self.assertEqual(len(commits), n + 1, 'the orphans ride the upload commit, no sweep commit')
        digest = self.S.meta_digest(self.root)
        self.assertTrue(commits[-1]['title'].startswith(f'[edubotics:{digest[:16]}] '))
        tree = self.hub_tree()
        self.assertNotIn('data/chunk-000/file-001.parquet', tree)
        self.assertNotIn('videos/observation.images.scene/chunk-000/file-001.mp4', tree)
        self.assertIn('.gitattributes', tree, 'hub-managed files are never deleted')
        self.assertIn('README.md', tree, "LeRobot's card rides the same commit")
        self.assertEqual(self.audit('upload_large_folder'), [])

    def test_the_tag_is_created_with_revision_the_commit(self):
        T.write_tree(self.root, T.BASE)
        self.assertTrue(self.upload(private=False, expected_hub_sha=None))
        commit = self.audit('create_commit')[-1]['sha']
        self.assertEqual(self.main(), commit)
        self.assertEqual([t['sha'] for t in self.audit('create_tag')], [commit])
        self.assertEqual(self.tag(), commit)
        record = self.S.read_record(self.root)
        self.assertEqual(record['hub_sha'], commit)
        self.assertNotIn('tag_ok', record)
        self.assertIsNone(self.reason())

    def test_a_failed_tag_move_is_tag_ok_false_and_the_tag_sentence(self):
        T.write_tree(self.root, T.BASE)
        self.faults({'op': 'create_tag', 'kind': '500', 'times': 3})
        self.assertFalse(self.upload(private=False, expected_hub_sha=None))
        self.assertEqual(self.reason(), self.texts.HUB_TAG_FAILED_DE)
        self.assertIs(self.S.read_record(self.root)['tag_ok'], False)
        self.assertEqual(len(self.audit('create_commit')), 1, 'the data landed')
        self.assertEqual(self.DM._last_upload_extras['code'], 'tag_failed')

    def test_a_failing_listing_fails_the_upload_in_german(self):
        T.write_tree(self.root, T.BASE)
        self.assertTrue(self.upload(private=False, expected_hub_sha=None))
        T.write_tree(self.root, T.SESSION2)
        self.faults({'op': 'list_repo_tree', 'kind': '503', 'times': None})
        n = len(self.audit('create_commit'))
        self.assertFalse(self.upload())
        self.assertEqual(self.reason(), self.texts.HF_SERVER_ERROR_DE)
        self.assertEqual(len(self.audit('create_commit')), n)

    # -- refusals, permission, extras ---------------------------------------------------

    def test_every_refusal_is_its_german_sentence(self):
        T.write_tree(self.root, T.BASE)
        marker = self.S.session_marker_path(self.root)
        marker.write_text('{}')
        self.assertFalse(self.upload(expected_hub_sha=None))
        self.assertEqual(self.reason(), self.texts.UPLOAD_IN_SESSION_DE)
        self.assertEqual(self.audit(), [], 'the crash marker refuses before any network call')
        marker.unlink()
        (self.root / 'BROKEN').write_text('x')                       # the engine stub: integrity fails
        self.assertFalse(self.upload(expected_hub_sha=None))
        self.assertEqual(self.reason(), self.texts.UPLOAD_BROKEN_DE)
        (self.root / 'BROKEN').unlink()
        self.assertFalse(self.upload('schule-org/omx_f_wuerfel', expected_hub_sha=None))
        self.assertEqual(self.reason(), self.texts.NAMESPACE_REFUSED_DE)
        self.assertTrue(self.upload(expected_hub_sha=None))
        self.assertFalse(self.upload(expected_hub_sha='0' * 40))
        self.assertEqual(self.reason(), self.texts.HUB_CHANGED_SINCE_CHECK_DE)
        self.other_pc(T.BASE, T.OTHER2)                              # the hub moved on to other data
        self.assertFalse(self.upload())                              # no key: decided now -> newer
        self.assertEqual(self.reason(), self.texts.UPLOAD_HUB_DIFFERS_DE)
        self.assertEqual(self.DM._last_upload_extras['code'], 'hub_differs')

    def test_expected_hub_sha_none_a_sha_and_absent(self):
        T.write_tree(self.root, T.BASE)
        self.assertTrue(self.upload(expected_hub_sha=None))           # nothing online: allowed
        head = self.main()
        T.write_tree(self.root, T.SESSION2)
        self.assertFalse(self.upload(expected_hub_sha=None))          # None, but a dataset is online
        self.assertEqual(self.reason(), self.texts.HUB_CHANGED_SINCE_CHECK_DE)
        self.assertTrue(self.upload(expected_hub_sha=head))           # the head the decision saw
        T.write_tree(self.root, {'meta/stats.json': b'{"s": 4}'})
        self.assertTrue(self.upload())                                 # absent: decided now (changed)

    def test_the_extras_and_the_per_file_progress(self):
        T.write_tree(self.root, T.BASE)
        self.S.update_record(self.root, T.REPO, display_name='Würfel in die Schale', private=True)
        self.assertTrue(self.upload(private=True, expected_hub_sha=None))
        extras = self.DM._last_upload_extras
        self.assertEqual(extras['repo_type'], 'dataset')
        self.assertEqual(extras['info_json'], {'fps': 30, 'total_episodes': 4, 'total_frames': None,
                                               'robot_type': 'omx_f', 'display_name': 'Würfel in die Schale',
                                               'private': True})
        self.assertNotIn('message_de', extras)
        items = self.progress_items()
        self.assertTrue(items)
        self.assertEqual([i['current'] for i in items], list(range(1, len(items) + 1)))
        self.assertEqual(items[-1]['current'], items[-1]['total'])
        self.assertEqual(items[-1]['percentage'], 100.0)
        json.dumps(extras)

    def test_an_unconfirmed_commit_is_success_with_its_sentence(self):
        T.write_tree(self.root, T.BASE)
        state = {'committed': False}
        base_api = self.hf.HfApi

        class Blind(base_api):
            def create_commit(api_self, *a, **k):
                out = super().create_commit(*a, **k)
                state['committed'] = True
                return out

            def list_repo_tree(api_self, *a, **k):
                if state['committed']:
                    raise RuntimeError('read-back fails')
                return super().list_repo_tree(*a, **k)
        with mock.patch.object(self.hf, 'HfApi', Blind):
            self.assertTrue(self.upload(expected_hub_sha=None))
        self.assertEqual(self.DM._last_upload_extras['message_de'], self.texts.UPLOAD_UNCONFIRMED_DE)
        self.assertEqual(self.S.read_record(self.root), {'v': 1, 'repo_id': T.REPO, 'tag_ok': False})

    def test_a_model_keeps_its_own_path(self):
        calls = []
        self.MOD.DataManager._upload_model, saved = (
            staticmethod(lambda *a: calls.append(a) or True), self.DM.__dict__['_upload_model'])
        try:
            self.assertTrue(self.DM.upload_huggingface_repo('lena-schmidt/act', 'model', str(self.root)))
        finally:
            self.MOD.DataManager._upload_model = saved
        self.assertEqual(calls, [('lena-schmidt/act', 'model', str(self.root), True)])
        self.assertEqual(self.audit(), [])


class TheDatasetBranchReachesOnlyHubSync(unittest.TestCase):
    """AST fences (A18, §E2): the dataset upload is hub_sync's, reached through
    `_sibling('hub_sync')` INSIDE the function; upload_large_folder lives only in
    the model branch, imported there; `_upload_dataset` pushes nothing itself."""

    @classmethod
    def setUpClass(cls):
        import ast
        cls.ast = ast
        cls.tree = ast.parse(DATA_MANAGER_PATH.read_text(encoding='utf-8'))
        cls.funcs = {n.name: n for n in ast.walk(cls.tree) if isinstance(n, ast.FunctionDef)}

    def _names(self, node):
        ast = self.ast
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.alias):
                out.add(n.name)
        return out

    def test_no_module_level_upload_large_folder_and_no_sweep(self):
        ast = self.ast
        for node in self.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.assertNotIn('upload_large_folder', [a.name for a in node.names])
                self.assertNotIn('CommitOperationDelete', [a.name for a in node.names])
        self.assertNotIn('_sync_dataset_repo_after_upload', self.funcs)

    def test_the_dataset_branch_reaches_only_hub_sync(self):
        guarded = self._names(self.funcs['_upload_dataset_guarded'])
        self.assertIn('upload', guarded)
        self.assertIn('_sibling', guarded)
        for banned in ('upload_large_folder', 'upload_folder', 'push_to_hub', 'create_commit'):
            self.assertNotIn(banned, guarded)
        self.assertIn('upload_large_folder', self._names(self.funcs['_upload_model']))
        self.assertNotIn('push_to_hub', self._names(self.funcs['_upload_dataset']))
        top = self._names(self.funcs['upload_huggingface_repo'])
        self.assertNotIn('upload_large_folder', top)


if __name__ == '__main__':
    unittest.main()
