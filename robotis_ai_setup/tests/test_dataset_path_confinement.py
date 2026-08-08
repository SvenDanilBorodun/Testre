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
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

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

    def test_browse_with_target_check_confines_its_CURRENT_PATH(self):
        res = self.fb.handle_browse_with_target_check('/etc', None, None, None)
        self.assertFalse(res['success'])

    # ── the target-check twin's own join (found by adversarial review) ──────
    #
    # The first pass confined `_handle_target_selection` but MISSED the
    # near-identical join inside `handle_browse_with_target_check`. That twin is
    # the MORE exposed one: useRosServiceCaller.js always sends
    # target_files/target_folders, so browse_file_callback routes
    # `action='browse'` here rather than to the sibling.
    #
    # The original test passed target_name=None, so it never entered the
    # vulnerable branch — it asserted the current_path confinement and was
    # NAMED as though it covered both. These cases pass a real target_name.

    def test_an_ABSOLUTE_target_name_in_the_target_check_twin_is_refused(self):
        res = self.fb.handle_browse_with_target_check(
            str(self.root), '/etc', {'hosts'}, None)
        self.assertFalse(
            res['success'],
            'absolute target_name escaped the target-check twin — this is the '
            'wire-reachable path the React client actually uses')
        self.assertEqual(res['items'], [])

    def test_a_dotdot_target_name_in_the_target_check_twin_is_refused(self):
        res = self.fb.handle_browse_with_target_check(
            str(self.root / 'alice'), '../..', {'x'}, None)
        self.assertFalse(res['success'])

    def test_an_absolute_target_name_cannot_reach_the_HF_TOKEN_directory(self):
        """The concrete harm: `.cache/huggingface` holds the login token."""
        token_dir = self.outside / 'huggingface'
        token_dir.mkdir()
        (token_dir / 'token').write_text('hf_secret')
        res = self.fb.handle_browse_with_target_check(
            str(self.root), str(token_dir), {'token'}, None)
        self.assertFalse(res['success'])
        listed = {i['name'] for i in res['items']}
        self.assertNotIn('token', listed)

    def test_a_legitimate_target_name_still_navigates(self):
        res = self.fb.handle_browse_with_target_check(
            str(self.root), 'alice', None, None)
        self.assertTrue(res['success'], res)
        self.assertEqual(pathlib.Path(res['current_path']), self.root / 'alice')

    def test_no_handler_reports_a_parent_above_the_root(self):
        """A raw os.path.dirname leaked one level above the root as a string."""
        for res in (
            self.fb.handle_get_path_action(str(self.root)),
            self.fb.handle_browse_action(str(self.root)),
            self.fb.handle_go_parent_action(str(self.root)),
            self.fb.handle_browse_with_target_check(str(self.root), None, {'x'}, None),
            self.fb.handle_go_parent_with_target_check(str(self.root), {'x'}, None),
        ):
            parent = res.get('parent_path') or str(self.root)
            self.assertTrue(
                pathlib.Path(parent) == self.root
                or self.root in pathlib.Path(parent).parents,
                f'a handler reported {parent!r}, above the root')


