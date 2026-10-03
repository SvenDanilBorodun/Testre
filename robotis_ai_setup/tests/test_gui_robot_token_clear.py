"""A student window that ends on its own clears the robot's Hugging Face token.

The token belongs to the student's ACCOUNT and sits in one tmpfs slot on the
robot. „Abmelden" clears it (the SPA calls `/register_hf_user` with an empty
token), but a student who just closes the window never signs out, so the next
person at the PC started on the previous student's token. The GUI is the one
party that SEES the window end (`webview_window._watch_subprocess`), so it asks
the NODE to clear the slot.

THREE DECISIONS THIS FILE HOLDS IN PLACE.

  * ONLY A NON-DELIBERATE END CLEARS. Every `destroy_all()` caller is followed
    by a container stop or a process exit, which already drops the tmpfs; a
    clear there would only race `compose down`. `destroy_all` therefore marks
    its own Popen, and the watcher reads that per-Popen mark, never the global
    `_deliberate_stop` (the NEXT spawn clears that one, so a late reader would
    classify the wrong spawn).
  * THE NODE DOES IT, THE FILE IS NEVER DELETED. The node refuses while a
    recording or an upload is running (that upload keeps its token), and it
    invalidates its namespace cache and publishes the new state. A `rm -f`
    from outside skips all three and makes a running `upload_large_folder`
    retry a failing call until the stall watchdog kills it.
  * A NEWER SESSION ALWAYS WINS. Every spawn (and the system-browser fallback)
    bumps a generation, and the retry loop asks whether its generation is still
    current after every sleep and again right before an attempt.

NOT EXECUTED HERE (no Windows, no WSL, no ROS): `wsl -d EduBotics -- bash`
with a nested `docker exec` on stdin, Jazzy's real `ros2 service call` output,
`timeout`/`nice` inside the image, and a real X-click producing rc 0. The
script is run under a real `bash` against stub `docker`/`ros2`; the rest is a
rig check.

Deps-free: stdlib only (tkinter, pywebview and pythonnet are never imported).
"""

import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

from gui.app import docker_manager, webview_window, wsl_bridge

_APP_DIR = pathlib.Path(webview_window.__file__).parent
_GUI_APP_SRC = _APP_DIR / "gui_app.py"
_SETUP_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REPO_ROOT = _SETUP_ROOT.parent
_CHANNEL_JS = (_REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src"
               / "features" / "hfToken" / "robotChannel.js")
_COMPOSE = _SETUP_ROOT / "docker" / "docker-compose.yml"

_WATCHER_NAME = "edubotics-webview-watchdog"


def _join_watchers(timeout=5.0):
    """Join every webview watcher thread. True when none is left alive."""
    deadline = time.monotonic() + timeout
    for thread in list(threading.enumerate()):
        if thread.name == _WATCHER_NAME:
            thread.join(max(0.0, deadline - time.monotonic()))
    return not any(t.name == _WATCHER_NAME and t.is_alive()
                   for t in threading.enumerate())


class _BlockingProc:
    """A child whose `wait()` (the watcher's call) blocks until `end()`.

    `terminate()` and `kill()` deliberately do NOT end it: a child that dies
    slowly after `destroy_all` is exactly the case where the watcher wakes up
    AFTER the next spawn has cleared the global `_deliberate_stop`. The
    `wait(timeout=…)` that `destroy_all` makes itself returns at once.
    """

    pid = 4343

    def __init__(self):
        self.rc = 0
        self.released = threading.Event()
        self.terminated = 0

    def poll(self):
        return self.rc if self.released.is_set() else None

    def wait(self, timeout=None):
        if timeout is not None:
            return self.rc
        if not self.released.wait(10.0):
            raise subprocess.TimeoutExpired("fake-webview-child", 10.0)
        return self.rc

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.terminated += 1

    def end(self, rc=0):
        self.rc = rc
        self.released.set()


