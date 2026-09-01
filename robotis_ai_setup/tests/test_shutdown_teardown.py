"""Closing EduBotics runs the teardown it claims to, on both sides of the
process boundary.

TWO INDEPENDENT DEFECTS, both of which shipped.

P1-1 — EVERY BROWSER-SIDE TEARDOWN HOOK WAS DEAD. `webview_window.destroy_all`
used `Popen.terminate()`, which on Windows is `TerminateProcess`: it delivers
neither WinForms' `FormClosed` nor the DOM's `pagehide`/`beforeunload`. Three
shipped consequences, one with a direct classroom symptom:

  * `useJetsonConnection` releases the exclusive claim on `pagehide` via
    `navigator.sendBeacon`, so the lock leaked for the full 5-minute sweeper
    window and THE NEXT STUDENT WAS REFUSED THE JETSON;
  * `JogPanel`'s unmount re-torque never ran, so a hand-guide session could end
    with the follower left limp until the 30 s `_manual_idle_watchdog`;
  * `RecordPanel`'s teardown never ran.

The handlers already existed and were correct — only DELIVERY was broken. The
fix posts WM_CLOSE first and keeps terminate as the backstop. It MUST match by
PID: `gui_app.py`'s tkinter root and the pywebview child are BOTH titled
exactly "EduBotics", so `_focus_existing_window`'s `FindWindowW(None,
"EduBotics")` is already ambiguous today and a close-by-title could shut the
setup GUI instead of the browser.

P1-2 — `self.running` IS NOT A "the stack is up" SIGNAL. `_do_start`'s
`finally` clears it on any failure occurring AFTER `start_containers`
succeeded, and the old `running == False` branch of `_on_close` then skipped
the container stop AND `_stop_camera_bridge()` / `_stop_phone_server()` — so a
live stack, both USB cameras and 0.0.0.0:8444 were all left behind.

P1-3 — THAT CLOSE HAD NO CONFIRMATION, and „Arme scannen" is a button a
student presses MID-LESSON when an arm drops out. One click closed the
EduBotics window, signed them out and threw away unsaved Blockly work, with no
prompt and no undo. The close is right and stays — it is the whole of P1-1's
handover fix — so the fix is a German confirmation in `_scan_arms`, raised
ONLY when `webview_window.has_live_window()`. At handover there is no window,
so the case the close was written for still scans with no dialog at all, and a
decline changes nothing whatsoever.

WHAT CANNOT BE TESTED HERE. This suite runs on macOS/Linux; there is no
user32, no WinForms, no WebView2 and no tkinter root. So the WM_CLOSE call
itself is exercised only for its OFF-WINDOWS behaviour (a no-op returning 0),
and the SEQUENCE around it is driven through the module's own seam with a fake
process. That a WM_CLOSE really produces a DOM `pagehide` inside WebView2 is
stated in the design and is NOT proven by this file. Neither is the Tk modal:
that `askyesno` pumps the event loop is a documented premise here, taken from
`_prompt_finalize_install`, not a result — what IS proven is that a re-entry
arriving through that pump changes nothing, by making the fake dialog re-enter.

Deps-free: `webview_window` is stdlib-only and `_on_close` is read with `ast`.
The scan methods are not imported either — importing gui_app would pull in
tkinter and pywebview — but they ARE executed, extracted from source and
compiled against injected doubles (`_load_method`), the same headless idiom
test_gui_robot_type and test_gui_install_lifecycle use.
"""

import ast
import pathlib
import re
import sys
import textwrap
import types
import unittest

from gui.app import webview_window
from gui.app.constants import ROBOT_PROFILES

_TRIPLE_QUOTED = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')


def _code_only(src: str) -> str:
    """Source with `#` comments and triple-quoted strings removed.

    Both are needed here: the module's docstrings name the very APIs the
    assertions ban, so scanning raw text would fail on the explanation instead
    of on the code.
    """
    src = _TRIPLE_QUOTED.sub('', src)
    return '\n'.join(
        ln for ln in src.split('\n') if not ln.lstrip().startswith('#'))

_GUI_APP_SRC = pathlib.Path(webview_window.__file__).parent / 'gui_app.py'
_DOCKER_MANAGER_SRC = pathlib.Path(webview_window.__file__).parent / 'docker_manager.py'