class TheBrowserAlsoServesTheModelCheckpointRoot(_Rig):
    """Availability: confining to the DATASET root alone broke „Modellpfad".

    React opens this browser from two entry points with two different seeds —
    `DEFAULT_PATHS.DATASET_PATH` and `DEFAULT_PATHS.POLICY_MODEL_PATH`, the
    latter being lerobot's `outputs/train`, which is NOT under the dataset
    root. Found by an adversarial review of the first pass.
    """

    def setUp(self):
        super().setUp()
        browse = _load(_BROWSE_PRIVATE_NAME, BROWSE_PATH)
        self.models = pathlib.Path(tempfile.mkdtemp(prefix='models_')).resolve()
        self.addCleanup(shutil.rmtree, self.models, ignore_errors=True)
        (self.models / 'act_run1').mkdir()
        self.fb = browse.FileBrowseUtils(roots=[self.root, self.models])

    def test_the_dataset_root_is_still_browsable(self):
        res = self.fb.handle_browse_action(str(self.root))
        self.assertTrue(res['success'], res)

    def test_the_model_root_is_browsable_too(self):
        res = self.fb.handle_browse_action(str(self.models))
        self.assertTrue(res['success'], res)
        self.assertIn('act_run1', {i['name'] for i in res['items']})

    def test_a_path_under_NEITHER_root_is_still_refused(self):
        res = self.fb.handle_browse_action(str(self.outside))
        self.assertFalse(res['success'])

    def test_go_parent_clamps_to_the_root_that_contains_the_path(self):
        """Not back to the dataset tree — that would be a confusing jump."""
        res = self.fb.handle_go_parent_action(str(self.models / 'act_run1'))
        self.assertEqual(pathlib.Path(res['current_path']), self.models)
        again = self.fb.handle_go_parent_action(str(self.models))
        self.assertEqual(pathlib.Path(again['current_path']), self.models)

    def test_an_EMPTY_root_list_refuses_everything_rather_than_allowing_all(self):
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine_any(str(self.root / 'alice' / 'ds1'), [])
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.confine_any(str(self.root / 'alice' / 'ds1'), None)


class SafeUnderPrimitive(_Rig):
    """The `root / <multi-component client string>` shape.

    ``get_training_info_callback`` builds
    ``get_weight_save_root_path() / request.train_config_path`` and ``open()``s
    the result. The commit that confined "EVERY client-supplied dataset path"
    edited that same file and MISSED it — it is not a dataset path, it is a
    model path, so it fell outside the sweep.

    :func:`safe_child` is the wrong primitive here: a legitimate value is
    ``<model>/pretrained_model/train_config.json``, and safe_child refuses every
    separator. Hence a sibling with the same guarantees and one fewer rule.
    """

    def test_a_plain_relative_path_is_accepted(self):
        self.assertEqual(
            self.dp.safe_under(self.root, 'act_run1/train_config.json'),
            self.root / 'act_run1' / 'train_config.json')

    def test_a_deep_relative_path_is_accepted(self):
        """The real shape — safe_child would refuse this."""
        rel = 'act_run1/pretrained_model/train_config.json'
        self.assertEqual(
            self.dp.safe_under(self.root, rel), self.root / rel)
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.safe_child(self.root, rel)

    def test_an_absolute_path_cannot_discard_the_root(self):
        """THE bug: `Path(root) / '/etc/passwd'` == `/etc/passwd`."""
        self.assertEqual(
            Path(self.root) / '/etc/passwd', Path('/etc/passwd'))  # the footgun
        for bad in ('/etc/passwd', '/root/.env', '/'):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.safe_under(self.root, bad)

    def test_an_absolute_path_INSIDE_the_root_is_accepted(self):
        """Deliberate, and the R2 lesson: judge where it LANDS, not its spelling.

        The file browser hands back absolute paths, so refusing them for being
        absolute is how a confinement turns into a self-inflicted refusal —
        exactly what „Modellpfad auswählen" did when the browsable set and
        React's seed named different directories. An earlier draft of
        safe_under had an explicit `os.path.isabs` refusal; it was removed
        because it is redundant against `confine` for every ESCAPING input and
        adds a new refusal for a legitimate one.
        """
        target = self.root / 'act_run1' / 'train_config.json'
        self.assertEqual(self.dp.safe_under(self.root, str(target)), target)

    def test_dotdot_walks_out_and_is_refused(self):
        for bad in ('..', '../../etc/passwd', 'a/../../../etc/passwd',
                    'a/b/../../..'):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.safe_under(self.root, bad)

    def test_a_symlink_out_of_the_root_is_refused(self):
        link = self.root / 'escape'
        try:
            os.symlink(str(self.outside), str(link))
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this host')
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.safe_under(self.root, 'escape/secret.json')

    def test_the_root_itself_is_refused(self):
        # It is a directory; `open()` on it raises IsADirectoryError, and a
        # config file is never the root.
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.safe_under(self.root, '.')

    def test_empty_none_non_str_and_NUL_are_refused(self):
        for bad in (None, '', '   ', 7, ['a'], 'a\x00b'):
            with self.assertRaises(self.dp.DatasetPathError):
                self.dp.safe_under(self.root, bad)

    def test_the_refusal_never_echoes_the_offending_path(self):
        # It is surfaced in the browser; reflecting an arbitrary caller string
        # into the UI is its own small problem.
        try:
            self.dp.safe_under(self.root, '/etc/passwd')
        except self.dp.DatasetPathError as e:
            self.assertNotIn('/etc/passwd', str(e))

    def test_a_sibling_sharing_a_string_prefix_is_refused(self):
        # `startswith` on the string form would accept this; the component-wise
        # ancestor test does not.
        sibling = self.root.parent / (self.root.name + '-evil')
        sibling.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, sibling, ignore_errors=True)
        with self.assertRaises(self.dp.DatasetPathError):
            self.dp.safe_under(self.root, f'../{sibling.name}/x.json')