class _WebviewRig(unittest.TestCase):
    """Real `webview_window` module, fake Popen, no real child, no real window."""

    def setUp(self):
        self.procs = []
        self.watchers = []
        self.commands = []
        self.calls = []
        self.called = threading.Event()

        def _fake_popen(cmd, **_kw):
            self.commands.append(cmd)
            proc = _BlockingProc()
            self.procs.append(proc)
            return proc

        self._patches = [
            mock.patch.object(webview_window, "is_available", lambda: True),
            mock.patch.object(webview_window.subprocess, "Popen", _fake_popen),
            mock.patch.object(webview_window, "_post_close_to_pid", lambda _pid: 0),
        ]
        for patch in self._patches:
            patch.start()
        webview_window._process = None
        webview_window.set_exit_callback(None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # Unblock every fake child and watcher first, so no thread outlives the
        # test and `discover` (one shared process) sees a clean module after it.
        webview_window.set_exit_callback(None)
        for proc in self.procs:
            proc.end()
        _join_watchers()
        for patch in reversed(self._patches):
            patch.stop()
        webview_window._process = None
        webview_window._runtime_missing.clear()
        webview_window._deliberate_stop.clear()

    def _record(self, generation):
        self.calls.append(generation)
        self.called.set()

    def _spawn(self):
        before = set(threading.enumerate())
        self.assertTrue(webview_window.open_student_window("http://localhost/?x=1"))
        self.watchers.extend(
            t for t in threading.enumerate()
            if t not in before and t.name == _WATCHER_NAME)
        return self.procs[-1]


class TheWatcherReportsAWindowThatEndedOnItsOwn(_WebviewRig):

    def test_a_window_the_student_closes_reports_its_generation_once(self):
        webview_window.set_exit_callback(self._record)
        proc = self._spawn()
        generation = webview_window.current_generation()
        proc.end(0)
        self.assertTrue(self.called.wait(5.0), "the exit callback never ran")
        self.assertTrue(_join_watchers())
        self.assertEqual(self.calls, [generation])

    def test_a_live_window_reports_nothing(self):
        webview_window.set_exit_callback(self._record)
        self._spawn()
        time.sleep(0.05)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.called.is_set())

    def test_a_late_crash_also_reports(self):
        """rc != 0 with `_runtime_missing` set: the window just vanishes."""
        webview_window.set_exit_callback(self._record)
        proc = self._spawn()
        proc.end(1)
        self.assertTrue(self.called.wait(5.0))
        self.assertTrue(_join_watchers())
        self.assertEqual(len(self.calls), 1)

    def test_a_fast_exit_reports_AND_flags_the_runtime_missing(self):
        """rc 3: the GUI's browser fallback fires and supersedes (see below)."""
        webview_window.set_exit_callback(self._record)
        proc = self._spawn()
        proc.end(3)
        self.assertTrue(self.called.wait(5.0))
        self.assertTrue(_join_watchers())
        self.assertTrue(webview_window.runtime_missing())
        self.assertEqual(len(self.calls), 1)

    def test_destroy_all_is_never_reported(self):
        webview_window.set_exit_callback(self._record)
        proc = self._spawn()
        webview_window.destroy_all()
        proc.end(1)
        self.assertTrue(_join_watchers(), "the watcher did not end")
        self.assertEqual(self.calls, [])

    def test_destroy_all_is_not_reported_even_when_released_after_a_new_spawn(self):
        """The reason the mark is per Popen: the NEXT spawn clears the global
        `_deliberate_stop`, so a watcher that wakes late and read it would take
        the first window's deliberate close for the student's own."""
        webview_window.set_exit_callback(self._record)
        first = self._spawn()
        webview_window.destroy_all()
        second = self._spawn()
        self.assertFalse(
            webview_window._deliberate_stop.is_set(),
            "the second spawn no longer clears the global flag: this test "
            "does not exercise the late-reader case")
        first.end(1)
        # Only the first watcher ends; the second child is still alive.
        self.watchers[0].join(5.0)
        self.assertFalse(self.watchers[0].is_alive(), "the first watcher did not end")
        self.assertEqual(self.calls, [], "a destroy_all close was reported")
        second.end(0)
        self.assertTrue(self.called.wait(5.0))
        self.assertEqual(len(self.calls), 1)

    def test_a_raising_callback_ends_the_watcher_cleanly(self):
        def _boom(_generation):
            self.called.set()
            raise RuntimeError("boom")

        uncaught = []
        original = threading.excepthook
        threading.excepthook = lambda args: uncaught.append(args.exc_type)
        self.addCleanup(setattr, threading, "excepthook", original)
        webview_window.set_exit_callback(_boom)
        proc = self._spawn()
        proc.end(0)
        self.assertTrue(self.called.wait(5.0))
        self.assertTrue(_join_watchers(), "the watcher did not end")
        self.assertEqual(uncaught, [], "the callback's exception escaped the watcher")

    def test_no_callback_ends_the_watcher_cleanly(self):
        proc = self._spawn()
        proc.end(0)
        self.assertTrue(_join_watchers())

    def test_set_exit_callback_none_removes_the_hook(self):
        webview_window.set_exit_callback(self._record)
        webview_window.set_exit_callback(None)
        proc = self._spawn()
        proc.end(0)
        self.assertTrue(_join_watchers())
        self.assertEqual(self.calls, [])


class TheWatcherIsHandedTheSessionItWasSpawnedUnder(unittest.TestCase):
    """Source fence: the generation and the Popen are captured INSIDE `_lock`.

    Reading `_process` (or the generation) after the lock was released sees
    whatever a racing `destroy_all` / second spawn left there. Not observable
    from a single-threaded test, so it is fenced on the AST.
    """

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(pathlib.Path(webview_window.__file__).read_text(encoding="utf-8"))
        cls.fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "open_student_window")

    def test_generation_is_assigned_from_begin_session_inside_the_lock(self):
        withs = [n for n in ast.walk(self.fn)
                 if isinstance(n, ast.With)
                 and any(ast.unparse(i.context_expr) == "_lock" for i in n.items)]
        self.assertEqual(len(withs), 1)
        assigned = [ast.unparse(n.value) for n in ast.walk(withs[0])
                    if isinstance(n, ast.Assign)
                    and [ast.unparse(t) for t in n.targets] == ["generation"]]
        self.assertEqual(assigned, ["begin_session()"])

    def test_the_thread_gets_the_captured_pair_not_a_fresh_read(self):
        threads = [n for n in ast.walk(self.fn)
                   if isinstance(n, ast.Call) and ast.unparse(n.func) == "threading.Thread"]
        self.assertEqual(len(threads), 1)
        args = [ast.unparse(k.value) for k in threads[0].keywords if k.arg == "args"]
        self.assertEqual(args, ["(proc, generation)"])
        self.assertNotIn("_process", ast.unparse(threads[0]))


