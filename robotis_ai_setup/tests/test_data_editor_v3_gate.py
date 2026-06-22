"""data_editor_v3 + dataset_edit_callback layout-gate contract (deps-free).

Locks the leLab-comparison PR-1 fix for the live data-destroying bug: the
legacy DataEditor performs v2.1 per-episode surgery while the v2.5.0 recorder
writes the v3.0 concatenated layout. These tests run WITHOUT lerobot/torch/
pandas/rclpy (CI python-tests installs only pyyaml) by sys.modules-stubbing
the heavy deps — the same pattern as test_lerobot_dataset_wrapper.py and
test_collision_monitor_contract.py.

Covered invariants:
  1. German pre-validation (empty / out-of-range / delete-all) fires BEFORE
     any upstream call.
  2. The swap dance: build tmp -> verify tmp -> src->bak -> tmp->src ->
     re-verify -> drop bak; the source dataset is byte-untouched on every
     failure path, including a promote-failure rollback.
  3. dataset_edit_callback routes v3.0 datasets to data_editor_v3, v2.1
     datasets to the legacy DataEditor, refuses mixed-version merges in
     German, and maps DataEditError to a German response.message.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
V3_PATH = PKG_ROOT / 'data_processing' / 'data_editor_v3.py'
WORKER_PATH = PKG_ROOT / 'data_processing' / 'edit_worker.py'
COMMUNICATOR_PATH = PKG_ROOT / 'communication' / 'communicator.py'


# ---- shared stub helpers -----------------------------------------------------------------

def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class _FakeLeRobotDataset:
    """Stands in for lerobot.datasets.lerobot_dataset.LeRobotDataset."""

    instances = []

    def __init__(self, repo_id=None, root=None, **kwargs):
        self.repo_id = repo_id
        self.root = Path(root) if root is not None else None
        _FakeLeRobotDataset.instances.append(self)


class _DatasetToolsCalls:
    """Mutable behavior holder for the fake lerobot.datasets.dataset_tools."""

    def __init__(self):
        self.delete_calls = []
        self.merge_calls = []
        # behavior hooks the tests swap per-case
        self.on_delete = None
        self.on_merge = None


TOOLS = _DatasetToolsCalls()


def _fake_delete_episodes(dataset, episode_indices, output_dir=None, repo_id=None):
    TOOLS.delete_calls.append(
        {'dataset': dataset, 'indices': list(episode_indices),
         'output_dir': Path(output_dir) if output_dir else None,
         'repo_id': repo_id}
    )
    if TOOLS.on_delete is not None:
        return TOOLS.on_delete(dataset, episode_indices, output_dir, repo_id)
    return None


def _fake_merge_datasets(datasets, output_repo_id, output_dir=None):
    TOOLS.merge_calls.append(
        {'datasets': list(datasets), 'output_repo_id': output_repo_id,
         'output_dir': Path(output_dir) if output_dir else None}
    )
    if TOOLS.on_merge is not None:
        return TOOLS.on_merge(datasets, output_repo_id, output_dir)
    return None


def _install_lerobot_stubs():
    _stub('lerobot')
    _stub('lerobot.datasets')
    _stub('lerobot.datasets.lerobot_dataset', LeRobotDataset=_FakeLeRobotDataset)
    _stub(
        'lerobot.datasets.dataset_tools',
        delete_episodes=_fake_delete_episodes,
        merge_datasets=_fake_merge_datasets,
    )


def _load_v3_module():
    """Load data_editor_v3 ONCE under its canonical dotted name.

    The canonical name matters: communicator.py catches DataEditError by
    class identity, so the routing tests must see the SAME module object.
    """
    canonical = 'physical_ai_server.data_processing.data_editor_v3'
    if canonical in sys.modules:
        return sys.modules[canonical]
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    spec = importlib.util.spec_from_file_location(canonical, str(V3_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    spec.loader.exec_module(module)
    sys.modules['physical_ai_server.data_processing'].data_editor_v3 = module
    return module


def _load_worker_module():
    """Load edit_worker ONCE under its canonical name + attach to the package.

    communicator.py imports it at top (`from …data_processing import
    edit_worker`); the data_processing stub has no __path__, so the submodule
    must be present as an attribute before communicator is exec'd.
    """
    canonical = 'physical_ai_server.data_processing.edit_worker'
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, str(WORKER_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    spec.loader.exec_module(module)
    sys.modules['physical_ai_server.data_processing'].edit_worker = module
    return module


def _make_v3_tree(root: Path, total_episodes: int, version: str = 'v3.0',
                  marker: str = ''):
    """Materialise a structurally valid v3.0 dataset tree."""
    (root / 'meta' / 'episodes').mkdir(parents=True, exist_ok=True)
    (root / 'data' / 'chunk-000').mkdir(parents=True, exist_ok=True)
    cam = root / 'videos' / 'observation.images.gripper' / 'chunk-000'
    cam.mkdir(parents=True, exist_ok=True)
    (root / 'meta' / 'info.json').write_text(json.dumps({
        'codebase_version': version,
        'total_episodes': total_episodes,
    }), encoding='utf-8')
    (root / 'meta' / 'episodes' / 'chunk-000.parquet').write_text('meta')
    (root / 'data' / 'chunk-000' / 'file-000.parquet').write_text('data')
    (cam / 'file-000.mp4').write_text('mp4')
    if marker:
        (root / marker).write_text('marker')


def _make_v21_tree(root: Path, total_episodes: int):
    (root / 'meta').mkdir(parents=True, exist_ok=True)
    (root / 'meta' / 'info.json').write_text(json.dumps({
        'codebase_version': 'v2.1',
        'total_episodes': total_episodes,
    }), encoding='utf-8')


def _make_corrupt_info_tree(root: Path, total_episodes: int = 3):
    """A REAL v3.0 layout on disk, but with a TRUNCATED (unparseable) info.json.

    This is the regression case: the directory exists and the tree is genuinely
    v3, but json.load raises JSONDecodeError so the version can't be read.
    """
    _make_v3_tree(root, total_episodes)
    # Truncated mid-object -> json.load raises JSONDecodeError (a ValueError).
    (root / 'meta' / 'info.json').write_text(
        '{"codebase_version": "v3.0", "total_episo', encoding='utf-8')


def _make_versionless_info_tree(root: Path, total_episodes: int = 3):
    """A real v3.0 layout whose info.json parses but lacks codebase_version."""
    _make_v3_tree(root, total_episodes)
    (root / 'meta' / 'info.json').write_text(
        json.dumps({'total_episodes': total_episodes}), encoding='utf-8')


class DataEditorV3ModuleTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_lerobot_stubs()
        cls.v3 = _load_v3_module()

    def setUp(self):
        TOOLS.delete_calls.clear()
        TOOLS.merge_calls.clear()
        TOOLS.on_delete = None
        TOOLS.on_merge = None
        _FakeLeRobotDataset.instances.clear()
        self.tmpdir = Path(tempfile.mkdtemp(prefix='v3gate_'))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    # -- version gate ----------------------------------------------------------------

    def test_is_v3_dataset(self):
        v3 = self.tmpdir / 'student' / 'ds_v3'
        _make_v3_tree(v3, 3)
        v21 = self.tmpdir / 'student' / 'ds_v21'
        _make_v21_tree(v21, 3)
        self.assertTrue(self.v3.is_v3_dataset(v3))
        self.assertFalse(self.v3.is_v3_dataset(v21))
        self.assertFalse(self.v3.is_v3_dataset(self.tmpdir / 'missing'))

    def test_is_v21_dataset_positive_only(self):
        # Routing to the destructive legacy editor keys on THIS. It must be a
        # positive v2.x check, never the negation of is_v3_dataset: a corrupt /
        # version-less / missing dataset is NOT positively v2.1.
        v21 = self.tmpdir / 's' / 'ds_v21'
        _make_v21_tree(v21, 3)
        v3 = self.tmpdir / 's' / 'ds_v3'
        _make_v3_tree(v3, 3)
        corrupt = self.tmpdir / 's' / 'ds_corrupt'
        _make_corrupt_info_tree(corrupt, 3)
        versionless = self.tmpdir / 's' / 'ds_nover'
        _make_versionless_info_tree(versionless, 3)
        self.assertTrue(self.v3.is_v21_dataset(v21))
        self.assertFalse(self.v3.is_v21_dataset(v3))
        self.assertFalse(self.v3.is_v21_dataset(corrupt))
        self.assertFalse(self.v3.is_v21_dataset(versionless))
        self.assertFalse(self.v3.is_v21_dataset(self.tmpdir / 's' / 'missing'))

    def test_delete_corrupt_info_is_german_beschaedigt_no_upstream(self):
        # A v3 dataset with a truncated info.json must fail loud in German and
        # touch neither upstream nor the on-disk info.json.
        src = self.tmpdir / 's' / 'ds'
        _make_corrupt_info_tree(src, 3)
        before = (src / 'meta' / 'info.json').read_text()
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [1])
        self.assertIn('beschädigt', str(ctx.exception))
        self.assertEqual(TOOLS.delete_calls, [],
                         'must reject before any upstream call')
        self.assertEqual((src / 'meta' / 'info.json').read_text(), before,
                         'corrupt info.json must be left byte-untouched')

    # -- pre-validation fires BEFORE upstream ----------------------------------------

    def test_delete_prevalidation_german_and_no_upstream_call(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3)

        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [])
        self.assertIn('Keine Episoden', str(ctx.exception))

        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [7])
        self.assertIn('gibt es nicht', str(ctx.exception))

        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [0, 1, 2])
        self.assertIn('Alle Episoden', str(ctx.exception))

        self.assertEqual(TOOLS.delete_calls, [],
                         'pre-validation must reject before any upstream call')

    def test_delete_missing_dataset_german(self):
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(self.tmpdir / 'nope'), [0])
        self.assertIn('nicht gefunden', str(ctx.exception))

    # -- happy path: swap ordering --------------------------------------------------

    def test_delete_happy_path_swaps_atomically(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')

        def build_valid(dataset, indices, output_dir, repo_id):
            _make_v3_tree(Path(output_dir), 3 - len(list(indices)),
                          marker='NEW_TREE')

        TOOLS.on_delete = build_valid

        remaining = self.v3.delete_episodes_v3(str(src), [1])

        self.assertEqual(remaining, 2)
        call = TOOLS.delete_calls[0]
        # Explicit output_dir/repo_id (upstream defaults would write a
        # *_modified sibling under the lerobot home instead). Compare
        # resolved: the module canonicalises (macOS /var -> /private/var).
        self.assertEqual(call['output_dir'],
                         src.resolve().parent / 'ds.tmp_edit')
        self.assertEqual(call['repo_id'], 'student/ds')
        self.assertEqual(call['indices'], [1])
        # Promoted tree in place, artifacts gone.
        self.assertTrue((src / 'NEW_TREE').exists())
        self.assertFalse((src / 'ORIGINAL').exists())
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())
        self.assertFalse((src.parent / 'ds.bak_edit').exists())
        self.assertEqual(
            json.loads((src / 'meta' / 'info.json').read_text())['total_episodes'], 2)

    # -- invalid tmp tree never replaces the source ----------------------------------

    def test_delete_invalid_result_keeps_source_untouched(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')

        def build_wrong_count(dataset, indices, output_dir, repo_id):
            _make_v3_tree(Path(output_dir), 99, marker='NEW_TREE')

        TOOLS.on_delete = build_wrong_count

        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [1])
        self.assertIn('stimmt nicht', str(ctx.exception))
        self.assertTrue((src / 'ORIGINAL').exists(), 'source must be untouched')
        self.assertFalse((src.parent / 'ds.tmp_edit').exists(), 'tmp cleaned')
        self.assertFalse((src.parent / 'ds.bak_edit').exists())

    def test_delete_upstream_failure_keeps_source_untouched(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')

        def explode(dataset, indices, output_dir, repo_id):
            raise RuntimeError('disk full')

        TOOLS.on_delete = explode

        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [1])
        self.assertIn('NICHT verändert', str(ctx.exception))
        self.assertTrue((src / 'ORIGINAL').exists())
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())

    # -- promote failure rolls the original back ------------------------------------

    def test_delete_promote_failure_restores_original(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')

        def build_valid(dataset, indices, output_dir, repo_id):
            _make_v3_tree(Path(output_dir), 2, marker='NEW_TREE')

        TOOLS.on_delete = build_valid

        real_rename = Path.rename

        def failing_second_rename(self_path, target):
            # First rename (src -> bak) succeeds; the promote (tmp -> src)
            # blows up, simulating a crash mid-swap.
            if str(self_path).endswith('.tmp_edit'):
                raise OSError('simulated promote failure')
            return real_rename(self_path, target)

        with mock.patch.object(Path, 'rename', failing_second_rename):
            with self.assertRaises(self.v3.DataEditError) as ctx:
                self.v3.delete_episodes_v3(str(src), [1])
        self.assertIn('wiederhergestellt', str(ctx.exception))
        # Original restored at the original path.
        self.assertTrue(src.is_dir())
        self.assertTrue((src / 'ORIGINAL').exists())
        self.assertFalse((src.parent / 'ds.bak_edit').exists())
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())

    # -- stale crash artifacts never block a new edit --------------------------------

    def test_delete_clears_stale_artifacts(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')
        stale_tmp = src.parent / 'ds.tmp_edit'
        stale_bak = src.parent / 'ds.bak_edit'
        stale_tmp.mkdir()
        (stale_tmp / 'junk').write_text('x')
        stale_bak.mkdir()

        def build_valid(dataset, indices, output_dir, repo_id):
            _make_v3_tree(Path(output_dir), 2, marker='NEW_TREE')

        TOOLS.on_delete = build_valid
        remaining = self.v3.delete_episodes_v3(str(src), [0])
        self.assertEqual(remaining, 2)
        self.assertTrue((src / 'NEW_TREE').exists())

    # -- merge ------------------------------------------------------------------------

    def test_merge_happy_path(self):
        a = self.tmpdir / 'student' / 'a'
        b = self.tmpdir / 'student' / 'b'
        _make_v3_tree(a, 2)
        _make_v3_tree(b, 3)
        out = self.tmpdir / 'student' / 'merged'

        def build_merged(datasets, output_repo_id, output_dir):
            _make_v3_tree(Path(output_dir), 5, marker='MERGED')

        TOOLS.on_merge = build_merged
        self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertTrue((out / 'MERGED').exists())
        self.assertEqual(len(TOOLS.merge_calls), 1)
        self.assertEqual(len(TOOLS.merge_calls[0]['datasets']), 2)

    def test_merge_rejects_single_source_and_nonempty_output(self):
        a = self.tmpdir / 'student' / 'a'
        _make_v3_tree(a, 2)
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a)], str(self.tmpdir / 'out'))
        self.assertIn('mindestens zwei', str(ctx.exception))

        b = self.tmpdir / 'student' / 'b'
        _make_v3_tree(b, 2)
        out = self.tmpdir / 'occupied'
        out.mkdir()
        (out / 'something').write_text('x')
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertIn('existiert bereits', str(ctx.exception))
        self.assertEqual(TOOLS.merge_calls, [])

    def test_merge_failure_cleans_output(self):
        a = self.tmpdir / 'student' / 'a'
        b = self.tmpdir / 'student' / 'b'
        _make_v3_tree(a, 2)
        _make_v3_tree(b, 3)
        out = self.tmpdir / 'student' / 'merged'

        def explode(datasets, output_repo_id, output_dir):
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            (Path(output_dir) / 'partial').write_text('x')
            raise RuntimeError('boom')

        TOOLS.on_merge = explode
        with self.assertRaises(self.v3.DataEditError):
            self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertFalse(out.exists(), 'partial merge output must be removed')


# ---- callback routing -----------------------------------------------------------------


class _FakeLogger:
    def error(self, *_a, **_k):
        pass

    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


class _FakeNode:
    def get_logger(self):
        return _FakeLogger()


class _FakeLegacyEditor:
    # Class-level call log: the edit now instantiates a fresh DataEditor()
    # inside edit_worker.run_edit, so a per-instance log would be invisible to
    # the test. Reset in setUp.
    calls = []

    def merge_datasets(self, paths, output):
        _FakeLegacyEditor.calls.append(('merge', list(paths), output))

    def delete_episode(self, path, idx):
        _FakeLegacyEditor.calls.append(('delete_one', path, idx))

    def delete_episodes_batch(self, path, indices):
        _FakeLegacyEditor.calls.append(('delete_batch', path, list(indices)))


class _EditRequest:
    def __init__(self, mode, merge_list=(), delete_path='', episodes=(),
                 output_path=''):
        self.mode = mode
        self.merge_dataset_list = list(merge_list)
        self.delete_dataset_path = delete_path
        self.delete_episode_num = list(episodes)
        self.output_path = output_path
        self.upload_huggingface = False


class _EditResponse:
    def __init__(self):
        self.success = False
        self.message = ''


def _install_ros_stubs():
    placeholder = type('_Placeholder', (), {})

    class _EditDatasetRequest:
        MERGE = 0
        DELETE = 1

    class _EditDataset:
        Request = _EditDatasetRequest

    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=placeholder)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=placeholder)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg',
          BrowserItem=placeholder, DatasetInfo=placeholder,
          TaskStatus=placeholder)
    _stub('physical_ai_interfaces.srv',
          BrowseFile=placeholder, EditDataset=_EditDataset,
          GetDatasetInfo=placeholder, GetImageTopicList=placeholder)
    _stub('rclpy')
    _stub('rclpy.node', Node=placeholder)
    _stub('rclpy.qos',
          DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL=1, VOLATILE=2),
          HistoryPolicy=types.SimpleNamespace(KEEP_LAST=1),
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2))
    _stub('rosbag_recorder')
    _stub('rosbag_recorder.srv', SendCommand=placeholder)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', CompressedImage=placeholder, JointState=placeholder)
    _stub('std_msgs')
    _stub('std_msgs.msg', Empty=placeholder, String=placeholder)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=placeholder)
    # Heavy in-package deps the callback never touches in these tests:
    _stub('physical_ai_server.communication')
    _stub('physical_ai_server.communication.multi_subscriber',
          MultiSubscriber=placeholder)
    _stub('physical_ai_server.data_processing.data_editor',
          DataEditor=_FakeLegacyEditor)
    _stub('physical_ai_server.utils')
    _stub('physical_ai_server.utils.file_browse_utils',
          FileBrowseUtils=placeholder)
    _stub('physical_ai_server.utils.parameter_utils',
          parse_topic_list=lambda *a, **k: [],
          parse_topic_list_with_names=lambda *a, **k: {})
    return _EditDataset


class DatasetEditCallbackRoutingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_lerobot_stubs()
        cls.v3 = _load_v3_module()
        cls.EditDataset = _install_ros_stubs()
        cls.worker = _load_worker_module()
        canonical = 'physical_ai_server.communication.communicator'
        spec = importlib.util.spec_from_file_location(
            canonical, str(COMMUNICATOR_PATH))
        module = importlib.util.module_from_spec(spec)
        sys.modules[canonical] = module
        spec.loader.exec_module(module)
        cls.communicator_mod = module

    def setUp(self):
        TOOLS.delete_calls.clear()
        TOOLS.merge_calls.clear()
        TOOLS.on_delete = None
        TOOLS.on_merge = None
        self.tmpdir = Path(tempfile.mkdtemp(prefix='v3route_'))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # Bypass __init__ (it wires real ROS services); set just the attrs the
        # callback touches. _edit_use_subprocess=False runs edit_worker.run_edit
        # in-process so the routing hits the stubbed v3 module + legacy editor.
        comm_cls = self.communicator_mod.Communicator
        self.comm = comm_cls.__new__(comm_cls)
        self.comm.node = _FakeNode()
        self.comm.data_editor = _FakeLegacyEditor()
        self.comm._edit_lock = threading.Lock()
        self.comm._edit_use_subprocess = False
        self.comm._edit_timeout_s = 3600
        _FakeLegacyEditor.calls = []

    def _call(self, request):
        return self.communicator_mod.Communicator.dataset_edit_callback(
            self.comm, request, _EditResponse())

    def test_v3_delete_routes_to_v3_path(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3)
        TOOLS.on_delete = (
            lambda d, i, o, r: _make_v3_tree(Path(o), 3 - len(list(i))))
        resp = self._call(_EditRequest(
            self.EditDataset.Request.DELETE, delete_path=str(src),
            episodes=[1]))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(TOOLS.delete_calls), 1, 'v3 path must be used')
        self.assertEqual(_FakeLegacyEditor.calls, [],
                         'legacy editor must NOT run on a v3.0 dataset')

    def test_v21_delete_routes_to_legacy_path(self):
        src = self.tmpdir / 'student' / 'ds21'
        _make_v21_tree(src, 3)
        resp = self._call(_EditRequest(
            self.EditDataset.Request.DELETE, delete_path=str(src),
            episodes=[1]))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(TOOLS.delete_calls, [],
                         'v3 path must NOT run on a v2.1 dataset')
        self.assertEqual(_FakeLegacyEditor.calls,
                         [('delete_one', str(src), 1)])

    def test_empty_delete_selection_is_german(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3)
        resp = self._call(_EditRequest(
            self.EditDataset.Request.DELETE, delete_path=str(src),
            episodes=[]))
        self.assertFalse(resp.success)
        self.assertIn('Keine Episoden', resp.message)

    def test_mixed_version_merge_rejected_german(self):
        a = self.tmpdir / 'student' / 'a3'
        b = self.tmpdir / 'student' / 'b21'
        _make_v3_tree(a, 2)
        _make_v21_tree(b, 2)
        resp = self._call(_EditRequest(
            self.EditDataset.Request.MERGE, merge_list=[str(a), str(b)],
            output_path=str(self.tmpdir / 'out')))
        self.assertFalse(resp.success)
        self.assertIn('unterschiedliche', resp.message)
        self.assertEqual(TOOLS.merge_calls, [])
        self.assertEqual(_FakeLegacyEditor.calls, [])

    def test_v3_merge_routes_to_v3_and_v21_merge_to_legacy(self):
        a = self.tmpdir / 'student' / 'a'
        b = self.tmpdir / 'student' / 'b'
        _make_v3_tree(a, 2)
        _make_v3_tree(b, 1)
        out = self.tmpdir / 'student' / 'merged'
        TOOLS.on_merge = (
            lambda ds, rid, o: _make_v3_tree(Path(o), 3))
        resp = self._call(_EditRequest(
            self.EditDataset.Request.MERGE, merge_list=[str(a), str(b)],
            output_path=str(out)))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(TOOLS.merge_calls), 1)
        self.assertEqual(_FakeLegacyEditor.calls, [])

        c = self.tmpdir / 'student' / 'c21'
        d = self.tmpdir / 'student' / 'd21'
        _make_v21_tree(c, 1)
        _make_v21_tree(d, 1)
        resp = self._call(_EditRequest(
            self.EditDataset.Request.MERGE, merge_list=[str(c), str(d)],
            output_path=str(self.tmpdir / 'out21')))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(TOOLS.merge_calls), 1, 'no new v3 merge call')
        self.assertEqual(
            _FakeLegacyEditor.calls,
            [('merge', [str(c), str(d)], str(self.tmpdir / 'out21'))])

    def test_dataediterror_maps_to_german_response(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3)
        resp = self._call(_EditRequest(
            self.EditDataset.Request.DELETE, delete_path=str(src),
            episodes=[0, 1, 2]))
        self.assertFalse(resp.success)
        self.assertIn('Alle Episoden', resp.message)
        self.assertNotIn('Error:', resp.message,
                         'must be the German DataEditError, not the generic')

    def test_v3_corrupt_info_delete_routes_to_v3_not_legacy(self):
        # The regression: a REAL v3.0 dataset with a truncated info.json. Under
        # the old routing this hit the DESTRUCTIVE legacy editor (English
        # FileNotFoundError on the single path; silent no-op + info.json
        # clobbered to {} + false success on the batch path). It must now route
        # to the v3 module: fail in German (beschädigt), never touch the legacy
        # editor, and leave the on-disk info.json byte-untouched.
        src = self.tmpdir / 'student' / 'ds'
        _make_corrupt_info_tree(src, 3)
        before = (src / 'meta' / 'info.json').read_text()

        resp = self._call(_EditRequest(
            self.EditDataset.Request.DELETE, delete_path=str(src),
            episodes=[1]))
        self.assertFalse(resp.success)
        self.assertIn('beschädigt', resp.message)
        self.assertNotIn('Error:', resp.message)
        self.assertEqual(_FakeLegacyEditor.calls, [],
                         'legacy editor must NOT run on a corrupt v3 dataset')
        self.assertEqual(TOOLS.delete_calls, [])

        # multi-episode (the old silent-no-op + info.json-clobber batch path)
        _FakeLegacyEditor.calls = []
        resp = self._call(_EditRequest(
            self.EditDataset.Request.DELETE, delete_path=str(src),
            episodes=[1, 2]))
        self.assertFalse(resp.success)
        self.assertIn('beschädigt', resp.message)
        self.assertEqual(_FakeLegacyEditor.calls, [])
        self.assertEqual((src / 'meta' / 'info.json').read_text(), before,
                         'corrupt info.json must be left byte-untouched')


if __name__ == '__main__':
    unittest.main()