def _calls_destroy_all_directly(fn):
    """True if THIS function's own body calls `webview_window.destroy_all()`.

    Nested `FunctionDef`s are not descended into, so an outer function does
    not inherit the call made by a closure it defines — `_scan_arms` must not
    be credited with what `_scan_arms._do_scan` does.

    A bare `webview_window.destroy_all` REFERENCE is deliberately not a match:
    `_launch_installer_and_exit` puts one in a `for teardown in (…)` tuple and
    calls it through the loop variable, which no `ast.Call` here can see.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'destroy_all'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'webview_window'):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _destroy_all_call_sites(tree):
    """QUALIFIED names of every function that closes the webview child.

    Qualified because `_do_scan` names TWO different nested functions —
    `_scan_arms`'s and `_scan_cameras`'s — and the camera scan deliberately
    does NOT close the window. A bare-name set cannot tell them apart, so it
    would report the arm scan as fenced while the camera scan quietly grew the
    same call, or vice versa.

    Class bodies do not extend the prefix, so methods keep their bare names.
    """
    sites = set()

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + child.name
                if _calls_destroy_all_directly(child):
                    sites.add(qual)
                walk(child, qual + '.')
            else:
                walk(child, prefix)

    walk(tree, '')
    return sites


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found — this test is stale')


def _body_without_docstring(fn):
    """`fn.body` with a leading docstring dropped, so body[0] is real code."""
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _nested(fn, name):
    """The nested `def name` inside `fn`. `_do_scan` exists twice in the file."""
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{fn.name}.{name} not found — this test is stale')


def _dialog_calls(node):
    """Every `messagebox.*` / confirmation-helper call in this subtree.

    Nested functions ARE descended into: the question a caller of this asks is
    „does clicking this button put a modal on screen", and a modal raised from
    a closure counts just as much as one raised inline.
    """
    hits = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == 'messagebox'):
            hits.add(f'messagebox.{func.attr}')
        elif (isinstance(func, ast.Attribute)
                and func.attr == '_confirm_arm_scan_closes_window'):
            hits.add('_confirm_arm_scan_closes_window')
    return hits


def _load_method(name, ns):
    """Extract `def name(self…)` from gui_app.py and exec it into ``ns``.

    The headless snippet-extraction idiom this repo already uses in
    test_gui_robot_type / test_gui_install_lifecycle / test_gui_elevation /
    test_camera_preview_render: importing `gui_app` pulls in tkinter and
    pywebview, neither of which this suite has, so the REAL method source is
    compiled against an injected namespace of doubles instead. ``ns`` becomes
    the function's globals, so every module-level name it touches must be
    there. The point is that these are the shipped statements, not a
    paraphrase — a source fence alone cannot say what a decline actually does.
    """
    src = _GUI_APP_SRC.read_text(encoding='utf-8')
    marker = f'    def {name}(self'
    start = src.index(marker)
    rest = src[start:]
    end = rest.find('\n    def ', len(marker))
    snippet = textwrap.dedent(rest[:end if end != -1 else len(rest)])
    exec(compile(snippet, str(_GUI_APP_SRC), 'exec'), ns)  # noqa: S102
    return ns[name]


def _top_level_calls(fn):
    """`ast.unparse` of every statement sitting at the function's OWN top level.

    A call found here provably runs on every path through the function, which
    is the property that separates "the window is closed" from "the window is
    closed IF an environment happened to be running".
    """
    out = set()
    for stmt in fn.body:
        out.add(ast.unparse(stmt))
    return out


class _FakeProc:
    """Stands in for the pywebview Popen child.

    `exits_after` is how many `poll()` calls it survives — the seam that lets a
    test say "the child closed itself during the grace window" without a real
    process or a real sleep.
    """

    def __init__(self, pid=4242, exits_after=None):
        self.pid = pid
        self._polls = 0
        self._exits_after = exits_after
        self.terminated = 0
        self.killed = 0
        self.waited = 0
        self._dead = False

    def poll(self):
        self._polls += 1
        if self._dead:
            return 0
        if self._exits_after is not None and self._polls > self._exits_after:
            self._dead = True
            return 0
        return None

    def terminate(self):
        self.terminated += 1
        self._dead = True

    def wait(self, timeout=None):
        self.waited += 1
        return 0

    def kill(self):
        self.killed += 1
        self._dead = True


class _Rig(unittest.TestCase):

    def setUp(self):
        self.posted = []

        def _fake_post(pid):
            self.posted.append(pid)
            return self.windows_found

        self.windows_found = 1
        self._real_post = webview_window._post_close_to_pid
        webview_window._post_close_to_pid = _fake_post
        self.addCleanup(
            setattr, webview_window, '_post_close_to_pid', self._real_post)

        # Keep the whole suite fast: the grace loop must not really sleep.
        self._real_timeout = webview_window.GRACEFUL_CLOSE_TIMEOUT_S
        self._real_poll = webview_window._GRACEFUL_POLL_S
        webview_window.GRACEFUL_CLOSE_TIMEOUT_S = 0.05
        webview_window._GRACEFUL_POLL_S = 0.001
        self.addCleanup(
            setattr, webview_window, 'GRACEFUL_CLOSE_TIMEOUT_S', self._real_timeout)
        self.addCleanup(
            setattr, webview_window, '_GRACEFUL_POLL_S', self._real_poll)

        self.addCleanup(setattr, webview_window, '_process', None)

    def _close(self, proc):
        webview_window._process = proc
        webview_window.destroy_all()


class TheChildIsAskedToCloseBeforeItIsKilled(_Rig):

    def test_WM_CLOSE_is_posted_before_terminate(self):
        proc = _FakeProc()
        self._close(proc)
        self.assertEqual(
            self.posted, [proc.pid],
            'nothing asked the webview child to close — every browser-side '
            'teardown hook (the Jetson release beacon, JogPanel’s re-torque) '
            'is dead again')

    def test_a_child_that_closes_itself_is_NOT_terminated(self):
        """The whole point: let the DOM run pagehide, then leave it alone."""
        proc = _FakeProc(exits_after=1)
        self._close(proc)
        self.assertEqual(proc.terminated, 0)
        self.assertEqual(proc.killed, 0)

    def test_the_wait_ENDS_when_the_child_exits(self):
        """Otherwise every close costs the full 2.5 s, which nothing observes.

        Dropping the `break` in the grace loop leaves the terminate assertions
        above green — the child is dead either way — so the only thing that can
        catch it is the elapsed time. Measured against the REAL constant, not
        the shortened one the other tests use, because the defect IS the
        constant being paid in full.
        """
        import time as _time
        webview_window.GRACEFUL_CLOSE_TIMEOUT_S = 1.0
        webview_window._GRACEFUL_POLL_S = 0.02
        proc = _FakeProc(exits_after=1)
        started = _time.monotonic()
        self._close(proc)
        elapsed = _time.monotonic() - started
        self.assertLess(
            elapsed, 0.5,
            f'the close waited {elapsed:.2f}s for a child that had already '
            f'exited — the grace loop no longer breaks on poll()')

    def test_a_child_that_ignores_WM_CLOSE_is_still_terminated(self):
        """A hung renderer must never keep the GUI open."""
        proc = _FakeProc()
        self._close(proc)
        self.assertEqual(proc.terminated, 1)

    def test_a_child_with_NO_window_skips_the_grace_period_entirely(self):
        """A crash during startup has nothing to post to — do not wait 2.5 s."""
        self.windows_found = 0
        proc = _FakeProc()
        self._close(proc)
        self.assertEqual(proc.terminated, 1)
        # Exactly the entry poll plus the pre-terminate poll: no grace loop.
        self.assertLessEqual(proc._polls, 3)

    def test_a_raising_post_still_terminates(self):
        def _boom(_pid):
            raise OSError('user32 unavailable')
        webview_window._post_close_to_pid = _boom
        proc = _FakeProc()
        self._close(proc)
        self.assertEqual(proc.terminated, 1)

    def test_an_already_dead_child_is_neither_posted_to_nor_killed(self):
        proc = _FakeProc()
        proc.terminate()          # already gone
        proc.terminated = 0
        self._close(proc)
        self.assertEqual(self.posted, [])
        self.assertEqual(proc.terminated, 0)
        self.assertEqual(proc.killed, 0)

    def test_it_is_safe_with_no_child_at_all(self):
        webview_window._process = None
        webview_window.destroy_all()  # must not raise

    def test_the_handle_is_cleared_afterwards(self):
        self._close(_FakeProc())
        self.assertIsNone(webview_window._process)

    def test_the_deliberate_stop_flag_is_set_so_the_watchdog_stays_quiet(self):
        # A non-zero exit after OUR close must not be reported as a missing
        # WebView2 runtime.
        webview_window._deliberate_stop.clear()
        self._close(_FakeProc())
        self.assertTrue(webview_window._deliberate_stop.is_set())


class TheCloseMatchesByPidAndNeverByTitle(unittest.TestCase):
    """The tkinter root and the pywebview child share the title "EduBotics"."""

    def setUp(self):
        src = pathlib.Path(webview_window.__file__).read_text(encoding='utf-8')
        self.tree = ast.parse(src)
        # Comments AND docstrings dropped. Every docstring in this module
        # explains the defect by NAMING the API it bans, so a scan that reads
        # prose can only ever fail on the prose — and the repair for that is to
        # reword a comment, which is the opposite of what the guard wants.
        self.code = _code_only(src)

    def test_it_uses_the_pid_enumeration_api(self):
        self.assertIn('EnumWindows', self.code)
        self.assertIn('GetWindowThreadProcessId', self.code)

    def test_it_never_looks_a_window_up_BY_TITLE(self):
        for banned in ('FindWindowW', 'FindWindowA', 'FindWindowExW'):
            self.assertNotIn(
                banned, self.code,
                f'{banned} matches by TITLE, and gui_app’s tkinter root is also '
                f'titled "EduBotics" — this could close the setup GUI instead '
                f'of the browser')

    def test_the_pid_it_posts_to_is_the_CHILD_process(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'destroy_all')
        body = ast.unparse(fn)
        self.assertIn(
            '_post_close_to_pid(proc.pid)', body,
            'destroy_all posts to something other than the child’s own pid')

    def test_it_is_a_no_op_off_windows(self):
        # This suite runs on macOS/Linux; the real function must return 0 here
        # rather than raising or reaching for user32.
        if sys.platform == 'win32':
            self.skipTest('Windows: the real enumeration runs')
        self.assertEqual(webview_window._post_close_to_pid(1), 0)

    def test_the_platform_guard_is_the_FIRST_statement(self):
        """Structural on purpose — the behavioural form is uncatchable here.

        Deleting `if sys.platform != "win32": return 0` leaves this whole suite
        green off Windows, because the very next statement is `import ctypes` +
        `ctypes.WinDLL` inside a `try`, which raises on macOS/Linux and returns
        the SAME 0 from the `except`. That is the identical shape CLAUDE.md
        already records for `_read_machine_id`'s `sys.platform` early return, so
        it gets the identical treatment: pin the STRUCTURE rather than pretend
        a behavioural test exists.

        It is not merely decorative — without it every close on a Pi-adjacent
        dev box or a Linux CI runner pays a ctypes import and an exception, and
        the German `[WARNUNG]` log line fires on a platform where nothing is
        wrong.
        """
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == '_post_close_to_pid')
        body = [s for s in fn.body
                if not (isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant))]
        self.assertTrue(body, '_post_close_to_pid has no code')
        first = ast.unparse(body[0])
        self.assertIn('sys.platform', first)
        self.assertIn("'win32'", first.replace('"', "'"))
        self.assertIn('return 0', first)

    def test_the_module_stays_deps_free(self):
        imports = set()
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                imports.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imports.add((node.module or '').split('.')[0])
        allowed = {
            '__future__', 'logging', 'os', 'subprocess', 'sys', 'threading',
            'time', 'pathlib', 'typing',
        }
        self.assertTrue(
            imports <= allowed,
            f'non-stdlib top-level import: {sorted(imports - allowed)}')


class TheCloseIsAuthoritativeAboutTheStack(unittest.TestCase):
    """P1-2. `self.running` reads False over a live stack.

    `_on_close` lives on a tkinter class this suite cannot import, so the shape
    is read off the AST. What is asserted is the DECISION and the ORDER, both
    of which were wrong before: the container state came from a flag, and three
    process-local resources were tied to that same flag.
    """

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        cls.fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_on_close':
                cls.fn = node
        if cls.fn is None:
            raise AssertionError('_on_close not found — this test is stale')
        body = list(cls.fn.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]   # ast.unparse keeps the docstring
        cls.code = '\n'.join(ast.unparse(s) for s in body)

    def test_it_asks_docker_and_does_not_only_trust_the_flag(self):
        self.assertIn(
            'docker_manager.any_container_running()', self.code,
            '_on_close decides on self.running alone — _do_start’s `finally` '
            'clears that flag on any failure AFTER start_containers succeeded, '
            'so a live stack is left behind')

    def test_the_flag_is_still_the_fast_path(self):
        # A running stack must not pay for a `docker ps` on every close.
        self.assertIn('bool(self.running) or docker_manager.any_container_running()',
                      self.code)

    def test_the_process_local_resources_are_stopped_UNCONDITIONALLY(self):
        """They are owned by THIS process, not by the container state.

        The camera bridge holds both USB cameras, the phone receiver holds
        0.0.0.0:8444, and the :8769 control bridge starts BEFORE _open_webview
        so a partial start leaves it up. The old `running == False` branch
        stopped only the last of the three.
        """
        for call in ('self._stop_camera_bridge()',
                     'self._stop_phone_server()',
                     'self._stop_rs_control_server()',
                     'webview_window.destroy_all()'):
            self.assertEqual(
                self.code.count(call), 1,
                f'{call} appears {self.code.count(call)} times — it must run '
                f'exactly once, on every path that closes')

    def test_they_are_NOT_nested_inside_the_stack_up_branch(self):
        """Structural: they must sit at the function's top level, not in an If."""
        top_level = set()
        for stmt in self.fn.body:
            if isinstance(stmt, ast.Expr):
                top_level.add(ast.unparse(stmt))
        for call in ('self._stop_camera_bridge()',
                     'self._stop_phone_server()',
                     'self._stop_rs_control_server()',
                     'webview_window.destroy_all()'):
            self.assertIn(
                call, top_level,
                f'{call} is nested inside a branch again — that is exactly how '
                f'the camera bridge came to be skipped')

    def test_declining_the_prompt_still_cancels_the_close(self):
        self.assertIn('askyesno', self.code)
        self.assertIn('return', self.code)
        self.assertNotIn(
            'self.root.destroy()\n    return', self.code,
            'the window is destroyed before the decline can cancel it')

    def test_the_containers_are_only_stopped_when_one_is_actually_up(self):
        # Stopping a stack that is not there is harmless but slow, and it would
        # also fire stop_cloud_only on a machine that never started anything.
        self.assertIn('docker_manager.stop_containers(gpu=self.gpu_available)',
                      self.code)
        self.assertIn('docker_manager.stop_cloud_only()', self.code)
        self.assertIn('if stack_up:', self.code)

    def test_the_keepalive_always_stops(self):
        # WSL2's ~60 s vmIdleTimeout is only held off while this runs; leaking
        # it past the GUI keeps a process alive for nothing.
        self.assertIn('docker_manager.stop_keepalive()', self.code)


