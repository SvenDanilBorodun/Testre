"""Client-supplied dataset paths are confined to the dataset root (2026-08-06).

The Daten tab has NO auth gate (only Training and Inferenz do) and reaches the
robot over an unauthenticated rosbridge, so every path below arrives from an
untrusted caller. Three surfaces used one verbatim:

  * ``edit_worker.run_edit`` — ``delete_dataset_path`` drove the DESTRUCTIVE
    episode delete, so any student could destroy any other's episodes;
    ``merge_dataset_list`` and ``output_path`` were equally unchecked and the
    output is WRITTEN to.
  * ``PhysicalAIServer.get_dataset_list_callback`` — ``root / user_id`` with a
    client-supplied ``user_id``, a directory-listing traversal.
  * ``FileBrowseUtils`` — no confinement at all; it walked to ``/``.

These tests are adversarial on purpose. The interesting cases are the ones a
`'..' in path` check would miss: an ABSOLUTE segment (pathlib DISCARDS the root
— ``Path('/a') / '/etc'`` is ``/etc``), a symlink planted inside the root, and a
sibling directory that merely shares a string prefix with the root.

Deps-free: dataset_paths is stdlib-only and edit_worker's heavy imports are
function-local, so this rides the existing robotis_ai_setup suite.
"""

import importlib.util
from pathlib import Path
import os
import shutil
import sys
import tempfile
import types
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
PATHS_PATH = PKG_ROOT / 'data_processing' / 'dataset_paths.py'
BROWSE_PATH = PKG_ROOT / 'utils' / 'file_browse_utils.py'


def _stub_pkg(name):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _load(canonical, path):
    """Load a module by path, never leaving a husk behind on failure."""
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(canonical, None)
        raise
    return module


def _load_dataset_paths():
    """Load dataset_paths under its CANONICAL name and attach it.

    Canonical, because ``edit_worker`` and ``file_browse_utils`` both do
    ``from physical_ai_server.data_processing import dataset_paths`` — and the
    stub package these tests install has no ``__path__``, so that import can
    only resolve via the ATTRIBUTE. Re-attached unconditionally: another test
    module may have (re)stubbed the package after we first ran.
    """
    _stub_pkg('physical_ai_server')
    _stub_pkg('physical_ai_server.data_processing')
    mod = _load('physical_ai_server.data_processing.dataset_paths', PATHS_PATH)
    sys.modules['physical_ai_server.data_processing'].dataset_paths = mod
    return mod


# file_browse_utils is loaded under a PRIVATE name, never its canonical one.
# test_data_editor_v3_gate.py stubs
# `physical_ai_server.utils.file_browse_utils` with a placeholder class, and
# `sys.modules` is process-global across a `discover` run — so under discover
# (the authoritative runner) the canonical name yields THAT placeholder and
# these tests would silently exercise a stub instead of the real confinement.
# Standalone they passed; that divergence is precisely the trap.
_BROWSE_PRIVATE_NAME = 'p0_confinement_private_file_browse_utils'


class _Rig(unittest.TestCase):
    """A dataset root with two students, plus traps outside it."""

    def setUp(self):
        self.dp = _load_dataset_paths()
        self.root = Path(tempfile.mkdtemp(prefix='dsroot_')).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / 'alice' / 'ds1').mkdir(parents=True)
        (self.root / 'bob' / 'ds2').mkdir(parents=True)

        self.outside = Path(tempfile.mkdtemp(prefix='outside_')).resolve()
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        (self.outside / 'secret').mkdir()

        # A directory that merely SHARES A STRING PREFIX with the root — the
        # case a `str.startswith` confinement gets wrong.
        self.sibling = Path(str(self.root) + '-evil')
        self.sibling.mkdir()
        self.addCleanup(shutil.rmtree, self.sibling, ignore_errors=True)

        # A symlink INSIDE the root pointing OUT of it.
        self.escape_link = self.root / 'alice' / 'escape'
        os.symlink(str(self.outside), str(self.escape_link))


