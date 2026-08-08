"""edit_worker contract: routing + subprocess marshalling (deps-free).

Locks the 2026-06-07 fix that moved dataset edits (Daten-tab delete/merge) out
of the synchronous /dataset/edit ROS callback into a nice'd subprocess so a slow
AV1 re-encode can no longer CPU-starve the executor and freeze the dashboard
(node-default service calls timing out). See
``docs/plans/2026-06-07-dataset-edit-cpu-isolation.md``.

Runs WITHOUT lerobot/torch/rclpy: data_editor_v3 has no top-level heavy imports
(lerobot is function-local), and these tests monkeypatch its routing functions,
so the real upstream is never reached. Same sys.modules-stub pattern as
test_data_editor_v3_gate.py.

Covered:
  1. run_edit routes delete/merge to the v3 vs legacy editor exactly as the old
     inline callback did, and maps DataEditError -> German {success: False}.
  2. build_command emits the nice-19 `python -m …edit_worker` prefix.
  3. parse_output extracts the LAST RESULT_MARKER line (and tolerates noise /
     malformed / absent markers).
  4. main() reads stdin, runs, and emits exactly one machine-readable marker
     line with the right exit code.
"""

import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
V3_PATH = PKG_ROOT / 'data_processing' / 'data_editor_v3.py'
WORKER_PATH = PKG_ROOT / 'data_processing' / 'edit_worker.py'
DATASET_PATHS_PATH = PKG_ROOT / 'data_processing' / 'dataset_paths.py'


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _load_module(canonical, path):
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Never leave a half-built husk registered: `sys.modules` is global
        # for the whole `discover` run and the short-circuit above would hand
        # this husk to every LATER test module, turning ONE honest ImportError
        # into ~30 misleading AttributeErrors in setUp — and making the file
        # GREEN standalone but RED under `discover`. See the twin note in
        # test_data_editor_v3_gate.py::_exec_or_unregister.
        sys.modules.pop(canonical, None)
        raise
    return module


class _FakeDataEditor:
    """Stands in for the legacy v2.1 DataEditor; records calls."""

    calls = []

    def merge_datasets(self, dataset_list, output_path):
        _FakeDataEditor.calls.append(('merge', list(dataset_list), output_path))

    def delete_episodes_batch(self, path, nums):
        _FakeDataEditor.calls.append(('delete_batch', path, list(nums)))

    def delete_episode(self, path, num):
        _FakeDataEditor.calls.append(('delete_single', path, num))


class EditWorkerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _stub('physical_ai_server')
        _stub('physical_ai_server.data_processing')
        cls.v3 = _load_module(
            'physical_ai_server.data_processing.data_editor_v3', V3_PATH)
        sys.modules['physical_ai_server.data_processing'].data_editor_v3 = cls.v3
        # Legacy editor module the worker imports lazily on the v2.1 path.
        _stub('physical_ai_server.data_processing.data_editor',
              DataEditor=_FakeDataEditor)
        # edit_worker imports dataset_paths at module level for the 2026-08-06
        # path confinement; the stub package has no __path__, so it must be an
        # ATTRIBUTE before edit_worker is exec'd.
        cls.dataset_paths = _load_module(
            'physical_ai_server.data_processing.dataset_paths', DATASET_PATHS_PATH)
        sys.modules['physical_ai_server.data_processing'].dataset_paths = (
            cls.dataset_paths)
        cls.worker = _load_module(
            'physical_ai_server.data_processing.edit_worker', WORKER_PATH)

    def setUp(self):
        _FakeDataEditor.calls.clear()
        # Dataset paths are now ROOT-CONFINED (2026-08-06), so these fixtures
        # live under a throwaway root passed explicitly. `root` is a parameter
        # rather than an env var precisely so tests can relocate it while
        # nothing reachable from the wire can — see dataset_paths.
        self.root = Path(tempfile.mkdtemp(prefix='editworker_')).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.DS = str(self.root / 'u' / 'ds')
        self.OLD = str(self.root / 'u' / 'old')
        self.GONE = str(self.root / 'u' / 'gone')
        self.A = str(self.root / 'u' / 'a')
        self.B = str(self.root / 'u' / 'b')
        self.OUT = str(self.root / 'u' / 'out')
        # Reset routing hooks to inert defaults each test. The legacy editor is
        # reached ONLY when is_v21_dataset is positively True; everything else
        # (v3 / missing / corrupt) routes to the v3 module.
        self.v3.is_v3_dataset = lambda p: False
        self.v3.is_v21_dataset = lambda p: False
        self.v3.dataset_dir_missing = lambda p: False
        self.v3.delete_episodes_v3 = mock.Mock(name='delete_episodes_v3')
        self.v3.merge_datasets_v3 = mock.Mock(name='merge_datasets_v3')

    def _run(self, payload):
        """run_edit against this test's confined root."""
        return self.worker.run_edit(payload, root=self.root)

    # ---- run_edit routing -------------------------------------------------------------

    def test_delete_empty_is_german_and_skips_upstream(self):
        self.v3.is_v3_dataset = lambda p: True
        res = self._run(
            {'mode': 'delete', 'delete_dataset_path': 'x', 'delete_episode_num': []})
        self.assertFalse(res['success'])
        self.assertIn('Keine Episoden', res['message'])
        self.v3.delete_episodes_v3.assert_not_called()

    def test_delete_v3_routes_to_v3_editor(self):
        self.v3.is_v3_dataset = lambda p: True
        res = self._run(
            {'mode': 'delete', 'delete_dataset_path': self.DS, 'delete_episode_num': [1]})
        self.assertTrue(res['success'])
        self.v3.delete_episodes_v3.assert_called_once()
        args, _ = self.v3.delete_episodes_v3.call_args
        self.assertEqual(args[0], self.DS)
        self.assertEqual(args[1], [1])
        self.assertEqual(_FakeDataEditor.calls, [])

    def test_delete_missing_path_routes_to_v3_for_german_error(self):
        # A missing path is not positively v2.1 (is_v21=False), so it routes to
        # the v3 module, which raises the German 'nicht gefunden' DataEditError.
        self.v3.is_v21_dataset = lambda p: False
        self._run(
            {'mode': 'delete', 'delete_dataset_path': self.GONE, 'delete_episode_num': [0]})
        self.v3.delete_episodes_v3.assert_called_once()
        self.assertEqual(_FakeDataEditor.calls, [])

    def test_delete_v21_single_routes_to_legacy(self):
        self.v3.is_v21_dataset = lambda p: True
        res = self._run(
            {'mode': 'delete', 'delete_dataset_path': self.OLD, 'delete_episode_num': [2]})
        self.assertTrue(res['success'])
        self.assertEqual(_FakeDataEditor.calls, [('delete_single', self.OLD, 2)])
        self.v3.delete_episodes_v3.assert_not_called()

    def test_delete_v21_batch_routes_to_legacy_batch(self):
        self.v3.is_v21_dataset = lambda p: True
        self._run(
            {'mode': 'delete', 'delete_dataset_path': self.OLD, 'delete_episode_num': [1, 2]})
        self.assertEqual(_FakeDataEditor.calls, [('delete_batch', self.OLD, [1, 2])])
        self.v3.delete_episodes_v3.assert_not_called()

    def test_delete_non_v21_batch_never_routes_to_legacy(self):
        # The destructive legacy batch editor is reachable ONLY for a positively
        # v2.1 dataset. A v3 / missing / corrupt-info dataset (is_v21=False)
        # routes to v3 even for a multi-episode delete — the old code sent these
        # to the silent-no-op + info.json-clobbering legacy batch path.
        self.v3.is_v21_dataset = lambda p: False
        self._run(
            {'mode': 'delete', 'delete_dataset_path': self.DS, 'delete_episode_num': [1, 2]})
        self.v3.delete_episodes_v3.assert_called_once()
        self.assertEqual(_FakeDataEditor.calls, [])

    def test_merge_all_v3_routes_to_v3(self):
        self.v3.is_v3_dataset = lambda p: True
        res = self._run(
            {'mode': 'merge', 'merge_dataset_list': [self.A, self.B], 'output_path': self.OUT})
        self.assertTrue(res['success'])
        self.v3.merge_datasets_v3.assert_called_once()

    def test_merge_mixed_versions_refused_in_german(self):
        flags = {self.A: True, self.B: False}
        self.v3.is_v3_dataset = lambda p: flags[p]
        res = self._run(
            {'mode': 'merge', 'merge_dataset_list': [self.A, self.B], 'output_path': self.OUT})
        self.assertFalse(res['success'])
        self.assertIn('unterschiedliche', res['message'])
        self.v3.merge_datasets_v3.assert_not_called()
        self.assertEqual(_FakeDataEditor.calls, [])

    def test_merge_all_v21_routes_to_legacy(self):
        self.v3.is_v21_dataset = lambda p: True
        res = self._run(
            {'mode': 'merge', 'merge_dataset_list': [self.A, self.B], 'output_path': self.OUT})
        self.assertTrue(res['success'])
        self.assertEqual(_FakeDataEditor.calls, [('merge', [self.A, self.B], self.OUT)])

    def test_merge_all_corrupt_refused_not_legacy(self):
        # All members unreadable -> neither positively v3 nor positively v2.1.
        # Must be refused in German, never silently legacy-merged into a broken
        # output reported as success.
        res = self._run(
            {'mode': 'merge', 'merge_dataset_list': [self.A, self.B], 'output_path': self.OUT})
        self.assertFalse(res['success'])
        self.assertIn('beschädigt', res['message'])
        self.v3.merge_datasets_v3.assert_not_called()
        self.assertEqual(_FakeDataEditor.calls, [])

    def test_data_edit_error_maps_to_german_failure(self):
        self.v3.is_v3_dataset = lambda p: True
        self.v3.delete_episodes_v3 = mock.Mock(
            side_effect=self.v3.DataEditError('Deutsche Fehlermeldung'))
        res = self._run(
            {'mode': 'delete', 'delete_dataset_path': self.DS, 'delete_episode_num': [0]})
        self.assertFalse(res['success'])
        self.assertEqual(res['message'], 'Deutsche Fehlermeldung')

    def test_unexpected_error_is_caught(self):
        self.v3.is_v3_dataset = lambda p: True
        self.v3.delete_episodes_v3 = mock.Mock(side_effect=ValueError('boom'))
        res = self._run(
            {'mode': 'delete', 'delete_dataset_path': self.DS, 'delete_episode_num': [0]})
        self.assertFalse(res['success'])
        self.assertIn('boom', res['message'])

    def test_unknown_mode(self):
        res = self._run({'mode': 'frobnicate'})
        self.assertFalse(res['success'])
        self.assertIn('Unknown edit mode', res['message'])

    # ---- build_command ----------------------------------------------------------------

    def test_build_command_is_nice_19_module(self):
        cmd = self.worker.build_command('/usr/bin/python3')
        self.assertEqual(
            cmd,
            ['nice', '-n', '19', '/usr/bin/python3', '-m',
             'physical_ai_server.data_processing.edit_worker'],
        )

    def test_build_command_nice_override(self):
        cmd = self.worker.build_command('/py', nice_level='10')
        self.assertEqual(cmd[:3], ['nice', '-n', '10'])

    # ---- parse_output -----------------------------------------------------------------

    def test_parse_output_extracts_marker_amid_noise(self):
        stdout = (
            'Processing observation.images.scene: 100%\n'
            'Svt[info]: SVT-AV1 Encoder\n'
            + self.worker.RESULT_MARKER
            + json.dumps({'success': True, 'message': 'ok'}) + '\n'
        )
        self.assertEqual(
            self.worker.parse_output(stdout), {'success': True, 'message': 'ok'})

    def test_parse_output_last_marker_wins(self):
        stdout = (
            self.worker.RESULT_MARKER + json.dumps({'success': False, 'message': 'a'}) + '\n'
            + self.worker.RESULT_MARKER + json.dumps({'success': True, 'message': 'b'}) + '\n'
        )
        self.assertEqual(
            self.worker.parse_output(stdout), {'success': True, 'message': 'b'})

    def test_parse_output_absent_and_malformed_return_none(self):
        self.assertIsNone(self.worker.parse_output('only noise\nmore noise'))
        self.assertIsNone(self.worker.parse_output(self.worker.RESULT_MARKER + 'not-json'))
        self.assertIsNone(self.worker.parse_output(''))

    # ---- main() round-trip ------------------------------------------------------------

    def _run_main(self, stdin_text):
        out = io.StringIO()
        with mock.patch.object(sys, 'stdin', io.StringIO(stdin_text)), \
                mock.patch.object(sys, 'stdout', out):
            rc = self.worker.main()
        return rc, out.getvalue()

    def test_main_success_emits_marker_rc0(self):
        with mock.patch.object(
                self.worker, 'run_edit',
                return_value={'success': True, 'message': 'done'}):
            rc, out = self._run_main(json.dumps({'mode': 'delete'}))
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.worker.parse_output(out), {'success': True, 'message': 'done'})

    def test_main_failure_rc1(self):
        with mock.patch.object(
                self.worker, 'run_edit',
                return_value={'success': False, 'message': 'nope'}):
            rc, out = self._run_main(json.dumps({'mode': 'delete'}))
        self.assertEqual(rc, 1)
        self.assertEqual(self.worker.parse_output(out)['success'], False)

    def test_main_invalid_payload_rc2_still_marks(self):
        rc, out = self._run_main('this is not json')
        self.assertEqual(rc, 2)
        result = self.worker.parse_output(out)
        self.assertIsNotNone(result)
        self.assertFalse(result['success'])


if __name__ == '__main__':
    unittest.main()