class StoppingTheEnvironmentAlsoClosesTheBrowserWindow(unittest.TestCase):
    """The handover hole on the PRIMARY lifecycle path.

    „Umgebung stoppen" then „Umgebung starten" is how the product is operated,
    and `_do_start` ends in `_open_webview()`. `_stop_environment` tore down the
    camera bridge, the phone server, the `:8769` bridge, the containers and the
    keepalive — and left the WEBVIEW CHILD alive. So the next start hit
    `open_student_window`'s live-child short-circuit, which DISCARDS the freshly
    minted `?fresh=<nonce>` URL and returns True: the GUI logged that a window
    had been opened while the next student was looking at the previous one's
    window, with their live self-refreshing Supabase session, and no document
    ever loaded so the boot scrub never ran.

    PRE-EXISTING, not caused by the scrub: the profile `rmtree` that preceded it
    sat BELOW the very same short-circuit, so it never ran on this path either.
    Found by an adversarial review of the follow-up.
    """

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        cls.tree = tree
        cls.fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_stop_environment':
                cls.fn = node
        if cls.fn is None:
            raise AssertionError('_stop_environment not found — this test is stale')
        cls.code = ast.unparse(cls.fn)

    def test_it_closes_the_webview_child(self):
        self.assertIn(
            'webview_window.destroy_all()', self.code,
            '„Umgebung stoppen" leaves the browser window alive, so the next '
            '„Umgebung starten" short-circuits and hands the next student the '
            'previous one\'s window and session')

    def test_it_still_stops_everything_it_did_before(self):
        for call in ('self._stop_camera_bridge()', 'self._stop_phone_server()',
                     'self._stop_rs_control_server()',
                     'docker_manager.stop_keepalive()'):
            self.assertIn(call, self.code)

    def test_the_close_happens_BEFORE_the_containers_go_down(self):
        """Ordering, so the graceful close still has a live rosbridge.

        `destroy_all` posts WM_CLOSE and gives the DOM a moment; JogPanel's
        unmount re-torque is a rosbridge service call, and the Jetson release
        beacon is an HTTPS POST. Tearing the containers down first would make
        the re-torque unreachable — the exact teardown the graceful close
        exists to deliver.
        """
        close = self.code.index('webview_window.destroy_all()')
        stop = min(
            self.code.index('docker_manager.stop_containers'),
            self.code.index('docker_manager.stop_cloud_only'),
        )
        self.assertLess(close, stop)

    def test_every_path_that_ends_a_session_closes_the_child(self):
        """Enumerated, and EXACT: another one must be a decision, not an oversight.

        Grown from two to four, deliberately. The two that joined tear down the
        environment on paths that end the previous student's session just as
        surely as „Umgebung stoppen" does, and both left the child alive:

          * `_scan_arms._do_scan` — „Arme scannen" stops the containers to free
            the Dynamixel bus. Repro that shipped: A leaves the window open, B
            clicks „Arme scannen", B clicks „Umgebung starten", `_open_webview`
            mints a fresh nonce, the live-child short-circuit discards it, and
            B is on A's live self-renewing Supabase session.
          * `_run_prerequisite_checks_body` — the „Vorherige Sitzung wird
            aufgeräumt…" path, which four call sites re-run on worker threads.

        `_scan_cameras._do_scan` is NOT here and must not join: it stops no
        environment and ends no session. That is why these names are QUALIFIED
        — both nested functions are called `_do_scan`.

        `_launch_installer_and_exit` is not here either, and that is not a gap:
        it passes the bare `webview_window.destroy_all` into a
        `for teardown in (…)` tuple and calls it through the loop variable, a
        shape no call-site scan can attribute. It does close the child.

        STILL FOUR after the „Arme scannen" confirmation was added, checked
        deliberately: the dialog gates whether `_scan_arms` reaches `_do_scan`
        at all, it does not move or duplicate the close. The new helper,
        `_confirm_arm_scan_closes_window`, only ASKS — it must never appear in
        this set, because a function that both prompts and closes could not
        offer a decline that changes nothing.
        """
        self.assertEqual(
            _destroy_all_call_sites(self.tree),
            {
                '_on_close',
                # The call lives in `_stop_environment`'s worker closure, not
                # in its own body — the class above reads the whole function
                # with `ast.unparse` and so cannot see that distinction.
                '_stop_environment._do_stop',
                '_run_prerequisite_checks_body',
                '_scan_arms._do_scan',
            },
            'the set of functions closing the webview child changed — if that '
            'was intended, update this enumeration and say why; if it was not, '
            'a path that ends a session is handing the next student the '
            'previous one\'s window and live session')