class ConfinePrimitive(_Rig):

    def test_a_real_dataset_is_accepted(self):
        self.assertEqual(
            self.dp.confine(self.root / 'alice' / 'ds1', self.root),
            self.root / 'alice' / 'ds1')

    def test_a_path_that_does_not_exist_yet_is_accepted(self):
        # A merge OUTPUT legitimately does not exist. Confinement must not
        # become an existence check.
        self.assertEqual(
            self.dp.confine(self.root / 'alice' / 'new', self.root),
            self.root / 'alice' / 'new')

    def test_an_absolute_path_escapes_and_is_refused(self):
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine('/etc/passwd', self.root)

    def test_dotdot_is_refused(self):
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine(str(self.root) + '/alice/../../..', self.root)

    def test_a_symlink_pointing_out_of_the_root_is_refused(self):
        # Judged by where it LANDS, not how it is spelled.
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine(self.escape_link / 'secret', self.root)

    def test_a_string_prefix_sibling_is_refused(self):
        """`/tmp/dsroot_x-evil` must not pass as inside `/tmp/dsroot_x`."""
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine(self.sibling, self.root)

    def test_the_root_itself_is_refused_by_default(self):
        # An edit targeting the root would take every student's work.
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine(self.root, self.root)

    def test_the_root_is_allowed_only_when_explicitly_asked(self):
        # The file browser needs to LIST the root; edits must not target it.
        self.assertEqual(
            self.dp.confine(self.root, self.root, allow_root=True), self.root)

    def test_empty_and_none_are_refused(self):
        for bad in (None, '', '   '):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.confine(bad, self.root)

    def test_a_non_path_type_is_refused_rather_than_coerced(self):
        # str(['/etc']) is a path-shaped string; a malformed payload must not
        # be silently stringified into one.
        for bad in (['/etc'], 42, {'p': '/etc'}):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.confine(bad, self.root)

    def test_a_nul_byte_is_refused(self):
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine(str(self.root) + '/a\x00b', self.root)

    def test_the_refusal_message_is_german(self):
        try:
            self.dp.confine('/etc/passwd', self.root)
            self.fail('expected refusal')
        except self.dp.DatasetPathError as e:
            msg = str(e)
        # Rule §1: student-facing, literal umlauts (never ae/oe/ue/ss).
        self.assertIn('außerhalb', msg)
        self.assertNotIn('ausserhalb', msg)

    def test_the_refusal_does_not_echo_the_offending_path_back(self):
        # It is rendered in the browser; reflecting caller-supplied text there
        # is its own small problem.
        try:
            self.dp.confine('/etc/passwd', self.root)
            self.fail('expected refusal')
        except self.dp.DatasetPathError as e:
            self.assertNotIn('/etc/passwd', str(e))


class SafeChildPrimitive(_Rig):
    """The `root / client_string` shape used by get_dataset_list_callback.

    safe_child has TWO layers and mutation testing showed they divide the work
    differently than the test names suggest, so record it here rather than let
    a later reader assume:

      * the SEPARATOR check catches '/etc' and 'a/b' (both contain os.sep),
      * the `confine` call catches '..' (no separator, so it reaches the join).

    Reverting the confine call to the naive `Path(root) / name` is therefore
    killed by ``test_dotdot_as_a_user_id_is_refused`` and NOT by
    ``test_an_absolute_user_id_cannot_discard_the_root`` — both layers are
    load-bearing and neither is redundant.
    """

    def test_a_plain_user_id_is_accepted(self):
        self.assertEqual(
            self.dp.safe_child(self.root, 'alice'), self.root / 'alice')

    def test_an_absolute_user_id_cannot_discard_the_root(self):
        """THE bug: `Path(root) / '/etc'` == `/etc`."""
        self.assertEqual(Path(self.root) / '/etc', Path('/etc'))  # the footgun
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.safe_child(self.root, '/etc')

    def test_dotdot_as_a_user_id_is_refused(self):
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.safe_child(self.root, '..')

    def test_a_nested_user_id_is_refused(self):
        for bad in ('a/b', 'alice/../../etc'):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.safe_child(self.root, bad)

    def test_empty_none_and_non_str_are_refused(self):
        for bad in (None, '', '  ', 7, ['alice']):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.safe_child(self.root, bad)


