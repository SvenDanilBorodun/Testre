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

WHAT CANNOT BE TESTED HERE. This suite runs on macOS/Linux; there is no
user32, no WinForms, no WebView2 and no tkinter root. So the WM_CLOSE call
itself is exercised only for its OFF-WINDOWS behaviour (a no-op returning 0),
and the SEQUENCE around it is driven through the module's own seam with a fake
process. That a WM_CLOSE really produces a DOM `pagehide` inside WebView2 is
stated in the design and is NOT proven by this file.

Deps-free: `webview_window` is stdlib-only and `_on_close` is read with `ast`
(importing gui_app would pull tkinter).
"""

import ast
import pathlib
import re
import sys
import unittest

from gui.app import webview_window

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


if __name__ == '__main__':
    unittest.main()