class TheSCANPathClosesTheChildToo(unittest.TestCase):
    """„Arme scannen" is a session-ending path, and it was not treated as one.

    It calls `ensure_environment_stopped`, `_stop_camera_bridge` and
    `_stop_rs_control_server` — the same handover ritual `_stop_environment`
    performs — and then left the WebView2 child alive. The next
    „Umgebung starten" therefore hit `open_student_window`'s live-child
    short-circuit, which DISCARDS the freshly minted `?fresh=<nonce>` URL and
    returns True: no document loads, `bootScrub` never runs, and the GUI logs
    success while the next student looks at the previous one's window.

    BEHAVIOUR CHANGE recorded on purpose, and SINCE REVISED. When the close
    was added, `_scan_arms` had no confirmation, so one click on
    „Arme scannen" destroyed the window with no prompt — judged acceptable
    because the same click already stops the containers, so the window is
    dead-ended either way. That judgement held for the handover it was written
    for and failed for the student who clicks „Arme scannen" MID-LESSON
    because an arm dropped out: they were signed out and lost unsaved Blockly
    work, silently. `_scan_arms` now confirms first, but ONLY when
    `webview_window.has_live_window()` — so the handover case (no window, the
    common one) still scans with no prompt at all.

    NOTHING IN THIS CLASS CHANGES. The close is unconditional WITHIN the scan
    and stays that way; the gate sits one level up, in `_scan_arms`, and is
    fenced by `TheScanAsksFirstWhenThereIsASessionToLose` and
    `TheDialogIsOnlyOnThePathThatEndsASession` below. Every assertion here
    describes what a CONFIRMED (or unprompted) scan does, which is byte for
    byte what it did before.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        cls.scan_arms = _func(cls.tree, '_scan_arms')
        cls.do_scan = None
        for node in ast.walk(cls.scan_arms):
            if isinstance(node, ast.FunctionDef) and node.name == '_do_scan':
                cls.do_scan = node
        if cls.do_scan is None:
            raise AssertionError(
                '_scan_arms._do_scan not found — this test is stale')

    def test_the_arm_scan_closes_the_webview_child(self):
        self.assertTrue(
            _calls_destroy_all_directly(self.do_scan),
            '„Arme scannen" tears the environment down and leaves the browser '
            'window alive, so the next „Umgebung starten" short-circuits and '
            'hands the next student the previous one\'s window and session')

    def test_the_close_is_NOT_conditional_on_an_environment_having_been_up(self):
        """The case that needs it most is the one where that condition is False.

        The scan's other teardown calls sit INSIDE
        `if docker_manager.ensure_environment_stopped(…):`. A stale window
        outlives a stopped environment — a student who clicked
        „Umgebung stoppen" and walked away has no environment running and a
        live window — so nesting the close there would skip exactly that
        student.
        """
        self.assertIn(
            'try:\n    webview_window.destroy_all()\nexcept Exception:\n    pass',
            _top_level_calls(self.do_scan),
            'the webview close is nested inside a branch again — it must run '
            'on every path through the scan, environment up or not')

    def test_the_close_happens_BEFORE_the_containers_go_down(self):
        """Same ordering rule `_stop_environment` keeps, same reason.

        `destroy_all` posts WM_CLOSE and gives the DOM a moment; JogPanel's
        unmount re-torque is a rosbridge service call and the Jetson release
        beacon is an HTTPS POST, so tearing the containers down first makes the
        teardown the graceful close exists to deliver unreachable.
        """
        code = ast.unparse(self.do_scan)
        self.assertIn('webview_window.destroy_all()', code)
        self.assertIn('docker_manager.ensure_environment_stopped', code)
        self.assertLess(
            code.index('webview_window.destroy_all()'),
            code.index('docker_manager.ensure_environment_stopped'),
            'the containers are torn down before the child is asked to close, '
            'so JogPanel\'s re-torque and the Jetson release beacon have no '
            'rosbridge left to reach')

    def test_a_failing_close_does_not_abort_the_scan(self):
        """The student asked for a scan; a dead pywebview must not eat it."""
        for stmt in self.do_scan.body:
            if (isinstance(stmt, ast.Try)
                    and 'webview_window.destroy_all()' in ast.unparse(stmt)):
                self.assertTrue(stmt.handlers, 'bare try with no except')
                return
        self.fail('the webview close in _do_scan is not best-effort — an '
                  'exception there would abort the arm scan')

    def test_the_CAMERA_scan_deliberately_does_NOT_close_the_child(self):
        """The other `_do_scan`, and the reason the enumeration is qualified.

        `_scan_cameras._do_scan` stops no environment and ends no session — it
        stops the previews it owns and rescans. Closing the student's window
        there would destroy a live session for a camera rescan mid-lesson.
        """
        scan_cameras = _func(self.tree, '_scan_cameras')
        cam_do_scan = None
        for node in ast.walk(scan_cameras):
            if isinstance(node, ast.FunctionDef) and node.name == '_do_scan':
                cam_do_scan = node
        self.assertIsNotNone(
            cam_do_scan, '_scan_cameras._do_scan not found — test is stale')
        self.assertFalse(
            _calls_destroy_all_directly(cam_do_scan),
            'the CAMERA scan now closes the webview child — that destroys a '
            'live student session for a camera rescan; only the ARM scan ends '
            'a session')


class ThePrerequisiteCleanupClosesTheChildToo(unittest.TestCase):
    """The „Vorherige Sitzung wird aufgeräumt…" path.

    It calls `ensure_environment_stopped` for the same handover reason and left
    the child alive. Four call sites re-run `_run_prerequisite_checks` on
    worker threads, so this is the most-travelled teardown in the GUI.

    THE LIMIT IS PART OF THE CONTRACT: `destroy_all` is PROCESS-LOCAL —
    `webview_window._process` is a module global written only by THIS process's
    `open_student_window`. So despite the status text, the call cannot reach a
    window orphaned by a crashed or Task-Manager-killed GUI; it covers the
    same-process re-check path. That residual is recorded in
    docs/KNOWN-ISSUES.md and closing it needs a kill-by-cmdline mechanism that
    does not exist.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        cls.fn = _func(cls.tree, '_run_prerequisite_checks_body')

    def test_it_closes_the_webview_child(self):
        self.assertTrue(
            _calls_destroy_all_directly(self.fn),
            'the prerequisite cleanup stops the environment and leaves the '
            'browser window alive')

    def test_the_close_is_NOT_conditional_on_an_environment_having_been_up(self):
        self.assertIn(
            'try:\n    webview_window.destroy_all()\nexcept Exception:\n    pass',
            _top_level_calls(self.fn),
            'the webview close is nested inside a branch — a stale window '
            'outlives a stopped environment, so the branch is False in exactly '
            'the case that needs the close')

    def test_the_close_happens_BEFORE_the_containers_go_down(self):
        code = ast.unparse(self.fn)
        self.assertIn('webview_window.destroy_all()', code)
        self.assertIn('docker_manager.ensure_environment_stopped', code)
        self.assertLess(
            code.index('webview_window.destroy_all()'),
            code.index('docker_manager.ensure_environment_stopped'),
            'the containers are torn down before the child is asked to close')

    def test_a_failing_close_does_not_abort_the_prerequisite_run(self):
        """This runs on the startup path; a raise here would brick the GUI."""
        for stmt in self.fn.body:
            if (isinstance(stmt, ast.Try)
                    and 'webview_window.destroy_all()' in ast.unparse(stmt)):
                self.assertTrue(stmt.handlers, 'bare try with no except')
                return
        self.fail('the webview close in _run_prerequisite_checks_body is not '
                  'best-effort')

    def test_it_still_runs_BEFORE_the_rootfs_gate(self):
        """CLAUDE.md invariant: that gate `return`s, so nothing may sit above it.

        The teardown must stay at the earliest point dockerd is known
        reachable and above `_rootfs_rebuild_required()`, because a rootfs
        mismatch means the distro is OLD — i.e. exactly the population whose
        persisted container configs still carry `restart: unless-stopped`.
        Adding the webview close must not have pushed either one below it.
        """
        code = ast.unparse(self.fn)
        for needle in ('self._rootfs_rebuild_required()',
                       'webview_window.destroy_all()',
                       'docker_manager.ensure_environment_stopped'):
            self.assertIn(needle, code, f'{needle} is gone — test is stale')
        gate = code.index('self._rootfs_rebuild_required()')
        self.assertLess(code.index('webview_window.destroy_all()'), gate)
        self.assertLess(
            code.index('docker_manager.ensure_environment_stopped'), gate)


