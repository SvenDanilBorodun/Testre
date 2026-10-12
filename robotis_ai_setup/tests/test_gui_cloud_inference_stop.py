"""A student window that ends on its own stops a running Cloud-Inferenz run first (spec §5.10, D17).

The robot drives the arm from the cloud's plans while the student's page heartbeats the cloud
API; a window that just closes never says „Stoppen". The Windows GUI is the one party that SEES
the window end (``webview_window._watch_subprocess``), so before it clears the robot's Hugging
Face token (which may sleep for minutes) it asks the NODE, once, to end a cloud run:
``/edubotics/cloud_inference/stop`` (std_srvs/Trigger), which ends ONLY a cloud run.

THE DECISIONS THIS FILE HOLDS IN PLACE.

  * ONE ATTEMPT, NO RETRY. The cloud API ends a run whose page went silent after 45 s on its
    own; that is the floor. A failed stop logs one German line, but only while the closed
    window's session is still the current one.
  * THE COMMON CASE STARTS NO ROS CLI. The node writes a marker file while a cloud run is active
    (``cloud_contract.ACTIVE_MARKER_PATH``); without it the script answers ``idle`` after a
    ``test -e`` and never spawns ``ros2``.
  * CLOUD STOP BEFORE THE HF CLEAR, in two separate ``try`` blocks of
    ``gui_app._on_student_window_closed``: a failing stop never skips the token clear.
  * A NEWER SESSION WINS: ``is_current(generation)`` is asked before the single-flight lock and
    again under it, right before the attempt.

NOT EXECUTED HERE (no Windows, no WSL, no ROS): ``wsl -d EduBotics -- bash`` with the script on
stdin, Jazzy's real ``ros2 service call`` output. The script runs under a real ``bash`` against
stub ``docker``/``ros2``; the rest is a rig check.

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
_CONTRACT = (_REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
             / "inference" / "cloud_contract.py")
_COMPOSE = _SETUP_ROOT / "docker" / "docker-compose.yml"


def _contract_constant(name):
    tree = ast.parse(_CONTRACT.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in cloud_contract.py")


def _fake_run(stdout="", stderr="", returncode=0, record=None):
    def _run(cmd, timeout=None, check=True, **kw):
        if record is not None:
            record.append({"cmd": cmd, "timeout": timeout, "check": check})
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return _run


class TheConstantsAreTheNodesOwn(unittest.TestCase):

    def test_service_and_marker_equal_cloud_contract(self):
        self.assertEqual(docker_manager.CLOUD_STOP_SERVICE, _contract_constant("STOP_SERVICE"))
        self.assertEqual(docker_manager.CLOUD_STOP_MARKER, _contract_constant("ACTIVE_MARKER_PATH"))
        self.assertEqual(docker_manager.CLOUD_STOP_SERVICE, "/edubotics/cloud_inference/stop")
        self.assertEqual(docker_manager.CLOUD_STOP_MARKER, "/tmp/edubotics-cloud-inference.active")

    def test_the_outcome_vocabulary(self):
        self.assertEqual(
            (docker_manager.CLOUD_STOP_STOPPED, docker_manager.CLOUD_STOP_IDLE,
             docker_manager.CLOUD_STOP_NO_CONTAINER, docker_manager.CLOUD_STOP_UNREACHABLE,
             docker_manager.CLOUD_STOP_SUPERSEDED),
            ("stopped", "idle", "no_container", "unreachable", "superseded"))
        self.assertEqual(docker_manager.CLOUD_STOP_SENTINEL, "EDUBOTICS_CLOUD_STOP=")
        self.assertEqual(docker_manager.CLOUD_STOP_EXEC_TIMEOUT_S, 15)


class TheStopScriptIsFixed(unittest.TestCase):

    script = docker_manager._CLOUD_STOP_SCRIPT

    def test_it_calls_the_stop_service_with_trigger_and_an_empty_request(self):
        self.assertIn(f'ros2 service call {docker_manager.CLOUD_STOP_SERVICE} std_srvs/srv/Trigger "{{}}"',
                      self.script)
        self.assertIn("c=physical_ai_server\n", self.script)
        self.assertRegex(_COMPOSE.read_text(encoding="utf-8"),
                         r"(?m)^\s*container_name:\s*physical_ai_server\s*$")

    def test_the_marker_is_only_tested_never_written(self):
        uses = [ln for ln in self.script.splitlines() if docker_manager.CLOUD_STOP_MARKER in ln]
        self.assertEqual(len(uses), 1, uses)
        self.assertIn(f"test -e {docker_manager.CLOUD_STOP_MARKER}", uses[0])

    def test_it_never_deletes_or_writes_a_file(self):
        for banned in (r"\brm\b", r"\bunlink\b", r"\btruncate\b", r"\btee\b", r"\btouch\b",
                       r"\bmktemp\b"):
            with self.subTest(banned):
                self.assertIsNone(re.search(banned, self.script), banned)
        remainder = self.script
        for allowed in ("2>/dev/null", "2>&1", "</dev/null"):
            remainder = remainder.replace(allowed, "")
        self.assertNotIn(">", remainder, "a redirect that can write a file")
        self.assertNotIn("<", remainder)

    def test_every_docker_exec_reads_dev_null(self):
        execs = [ln for ln in self.script.splitlines() if "docker exec" in ln]
        self.assertEqual(len(execs), 2)
        for line in execs:
            self.assertIn("</dev/null", line)

    def test_the_cli_is_bounded_and_niced(self):
        self.assertIn("timeout -k 1 8 nice -n 19 ros2 service call", self.script)

    def test_nothing_is_interpolated_and_no_token_shaped_text(self):
        self.assertNotIn("{0", self.script)
        self.assertNotIn("%s", self.script)
        self.assertIsNone(re.search(r"[A-Za-z0-9_-]{43}", self.script))
        self.assertNotIn("token", self.script.lower())

    def test_each_outcome_is_printed_with_the_sentinel_the_parser_reads(self):
        for outcome in ("stopped", "idle", "no_container", "unreachable"):
            with self.subTest(outcome):
                self.assertIn(docker_manager.CLOUD_STOP_SENTINEL + outcome, self.script)
        self.assertNotIn(docker_manager.CLOUD_STOP_SENTINEL + "superseded", self.script)

    def test_the_script_is_what_the_stdin_carrier_gets(self):
        record = []
        with mock.patch.object(docker_manager.wsl_bridge, "run",
                               _fake_run("EDUBOTICS_CLOUD_STOP=idle\n", record=record)):
            self.assertEqual(docker_manager.stop_cloud_inference(), "idle")
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["cmd"], docker_manager._CLOUD_STOP_SCRIPT)
        self.assertIs(record[0]["check"], False)
        self.assertEqual(record[0]["timeout"], docker_manager.CLOUD_STOP_EXEC_TIMEOUT_S)


class TheSentinelIsTheOnlyThingRead(unittest.TestCase):

    def _stop(self, **kw):
        with mock.patch.object(docker_manager.wsl_bridge, "run", _fake_run(**kw)):
            return docker_manager.stop_cloud_inference()

    def test_the_four_outcomes(self):
        for outcome in ("stopped", "idle", "no_container", "unreachable"):
            with self.subTest(outcome):
                self.assertEqual(self._stop(stdout=f"EDUBOTICS_CLOUD_STOP={outcome}\n"), outcome)

    def test_noise_crlf_and_the_exit_code_are_tolerated(self):
        self.assertEqual(
            self._stop(stdout="wsl: warning\r\nEDUBOTICS_CLOUD_STOP=stopped\r\n", returncode=5),
            "stopped")

    def test_anything_else_is_unreachable(self):
        for stdout in ("", "hello\n", "EDUBOTICS_CLOUD_STOP=weird\n",
                       "EDUBOTICS_CLOUD_STOP=superseded\n", None, b"EDUBOTICS_CLOUD_STOP=idle"):
            with self.subTest(stdout=stdout):
                self.assertEqual(self._stop(stdout=stdout), "unreachable")
        self.assertEqual(self._stop(stdout="", stderr="EDUBOTICS_CLOUD_STOP=stopped\n"), "unreachable")

    def test_every_exception_is_unreachable_and_never_raised(self):
        for exc in (wsl_bridge.WSLError("timed out after 15s"), FileNotFoundError("wsl"),
                    OSError("broken pipe"), RuntimeError("anything"),
                    subprocess.TimeoutExpired("wsl", 15)):
            with self.subTest(type(exc).__name__):
                with mock.patch.object(docker_manager.wsl_bridge, "run", side_effect=exc):
                    self.assertEqual(docker_manager.stop_cloud_inference(), "unreachable")


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
                  test)
                    [ "$STUB_MARKER" = "present" ] && exit 0
                    exit 1;;
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


_ROS_OK = ("requester: making request: std_srvs.srv.Trigger_Request()\n\nresponse:\n"
           "std_srvs.srv.Trigger_Response(success=True, message='Cloud-Inferenz gestoppt.')")
_ROS_IDLE = ("requester: making request: std_srvs.srv.Trigger_Request()\n\nresponse:\n"
             "std_srvs.srv.Trigger_Response(success=False, message='Gerade läuft keine Cloud-Inferenz.')")


@unittest.skipIf(sys.platform == "win32", "needs a POSIX bash with stub executables")
@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TheScriptRunsUnderRealBashAgainstStubs(unittest.TestCase):

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
            "STUB_MARKER": "present",
            "STUB_ROS": "ok",
            "STUB_ROS_OUT": _ROS_OK,
        }
        env.update(stub)
        return env

    def _run_script(self, **stub):
        result = subprocess.run(
            ["bash"], input=docker_manager._CLOUD_STOP_SCRIPT.encode("utf-8"),
            env=self._env(**stub), capture_output=True, timeout=30)
        stdout = result.stdout.decode("utf-8", "replace")
        sentinel_lines = [ln for ln in stdout.splitlines()
                          if ln.startswith(docker_manager.CLOUD_STOP_SENTINEL)]
        self.assertEqual(len(sentinel_lines), 1, (stdout, result.stderr))
        self.assertEqual(result.returncode, 0, result.stderr)
        return docker_manager._cloud_stop_outcome(stdout)

    def test_a_stopped_container_is_no_container(self):
        self.assertEqual(self._run_script(STUB_RUNNING="false"), "no_container")
        self.assertEqual(self._run_script(STUB_RUNNING="error"), "no_container")
        self.assertFalse((self.dir / "inner.txt").exists())

    def test_no_marker_is_idle_without_the_ros_cli(self):
        self.assertEqual(self._run_script(STUB_MARKER="absent"), "idle")
        self.assertFalse((self.dir / "inner.txt").exists())

    def test_the_node_stopping_is_stopped(self):
        self.assertEqual(self._run_script(), "stopped")

    def test_the_node_saying_nothing_runs_is_idle(self):
        self.assertEqual(self._run_script(STUB_ROS_OUT=_ROS_IDLE), "idle")

    def test_a_service_that_never_answers_is_unreachable(self):
        self.assertEqual(
            self._run_script(STUB_ROS_OUT="waiting for service to become available..."),
            "unreachable")
        self.assertEqual(self._run_script(STUB_ROS="fail"), "unreachable")

    def test_the_inner_command_reaches_ros2_with_the_exact_argv(self):
        self.assertEqual(self._run_script(), "stopped")
        inner = (self.dir / "inner.txt").read_text(encoding="utf-8")
        self.assertTrue(inner.endswith(
            'ros2 service call /edubotics/cloud_inference/stop std_srvs/srv/Trigger "{}"'), inner)
        patched = (inner
                   .replace("/opt/ros/jazzy/setup.bash", str(self.dir / "ros_setup.bash"))
                   .replace("/root/ros2_ws/install/setup.bash", str(self.dir / "ws_setup.bash")))
        self.assertNotEqual(patched, inner)
        result = subprocess.run(["bash", "-c", patched], env=self._env(),
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.dir / "ros2_argv.txt").read_text(encoding="utf-8").splitlines(),
            ["service", "call", "/edubotics/cloud_inference/stop", "std_srvs/srv/Trigger", "{}"])
        calls = (self.dir / "calls.log").read_text(encoding="utf-8")
        self.assertIn("timeout -k 1 8 nice -n 19 ros2 service call", calls)

    def test_every_docker_call_goes_to_the_server_container(self):
        self._run_script()
        docker_calls = [ln for ln in (self.dir / "calls.log").read_text(
            encoding="utf-8").splitlines() if ln.startswith("docker ")]
        self.assertEqual(len(docker_calls), 3, docker_calls)
        self.assertIn("inspect", docker_calls[0])
        self.assertIn(f"test -e {docker_manager.CLOUD_STOP_MARKER}", docker_calls[1])
        for line in docker_calls:
            self.assertIn("physical_ai_server", line)


class TheOneAttemptAfterAWindowClose(unittest.TestCase):

    def setUp(self):
        self.attempts = 0
        self.logs = []
        self.current = True
        self.answer = "stopped"

    def _attempt(self):
        self.attempts += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def _run(self, **kw):
        kwargs = dict(attempt=self._attempt, log=self.logs.append)
        kwargs.update(kw)
        return docker_manager.stop_cloud_inference_after_window_close(
            7, lambda generation: self.current and generation == 7, **kwargs)

    def test_stopped_idle_and_no_container_are_one_silent_attempt(self):
        for outcome in ("stopped", "idle", "no_container"):
            with self.subTest(outcome):
                self.attempts, self.logs, self.answer = 0, [], outcome
                self.assertEqual(self._run(), outcome)
                self.assertEqual((self.attempts, self.logs), (1, []))

    def test_unreachable_is_one_attempt_and_one_warning_line(self):
        self.answer = "unreachable"
        self.assertEqual(self._run(), "unreachable")
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.logs, [docker_manager.CLOUD_STOP_UNREACHABLE_DE])

    def test_superseded_before_never_attempts(self):
        self.current = False
        self.assertEqual(self._run(), "superseded")
        self.assertEqual((self.attempts, self.logs), (0, []))

    def test_superseded_while_the_lock_is_awaited_never_attempts(self):
        checks = []

        def _is_current(_generation):
            checks.append(1)
            return len(checks) < 2

        outcome = docker_manager.stop_cloud_inference_after_window_close(
            7, _is_current, log=self.logs.append, attempt=self._attempt)
        self.assertEqual(outcome, "superseded")
        self.assertEqual(self.attempts, 0)
        self.assertEqual(len(checks), 2)

    def test_superseded_after_an_unreachable_attempt_logs_nothing(self):
        def _attempt():
            self.attempts += 1
            self.current = False
            return "unreachable"

        self.assertEqual(self._run(attempt=_attempt), "unreachable")
        self.assertEqual(self.logs, [])

    def test_a_raising_attempt_or_an_unknown_answer_is_unreachable(self):
        for answer in (RuntimeError("boom"), "bananas", None):
            with self.subTest(answer=answer):
                self.attempts, self.logs, self.answer = 0, [], answer
                self.assertEqual(self._run(), "unreachable")
                self.assertEqual(self.logs, [docker_manager.CLOUD_STOP_UNREACHABLE_DE])

    def test_a_raising_log_or_is_current_never_propagates(self):
        self.answer = "unreachable"

        def _bad_log(_line):
            raise OSError("log sink gone")

        self.assertEqual(self._run(log=_bad_log), "unreachable")

        def _boom(_generation):
            raise RuntimeError("no answer")

        self.attempts = 0
        outcome = docker_manager.stop_cloud_inference_after_window_close(7, _boom, attempt=self._attempt)
        self.assertEqual(outcome, "superseded")
        self.assertEqual(self.attempts, 0)

    def test_no_log_callable_is_fine(self):
        self.answer = "unreachable"
        self.assertEqual(self._run(log=None), "unreachable")

    def test_the_default_attempt_is_resolved_at_call_time(self):
        with mock.patch.object(docker_manager, "stop_cloud_inference", return_value="idle") as fn:
            outcome = docker_manager.stop_cloud_inference_after_window_close(7, lambda _g: True)
        self.assertEqual(outcome, "idle")
        fn.assert_called_once_with()

    def test_there_is_no_retry_and_no_sleep_parameter(self):
        import inspect
        params = inspect.signature(docker_manager.stop_cloud_inference_after_window_close).parameters
        self.assertEqual(list(params), ["generation", "is_current", "log", "attempt"])
        self.assertEqual(params["attempt"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_no_two_threads_are_ever_inside_an_attempt(self):
        inside, peak, done = [], [], []
        guard = threading.Lock()

        def _attempt():
            with guard:
                inside.append(1)
                peak.append(len(inside))
            time.sleep(0.02)
            with guard:
                inside.pop()
                done.append(1)
            return "stopped"

        threads = [threading.Thread(
            target=docker_manager.stop_cloud_inference_after_window_close,
            args=(7, lambda _g: True), kwargs={"attempt": _attempt}, daemon=True)
            for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        self.assertEqual(len(done), 4)
        self.assertEqual(max(peak), 1)

    def test_its_lock_is_not_the_hf_clear_lock(self):
        self.assertIsNot(docker_manager._cloud_stop_lock, docker_manager._hf_clear_lock)


class TheStudentVisibleLineFollowsTheGuiRules(unittest.TestCase):

    def test_the_line(self):
        line = docker_manager.CLOUD_STOP_UNREACHABLE_DE
        self.assertTrue(line.startswith("[WARNUNG] "))
        self.assertNotIn("–", line)
        # German with literal umlauts (Rule §1): the sentence needs none, so the
        # fence is the absence of a transliteration rather than an umlaut's presence.
        self.assertNotRegex(line, r"(?i)ae|oe|ue")
        # The run ends 45 s after the page's LAST heartbeat plus the container's
        # next session check (up to ~10 s and one RPC), i.e. up to ~55 s after the
        # window closed: never promise a bound the run cannot keep (V1-7).
        self.assertTrue(line.endswith("Er endet nach etwa einer Minute von selbst."))
        self.assertNotIn("45 Sekunden", line)
        self.assertNotIn("spätestens", line)
        self.assertNotIn("EDUBOTICS_CLOUD_STOP", line)
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
    raise AssertionError(f"{cls}.{name} not found")


def _body(fn):
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _load_method(name, ns):
    src = _GUI_APP_SRC.read_text(encoding="utf-8")
    marker = f"    def {name}(self"
    start = src.index(marker)
    rest = src[start:]
    end = rest.find("\n    def ", len(marker))
    snippet = textwrap.dedent(rest[:end if end != -1 else len(rest)])
    exec(compile(snippet, str(_GUI_APP_SRC), "exec"), ns)  # noqa: S102
    return ns[name]


class TheWindowCloseHookStopsTheCloudRunFirst(unittest.TestCase):

    def test_two_separate_try_blocks_cloud_stop_first(self):
        stmts = _body(_method(_gui_tree(), "_on_student_window_closed"))
        self.assertEqual(len(stmts), 2, [ast.unparse(s)[:60] for s in stmts])
        first, second = stmts
        self.assertIsInstance(first, ast.Try)
        self.assertIsInstance(second, ast.Try)
        self.assertIn("docker_manager.stop_cloud_inference_after_window_close(", ast.unparse(first.body[0]))
        self.assertIn("docker_manager.clear_robot_hf_token_after_window_close(", ast.unparse(second.body[0]))
        for block in (first, second):
            self.assertEqual(len(block.handlers), 1)
            self.assertEqual(ast.unparse(block.handlers[0].type), "Exception")

    def test_the_hook_forwards_generation_the_predicate_and_the_log_in_order(self):
        order = []
        sentinel = object()
        docker = types.SimpleNamespace(
            stop_cloud_inference_after_window_close=lambda *a, **k: order.append(("stop", a, k)),
            clear_robot_hf_token_after_window_close=lambda *a, **k: order.append(("clear", a, k)))
        hook = _load_method("_on_student_window_closed",
                            {"docker_manager": docker,
                             "webview_window": types.SimpleNamespace(is_current_generation=sentinel)})
        owner = types.SimpleNamespace(_log=lambda _m: None)
        self.assertIsNone(hook(owner, 41))
        self.assertEqual([o[0] for o in order], ["stop", "clear"])
        self.assertEqual(order[0][1:], ((41, sentinel), {"log": owner._log}))
        self.assertEqual(order[1][1:], ((41, sentinel), {"log": owner._log}))

    def test_a_raising_stop_still_clears_the_token(self):
        cleared = []

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        docker = types.SimpleNamespace(
            stop_cloud_inference_after_window_close=_boom,
            clear_robot_hf_token_after_window_close=lambda *a, **k: cleared.append(a))
        hook = _load_method("_on_student_window_closed",
                            {"docker_manager": docker,
                             "webview_window": types.SimpleNamespace(is_current_generation=None)})
        self.assertIsNone(hook(types.SimpleNamespace(_log=lambda _m: None), 3))
        self.assertEqual(len(cleared), 1)

    def test_the_stop_is_referenced_nowhere_else_in_gui_app(self):
        tree = _gui_tree()
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr.startswith("stop_cloud_inference")]
        hook = _method(tree, "_on_student_window_closed")
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(hook.lineno <= hits[0] <= hook.end_lineno)


if __name__ == "__main__":
    unittest.main()