class EditWorkerRefusesPathsOutsideTheRoot(_Rig):
    """run_edit is the single choke point shared by BOTH edit paths.

    Confining here rather than in the ROS callback is what makes the
    EDUBOTICS_DATASET_EDIT_SUBPROCESS=0 in-process rollback covered too.
    """

    def setUp(self):
        super().setUp()
        # data_editor_v3 has no heavy top-level imports; stub the two routing
        # functions so a refusal is provably a REFUSAL and not just a
        # downstream failure to find the tree.
        v3 = _load(
            'physical_ai_server.data_processing.data_editor_v3',
            PKG_ROOT / 'data_processing' / 'data_editor_v3.py')
        sys.modules['physical_ai_server.data_processing'].data_editor_v3 = v3
        self.worker = _load(
            'physical_ai_server.data_processing.edit_worker',
            PKG_ROOT / 'data_processing' / 'edit_worker.py')

        self.calls = []
        for name in ('delete_episodes_v3', 'merge_datasets_v3'):
            orig = getattr(v3, name)
            self.addCleanup(setattr, v3, name, orig)
        v3.delete_episodes_v3 = lambda *a, **k: self.calls.append(('del', a))
        v3.merge_datasets_v3 = lambda *a, **k: self.calls.append(('merge', a))
        orig_v3 = v3.is_v3_dataset
        orig_v21 = v3.is_v21_dataset
        self.addCleanup(setattr, v3, 'is_v3_dataset', orig_v3)
        self.addCleanup(setattr, v3, 'is_v21_dataset', orig_v21)
        v3.is_v3_dataset = lambda p: True
        v3.is_v21_dataset = lambda p: False

    def _delete(self, path):
        return self.worker.run_edit(
            {'mode': 'delete', 'delete_dataset_path': str(path),
             'delete_episode_num': [0]},
            root=self.root)

    def _merge(self, inputs, output):
        return self.worker.run_edit(
            {'mode': 'merge',
             'merge_dataset_list': [str(p) for p in inputs],
             'output_path': str(output)},
            root=self.root)

    def test_delete_inside_the_root_still_works(self):
        res = self._delete(self.root / 'alice' / 'ds1')
        self.assertTrue(res['success'], res)
        self.assertEqual([c[0] for c in self.calls], ['del'])

    def test_delete_outside_the_root_is_refused_and_never_reaches_the_editor(self):
        res = self._delete('/etc')
        self.assertFalse(res['success'])
        self.assertIn('außerhalb', res['message'])
        self.assertEqual(self.calls, [], 'the editor was invoked on a refused path')

    def test_delete_of_the_root_itself_is_refused(self):
        res = self._delete(self.root)
        self.assertFalse(res['success'])
        self.assertEqual(self.calls, [])

    def test_delete_via_dotdot_is_refused(self):
        res = self._delete(str(self.root / 'alice') + '/../../..')
        self.assertFalse(res['success'])
        self.assertEqual(self.calls, [])

    def test_delete_through_a_symlink_out_of_the_root_is_refused(self):
        res = self._delete(self.escape_link / 'secret')
        self.assertFalse(res['success'])
        self.assertEqual(self.calls, [])

    def test_merge_inside_the_root_still_works(self):
        res = self._merge([self.root / 'alice' / 'ds1', self.root / 'bob' / 'ds2'],
                          self.root / 'alice' / 'merged')
        self.assertTrue(res['success'], res)
        self.assertEqual([c[0] for c in self.calls], ['merge'])

    def test_EVERY_merge_input_is_checked_not_just_the_first(self):
        """A later escaping member would leak another tree into the output."""
        res = self._merge([self.root / 'alice' / 'ds1', '/etc'],
                          self.root / 'alice' / 'merged')
        self.assertFalse(res['success'])
        self.assertIn('außerhalb', res['message'])
        self.assertEqual(self.calls, [])

    def test_the_merge_OUTPUT_is_checked_too(self):
        """The output is WRITTEN to — an unchecked one is an arbitrary write."""
        res = self._merge([self.root / 'alice' / 'ds1'], '/etc/pwned')
        self.assertFalse(res['success'])
        self.assertIn('außerhalb', res['message'])
        self.assertEqual(self.calls, [])

    def test_the_root_is_never_taken_from_the_payload(self):
        """An attacker who could set the root would defeat the whole thing.

        run_edit reads it only from its own keyword argument, so a payload key
        of the same name must have no effect.
        """
        res = self.worker.run_edit(
            {'mode': 'delete', 'delete_dataset_path': '/etc',
             'delete_episode_num': [0], 'root': '/'},
            root=self.root)
        self.assertFalse(res['success'])
        self.assertEqual(self.calls, [])

    def test_production_callers_get_the_real_root_by_default(self):
        # No `root=` anywhere in the shipped call chain, so the default must be
        # the real dataset root rather than something permissive.
        import inspect
        sig = inspect.signature(self.worker.run_edit)
        self.assertIsNone(sig.parameters['root'].default)
        self.assertEqual(
            self.dp.dataset_root(),
            Path.home() / self.dp.DATASET_ROOT_RELATIVE)


