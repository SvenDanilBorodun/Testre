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

    def test_NOTHING_in_the_whole_gui_package_removes_a_tree(self):
        """Widened from `webview_window` to `gui/`, because the invitation was
        never in `webview_window` — it was in `constants.py`.

        `WEBVIEW_PROFILE_LEAF`'s comment described the leaf as the guard of an
        rmtree that "REFUSES to delete a directory not ending in it", for
        months after ae9a3aa deleted that rmtree. A comment claiming a wipe
        guard exists is how a wipe comes back: the next author reads it, finds
        the guard missing, and restores what the comment documents. The class
        above fences only `webview_window.py`, so a wipe written into
        `gui_app.py` — where the teardown paths that would call it live —
        walked straight past it.

        `shutil.which` is not a delete; `usbipd_resolver.py` uses it to resolve
        usbipd's absolute path and is the ONE legitimate `import shutil` here.
        """
        gui_app_dir = pathlib.Path(webview_window.__file__).parent
        sources = sorted(gui_app_dir.glob('*.py'))
        # Zero-file floor: a package rename would otherwise make this vacuous.
        self.assertGreaterEqual(
            len(sources), 10, f'read only {len(sources)} modules in '
                              f'{gui_app_dir} — this scan is stale')

        # TREE removal only. Single-file deletes are legitimate and present
        # (`os.remove` of a downloaded installer, of a rotated Protokoll, of a
        # write-probe) — banning those would be a different, unrelated rule.
        # It is the recursive form that takes the whole WebView2 profile.
        tree_removal = {'rmtree', 'rmdir', 'removedirs'}
        # ...and, at any depth of delete, the profile must never be the target.
        deletes = tree_removal | {'remove', 'unlink'}
        profile_names = {'WEBVIEW_PROFILE_DIR', 'WEBVIEW_PROFILE_LEAF'}

        offenders = []
        for src in sources:
            tree = ast.parse(src.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else None)
                if name is None:
                    continue
                if name in tree_removal:
                    offenders.append(
                        f'{src.name}: tree removal — {ast.unparse(node)}')
                elif name in deletes:
                    rendered = ast.unparse(node)
                    if any(pn in rendered for pn in profile_names):
                        offenders.append(
                            f'{src.name}: deletes the profile — {rendered}')
        self.assertEqual(
            offenders, [],
            'something in gui/ removes a directory tree, or deletes the '
            'WebView2 profile. That profile holds localStorage AND IndexedDB, '
            'so it takes the Blockly crash-recovery autosave and every '
            'machine-scoped key with it, and it fires for a student who merely '
            'reopened the window mid-lesson. The handover scrub is the SPA\'s '
            'job (bootScrub.js, keyed on ?fresh=).')

    def test_the_profile_leaf_comment_states_what_the_constant_IS_for(self):
        """It must document `storage_path`, not a deletion guard.

        The old text named only the wipe, so the constant's REAL job — being an
        explicit non-roaming `storage_path` instead of pywebview's roaming
        %APPDATA%\\pywebview default — was written down nowhere.
        """
        src = (pathlib.Path(webview_window.__file__).parent
               / 'constants.py').read_text(encoding='utf-8')
        block = src.split('WEBVIEW_PROFILE_LEAF =')[0].rsplit(
            '# --- Embedded WebView2 browser profile', 1)[-1]
        self.assertIn('storage_path', block,
                      'the leaf constant no longer says it is pywebview\'s '
                      'storage_path')
        self.assertIn('ROAM', block.upper(),
                      'the reason the path is pinned at all — %APPDATA% roams '
                      '— is no longer stated')


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