class TheTrainingInfoCallbackRoutesItsPathThroughDatasetPaths(unittest.TestCase):
    """The CALL SITE, asserted structurally.

    ``get_training_info_callback`` lives on ``PhysicalAIServer``, which needs
    rclpy and the compiled interfaces — neither is available in this deps-free
    suite, and neither is available to CI's python-tests job either. So the
    primitive is tested behaviourally above and the WIRING is read off the AST:
    the value handed to ``open()`` must come from ``dataset_paths``, and the
    naive join must not survive anywhere in the function.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        cls.ast = ast
        src = (PKG_ROOT / 'physical_ai_server.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        cls.fn = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == 'get_training_info_callback'):
                cls.fn = node
        if cls.fn is None:
            raise AssertionError(
                'get_training_info_callback not found — either it was renamed '
                'or this test is stale, and both mean nothing is checked')

    def _body(self):
        # Docstring dropped: ast.unparse keeps it, and this function's comments
        # and docstring describe the very expression the assertions ban.
        body = list(self.fn.body)
        if (body and isinstance(body[0], self.ast.Expr)
                and isinstance(body[0].value, self.ast.Constant)):
            body = body[1:]
        return '\n'.join(self.ast.unparse(s) for s in body)

    def test_the_opened_path_is_produced_by_dataset_paths(self):
        body = self._body()
        self.assertIn('dataset_paths.safe_under(', body)
        self.assertIn('DatasetPathError', body)

    def test_the_naive_join_is_gone(self):
        body = self._body()
        for naive in (
            'weight_save_root_path / train_config_path',
            'weight_save_root_path / request.train_config_path',
        ):
            self.assertNotIn(
                naive, body,
                'the client path is joined onto the model root without '
                'confinement — pathlib DISCARDS the root when the right-hand '
                'side is absolute, so open() reads any file in the container')

    def test_open_is_called_on_the_confined_variable(self):
        """Not on `request.…` directly, and not on a re-derived join."""
        opened = []
        for node in self.ast.walk(self.fn):
            if (isinstance(node, self.ast.Call)
                    and isinstance(node.func, self.ast.Name)
                    and node.func.id == 'open'):
                opened.append(self.ast.unparse(node.args[0]))
        self.assertTrue(opened, 'nothing is opened any more — test is stale')
        for arg in opened:
            self.assertNotIn('request.', arg)
        # And the variable it opens is the one assigned from safe_under.
        assigned = set()
        for node in self.ast.walk(self.fn):
            if isinstance(node, self.ast.Assign) and 'safe_under' in self.ast.unparse(node.value):
                assigned.update(
                    t.id for t in node.targets if isinstance(t, self.ast.Name))
        self.assertTrue(
            assigned, 'nothing is assigned from dataset_paths.safe_under')
        self.assertTrue(
            set(opened) & assigned,
            f'open() is called on {opened}, none of which is the confined '
            f'{sorted(assigned)}')


def _fn_body_src(path, name):
    """AST body of a function, docstring stripped.

    Same technique as the training-info fence above and for the same reason:
    these callbacks live on classes needing rclpy + the compiled interfaces,
    which neither this deps-free suite nor CI's python-tests job can import. The
    docstring is dropped because ``ast.unparse`` keeps it and several of these
    comments name the very expressions the assertions ban.
    """
    import ast
    tree = ast.parse(path.read_text(encoding='utf-8'))
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            fn = node
    if fn is None:
        raise AssertionError(
            f'{name} not found in {path.name} — either it was renamed or this '
            f'test is stale, and both mean nothing is checked')
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        body = body[1:]
    return '\n'.join(ast.unparse(s) for s in body)


class TheThreeRemainingClientPathsAreConfined(unittest.TestCase):
    """The paths the 2026-08-06 sweep framed itself out of seeing.

    That sweep was scoped to "every client-supplied DATASET path", so three
    values on the same unauthenticated rosbridge fell outside its own framing —
    the identical blind spot that already cost `get_training_info_callback` one
    round. Each was proven reachable by execution on 2026-08-08:

      * ``ControlHfServer.local_dir`` — `upload_huggingface_repo` opens with an
        ``shutil.rmtree`` of ``<local_dir>/.cache``. Run in the SHIPPED image,
        ``local_dir='/root'`` deleted every recorded dataset and the
        huggingface-cli token, surfacing only an unrelated German sync error.
      * ``GetDatasetInfo.dataset_path`` — arbitrary ``<dir>/meta/info.json``
        read plus a directory-existence oracle.
      * ``TaskInfo.policy_path`` — arbitrary ``<dir>/config.json`` read, with
        an attacker-planted field echoed into a student toast.
    """

    def test_the_hf_upload_local_dir_is_confined(self):
        body = _fn_body_src(PKG_ROOT / 'physical_ai_server.py',
                            'control_hf_server_callback')
        self.assertIn('dataset_paths.confine_any(', body)
        self.assertIn('DatasetPathError', body)

    def test_the_local_dir_confine_is_scoped_to_the_upload_mode(self):
        """Availability half: only 'upload' reads local_dir.

        `hf_api_worker` passes just repo_id/repo_type to download, delete and
        both list modes, so those senders leave local_dir EMPTY. An
        unconditional confine refuses them all — the same self-inflicted
        regression that broke „Modellpfad auswählen" twice.
        """
        body = _fn_body_src(PKG_ROOT / 'physical_ai_server.py',
                            'control_hf_server_callback')
        guard = "if mode == 'upload':"
        self.assertIn(guard, body)
        after_guard = body.split(guard, 1)[1]
        self.assertIn('confine_any(', after_guard,
                      'the confine escaped its upload-only guard')

    def test_the_worker_receives_the_confined_value_not_the_request(self):
        import ast
        src = (PKG_ROOT / 'physical_ai_server.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'control_hf_server_callback')
        # The dict handed to the worker must carry the local NAME, which the
        # confine reassigns — never `request.local_dir` read a second time.
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == 'local_dir':
                        self.assertNotIn('request.', ast.unparse(v))

    def test_the_dataset_info_path_is_confined(self):
        body = _fn_body_src(PKG_ROOT / 'communication' / 'communicator.py',
                            'get_dataset_info_callback')
        self.assertIn('dataset_paths.confine(', body)
        self.assertIn('DatasetPathError', body)

    def test_the_dataset_info_path_never_reaches_the_editor_raw(self):
        import ast
        src = (PKG_ROOT / 'communication' / 'communicator.py').read_text(
            encoding='utf-8')
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'get_dataset_info_callback')
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and 'get_dataset_info' in ast.unparse(node.func)):
                for arg in node.args:
                    self.assertNotIn('request.', ast.unparse(arg))

    def test_the_policy_path_is_confined_to_the_policy_roots(self):
        body = _fn_body_src(PKG_ROOT / 'inference' / 'inference_manager.py',
                            'validate_policy')
        self.assertIn('dataset_paths.confine_any(', body)
        self.assertIn('dataset_paths.policy_roots()', body)
        self.assertNotIn('browsable_roots', body)

    def test_the_policy_confine_runs_before_any_filesystem_call(self):
        """Order is the property: a later confine still leaks the oracle."""
        body = _fn_body_src(PKG_ROOT / 'inference' / 'inference_manager.py',
                            'validate_policy')
        confine_at = body.index('confine_any(')
        first_fs = min(
            (body.index(tok) for tok in ('os.path.exists', 'os.path.isdir')
             if tok in body),
            default=-1)
        self.assertGreater(first_fs, -1, 'no filesystem call left — test stale')
        self.assertLess(confine_at, first_fs)

    def test_the_policy_refusals_no_longer_echo_the_caller_path(self):
        body = _fn_body_src(PKG_ROOT / 'inference' / 'inference_manager.py',
                            'validate_policy')
        for echo in ('{policy_path}', '{self.policy_path}'):
            self.assertNotIn(
                echo, body,
                'a refusal interpolates the caller-supplied path back into a '
                'student-facing German toast')


class ThePolicyRootsAreTheOnesPoliciesActuallyLiveIn(unittest.TestCase):
    """`policy_roots` is not `browsable_roots`, and that is load-bearing.

    `inference_manager.get_saved_policies` enumerates
    ``~/.cache/huggingface/hub/models--*/snapshots/*/pretrained_model`` and
    feeds those exact strings to the React dropdown. Confining `policy_path` to
    the browsable pair would refuse every policy a student has — a security fix
    that bricks inference is not a fix.
    """

    def setUp(self):
        self.dp = _load_dataset_paths()

    def test_the_hub_cache_is_a_policy_root(self):
        roots = [str(r) for r in self.dp.policy_roots()]
        self.assertTrue(any(r.endswith('.cache/huggingface/hub') for r in roots),
                        roots)

    def test_the_model_root_is_a_policy_root(self):
        roots = [str(r) for r in self.dp.policy_roots()]
        self.assertIn(str(self.dp.model_root()), roots)

    def test_a_real_saved_policy_shape_is_accepted(self):
        # The exact shape get_saved_policies hands back.
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td).resolve()
            with mock.patch.object(pathlib.Path, 'home', staticmethod(lambda: home)):
                good = (home / self.dp.HF_HUB_CACHE_RELATIVE
                        / 'models--acme--act' / 'snapshots' / 'abc123'
                        / 'pretrained_model')
                good.mkdir(parents=True)
                self.assertEqual(
                    self.dp.confine_any(str(good), self.dp.policy_roots()),
                    good.resolve())

    def test_a_path_outside_every_policy_root_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td).resolve()
            with mock.patch.object(pathlib.Path, 'home', staticmethod(lambda: home)):
                with self.assertRaises(self.dp.DatasetPathError):
                    self.dp.confine_any('/etc/ssh', self.dp.policy_roots())

    def test_the_dataset_root_is_NOT_a_policy_root(self):
        # A policy is never a recorded dataset; a wider allowlist buys nothing.
        roots = [str(r) for r in self.dp.policy_roots()]
        self.assertNotIn(str(self.dp.dataset_root()), roots)

    def test_the_hub_cache_and_dataset_root_are_siblings_not_nested(self):
        # If one contained the other, confining to one would silently grant the
        # other — the two constants must stay independent.
        hub = str(self.dp.hf_hub_cache_root())
        data = str(self.dp.dataset_root())
        self.assertFalse(hub.startswith(data + os.sep))
        self.assertFalse(data.startswith(hub + os.sep))


if __name__ == '__main__':
    unittest.main()