class TheStackProbeIsBoundedAndFailsClosed(unittest.TestCase):
    """`any_container_running` runs on the Tk main thread inside `_on_close`."""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(_DOCKER_MANAGER_SRC.read_text(encoding='utf-8'))
        cls.fn = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == 'any_container_running'):
                cls.fn = node
        if cls.fn is None:
            raise AssertionError('any_container_running not found')
        body = [s for s in cls.fn.body
                if not (isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant))]
        cls.code = '\n'.join(ast.unparse(s) for s in body)

    def test_it_makes_exactly_ONE_subprocess_call(self):
        """Three `docker inspect`s at 10 s each would freeze the close for 30 s."""
        runs = [n for n in ast.walk(self.fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'run']
        self.assertEqual(len(runs), 1, 'the probe is no longer a single call')

    def test_that_call_is_bounded(self):
        self.assertIn('timeout=10', self.code)

    def test_every_failure_path_returns_False(self):
        """A broken probe must not turn closing the GUI into an unclosable modal."""
        returns = [ast.unparse(n.value) for n in ast.walk(self.fn)
                   if isinstance(n, ast.Return) and n.value is not None]
        self.assertIn('False', returns)
        for exc in ('FileNotFoundError', 'TimeoutExpired', 'OSError'):
            self.assertIn(exc, self.code)

    def test_it_re_checks_the_names_because_the_docker_filter_is_a_substring(self):
        self.assertIn('PROJECT_CONTAINERS', self.code)

    def test_it_is_ANY_and_not_ALL(self):
        # A PARTIAL stack is exactly the state that leaks; all_containers_running
        # answers a different question and is the wrong tool for a shutdown.
        self.assertNotIn('all(', self.code)


class TheScanAsksFirstWhenThereIsASessionToLose(unittest.TestCase):
    """„Arme scannen" mid-lesson used to sign the student out with no warning.

    The close itself is right and stays — see the class above for the handover
    it fixes. What was missing is that the SAME click also ends a session that
    may still be in use: a student whose arm dropped out clicks „Arme scannen",
    the EduBotics window vanishes, they are logged out, and unsaved Blockly
    work is gone. No prompt, no undo.

    So the click now confirms FIRST, and ONLY when there is a window to lose.
    At handover — the case that motivated the close, and the one every fresh
    student walks — `webview_window.has_live_window()` is False, no dialog is
    shown at all, and the scan runs exactly as it did before.

    Driven against the REAL `_scan_arms` and the REAL
    `_confirm_arm_scan_closes_window`, extracted headless (see `_load_method`)
    because importing gui_app needs tkinter. What the fakes stand in for is
    named at each seam; what CANNOT be exercised here is the Tk modal itself —
    that it pumps the event loop is the documented premise, not a result, and
    `test_a_second_click_while_the_dialog_is_up_is_a_NO_OP` encodes the
    consequence by making the fake dialog re-enter exactly the way a pending
    `root.after` does.
    """

    def _drive(self, live_window, answer, clicks=1, reenter=False):
        """Click „Arme scannen" `clicks` times against a rig of doubles.

        `live_window` is what `webview_window.has_live_window()` answers;
        `answer` is what the student clicks in the dialog; `reenter` makes the
        dialog re-enter `_scan_arms` while it is on screen.
        """
        rec = types.SimpleNamespace(
            asked=[], destroyed=0, threads=0, scans=0, env_stops=0,
            statuses=[], logs=[], button=[], button_arm=[], scheduled=[])

        class _SyncThread:
            """threading.Thread stand-in — runs target() inline on start()."""

            def __init__(self, target=None, daemon=None, name=None):
                self._target = target

            def start(self):
                rec.threads += 1
                if self._target is not None:
                    self._target()

        leader = types.SimpleNamespace(
            description='OpenRB-150', serial_path='/dev/serial/by-id/leader')
        follower = types.SimpleNamespace(
            description='OpenRB-150', serial_path='/dev/serial/by-id/follower')

        def _bump(field):
            setattr(rec, field, getattr(rec, field) + 1)

        def _scan_and_identify(image, arm_family='omx'):
            _bump('scans')
            return leader, follower

        def _ensure_stopped(log=None):
            _bump('env_stops')
            return False

        fake_webview = types.SimpleNamespace(
            has_live_window=lambda: live_window,
            destroy_all=lambda: _bump('destroyed'))

        def _askyesno(title, message, **kw):
            rec.asked.append((title, message, kw))
            if reenter:
                # EXACTLY the shipped re-entry vector: `askyesno` pumps the Tk
                # event loop, and `_prompt_bind_arms` leaves a pending
                # `root.after(200, self._scan_arms)` behind after an elevated
                # bind. At this instant `_scanning` is still False and the
                # button is still enabled.
                scan(owner)
            return answer

        fake_messagebox = types.SimpleNamespace(askyesno=_askyesno, NO='no')

        confirm = _load_method('_confirm_arm_scan_closes_window', {
            'webview_window': fake_webview,
            'messagebox': fake_messagebox,
        })
        scan = _load_method('_scan_arms', {
            'threading': types.SimpleNamespace(Thread=_SyncThread),
            'docker_manager': types.SimpleNamespace(
                ensure_environment_stopped=_ensure_stopped),
            'device_manager': types.SimpleNamespace(
                scan_and_identify_arms=_scan_and_identify,
                diagnose_usb_environment=lambda **kw: self.fail(
                    'the scan fell into the diagnose flow — the rig is stale'),
                get_diagnostics_log_path=lambda: '/tmp/diag.log'),
            'IMAGE_OPEN_MANIPULATOR': 'img',
            'ROBOT_PROFILES': ROBOT_PROFILES,
            'tk': types.SimpleNamespace(DISABLED='disabled', NORMAL='normal'),
            'webview_window': fake_webview,
        })

        def _after(delay, fn=None, *a):
            # Runs the callback inline: `_do_scan` marshals most of its UI work
            # back through `root.after`, so swallowing it would hide half of
            # what a confirmed scan does — and all of what a declined one must
            # not do.
            rec.scheduled.append(delay)
            if fn is not None:
                return fn()

        owner = types.SimpleNamespace(
            _scanning=False,
            _scan_confirm_open=False,
            btn_scan_leader=types.SimpleNamespace(
                config=lambda **kw: rec.button.append(kw.get('state'))),
            # The SECOND scan button. A leader-less robot type hides Schritt A
            # outright, so the button the student presses lives in the arm
            # frame instead; `_scan_arms` must disable and re-enable BOTH, or a
            # re-pack mid-scan puts a live button back over a running worker.
            # Recorded SEPARATELY so `rec.button` keeps pinning exactly the
            # transitions it always pinned.
            btn_scan_arm=types.SimpleNamespace(
                config=lambda **kw: rec.button_arm.append(kw.get('state'))),
            btn_stop=types.SimpleNamespace(config=lambda **kw: None),
            btn_open_browser=types.SimpleNamespace(config=lambda **kw: None),
            _selected_robot_profile=lambda: 'omx_full',
            root=types.SimpleNamespace(after=_after),
            progress=types.SimpleNamespace(
                start=lambda *a: None, stop=lambda *a: None),
            hardware=types.SimpleNamespace(leader=None, follower=None),
            leader_status_var=types.SimpleNamespace(set=lambda *a: None),
            follower_status_var=types.SimpleNamespace(set=lambda *a: None),
            _set_status=rec.statuses.append,
            _log=rec.logs.append,
            _clear_arm_repair=lambda: None,
            _stop_camera_bridge=lambda: None,
            _stop_rs_control_server=lambda: None,
            _update_start_button=lambda: None,
            _show_arm_repair=lambda *a: None,
            running=False,
        )
        owner._confirm_arm_scan_closes_window = lambda: confirm(owner)

        for _ in range(clicks):
            scan(owner)
        return owner, rec

    # ── no window: the handover case, and the common one ─────────────

    def test_with_NO_window_open_nothing_is_asked(self):
        """A fresh student's first scan must not meet a dialog.

        There is nothing to close and nothing to lose, so a prompt here is
        pure friction on the most-travelled path in the GUI.
        """
        _owner, rec = self._drive(live_window=False, answer=False)
        self.assertEqual(
            rec.asked, [],
            'a confirmation was shown with no student window open — that is '
            'a dialog in front of every handover, warning about a close the '
            'student cannot see')

    def test_with_NO_window_open_the_scan_runs_exactly_as_before(self):
        _owner, rec = self._drive(live_window=False, answer=False)
        self.assertEqual(rec.scans, 1, 'the scan did not run')
        self.assertEqual(rec.destroyed, 1, 'the webview close was skipped')
        self.assertEqual(rec.env_stops, 1, 'the environment teardown was skipped')

    # ── a live window: ask, and mean it ──────────────────────────────

    def test_a_LIVE_window_is_asked_about_before_anything_happens(self):
        _owner, rec = self._drive(live_window=True, answer=True)
        self.assertEqual(
            len(rec.asked), 1,
            'clicking „Arme scannen" with the student window up asked '
            'nothing — the student is signed out and loses unsaved work with '
            'no warning, which is the whole defect')

    def test_confirming_scans_byte_for_byte_as_it_did_before(self):
        """The handover fix must survive the dialog completely intact."""
        owner, rec = self._drive(live_window=True, answer=True)
        self.assertEqual(rec.destroyed, 1,
                         'the confirmed scan no longer closes the student '
                         'window — the next student inherits the previous '
                         'one\'s live Supabase session')
        self.assertEqual(rec.env_stops, 1)
        self.assertEqual(rec.scans, 1)
        self.assertEqual(rec.threads, 1)
        self.assertIsNotNone(owner.hardware.leader)
        self.assertIsNotNone(owner.hardware.follower)
        self.assertEqual(
            rec.button, ['disabled', 'normal'],
            'the button was not disabled for the scan and re-enabled after it')
        self.assertEqual(
            rec.button_arm, ['disabled', 'normal'],
            'the leader-less profiles\' scan button was left out of the '
            'disable/re-enable pair — it is the ONLY button on those profiles')
        self.assertTrue(any('Roboterarme werden gesucht' in s
                            for s in rec.statuses))

    def test_declining_does_ABSOLUTELY_NOTHING(self):
        """Cancel is total: no teardown, no scan, no UI trace, no state.

        Anything here that is not zero is a side effect of a click the student
        took back — and `destroyed` in particular would mean the dialog closed
        their window anyway, i.e. the dialog made things worse than no dialog.
        """
        owner, rec = self._drive(live_window=True, answer=False)
        self.assertEqual(rec.destroyed, 0, 'a declined scan closed the window')
        self.assertEqual(rec.env_stops, 0,
                         'a declined scan tore the environment down')
        self.assertEqual(rec.scans, 0, 'a declined scan scanned anyway')
        self.assertEqual(rec.threads, 0, 'a declined scan started a worker')
        self.assertEqual(rec.statuses, [], 'a declined scan wrote a status')
        self.assertEqual(rec.logs, [], 'a declined scan wrote to the log')
        self.assertEqual(rec.button, [],
                         'a declined scan touched the scan button')
        self.assertEqual(rec.button_arm, [],
                         'a declined scan touched the arm scan button')
        self.assertEqual(rec.scheduled, [],
                         'a declined scan scheduled UI work (progress bar)')
        self.assertIsNone(owner.hardware.leader)
        self.assertIsNone(owner.hardware.follower)

    def test_declining_leaves_the_flags_exactly_as_it_found_them(self):
        """A stranded flag would disable scanning until the GUI restarts."""
        owner, _rec = self._drive(live_window=True, answer=False)
        self.assertFalse(owner._scanning)
        self.assertFalse(owner._scan_confirm_open)

    def test_declining_does_not_latch_the_button_off(self):
        """The student changes their mind: the second click must still ask."""
        _owner, rec = self._drive(live_window=True, answer=False, clicks=2)
        self.assertEqual(
            len(rec.asked), 2,
            'the second click was swallowed — declining once left the scan '
            'permanently guarded')
        self.assertEqual(rec.scans, 0)

    def test_declining_then_confirming_scans(self):
        _owner, rec = self._drive(live_window=True, answer=False)
        self.assertEqual(rec.scans, 0)
        _owner2, rec2 = self._drive(live_window=True, answer=True)
        self.assertEqual(rec2.scans, 1)

    # ── the hazard the guard exists for ──────────────────────────────

    def test_a_second_click_while_the_dialog_is_up_is_a_NO_OP(self):
        """Re-entrancy, encoded: `askyesno` pumps the Tk event loop.

        `_scanning` is not set until AFTER the dialog answers, and the button
        is still enabled, so without `_scan_confirm_open` a pending
        `root.after(200, self._scan_arms)` — which `_prompt_bind_arms` leaves
        behind after an elevated bind — stacks a SECOND dialog on the first and,
        on two confirms, races two workers onto the same /dev/serial ports.
        """
        _owner, rec = self._drive(live_window=True, answer=True, reenter=True)
        self.assertEqual(
            len(rec.asked), 1,
            'a second dialog stacked on the first while it was open')
        self.assertEqual(
            rec.scans, 1,
            'two arm scans ran from one click — both open the same '
            '/dev/serial ports and both would fail to identify')
        self.assertEqual(rec.threads, 1)
        self.assertEqual(rec.destroyed, 1)

    def test_the_reentrancy_guard_is_read_where_the_reentry_LANDS(self):
        """Source fence: `_scan_arms` must check it beside `_scanning`."""
        tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        body = _body_without_docstring(_func(tree, '_scan_arms'))
        guard = ast.unparse(body[0])
        self.assertIn(
            '_scan_confirm_open', guard,
            'the FIRST thing `_scan_arms` does is no longer the re-entrancy '
            'check — a `root.after` firing while the dialog pumps then stacks '
            'a second dialog and a second scan')
        self.assertIn('_scanning', guard)

    def test_the_guard_is_cleared_in_a_finally(self):
        """An exception out of the dialog must not strand the scan button."""
        tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))
        fn = _func(tree, '_confirm_arm_scan_closes_window')
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        self.assertTrue(tries, 'the dialog is not wrapped in try/finally')
        self.assertTrue(
            any('_scan_confirm_open = False'
                in '\n'.join(ast.unparse(s) for s in t.finalbody)
                for t in tries if t.finalbody),
            'the re-entrancy guard is not cleared in a `finally` — a raising '
            'dialog would leave „Arme scannen" permanently guarded')

    # ── the message the student actually reads ───────────────────────

    def test_the_message_names_both_things_that_are_lost(self):
        _owner, rec = self._drive(live_window=True, answer=True)
        title, message, _kw = rec.asked[0]
        self.assertEqual(title, 'Arme scannen')
        for phrase in ('geschlossen', 'abgemeldet',
                       'Nicht gespeicherte Änderungen gehen verloren',
                       'Trotzdem scannen?'):
            self.assertIn(phrase, message,
                          f'the confirmation no longer says „{phrase}"')

    def test_the_message_uses_literal_umlauts(self):
        """Rule §1: ae/oe/ue/ss is not German, it is a broken encoding."""
        _owner, rec = self._drive(live_window=True, answer=True)
        _title, message, _kw = rec.asked[0]
        self.assertIn('Änderungen', message)
        for bad in ('Aenderungen', 'aenderungen', 'Aenderung'):
            self.assertNotIn(bad, message)

    def test_the_dialog_defaults_to_NOT_scanning(self):
        """Same choice `_prompt_finalize_install`'s data-loss dialog makes.

        A stray Enter must not be the thing that ends the lesson.
        """
        _owner, rec = self._drive(live_window=True, answer=True)
        _title, _message, kw = rec.asked[0]
        self.assertEqual(kw.get('default'), 'no',
                         'the confirmation defaults to scanning — one stray '
                         'keypress then destroys the session it warns about')