class ClosingTheChildIsWhatLetsTheNextNonceThrough(unittest.TestCase):
    """The two halves of the handover hole, driven end to end.

    `open_student_window`'s live-child short-circuit returns True and DISCARDS
    the URL it was handed. `_open_webview` mints a new `?fresh=<nonce>` on
    EVERY call, so with a child alive the fresh nonce never reaches a document
    and `bootScrub` never runs — while the GUI logs that a window was opened.

    So the nonce is only half the fix; the other half is that every path which
    ends a student's session must CLOSE the child first. This exercises that
    causally: same module, same seam, once without the close and once with it.

    THE SHORT-CIRCUIT ITSELF STAYS. `gui_app.py`'s „Web-Oberfläche öffnen"
    button calls straight into `open_student_window`, and its documented
    behaviour is to be a no-op while a window is up. Making it relaunch on a
    differing URL would fire on EVERY click (the nonce differs every time) and
    would destroy a live student's window and unsaved Blockly work on one
    click. `test_the_button_is_still_a_no_op_with_a_live_child` is the fence
    against that "fix".
    """

    def setUp(self):
        self.spawned = []

        class _FakeProc:
            pid = 4242

            def __init__(self):
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.alive = False

            def kill(self):
                self.alive = False

            def wait(self, timeout=None):
                # Deliberately does NOT mark the child dead: the real
                # `_watch_subprocess` thread calls `wait()` on every spawn, so
                # a `wait` that killed the fake would silently un-do the
                # live-child state these tests are about, and every assertion
                # below would pass for the wrong reason.
                return 0

        def _fake_popen(cmd, **_kw):
            self.spawned.append(cmd)
            return _FakeProc()

        self._real_available = webview_window.is_available
        webview_window.is_available = lambda: True
        self.addCleanup(
            setattr, webview_window, 'is_available', self._real_available)

        self._real_popen = webview_window.subprocess.Popen
        webview_window.subprocess.Popen = _fake_popen
        self.addCleanup(
            setattr, webview_window.subprocess, 'Popen', self._real_popen)

        # Off Windows `_post_close_to_pid` returns 0, so `destroy_all` skips the
        # grace window entirely and goes straight to terminate. Stubbed anyway
        # so this does not depend on which platform the suite runs on.
        self._real_post = webview_window._post_close_to_pid
        webview_window._post_close_to_pid = lambda _pid: 0
        self.addCleanup(
            setattr, webview_window, '_post_close_to_pid', self._real_post)

        webview_window._process = None
        self.addCleanup(setattr, webview_window, '_process', None)

    def _urls(self):
        return [cmd[cmd.index('--url') + 1] for cmd in self.spawned]

    def test_the_button_is_still_a_no_op_with_a_live_child(self):
        """„Web-Oberfläche öffnen" mid-lesson must not restart the window."""
        self.assertTrue(webview_window.open_student_window(
            'http://localhost/?robot=omx_full&fresh=aaaaaaaaaaaaaaaa'))
        self.assertTrue(webview_window.open_student_window(
            'http://localhost/?robot=omx_full&fresh=bbbbbbbbbbbbbbbb'))
        self.assertEqual(
            len(self.spawned), 1,
            'the second call relaunched — a differing URL must NOT respawn, or '
            'every click of „Web-Oberfläche öffnen" destroys the student\'s '
            'live window and unsaved Blockly work (the nonce differs every '
            'time, so "relaunch when the URL differs" fires always)')

    def test_without_the_close_the_second_nonce_never_reaches_a_document(self):
        """The shipped bug, stated as a test: this is what „Arme scannen" did."""
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_A')
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_B')
        self.assertNotIn(
            'http://localhost/?fresh=STUDENT_B', self._urls(),
            'the second URL was spawned — the short-circuit is gone, see the '
            'test above for why that is worse')

    def test_destroy_all_first_and_the_next_spawn_carries_the_fresh_nonce(self):
        """The fix, stated as a test: what the scan/prereq paths now do."""
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_A')
        webview_window.destroy_all()
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_B')
        self.assertEqual(
            self._urls(),
            ['http://localhost/?fresh=STUDENT_A',
             'http://localhost/?fresh=STUDENT_B'],
            'closing the child did not free the next spawn — student B is '
            'still on student A\'s window, so bootScrub never runs and A\'s '
            'live Supabase session survives the handover')

    def test_destroy_all_is_safe_with_no_child_at_all(self):
        """The scan/prereq paths call it unconditionally, including at startup."""
        webview_window.destroy_all()   # must not raise
        webview_window.destroy_all()
        self.assertEqual(self.spawned, [])


