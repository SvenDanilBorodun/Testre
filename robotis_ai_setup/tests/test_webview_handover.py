"""Student handover does not carry identity across, in either direction.

Scenario: ONE shared Windows account, student A closes the window and walks
away without clicking „Abmelden" — the path students actually take. A's
Supabase session survives into B's launch, because auth-js defaults BOTH
`persistSession` and `autoRefreshToken`: a durable, self-renewing credential,
not an expiring JWT. It authorised `/me/export` (the GDPR subject-access
endpoint), `POST /me/delete`, and `/trainings/start` against A's shared
workgroup credits, and it restored A's Blockly program into B's editor.

THE FIX HAS TWO HALVES AND THEY LIVE IN DIFFERENT LANGUAGES.

  * Python, here: pywebview gets an EXPLICIT non-roaming `storage_path`, and
    the GUI stamps `?fresh=<nonce>` onto every FRESHLY SPAWNED window.
  * JavaScript: `src/utils/bootScrub.js` answers that nonce at boot, clearing
    exactly `sessionScope.js`'s STUDENT_SCOPED_KEYS plus the persisted Supabase
    session — before Redux and supabase-js read storage during module
    evaluation.

WHAT THIS FILE REPLACED, and why the replacement is not a weakening. The first
shape of this fix was an rmtree of the whole WebView2 user-data folder at spawn.
That folder holds localStorage AND IndexedDB, so it destroyed the Blockly
crash-recovery autosave and every MACHINE_SCOPED_KEYS entry — `edubotics_robotType`
(whose loss costs the next student an arm re-scan), `edubotics_audio_muted`, the
four dock keys, `edubotics_urdf_open` — and it fired for a student who merely
closed the window and clicked „Web-Oberfläche öffnen" again mid-lesson. The
NO-RMTREE assertion below is the fence against it coming back.

THE DRIFT THIS FILE EXISTS FOR is now cross-language: the query parameter the
GUI emits must be the one the SPA reads. Drift there is SILENT — the window
opens, everything works, and no student is ever scrubbed again.

Deps-free: `webview_window`'s module imports are stdlib plus `.constants`,
which is itself stdlib-only, so this rides `ci.yml::python-tests` unchanged.
"""

import ast
import os
import pathlib
import re
import unittest

from gui.app import constants, webview_window

_WEBVIEW_SRC = pathlib.Path(webview_window.__file__)
_GUI_APP_SRC = _WEBVIEW_SRC.parent / "gui_app.py"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SPA_BOOT_SCRUB = (
    _REPO_ROOT / "physical_ai_tools" / "physical_ai_manager"
    / "src" / "utils" / "bootScrub.js"
)


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


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


class TheProfileIsPersistentExplicitAndNonRoaming(unittest.TestCase):
    """The half of the fix that survived the rmtree being removed."""

    def setUp(self):
        self.tree = ast.parse(_WEBVIEW_SRC.read_text(encoding='utf-8'))

    def _webview_start_call(self):
        start = None
        for node in ast.walk(_func(self.tree, 'run_in_process')):
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
            'expression')
        self.assertEqual(arg.id, 'WEBVIEW_PROFILE_DIR')

    def test_private_mode_stays_False(self):
        """Owner decision: keep it. The fix is WHERE it persists, not whether."""
        start = self._webview_start_call()
        pm = [k.value for k in start.keywords if k.arg == 'private_mode']
        self.assertTrue(pm, 'private_mode no longer passed')
        self.assertIs(pm[0].value, False)

    def test_webview_window_stays_deps_free(self):
        """A heavy import here would take this whole fence out of python-tests."""
        imports = _module_imports(_WEBVIEW_SRC)
        allowed = {
            '__future__', 'logging', 'os', 'subprocess', 'sys',
            'threading', 'time', 'pathlib', 'typing',
        }
        self.assertTrue(
            imports <= allowed,
            f'webview_window grew a non-stdlib top-level import: '
            f'{sorted(imports - allowed)}')


class TheProfileIsNeverDeleted(unittest.TestCase):
    """The regression fence. An rmtree here destroys the student's own work.

    The WebView2 user-data folder is where localStorage AND IndexedDB live, so
    deleting it takes the Blockly crash-recovery autosave and every
    MACHINE_SCOPED_KEYS entry with the leak it was aimed at. The handover scrub
    belongs in the SPA, where the STUDENT/MACHINE partition is expressed.
    """

    def setUp(self):
        self.src = _WEBVIEW_SRC.read_text(encoding='utf-8')
        self.tree = ast.parse(self.src)

    def test_nothing_in_webview_window_removes_a_tree(self):
        calls = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    calls.add(fn.attr)
                elif isinstance(fn, ast.Name):
                    calls.add(fn.id)
        for destructive in ('rmtree', 'unlink', 'remove', 'rmdir'):
            self.assertNotIn(
                destructive, calls,
                f'webview_window calls {destructive}() — the WebView2 profile '
                f'holds the Blockly autosave (IndexedDB) and every '
                f'machine-scoped key. The handover scrub is the SPA\'s job.')

    def test_it_does_not_even_import_shutil(self):
        self.assertNotIn(
            'shutil', _module_imports(_WEBVIEW_SRC),
            'shutil is back in webview_window — the only thing it was ever '
            'used for here was the profile rmtree')

    def test_open_student_window_only_spawns(self):
        """No wipe/clear/reset step may re-appear at the spawn point."""
        body = ast.unparse(_func(self.tree, 'open_student_window')).lower()
        for banned in ('wipe', 'rmtree', 'shutil'):
            self.assertNotIn(
                banned, body,
                f'open_student_window mentions {banned!r} again')