class TheLegacyRuntimeMissingBranchIsUnchanged(_WebviewRig):
    """The new hook sits AFTER the old rc / `_deliberate_stop` logic, untouched."""

    def test_a_non_zero_exit_that_was_not_deliberate_flags_the_runtime(self):
        proc = self._spawn()
        proc.end(3)
        self.assertTrue(_join_watchers())
        self.assertTrue(webview_window.runtime_missing())

    def test_a_non_zero_exit_during_a_deliberate_stop_does_not(self):
        proc = self._spawn()
        webview_window.destroy_all()
        proc.end(1)
        self.assertTrue(_join_watchers())
        self.assertFalse(webview_window.runtime_missing())

    def test_a_clean_exit_does_not(self):
        proc = self._spawn()
        proc.end(0)
        self.assertTrue(_join_watchers())
        self.assertFalse(webview_window.runtime_missing())


class TheGenerationSupersedesQueuedCleanup(_WebviewRig):

    def test_every_spawn_is_a_new_generation(self):
        start = webview_window.current_generation()
        self._spawn()
        self.assertEqual(webview_window.current_generation(), start + 1)
        self.procs[-1].end(0)
        self.assertTrue(_join_watchers())
        self._spawn()
        self.assertEqual(webview_window.current_generation(), start + 2)

    def test_a_refused_spawn_is_a_new_generation_too(self):
        start = webview_window.current_generation()
        with mock.patch.object(webview_window, "is_available", lambda: False):
            self.assertFalse(webview_window.open_student_window("http://x/"))
        self.assertEqual(webview_window.current_generation(), start + 1)
        self.assertEqual(self.commands, [])

    def test_the_live_child_short_circuit_is_not_a_new_session(self):
        self._spawn()
        generation = webview_window.current_generation()
        self.assertTrue(webview_window.open_student_window("http://localhost/?x=2"))
        self.assertEqual(webview_window.current_generation(), generation)
        self.assertEqual(len(self.commands), 1)

    def test_begin_session_is_what_supersedes(self):
        self._spawn()
        first = webview_window.current_generation()
        self.assertTrue(webview_window.is_current_generation(first))
        returned = webview_window.begin_session()
        self.assertEqual(returned, first + 1)
        self.assertFalse(webview_window.is_current_generation(first))
        self.assertTrue(webview_window.is_current_generation(returned))

    def test_the_first_windows_generation_is_not_current_once_the_second_opened(self):
        webview_window.set_exit_callback(self._record)
        first = self._spawn()
        first_generation = webview_window.current_generation()
        first.end(0)
        self.assertTrue(self.called.wait(5.0))
        self.assertTrue(_join_watchers())
        self._spawn()
        self.assertFalse(webview_window.is_current_generation(first_generation))
        self.assertTrue(webview_window.is_current_generation(
            webview_window.current_generation()))

    def test_the_watcher_gets_the_generation_captured_under_the_lock(self):
        """Two windows in a row: each callback carries ITS spawn's generation,
        not whatever the module global says by the time the child exits."""
        webview_window.set_exit_callback(self._record)
        first = self._spawn()
        first_generation = webview_window.current_generation()
        first.end(0)
        self.assertTrue(_join_watchers())
        second = self._spawn()
        second_generation = webview_window.current_generation()
        second.end(0)
        self.assertTrue(_join_watchers())
        self.assertEqual(self.calls, [first_generation, second_generation])
        self.assertNotEqual(first_generation, second_generation)


class ABlockingCallbackBlocksNothingElse(_WebviewRig):

    def test_open_has_live_window_and_destroy_all_stay_prompt(self):
        gate = threading.Event()
        entered = threading.Event()

        def _slow(_generation):
            entered.set()
            gate.wait(10.0)

        self.addCleanup(gate.set)
        webview_window.set_exit_callback(_slow)
        first = self._spawn()
        first.end(0)
        self.assertTrue(entered.wait(5.0), "the callback never started")

        outcome = {}

        def _others():
            outcome["open"] = webview_window.open_student_window("http://localhost/?x=3")
            outcome["live"] = webview_window.has_live_window()
            webview_window.destroy_all()
            outcome["after"] = webview_window.has_live_window()

        worker = threading.Thread(target=_others, daemon=True)
        worker.start()
        worker.join(5.0)
        self.assertFalse(
            worker.is_alive(),
            "a blocking exit callback held up open_student_window, "
            "has_live_window or destroy_all")
        self.assertEqual(outcome, {"open": True, "live": True, "after": False})


# ── docker_manager ────────────────────────────────────────────────────────


def _js_string(name):
    text = _CHANNEL_JS.read_text(encoding="utf-8")
    matches = re.findall(rf"export const {name} = '([^'\\]*)';", text)
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _fake_run(stdout="", stderr="", returncode=0, record=None):
    def _run(cmd, timeout=30, check=True, distro=None):
        if record is not None:
            record.append({"cmd": cmd, "timeout": timeout, "check": check})
        return types.SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=returncode)
    return _run


