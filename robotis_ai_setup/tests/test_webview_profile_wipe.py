"""Student handover does not carry identity across, in either direction.

Scenario: ONE shared Windows account, student A closes the window and walks
away without clicking „Abmelden" — the path students actually take.

  (1) A's Supabase session survived into B's launch. `lib/supabaseClient.js`
      calls `createClient` with NO options, so auth-js defaults BOTH
      `persistSession` and `autoRefreshToken`: a durable, self-renewing
      credential, not an expiring JWT. It authorised `/me/export` (the GDPR
      subject-access endpoint — A's profile, trainings, memberships, workflows,
      datasets and teacher-written progress entries in one call), `POST
      /me/delete`, and `/trainings/start` against A's shared workgroup credits.
      It also restored A's Blockly program into B's editor.

      The fix is a SPAWN-time wipe of an explicit `storage_path`.
      Close-time is impossible: `destroy_all` uses `Popen.terminate()`
      (Windows `TerminateProcess`), which delivers neither WinForms'
      `FormClosed` nor the DOM's `pagehide`, and a close can be a crash or a
      power cut anyway. Spawn always runs.

  (2) `DataManager._save_repo_name` is built from the CLIENT-SUPPLIED
      `task_info.user_id`, so B's recordings uploaded into A's HuggingFace
      namespace and SUCCEEDED. The upload now refuses a namespace the rig's own
      token does not own.

THE DRIFT THIS FILE EXISTS FOR: the wipe target and the `storage_path=`
argument must be the SAME constant. If they diverge, the failure is SILENT —
we would wipe a directory nobody uses while the real profile persists — so it
is asserted structurally (AST), not behaviourally.

Deps-free: `webview_window`'s module imports are stdlib plus `.constants`,
which is itself stdlib-only, so this rides `ci.yml::python-tests` unchanged.
"""

import ast
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

from gui.app import constants, webview_window

_WEBVIEW_SRC = pathlib.Path(webview_window.__file__)