class FileBrowserIsConfined(_Rig):

    def setUp(self):
        super().setUp()
        browse = _load(_BROWSE_PRIVATE_NAME, BROWSE_PATH)
        # Guard against silently testing a stub (see _BROWSE_PRIVATE_NAME).
        self.assertTrue(
            hasattr(browse.FileBrowseUtils, '_confine'),
            'loaded a stubbed FileBrowseUtils instead of the real module')
        self.fb = browse.FileBrowseUtils(root=self.root)

    def test_browsing_the_root_lists_the_student_folders(self):
        res = self.fb.handle_browse_action(str(self.root))
        self.assertTrue(res['success'], res)
        names = sorted(i['name'] for i in res['items'])
        self.assertEqual(names, ['alice', 'bob'])

    def test_an_empty_path_starts_at_the_root_not_at_HOME(self):
        res = self.fb.handle_get_path_action('')
        self.assertTrue(res['success'])
        self.assertEqual(Path(res['current_path']), self.root)

    def test_browsing_an_absolute_path_outside_the_root_is_refused(self):
        res = self.fb.handle_browse_action('/etc')
        self.assertFalse(res['success'])
        self.assertEqual(res['items'], [])

    def test_go_parent_CLAMPS_at_the_root_instead_of_walking_to_slash(self):
        """One call at a time was how the whole filesystem got enumerated."""
        cur = str(self.root)
        for _ in range(6):
            res = self.fb.handle_go_parent_action(cur)
            cur = res['current_path'] or cur
            self.assertEqual(Path(cur), self.root)

    def test_the_reported_parent_never_points_above_the_root(self):
        res = self.fb.handle_browse_action(str(self.root))
        self.assertEqual(Path(res['parent_path']), self.root)

    def test_an_absolute_target_name_cannot_discard_the_current_path(self):
        """os.path.join(cur, '/etc') == '/etc' — the same footgun."""
        res = self.fb.handle_browse_action(str(self.root), '/etc')
        self.assertFalse(res['success'])

    def test_a_dotdot_target_name_is_refused(self):
        res = self.fb.handle_browse_action(str(self.root / 'alice'), '..')
        self.assertFalse(res['success'])

    def test_following_a_symlink_out_of_the_root_is_refused(self):
        res = self.fb.handle_browse_action(str(self.escape_link))
        self.assertFalse(res['success'])

    def test_hidden_entries_are_not_listed_any_more(self):
        """The old `except .cache` exception exposed the HF token directory."""
        (self.root / '.hidden').mkdir()
        (self.root / '.cache').mkdir()
        res = self.fb.handle_browse_action(str(self.root))
        names = {i['name'] for i in res['items']}
        self.assertNotIn('.hidden', names)
        self.assertNotIn('.cache', names)
        self.assertEqual(names, {'alice', 'bob'})

    def test_go_parent_with_target_check_is_confined_too(self):
        res = self.fb.handle_go_parent_with_target_check('/etc', None, None)
        self.assertFalse(res['success'])

    def test_browse_with_target_check_is_confined_too(self):
        res = self.fb.handle_browse_with_target_check('/etc', None, None, None)
        self.assertFalse(res['success'])


if __name__ == '__main__':
    unittest.main()