class TheClearScriptIsFixedAndNeverTouchesTheFile(unittest.TestCase):

    script = docker_manager._HF_CLEAR_SCRIPT

    def test_it_calls_the_nodes_own_service_with_an_empty_token(self):
        self.assertIn(docker_manager.HF_CLEAR_SERVICE, self.script)
        self.assertIn(docker_manager.HF_CLEAR_SERVICE_TYPE, self.script)
        self.assertIn("physical_ai_server", self.script)
        self.assertIn(r'"{token: \"\"}"', self.script)

    def test_service_and_type_are_the_ones_the_web_client_calls(self):
        self.assertEqual(docker_manager.HF_CLEAR_SERVICE, _js_string("SET_SERVICE"))
        self.assertEqual(docker_manager.HF_CLEAR_SERVICE_TYPE, _js_string("SERVICE_TYPE"))

    def test_the_container_is_the_servers_compose_name(self):
        self.assertRegex(
            _COMPOSE.read_text(encoding="utf-8"),
            r"(?m)^\s*container_name:\s*physical_ai_server\s*$")
        self.assertIn("c=physical_ai_server\n", self.script)
        self.assertIn("physical_ai_server", docker_manager.PROJECT_CONTAINERS)

    def test_it_never_deletes_or_writes_a_file(self):
        for banned in (r"\brm\b", r"\bunlink\b", r"\btruncate\b", r"\btee\b",
                       r"\bshred\b", r"\bmktemp\b", "/run/edubotics-hf"):
            with self.subTest(banned):
                self.assertIsNone(re.search(banned, self.script), banned)
        # The only redirections allowed are the three that discard or merge.
        remainder = self.script
        for allowed in ("2>/dev/null", "2>&1", "</dev/null"):
            remainder = remainder.replace(allowed, "")
        self.assertNotIn(">", remainder, "a redirect that can write a file")
        self.assertNotIn("<", remainder)

    def test_the_slot_path_is_only_ever_tested_never_written(self):
        uses = [ln for ln in self.script.splitlines() if "HF_TOKEN_PATH" in ln]
        self.assertEqual(len(uses), 1, uses)
        self.assertIn('[ -s "$HF_TOKEN_PATH" ]', uses[0])

    def test_it_contains_no_token_shaped_text(self):
        # `/register_hf_user` legitimately contains "hf_"; a token is hf_ plus
        # a long run of alphanumerics.
        self.assertIsNone(re.search(r"hf_[A-Za-z0-9]{10,}", self.script))
        self.assertNotIn("HF_TOKEN=", self.script)

    def test_every_docker_exec_reads_dev_null(self):
        execs = [ln for ln in self.script.splitlines() if "docker exec" in ln]
        self.assertEqual(len(execs), 2)
        for line in execs:
            self.assertIn("</dev/null", line)

    def test_the_cli_is_bounded_and_niced(self):
        self.assertIn("timeout -k 1 8 nice -n 19 ros2 service call", self.script)

    def test_each_outcome_is_printed_with_the_sentinel_the_parser_reads(self):
        for outcome in docker_manager._HF_CLEAR_ANSWERS:
            with self.subTest(outcome):
                self.assertIn(docker_manager.HF_CLEAR_SENTINEL + outcome, self.script)
        self.assertNotIn(
            docker_manager.HF_CLEAR_SENTINEL + docker_manager.HF_CLEAR_SUPERSEDED,
            self.script)

    def test_the_script_is_what_the_stdin_carrier_gets(self):
        record = []
        with mock.patch.object(
                docker_manager.wsl_bridge, "run",
                _fake_run("EDUBOTICS_HF_CLEAR=empty\n", record=record)):
            docker_manager.clear_robot_hf_token()
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["cmd"], docker_manager._HF_CLEAR_SCRIPT)
        self.assertIs(record[0]["check"], False)
        self.assertEqual(record[0]["timeout"], docker_manager.HF_CLEAR_EXEC_TIMEOUT_S)


class TheSentinelIsTheOnlyThingRead(unittest.TestCase):

    def _clear(self, **kw):
        with mock.patch.object(docker_manager.wsl_bridge, "run", _fake_run(**kw)):
            return docker_manager.clear_robot_hf_token()

    def test_the_five_outcomes(self):
        for outcome in ("cleared", "empty", "no_container", "refused", "unreachable"):
            with self.subTest(outcome):
                self.assertEqual(
                    self._clear(stdout=f"EDUBOTICS_HF_CLEAR={outcome}\n"), outcome)

    def test_surrounding_noise_and_crlf_are_tolerated(self):
        self.assertEqual(
            self._clear(stdout="wsl: some warning\r\nEDUBOTICS_HF_CLEAR=cleared\r\n"),
            "cleared")

    def test_the_exit_code_is_ignored(self):
        self.assertEqual(
            self._clear(stdout="EDUBOTICS_HF_CLEAR=cleared\n", returncode=3), "cleared")

    def test_no_sentinel_is_unreachable(self):
        self.assertEqual(self._clear(stdout="hello\n"), "unreachable")
        self.assertEqual(self._clear(stdout=""), "unreachable")

    def test_a_sentinel_on_stderr_only_is_unreachable(self):
        self.assertEqual(
            self._clear(stdout="", stderr="EDUBOTICS_HF_CLEAR=cleared\n"),
            "unreachable")

    def test_an_unknown_or_internal_value_is_unreachable(self):
        self.assertEqual(self._clear(stdout="EDUBOTICS_HF_CLEAR=weird\n"), "unreachable")
        self.assertEqual(
            self._clear(stdout="EDUBOTICS_HF_CLEAR=superseded\n"), "unreachable")

    def test_a_non_string_stdout_is_unreachable(self):
        self.assertEqual(self._clear(stdout=None), "unreachable")

    def test_every_exception_is_unreachable_and_never_raised(self):
        for exc in (wsl_bridge.WSLError("timed out after 15s"),
                    FileNotFoundError("wsl"), OSError("broken pipe"),
                    RuntimeError("anything"), subprocess.TimeoutExpired("wsl", 15)):
            with self.subTest(type(exc).__name__):
                with mock.patch.object(
                        docker_manager.wsl_bridge, "run", side_effect=exc):
                    self.assertEqual(docker_manager.clear_robot_hf_token(), "unreachable")