def _module_imports(path):
    """Top-level EXTERNAL import names of a module, without executing it.

    Intra-package relative imports (`from .constants import …`, level > 0) are
    excluded: they are not a dependency on anything outside this tree. The
    check that matters is that no THIRD-PARTY module appears — that is what
    would take this fence out of the deps-free suite.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative, in-package
            names.add((node.module or '').split('.')[0])
    return names


class TheWipeTargetAndStoragePathAreTheSameConstant(unittest.TestCase):
    """The AST fence. Drift here is silent, so nothing behavioural catches it."""

    def setUp(self):
        self.tree = ast.parse(_WEBVIEW_SRC.read_text(encoding='utf-8'))

    def _func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f'{name} not found in webview_window.py')

    def _webview_start_call(self):
        start = None
        for node in ast.walk(self._func('run_in_process')):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'start'):
                start = node
        self.assertIsNotNone(start, 'webview.start(...) not found')
        return start

    def test_run_in_process_passes_storage_path_at_all(self):
        """Without it pywebview uses the ROAMING %APPDATA%\\pywebview default."""
        kwargs = {k.arg for k in self._webview_start_call().keywords}
        self.assertIn(
            'storage_path', kwargs,
            'webview.start has no storage_path — the profile falls back to the '
            'ROAMING %APPDATA%\\pywebview, which follows a student to every PC')

    def test_storage_path_is_the_shared_constant_not_a_literal(self):
        start = self._webview_start_call()
        arg = [k.value for k in start.keywords if k.arg == 'storage_path'][0]
        self.assertIsInstance(
            arg, ast.Name,
            'storage_path must be the shared constant, not an inline '
            'expression — an inline path can drift from the wipe target '
            'silently')
        self.assertEqual(arg.id, 'WEBVIEW_PROFILE_DIR')

    def test_the_wipe_defaults_to_that_same_constant(self):
        wipe = self._func('wipe_browser_profile')
        self.assertTrue(wipe.args.defaults, 'wipe has no default target')
        default = wipe.args.defaults[-1]
        self.assertIsInstance(default, ast.Name)
        self.assertEqual(
            default.id, 'WEBVIEW_PROFILE_DIR',
            'the wipe target and storage_path have DRIFTED — the wipe would '
            'clear a directory nobody uses while the real profile persists')

    def test_private_mode_stays_False(self):
        """Owner decision: keep it. The fix is WHERE it persists, not whether."""
        start = self._webview_start_call()
        pm = [k.value for k in start.keywords if k.arg == 'private_mode']
        self.assertTrue(pm, 'private_mode no longer passed')
        self.assertIs(pm[0].value, False)

    def test_the_wipe_runs_at_SPAWN_inside_open_student_window(self):
        calls = [
            n.func.id for n in ast.walk(self._func('open_student_window'))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        self.assertIn(
            'wipe_browser_profile', calls,
            'nothing wipes the profile at spawn; a close-time hook cannot work '
            'because destroy_all uses Popen.terminate()')

    def test_the_wipe_is_AFTER_the_live_child_short_circuit(self):
        """Re-opening while a window is OPEN must not wipe it mid-session.

        The short-circuit `return`s for a live child, so the wipe simply has to
        sit below it. Asserted by statement ORDER inside the `with _lock` body.
        """
        func = self._func('open_student_window')
        withs = [n for n in ast.walk(func) if isinstance(n, ast.With)]
        self.assertTrue(withs, 'the _lock `with` block is gone')
        body = withs[0].body
        early_return_idx = None
        wipe_idx = None
        for i, stmt in enumerate(body):
            src = ast.unparse(stmt)
            if early_return_idx is None and 'poll() is None' in src and 'return' in src:
                early_return_idx = i
            if 'wipe_browser_profile' in src:
                wipe_idx = i
        self.assertIsNotNone(early_return_idx, 'live-child short-circuit not found')
        self.assertIsNotNone(wipe_idx, 'wipe call not found in the lock body')
        self.assertGreater(
            wipe_idx, early_return_idx,
            'the wipe runs BEFORE the live-child short-circuit — re-clicking '
            '„Web-Oberfläche öffnen" would yank the profile out from under the '
            'student currently using the window')

    def test_webview_window_stays_deps_free(self):
        """A heavy import here would take this whole fence out of python-tests."""
        imports = _module_imports(_WEBVIEW_SRC)
        allowed = {
            '__future__', 'logging', 'os', 'shutil', 'subprocess', 'sys',
            'threading', 'pathlib', 'typing',
        }
        self.assertTrue(
            imports <= allowed,
            f'webview_window grew a non-stdlib top-level import: '
            f'{sorted(imports - allowed)}')


class TheWipeIsSafeToRun(unittest.TestCase):
    """It is an rmtree, and its SIBLING holds .env (the HuggingFace token)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='wvwipe_')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _profile(self, parent=None):
        p = (parent or self.tmp) / constants.WEBVIEW_PROFILE_LEAF
        p.mkdir(parents=True, exist_ok=True)
        (p / 'Local State').write_text('session-cookie-of-student-A')
        (p / 'Default').mkdir(exist_ok=True)
        (p / 'Default' / 'Local Storage').write_text('sb-xxx-auth-token')
        return p

    def test_it_deletes_a_real_profile(self):
        p = self._profile()
        self.assertTrue(webview_window.wipe_browser_profile(str(p)))
        self.assertFalse(p.exists())

    def test_an_absent_profile_is_a_success_not_an_error(self):
        p = self.tmp / constants.WEBVIEW_PROFILE_LEAF
        self.assertTrue(webview_window.wipe_browser_profile(str(p)))

    def test_it_REFUSES_a_directory_that_is_not_the_dedicated_leaf(self):
        """%LOCALAPPDATA%\\EduBotics holds .env — the HF token and arm ports."""
        sibling = self.tmp / 'EduBotics'
        sibling.mkdir()
        (sibling / '.env').write_text('HF_TOKEN=hf_secret')
        self.assertFalse(webview_window.wipe_browser_profile(str(sibling)))
        self.assertTrue(
            (sibling / '.env').exists(),
            'the wipe deleted the .env — the HuggingFace token and arm ports')

    def test_it_REFUSES_a_filesystem_root(self):
        self.assertFalse(webview_window.wipe_browser_profile(os.sep))

    def test_it_REFUSES_an_empty_or_odd_target(self):
        for bad in ('', '   ', 'C:\\', '/'):
            self.assertFalse(
                webview_window.wipe_browser_profile(bad), repr(bad))

    def test_it_leaves_the_sibling_env_alone_when_wiping_the_real_leaf(self):
        # The realistic layout: EduBotics/{.env, phone-cert/, webview-profile/}
        base = self.tmp / 'EduBotics'
        base.mkdir()
        (base / '.env').write_text('HF_TOKEN=hf_secret')
        (base / 'phone-cert').mkdir()
        (base / 'phone-cert' / 'cert.pem').write_text('x')
        p = self._profile(parent=base)

        self.assertTrue(webview_window.wipe_browser_profile(str(p)))
        self.assertFalse(p.exists())
        self.assertTrue((base / '.env').exists())
        self.assertTrue((base / 'phone-cert' / 'cert.pem').exists())


class TheProfileLivesUnderLocalAppDataNotRoaming(unittest.TestCase):

    def test_the_default_is_under_LOCALAPPDATA_EduBotics(self):
        # %APPDATA% (roaming) is pywebview's default and is exactly what
        # carried a student's live session to every PC in the school.
        d = constants.WEBVIEW_PROFILE_DIR
        self.assertIn('EduBotics', d)
        self.assertTrue(d.endswith(constants.WEBVIEW_PROFILE_LEAF))

    def test_it_sits_beside_the_env_file_not_above_it(self):
        env_dir = os.path.dirname(constants.ENV_FILE)
        self.assertEqual(
            os.path.dirname(constants.WEBVIEW_PROFILE_DIR), env_dir,
            'the profile dir must be a LEAF beside .env, never .env\'s own dir')

    def test_the_leaf_name_is_shared_with_the_wipe_guard(self):
        # The guard refuses anything not ending in this leaf; if the constant
        # and the guard disagreed the wipe would refuse its own target.
        self.assertEqual(
            os.path.basename(constants.WEBVIEW_PROFILE_DIR),
            constants.WEBVIEW_PROFILE_LEAF)


if __name__ == '__main__':
    unittest.main()