class TheGuiStampsAFreshWindowMarker(unittest.TestCase):
    """`?fresh=<nonce>` is what tells the SPA the window is newly spawned."""

    def setUp(self):
        self.src = _GUI_APP_SRC.read_text(encoding='utf-8')
        self.open_webview = _func(ast.parse(self.src), '_open_webview')

    def _appended_params(self):
        """Every `<name>=` prefix appended to the URL query in _open_webview.

        Read off the f-strings rather than the built URL, because the URL is
        assembled from a list at runtime and this suite has no tkinter.
        """
        names = set()
        for node in ast.walk(self.open_webview):
            if not isinstance(node, ast.JoinedStr):
                continue
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=$', head.value)
                if m:
                    names.add(m.group(1))
        return names

    def test_it_is_actually_parsed(self):
        # Zero-find floor: a rename of _open_webview or a change of the URL
        # assembly would otherwise make everything below pass having read
        # nothing.
        params = self._appended_params()
        self.assertGreaterEqual(len(params), 2, params)
        self.assertIn('robot', params, 'the known-good sibling param is gone')

    def test_the_url_carries_the_fresh_window_marker(self):
        self.assertIn(
            'fresh', self._appended_params(),
            'the spawned window carries no ?fresh= marker, so the SPA boot '
            'scrub never runs and student A\'s Supabase session survives into '
            'student B\'s launch')

    def test_the_marker_is_a_NONCE_not_a_constant(self):
        """A constant flag fails in BOTH directions.

        The SPA latches the value in sessionStorage so a `useVersionCheck`
        reload does not re-scrub mid-lesson. With a constant, either the latch
        suppresses every later SPAWN too (a silent leak) or there is no latch
        and every reload signs the student out.
        """
        src = ast.unparse(self.open_webview)
        m = re.search(r"f'fresh=\{([^}]+)\}'", src) or re.search(
            r'f"fresh=\{([^}]+)\}"', src)
        self.assertIsNotNone(
            m, f'`fresh=` is not an f-string interpolation in:\n{src}')
        expr = m.group(1)
        self.assertTrue(
            re.search(r'\b(secrets|uuid|token_hex|token_urlsafe|uuid4)\b', expr),
            f'the fresh-window marker is not drawn from a randomness source: '
            f'{expr!r}')

    def test_a_second_call_would_produce_a_different_marker(self):
        """Not vacuous: prove the chosen source really varies."""
        import secrets as _secrets
        self.assertNotEqual(_secrets.token_hex(8), _secrets.token_hex(8))


class TheTwoLanguagesAgreeOnTheParameterName(unittest.TestCase):
    """The one silent drift left, and it spans Python and JavaScript.

    If the GUI stamps `?fresh=` while the SPA reads `?neu=`, nothing breaks
    visibly: the window opens, the app works, and no student is ever scrubbed.
    """

    def setUp(self):
        if not _SPA_BOOT_SCRUB.is_file():
            self.fail(
                f'the SPA half of the handover fix is missing: '
                f'{_SPA_BOOT_SCRUB} — either it was deleted or this test\'s '
                f'path is stale, and both mean nothing is being compared')
        self.js = _SPA_BOOT_SCRUB.read_text(encoding='utf-8')

    def _js_param_name(self):
        m = re.search(
            r"export\s+const\s+BOOT_SCRUB_PARAM\s*=\s*'([^']+)'", self.js)
        self.assertIsNotNone(
            m, 'bootScrub.js no longer exports BOOT_SCRUB_PARAM as a literal')
        return m.group(1)

    def test_the_param_the_gui_emits_is_the_one_the_spa_reads(self):
        gui = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        emitted = set()
        for node in ast.walk(_func(gui, '_open_webview')):
            if isinstance(node, ast.JoinedStr):
                head = node.values[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=$', head.value)
                    if m:
                        emitted.add(m.group(1))
        self.assertIn(
            self._js_param_name(), emitted,
            f'bootScrub.js reads ?{self._js_param_name()}= but the GUI emits '
            f'{sorted(emitted)} — the boot scrub would silently never run')

    def _js_code(self):
        """bootScrub.js with comment lines dropped.

        The same line-oriented strip `sessionScope.test.js` uses, for the same
        reason: this module's header narrates the rmtree regression in prose and
        names both `MACHINE_SCOPED_KEYS` and the calls below while doing so, so
        a scan that reads comments only teaches the next author to rephrase one.
        """
        return '\n'.join(
            ln for ln in self.js.split('\n')
            if not re.match(r'^\s*(//|\*|/\*)', ln))

    def test_the_spa_scrubs_the_student_partition_and_the_supabase_session(self):
        # Both halves matter, and they are separate defects: the student keys
        # are the identity default the next student would record under, the
        # Supabase session is the durable self-renewing credential that
        # authorises /me/export and /trainings/start. Asserted as CALLS, not as
        # imports — an import that is never called scrubs nothing.
        code = self._js_code()
        self.assertRegex(code, r'\bclearStudentScopedStorage\s*\(\s*\)')
        self.assertRegex(code, r'\bclearSupabaseSessionKeys\s*\(\s*\)')

    def test_the_spa_does_not_touch_the_machine_partition(self):
        # The rmtree's other victim. sessionScope owns that list; bootScrub must
        # not grow a second opinion about it.
        self.assertNotIn('MACHINE_SCOPED_KEYS', self._js_code())


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

    def test_the_leaf_name_matches_the_resolved_dir(self):
        self.assertEqual(
            os.path.basename(constants.WEBVIEW_PROFILE_DIR),
            constants.WEBVIEW_PROFILE_LEAF)


if __name__ == '__main__':
    unittest.main()