def _stub_scripts():
    return {
        "docker": textwrap.dedent(r"""
            #!/bin/bash
            printf '%s\n' "docker $*" >> "$STUB_DIR/calls.log"
            case "$1" in
              inspect)
                [ "$STUB_RUNNING" = "error" ] && exit 1
                echo "$STUB_RUNNING"; exit 0;;
              exec)
                case "$3" in
                  sh)
                    [ "$STUB_SLOT" = "fail" ] && exit 1
                    echo "$STUB_SLOT"; exit 0;;
                  bash)
                    printf '%s' "$5" > "$STUB_DIR/inner.txt"
                    [ "$STUB_ROS" = "fail" ] && exit 1
                    printf '%s\n' "$STUB_ROS_OUT"; exit 0;;
                esac;;
            esac
            exit 99
        """).lstrip(),
        "ros2": textwrap.dedent(r"""
            #!/bin/bash
            printf '%s\n' "$@" > "$STUB_DIR/ros2_argv.txt"
            printf '%s\n' "$STUB_ROS_OUT"
        """).lstrip(),
        "timeout": textwrap.dedent(r"""
            #!/bin/bash
            printf '%s\n' "timeout $*" >> "$STUB_DIR/calls.log"
            [ "$1" = "-k" ] && shift 2
            shift
            exec "$@"
        """).lstrip(),
        "nice": textwrap.dedent(r"""
            #!/bin/bash
            printf '%s\n' "nice $*" >> "$STUB_DIR/calls.log"
            [ "$1" = "-n" ] && shift 2
            exec "$@"
        """).lstrip(),
    }


_ROS_OK = ("requester: making request: physical_ai_interfaces.srv.SetHFUser_Request"
           "(token='')\n\nresponse:\nphysical_ai_interfaces.srv.SetHFUser_Response"
           "(user_id_list=[], success=True, message='ok')")
_ROS_REFUSED = _ROS_OK.replace("success=True", "success=False")