class TheModuleCanBeAskedWhetherAWindowIsUp(unittest.TestCase):
    """`has_live_window` — the read that decides whether „Arme scannen" asks.

    „Arme scannen" closes the student's window and logs them out. That close
    STAYS (the class above is the whole reason it exists), but a student who
    clicks it mid-lesson — their arm dropped out — was signed out with no
    warning and lost unsaved Blockly work. So the click now confirms FIRST,
    and only when there is a window to lose: at handover there is none, and a
    dialog in front of every fresh student's first scan would be noise.

    This is that predicate, driven against the REAL module through the same
    fake-Popen seam the class above uses — `gui_app` must never reach into
    `webview_window._process` itself, because `open_student_window` and
    `destroy_all` swap it under `_lock`.

    The answer must track the child's ACTUAL liveness, not merely whether a
    handle was ever stored: a student who closed the window themselves has no
    session left to warn about.
    """

    def setUp(self):
        self.spawned = []

        class _FakeProc:
            pid = 4242

            def __init__(self):
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.alive = False

            def kill(self):
                self.alive = False

            def wait(self, timeout=None):
                # Same reason as the class above: `_watch_subprocess` calls
                # `wait()` on every spawn, so a `wait` that killed the fake
                # would un-do the live-child state these tests are about.
                return 0

        self.procs = []

        def _fake_popen(cmd, **_kw):
            self.spawned.append(cmd)
            proc = _FakeProc()
            self.procs.append(proc)
            return proc

        self._real_available = webview_window.is_available
        webview_window.is_available = lambda: True
        self.addCleanup(
            setattr, webview_window, 'is_available', self._real_available)

        self._real_popen = webview_window.subprocess.Popen
        webview_window.subprocess.Popen = _fake_popen
        self.addCleanup(
            setattr, webview_window.subprocess, 'Popen', self._real_popen)

        self._real_post = webview_window._post_close_to_pid
        webview_window._post_close_to_pid = lambda _pid: 0
        self.addCleanup(
            setattr, webview_window, '_post_close_to_pid', self._real_post)

        webview_window._process = None
        self.addCleanup(setattr, webview_window, '_process', None)

    def test_no_child_at_all_means_no_window(self):
        """The handover case, and the common one — no dialog must be shown."""
        self.assertFalse(webview_window.has_live_window())

    def test_a_spawned_child_means_a_window_is_up(self):
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_A')
        self.assertTrue(
            webview_window.has_live_window(),
            'a live student window reads as absent — „Arme scannen" would '
            'close it and sign the student out with no warning, which is the '
            'whole defect this predicate exists to fix')

    def test_a_closed_child_means_no_window_again(self):
        """After the scan's own `destroy_all` the next question must say no."""
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_A')
        webview_window.destroy_all()
        self.assertFalse(webview_window.has_live_window())

    def test_a_child_that_EXITED_ON_ITS_OWN_is_not_a_window(self):
        """The student closed the window themselves; the handle is still set.

        `_process` is only cleared by `destroy_all`, so a stored handle proves
        nothing. Asking `poll()` is what separates „there is a session to lose"
        from „there is a stale Popen object", and getting this wrong would put
        a data-loss dialog in front of a student with nothing to lose.
        """
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_A')
        self.procs[0].alive = False
        self.assertIsNotNone(
            webview_window._process,
            'the handle was cleared by something else — this test no longer '
            'exercises the exited-child case')
        self.assertFalse(webview_window.has_live_window())

    @staticmethod
    def _code():
        """`has_live_window`'s body, docstring dropped.

        The docstring explains the very mechanics the assertions below fence,
        so unparsing it too would let a function that does none of them pass on
        its own prose.
        """
        tree = ast.parse(_WEBVIEW_SRC.read_text(encoding='utf-8'))
        body = list(_func(tree, 'has_live_window').body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        return '\n'.join(ast.unparse(stmt) for stmt in body)

    def test_it_asks_the_process_and_does_not_just_test_the_handle(self):
        """Source fence for the test above, so a rewrite cannot lose it."""
        self.assertIn('poll()', self._code(),
                      'has_live_window trusts the stored handle instead of '
                      'asking whether the child is still alive')

    def test_it_takes_the_lock_the_way_destroy_all_does(self):
        """`_process` is swapped under `_lock`; a bare read can see a mid-swap
        value, and the whole point of adding a public accessor was that no
        caller has to know that."""
        self.assertIn('with _lock:', self._code())

    def test_it_never_closes_anything(self):
        """A predicate, not an action: asking must never end a session."""
        webview_window.open_student_window('http://localhost/?fresh=STUDENT_A')
        for _ in range(3):
            webview_window.has_live_window()
        self.assertTrue(self.procs[0].alive,
                        'has_live_window killed the child it was asked about')
        self.assertEqual(len(self.spawned), 1)


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