class TheDialogIsOnlyOnThePathThatEndsASession(unittest.TestCase):
    """Where the confirmation must NOT appear, fenced one path at a time.

    Three separate places close or could close the child, and only one of them
    is a student mid-lesson clicking a button:

      * `_scan_arms._do_scan` runs on a WORKER thread — a Tk dialog raised
        there is a cross-thread call into the mainloop, which is undefined
        behaviour on Windows and can deadlock the GUI.
      * `_scan_cameras` ends no session and closes no window; a dialog there
        warns about something that will not happen.
      * `_run_prerequisite_checks_body` is a startup / post-failure path that
        four call sites re-run on worker threads. Same thread problem, plus it
        would put a modal in front of the GUI before the student has done
        anything.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding='utf-8'))

    def test_the_ARM_scan_asks_on_the_MAIN_thread_not_in_the_worker(self):
        """`_scan_arms` is the button's `command`; `_do_scan` is not.

        The proof that `_scan_arms` runs on the mainloop is in its own body:
        it reads tk StringVars (`_selected_robot_profile`) with a comment
        saying it must, because they are not thread-safe.
        """
        scan_arms = _func(self.tree, '_scan_arms')
        self.assertIn(
            '_confirm_arm_scan_closes_window', _dialog_calls(scan_arms),
            'the „Arme scannen" confirmation is gone — the student is signed '
            'out mid-lesson with no warning again')
        self.assertEqual(
            _dialog_calls(_nested(scan_arms, '_do_scan')), set(),
            'the confirmation moved into the scan worker — a Tk dialog from a '
            'non-mainloop thread is undefined behaviour on Windows, and by '
            'then the window is already being closed anyway')

    def test_the_CAMERA_scan_has_no_dialog(self):
        """It stops no environment, closes no window and ends no session."""
        self.assertEqual(
            _dialog_calls(_func(self.tree, '_scan_cameras')), set(),
            'the camera rescan now prompts about closing the student window — '
            'it does not close it, so the warning is false')

    def test_the_PREREQUISITE_cleanup_has_no_dialog(self):
        """Startup / post-failure, on worker threads, four call sites."""
        self.assertEqual(
            _dialog_calls(_func(self.tree, '_run_prerequisite_checks_body')),
            set(),
            'the prerequisite cleanup now prompts — it runs on worker threads '
            'from four call sites and none of them is a student clicking a '
            'button, so this is a modal nobody asked for on a thread that '
            'cannot safely raise one')

    def test_the_confirmation_precedes_EVERY_state_change(self):
        """A decline must reach no statement that mutates anything.

        Position, not intent: the gate is the guard plus the confirm call, and
        `_scanning = True` / the button disable must both sit below them.
        """
        body = _func(self.tree, '_scan_arms').body
        gate = next(
            i for i, stmt in enumerate(body)
            if '_confirm_arm_scan_closes_window' in ast.unparse(stmt))
        for marker in ('self._scanning = True',
                       'self.btn_scan_leader.config'):
            idx = next(i for i, stmt in enumerate(body)
                       if marker in ast.unparse(stmt))
            self.assertLess(
                gate, idx,
                f'„{marker}" runs before the confirmation — declining then '
                f'leaves the GUI mutated by a click the student took back')

    def test_the_gate_RETURNS_and_does_not_fall_through(self):
        """A decline that only skips a branch would still scan."""
        body = _func(self.tree, '_scan_arms').body
        gate = next(stmt for stmt in body
                    if '_confirm_arm_scan_closes_window' in ast.unparse(stmt))
        self.assertIsInstance(gate, ast.If)
        self.assertTrue(
            all(isinstance(s, ast.Return) for s in gate.body),
            'the declined branch does something other than return — the only '
            'correct answer to „nein" is to do nothing at all')


if __name__ == '__main__':
    unittest.main()