@unittest.skipIf(sys.platform == "win32", "needs a POSIX bash with stub executables")
@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TheScriptRunsUnderRealBashAgainstStubs(unittest.TestCase):
    """The shipped statements, executed: quoting, `case` arms and sentinels.

    What this cannot prove (rig check): that `wsl.exe` hands the stdin script
    to bash unchanged, and what the real `ros2 service call` prints.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        for name, body in _stub_scripts().items():
            path = self.dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        (self.dir / "ros_setup.bash").write_text("", encoding="utf-8")
        (self.dir / "ws_setup.bash").write_text("", encoding="utf-8")

    def _env(self, **stub):
        env = {
            "PATH": str(self.dir) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_DIR": str(self.dir),
            "STUB_RUNNING": "true",
            "STUB_SLOT": "present",
            "STUB_ROS": "ok",
            "STUB_ROS_OUT": _ROS_OK,
        }
        env.update(stub)
        return env

    def _run_script(self, **stub):
        result = subprocess.run(
            ["bash"], input=docker_manager._HF_CLEAR_SCRIPT.encode("utf-8"),
            env=self._env(**stub), capture_output=True, timeout=30)
        stdout = result.stdout.decode("utf-8", "replace")
        sentinel_lines = [ln for ln in stdout.splitlines()
                          if ln.startswith(docker_manager.HF_CLEAR_SENTINEL)]
        self.assertEqual(len(sentinel_lines), 1, (stdout, result.stderr))
        self.assertEqual(result.returncode, 0, result.stderr)
        return docker_manager._hf_clear_outcome(stdout)

    def test_a_stopped_container_is_no_container(self):
        self.assertEqual(self._run_script(STUB_RUNNING="false"), "no_container")
        self.assertEqual(self._run_script(STUB_RUNNING="error"), "no_container")
        self.assertFalse((self.dir / "inner.txt").exists())

    def test_an_empty_slot_never_starts_the_ros_cli(self):
        self.assertEqual(self._run_script(STUB_SLOT="empty"), "empty")
        self.assertFalse((self.dir / "inner.txt").exists())

    def test_a_probe_that_fails_is_unreachable_without_the_cli(self):
        self.assertEqual(self._run_script(STUB_SLOT="fail"), "unreachable")
        self.assertFalse((self.dir / "inner.txt").exists())

    def test_the_node_clearing_is_cleared(self):
        self.assertEqual(self._run_script(), "cleared")

    def test_the_node_refusing_is_refused(self):
        self.assertEqual(self._run_script(STUB_ROS_OUT=_ROS_REFUSED), "refused")

    def test_a_service_that_never_answers_is_unreachable(self):
        self.assertEqual(
            self._run_script(STUB_ROS_OUT="waiting for service to become available..."),
            "unreachable")
        self.assertEqual(self._run_script(STUB_ROS="fail"), "unreachable")

    def test_the_inner_command_reaches_ros2_with_the_exact_argv(self):
        self.assertEqual(self._run_script(), "cleared")
        inner = (self.dir / "inner.txt").read_text(encoding="utf-8")
        self.assertTrue(inner.endswith(
            'ros2 service call /register_hf_user '
            'physical_ai_interfaces/srv/SetHFUser "{token: \\"\\"}"'), inner)
        for path in ("/opt/ros/jazzy/setup.bash", "/root/ros2_ws/install/setup.bash"):
            self.assertIn(f"source {path}", inner)
        patched = (inner
                   .replace("/opt/ros/jazzy/setup.bash", str(self.dir / "ros_setup.bash"))
                   .replace("/root/ros2_ws/install/setup.bash",
                            str(self.dir / "ws_setup.bash")))
        self.assertNotEqual(patched, inner)
        result = subprocess.run(
            ["bash", "-c", patched], env=self._env(), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.dir / "ros2_argv.txt").read_text(encoding="utf-8").splitlines(),
            ["service", "call", "/register_hf_user",
             "physical_ai_interfaces/srv/SetHFUser", '{token: ""}'])
        calls = (self.dir / "calls.log").read_text(encoding="utf-8")
        self.assertIn("timeout -k 1 8 nice -n 19 ros2 service call", calls)
        self.assertIn("nice -n 19 ros2 service call", calls)

    def test_every_docker_call_goes_to_the_server_container(self):
        self._run_script()
        docker_calls = [ln for ln in (self.dir / "calls.log").read_text(
            encoding="utf-8").splitlines() if ln.startswith("docker ")]
        self.assertEqual(len(docker_calls), 3, docker_calls)
        self.assertIn("inspect", docker_calls[0])
        for line in docker_calls:
            self.assertIn("physical_ai_server", line)


class TheRetryLoop(unittest.TestCase):
    """`clear_robot_hf_token_after_window_close` with every seam injected."""

    def setUp(self):
        self.attempts = 0
        self.sleeps = []
        self.logs = []
        self.current = True
        self.answers = []

    def _attempt(self):
        index = self.attempts
        self.attempts += 1
        answer = self.answers[min(index, len(self.answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _run(self, **kw):
        kwargs = dict(
            attempt=self._attempt, sleep=self.sleeps.append,
            log=self.logs.append)
        kwargs.update(kw)
        return docker_manager.clear_robot_hf_token_after_window_close(
            7, lambda generation: self.current and generation == 7, **kwargs)

    def test_cleared_on_the_first_try_is_one_attempt_no_sleep_no_log(self):
        self.answers = ["cleared"]
        self.assertEqual(self._run(), "cleared")
        self.assertEqual((self.attempts, self.sleeps, self.logs), (1, [], []))

    def test_empty_and_no_container_are_one_attempt(self):
        for outcome in ("empty", "no_container"):
            with self.subTest(outcome):
                self.attempts, self.sleeps, self.logs = 0, [], []
                self.answers = [outcome]
                self.assertEqual(self._run(), outcome)
                self.assertEqual((self.attempts, self.sleeps, self.logs), (1, [], []))

    def test_superseded_before_the_first_attempt_never_attempts(self):
        self.current = False
        self.answers = ["cleared"]
        self.assertEqual(self._run(), "superseded")
        self.assertEqual((self.attempts, self.sleeps, self.logs), (0, [], []))

    def test_superseded_between_attempts_stops_silently(self):
        self.answers = ["refused"]

        def _sleep(seconds):
            self.sleeps.append(seconds)
            self.current = False

        self.assertEqual(self._run(sleep=_sleep), "superseded")
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.sleeps, [docker_manager.HF_CLEAR_RETRY_DELAYS_S[0]])
        self.assertEqual(self.logs, [])

    def test_superseded_while_the_lock_is_awaited_never_attempts(self):
        """Supersession that lands after the first check but before the attempt."""
        self.answers = ["cleared"]
        checks = []

        def _is_current(_generation):
            checks.append(1)
            return len(checks) < 2

        outcome = docker_manager.clear_robot_hf_token_after_window_close(
            7, _is_current, log=self.logs.append, attempt=self._attempt,
            sleep=self.sleeps.append)
        self.assertEqual(outcome, "superseded")
        self.assertEqual(self.attempts, 0)

    def test_refused_five_times_is_five_attempts_the_delays_and_one_info_line(self):
        self.answers = ["refused"]
        self.assertEqual(self._run(), "refused")
        self.assertEqual(self.attempts, 5)
        self.assertEqual(self.sleeps, list(docker_manager.HF_CLEAR_RETRY_DELAYS_S))
        self.assertEqual(self.logs, [docker_manager.HF_CLEAR_REFUSED_DE])
        self.assertTrue(self.logs[0].startswith("[INFO] "))

    def test_unreachable_five_times_is_one_warning_line(self):
        self.answers = ["unreachable"]
        self.assertEqual(self._run(), "unreachable")
        self.assertEqual(self.attempts, 5)
        self.assertEqual(self.logs, [docker_manager.HF_CLEAR_UNREACHABLE_DE])
        self.assertTrue(self.logs[0].startswith("[WARNUNG] "))

    def test_the_last_answer_picks_the_line(self):
        self.answers = ["unreachable", "refused"]
        self.assertEqual(self._run(), "refused")
        self.assertEqual(self.logs, [docker_manager.HF_CLEAR_REFUSED_DE])

    def test_refused_then_cleared_retries_and_logs_nothing(self):
        self.answers = ["refused", "refused", "cleared"]
        self.assertEqual(self._run(), "cleared")
        self.assertEqual(self.attempts, 3)
        self.assertEqual(self.sleeps, list(docker_manager.HF_CLEAR_RETRY_DELAYS_S[:2]))
        self.assertEqual(self.logs, [])

    def test_superseded_after_the_last_attempt_logs_nothing(self):
        self.answers = ["refused"]
        calls = []

        def _attempt():
            calls.append(1)
            if len(calls) == 5:
                self.current = False
            return "refused"

        self.assertEqual(self._run(attempt=_attempt), "refused")
        self.assertEqual(len(calls), 5)
        self.assertEqual(self.logs, [])

    def test_a_raising_attempt_is_an_unreachable_answer(self):
        self.answers = [RuntimeError("boom")]
        self.assertEqual(self._run(), "unreachable")
        self.assertEqual(self.attempts, 5)
        self.assertEqual(self.logs, [docker_manager.HF_CLEAR_UNREACHABLE_DE])

    def test_a_raising_log_does_not_propagate_and_the_lock_is_free_after(self):
        self.answers = ["refused"]

        def _bad_log(_line):
            raise OSError("log sink gone")

        self.assertEqual(self._run(log=_bad_log), "refused")
        self.answers = ["cleared"]
        self.attempts = 0
        self.assertEqual(self._run(), "cleared")

    def test_an_unknown_answer_counts_as_unreachable(self):
        self.answers = ["bananas"]
        self.assertEqual(self._run(), "unreachable")
        self.assertEqual(self.logs, [docker_manager.HF_CLEAR_UNREACHABLE_DE])

    def test_a_is_current_that_raises_reads_as_superseded(self):
        def _boom(_generation):
            raise RuntimeError("no answer")

        self.answers = ["cleared"]
        outcome = docker_manager.clear_robot_hf_token_after_window_close(
            7, _boom, attempt=self._attempt, sleep=self.sleeps.append)
        self.assertEqual(outcome, "superseded")
        self.assertEqual(self.attempts, 0)

    def test_no_log_callable_is_fine(self):
        self.answers = ["refused"]
        self.assertEqual(self._run(log=None), "refused")

    def test_the_default_attempt_is_resolved_at_call_time(self):
        with mock.patch.object(
                docker_manager, "clear_robot_hf_token", return_value="cleared") as fn:
            outcome = docker_manager.clear_robot_hf_token_after_window_close(
                7, lambda _g: True, sleep=self.sleeps.append)
        self.assertEqual(outcome, "cleared")
        fn.assert_called_once_with()

    def test_the_budget_is_five_attempts_about_eight_and_a_half_minutes(self):
        delays = docker_manager.HF_CLEAR_RETRY_DELAYS_S
        self.assertEqual(delays, (15, 45, 120, 300))
        worst_case = sum(delays) + (len(delays) + 1) * docker_manager.HF_CLEAR_EXEC_TIMEOUT_S
        self.assertLessEqual(worst_case, 10 * 60)

    def test_no_two_threads_are_ever_inside_an_attempt(self):
        inside = []
        peak = []
        guard = threading.Lock()
        done = []

        def _attempt():
            with guard:
                inside.append(1)
                peak.append(len(inside))
            time.sleep(0.02)
            with guard:
                inside.pop()
                done.append(1)
            return "cleared"

        threads = [
            threading.Thread(
                target=docker_manager.clear_robot_hf_token_after_window_close,
                args=(7, lambda _g: True),
                kwargs={"attempt": _attempt, "sleep": lambda _s: None},
                daemon=True)
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        self.assertEqual(len(done), 4)
        self.assertEqual(max(peak), 1, "two attempts ran at the same time")


class TheStudentVisibleLinesFollowTheGuiRules(unittest.TestCase):

    LINES = (docker_manager.HF_CLEAR_REFUSED_DE, docker_manager.HF_CLEAR_UNREACHABLE_DE)

    def test_markers_are_from_the_closed_vocabulary(self):
        self.assertTrue(docker_manager.HF_CLEAR_REFUSED_DE.startswith("[INFO] "))
        self.assertTrue(docker_manager.HF_CLEAR_UNREACHABLE_DE.startswith("[WARNUNG] "))

    def test_no_en_dash_and_real_umlauts(self):
        for line in self.LINES:
            with self.subTest(line[:20]):
                self.assertNotIn("–", line)
                self.assertRegex(line, r"[äöüß]")

    def test_no_token_fingerprint_or_output_can_be_in_a_line(self):
        for line in self.LINES:
            with self.subTest(line[:20]):
                self.assertIsNone(re.search(r"hf_[A-Za-z0-9]{10,}", line))
                self.assertNotIn("EDUBOTICS_HF_CLEAR", line)
                self.assertNotIn("{", line)


# ── gui_app wiring ────────────────────────────────────────────────────────


def _gui_tree():
    return ast.parse(_GUI_APP_SRC.read_text(encoding="utf-8"))


def _method(tree, name, cls="EduBoticsApp"):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"{cls}.{name} not found — this test is stale")


def _body(fn):
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _load_method(name, ns):
    """The shipped method source, compiled against doubles (no tkinter here)."""
    src = _GUI_APP_SRC.read_text(encoding="utf-8")
    marker = f"    def {name}(self"
    start = src.index(marker)
    rest = src[start:]
    end = rest.find("\n    def ", len(marker))
    snippet = textwrap.dedent(rest[:end if end != -1 else len(rest)])
    exec(compile(snippet, str(_GUI_APP_SRC), "exec"), ns)  # noqa: S102
    return ns[name]


def _is_call_of(stmt, owner, attr):
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == attr
            and ast.unparse(stmt.value.func.value) == owner)


class TheGuiRegistersAndForwards(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = _gui_tree()

    def test_init_registers_the_hook_right_after_the_legacy_purge(self):
        stmts = _body(_method(self.tree, "__init__"))
        purge = [i for i, s in enumerate(stmts)
                 if _is_call_of(s, "self", "_purge_legacy_hf_token")]
        self.assertEqual(len(purge), 1)
        nxt = stmts[purge[0] + 1]
        self.assertTrue(
            _is_call_of(nxt, "webview_window", "set_exit_callback"), ast.unparse(nxt))
        self.assertEqual(
            ast.unparse(nxt.value.args[0]), "self._on_student_window_closed")
        self.assertEqual(
            sum(1 for s in ast.walk(_method(self.tree, "__init__"))
                if isinstance(s, ast.Attribute) and s.attr == "set_exit_callback"), 1)

    def test_the_hook_forwards_generation_the_predicate_and_the_log(self):
        calls = []
        sentinel = object()
        docker = types.SimpleNamespace(
            clear_robot_hf_token_after_window_close=lambda *a, **k: calls.append((a, k)))
        window = types.SimpleNamespace(is_current_generation=sentinel)
        hook = _load_method("_on_student_window_closed",
                            {"docker_manager": docker, "webview_window": window})
        owner = types.SimpleNamespace(_log=lambda _m: None)
        self.assertIsNone(hook(owner, 41))
        self.assertEqual(calls, [((41, sentinel), {"log": owner._log})])
        self.assertIs(calls[0][1]["log"], owner._log)

    def test_the_hook_swallows_a_raise(self):
        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        hook = _load_method(
            "_on_student_window_closed",
            {"docker_manager": types.SimpleNamespace(
                clear_robot_hf_token_after_window_close=_boom),
             "webview_window": types.SimpleNamespace(is_current_generation=None)})
        self.assertIsNone(hook(types.SimpleNamespace(_log=lambda _m: None), 1))

    def test_the_hook_runs_off_the_tk_thread_and_names_no_widget(self):
        code = "\n".join(
            ast.unparse(s) for s in _body(_method(self.tree, "_on_student_window_closed")))
        for banned in ("self.root", "messagebox", ".get(", ".set(", ".after("):
            self.assertNotIn(banned, code)

    def test_the_clear_is_referenced_nowhere_else_in_gui_app(self):
        hits = []
        for node in ast.walk(self.tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name and name.startswith("clear_robot_hf_token"):
                hits.append((node.lineno, name))
        hook = _method(self.tree, "_on_student_window_closed")
        inside = [h for h in hits if hook.lineno <= h[0] <= hook.end_lineno]
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(inside, hits, "the clear is called outside its hook")

    def test_the_fallback_starts_a_new_session_before_any_dialog(self):
        fn = _method(self.tree, "_webview_fallback")
        stmts = _body(fn)
        self.assertTrue(_is_call_of(stmts[0], "webview_window", "begin_session"),
                        ast.unparse(stmts[0]))
        dialog_lines = [n.lineno for n in ast.walk(fn)
                        if isinstance(n, ast.Name) and n.id == "messagebox"]
        self.assertTrue(dialog_lines)
        self.assertLess(stmts[0].lineno, min(dialog_lines))

    def test_the_gui_still_has_no_token_surface(self):
        src = _GUI_APP_SRC.read_text(encoding="utf-8")
        for gone in ("hf_token_status", "hf_token_var", "hf_token_entry", "Schritt D:"):
            self.assertNotIn(gone, src)


class TheDeliberateCloseIsMarkedBeforeItIsRequested(unittest.TestCase):
    """Every `destroy_all` caller is followed by a container stop or an exit."""

    def test_destroy_all_marks_its_own_popen_before_it_asks_the_child_to_close(self):
        tree = ast.parse(pathlib.Path(webview_window.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "destroy_all")
        mark = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "setattr"]
        close = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_post_close_to_pid"]
        terminate = [n.lineno for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "terminate"]
        self.assertEqual(len(mark), 1)
        self.assertTrue(close and terminate)
        self.assertLess(mark[0], min(close))
        self.assertLess(mark[0], min(terminate))


# ── tkinter-free import ───────────────────────────────────────────────────


class TheTwoModulesImportWithoutTheGuiToolkits(unittest.TestCase):

    def test_webview_window_and_docker_manager_import_without_tk_or_pywebview(self):
        code = (
            "import sys\n"
            "for name in ('tkinter', 'webview', 'clr'):\n"
            "    sys.modules[name] = None\n"
            "import gui.app.webview_window, gui.app.docker_manager\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(_SETUP_ROOT),
            capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
