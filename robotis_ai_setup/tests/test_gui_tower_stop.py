# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""„Stoppen" on the V1 under the camera tower asks first where the limp arm
falls (spec 40 WP-GUISTOP, 52 §5.3 (c) and §6.3, owner decision D56 (c)).

Stopping the environment de-energises the arm, and a V1 whose limp arm falls
BACK falls toward the camera tower. On ``v1_studio`` + ``tower_v1`` the Windows
GUI reads the V1 driver's ``/edubotics/tower_state`` ONCE before anything stops
(``docker_manager.read_tower_fall``, a fixed ``wsl_bridge.run`` script) and
asks on ``back`` or ``unknown``; ``forward`` and ``none`` stop at once.

THE DECISIONS THIS FILE HOLDS IN PLACE.

  * ONLY THE V1 UNDER THE TOWER ASKS. The OMX profiles, the legacy camera and
    cloud-only mode stop exactly as before and never reach WSL.
  * THE CHECK ENDS BEFORE THE STOP BEGINS. ``_stop_environment`` is not
    touched: the stop's order (VA-07, ``test_shutdown_teardown``) and its
    budget stay as they are; „Trotzdem stoppen" runs that unchanged stop and
    „Abbrechen" stops nothing.
  * NO ANSWER ASKS. A script that hangs, a WSL that fails, a sentinel nobody
    knows: ``unknown`` within TOWER_STATE_EXEC_TIMEOUT_S (15 s, the bound of
    the GUI's other robot scripts), and ``unknown`` shows the dialog. Only a
    stopped arm container is ``none``: an arm no container holds cannot be
    made limp by the stop.
  * BOTH PLATFORMS SAY THE SAME THING: the texts are the Pi's
    (``TowerHint/towerHintCopy.js``).
  * THE PICTURE IS THE REAL ARM: WP-ART's renders of the shipped V1 URDF,
    byte for byte, shipped by build.spec's ``('assets', 'assets')``.

NOT EXECUTED HERE (no Windows, no WSL, no ROS, no Tk in the CI venv):
``wsl -d EduBotics -- bash``, Jazzy's real ``ros2 topic echo`` and a real Tk
window. The script runs under a real ``bash`` against stub ``docker``/``ros2``
with the driver's own payload rendered the way ``ros2 topic echo`` prints it;
the dialog runs against a recording stand-in for tkinter. How often a slow PC
answers ``unknown`` at HOME is rig gate RG-STOP.
"""

import ast
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

import yaml

from gui.app import docker_manager, tower_stop_dialog, webview_window, wsl_bridge
from gui.app.constants import ROBOT_PROFILES

_APP_DIR = pathlib.Path(webview_window.__file__).parent
_GUI_DIR = _APP_DIR.parent
_GUI_APP_SRC = _APP_DIR / "gui_app.py"
_SETUP_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REPO_ROOT = _SETUP_ROOT.parent
_DRIVER = _SETUP_ROOT / "docker" / "open_manipulator" / "feetech_arm_node.py"
_COMPOSE = _SETUP_ROOT / "docker" / "docker-compose.yml"
_COPY_JS = (_REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src" / "components"
            / "TowerHint" / "towerHintCopy.js")
_FRAME_DIR = _GUI_DIR / "assets" / "v1_tower_hint"
_BUILD_SPEC = _GUI_DIR / "build.spec"

# WP-ART's SHA256SUMS for the GUI frames (scratch/art, 2026-11-20): the stop
# dialog's limp fall, rendered from public/v1-urdf/v1.urdf — frames 0-11 from
# the observation pose (back, toward the tower), 12-23 from HOME (forward).
WP_ART_FRAME_SHA256 = {
    "fall-00.png": "9d4a3b2e4003eec15d337b5416afab250f3251c3618fdf37ac8347d9c928a3d2",
    "fall-01.png": "9d4a3b2e4003eec15d337b5416afab250f3251c3618fdf37ac8347d9c928a3d2",
    "fall-02.png": "4e3258123991ec72aae15bfad761979ee8145de6d86df5808a2c825de053c2e7",
    "fall-03.png": "7d05ea61e6e711b9158e9d5fa25bc60ec0b03b7e5dc80bff23ebbe6997b3732b",
    "fall-04.png": "e808880b02acea67b9a4d6d7b432d7b696ae8de4c9c19e2d0499fd3fa040a10e",
    "fall-05.png": "2a58d52675b26114035c5899af8a39386bf361425a4054322497768e040a3cd1",
    "fall-06.png": "03e2237050a2f3739600ea5f785005c624042bbfe9abceef9a8880d8c6e543ad",
    "fall-07.png": "0e17975ea84585c134cdecde594299eb7fcf656add7c85c3b10c774a4d0ee1d8",
    "fall-08.png": "c4106f44d4637890956e4de5e1dda396c148a98239ca1697ff96686e9d226b35",
    "fall-09.png": "8e57fe86f2ad06fc74a94e5ddf11c8e597fd35a437f24a068535dde9b34c2afa",
    "fall-10.png": "9103e417ac207e45d268afc93a2460d4edd4b380e3b896545dc89fc01cebd578",
    "fall-11.png": "9103e417ac207e45d268afc93a2460d4edd4b380e3b896545dc89fc01cebd578",
    "fall-12.png": "a3cc6aef2b595065aa3106473e5d14e0a1d5f6e205862226189ec81a84b431df",
    "fall-13.png": "a3cc6aef2b595065aa3106473e5d14e0a1d5f6e205862226189ec81a84b431df",
    "fall-14.png": "dfb36955e975a17d21a39207d585b7fcb165bab8c9192df3771a7a889c4e1f86",
    "fall-15.png": "7706b19f68d169be0d069d7b74abfef6001431bc0215d6370ec36e7347873a3d",
    "fall-16.png": "605b5e3ce1b2b400a3fc843186a29fa1acade82d1be8f6cc337a322677b9e36d",
    "fall-17.png": "f44db4c333245aac5f342f1cff45928b3b093decb2f297b52fa1c6e23f50c8eb",
    "fall-18.png": "f35fbcd119525ef2f575e471d16b07bc97db2ddd4eeef09adfbf570789cdc734",
    "fall-19.png": "9419e169b828e2184c4e52b463b7b9ca124b3a4479fe2751b8d0a51c48f999cd",
    "fall-20.png": "d149b32a5c8a6dcf386269a8cf9656725c249be55159b55e9d7d7ea6225fd94c",
    "fall-21.png": "c8b16986906fa841d0494ad2cbfba4fdbcd6a38dd77175fbe05fbfa900f836f4",
    "fall-22.png": "b40bce69d297c8cbaf17f06f0b44a86c51b6e66de5a2699410f2ff6f628e749d",
    "fall-23.png": "b40bce69d297c8cbaf17f06f0b44a86c51b6e66de5a2699410f2ff6f628e749d",
}

V1 = "v1_studio"
TOWER = "tower_v1"
LEGACY = "legacy"


def _driver_assign(name):
    """The literal value of the driver's module-level ``name``."""
    for node in ast.parse(_DRIVER.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in feetech_arm_node.py")


def _driver_payload():
    """The driver's own ``tower_state_payload``, compiled from its source (the
    module itself needs rclpy)."""
    for node in ast.parse(_DRIVER.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == "tower_state_payload":
            ns = {"json": json}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(_DRIVER), "exec"), ns)  # noqa: S102
            return ns["tower_state_payload"]
    raise AssertionError("tower_state_payload not found in feetech_arm_node.py")


def _as_ros2_topic_echo_prints(payload):
    """What ``ros2 topic echo --once`` prints for a std_msgs/String: Jazzy's
    ``rosidl_runtime_py.message_to_yaml`` (yaml.dump with allow_unicode and
    width=sys.maxsize, strings over --truncate-length 128 cut with '...'),
    ended by ``---``."""
    if len(payload) > 128:
        payload = payload[:128] + "..."
    return yaml.dump({"data": payload}, allow_unicode=True, width=sys.maxsize,
                     default_flow_style=False) + "---\n"


def _fake_run(stdout="", stderr="", returncode=0, record=None):
    def _run(cmd, timeout=None, check=True, **kw):
        if record is not None:
            record.append({"cmd": cmd, "timeout": timeout, "check": check})
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return _run


class TheScriptIsFixed(unittest.TestCase):

    @property
    def script(self):
        return docker_manager.TOWER_STATE_SCRIPT

    def test_the_topic_is_the_drivers(self):
        self.assertEqual(docker_manager.TOWER_STATE_TOPIC, _driver_assign("TOWER_STATE_TOPIC"))
        self.assertEqual(docker_manager.TOWER_STATE_TOPIC, "/edubotics/tower_state")
        self.assertIn(f" {docker_manager.TOWER_STATE_TOPIC} std_msgs/msg/String'", self.script)

    def test_the_vocabulary(self):
        self.assertEqual(
            (docker_manager.TOWER_FALL_FORWARD, docker_manager.TOWER_FALL_BACK,
             docker_manager.TOWER_FALL_UNKNOWN, docker_manager.TOWER_FALL_NONE),
            ("forward", "back", "unknown", "none"))
        self.assertEqual(docker_manager.TOWER_FALL_SENTINEL, "EDUBOTICS_TOWER_FALL=")
        for outcome in ("forward", "back", "unknown", "none"):
            with self.subTest(outcome):
                self.assertIn(docker_manager.TOWER_FALL_SENTINEL + outcome, self.script)

    def test_the_bounds_are_the_guis_other_robot_scripts(self):
        """R4, 56 N-13: the CLI is bounded inside the container like the token
        clear and the cloud stop, and the round trip like them (15 s)."""
        self.assertIn("timeout -k 1 8 nice -n 19 ros2 topic echo --once ", self.script)
        self.assertEqual(docker_manager.TOWER_STATE_EXEC_TIMEOUT_S, 15)
        self.assertEqual(docker_manager.TOWER_STATE_EXEC_TIMEOUT_S,
                         docker_manager.CLOUD_STOP_EXEC_TIMEOUT_S)

    def test_it_reads_the_latched_message_with_the_publishers_qos(self):
        """The driver publishes reliable + transient-local depth 1. Jazzy's echo
        starts from the sensor_data profile (best effort) once any QoS flag is
        given, so both flags are spelled out."""
        self.assertIn("--qos-reliability reliable --qos-durability transient_local", self.script)

    def test_the_echo_runs_in_the_server_container_and_none_means_no_arm_container(self):
        self.assertIn("a=open_manipulator\n", self.script)
        self.assertIn("c=physical_ai_server\n", self.script)
        compose = _COMPOSE.read_text(encoding="utf-8")
        for name in ("open_manipulator", "physical_ai_server"):
            self.assertRegex(compose, rf"(?m)^\s*container_name:\s*{name}\s*$")
        none_line = [ln for ln in self.script.splitlines() if "EDUBOTICS_TOWER_FALL=none" in ln]
        self.assertEqual(len(none_line), 1, none_line)
        self.assertIn('docker inspect -f \'{{.State.Running}}\' "$a"', none_line[0])

    def test_every_docker_exec_reads_dev_null(self):
        execs = [ln for ln in self.script.splitlines() if "docker exec" in ln]
        self.assertEqual(len(execs), 1)
        self.assertIn("</dev/null", execs[0])

    def test_it_never_writes_a_file(self):
        for banned in (r"\brm\b", r"\bunlink\b", r"\btruncate\b", r"\btee\b", r"\btouch\b",
                       r"\bmktemp\b"):
            with self.subTest(banned):
                self.assertIsNone(re.search(banned, self.script), banned)
        remainder = self.script
        for allowed in ("2>/dev/null", "</dev/null"):
            remainder = remainder.replace(allowed, "")
        self.assertNotIn(">", remainder, "a redirect that can write a file")
        self.assertNotIn("<", remainder)

    def test_nothing_is_interpolated(self):
        self.assertNotIn("{0", self.script)
        self.assertNotIn("%s", self.script)

    def test_the_script_is_what_the_stdin_carrier_gets(self):
        record = []
        with mock.patch.object(docker_manager.wsl_bridge, "run",
                               _fake_run("EDUBOTICS_TOWER_FALL=forward\n", record=record)):
            self.assertEqual(docker_manager.read_tower_fall(), "forward")
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["cmd"], docker_manager.TOWER_STATE_SCRIPT)
        self.assertIs(record[0]["check"], False)
        self.assertEqual(record[0]["timeout"], docker_manager.TOWER_STATE_EXEC_TIMEOUT_S)


class TheSentinelIsTheOnlyThingRead(unittest.TestCase):

    def _read(self, **kw):
        with mock.patch.object(docker_manager.wsl_bridge, "run", _fake_run(**kw)):
            return docker_manager.read_tower_fall()

    def test_the_four_answers(self):
        for outcome in ("forward", "back", "unknown", "none"):
            with self.subTest(outcome):
                self.assertEqual(self._read(stdout=f"EDUBOTICS_TOWER_FALL={outcome}\n"), outcome)

    def test_noise_crlf_and_the_exit_code_are_tolerated(self):
        self.assertEqual(
            self._read(stdout="wsl: warning\r\nEDUBOTICS_TOWER_FALL=back\r\n", returncode=3), "back")

    def test_anything_else_is_unknown(self):
        for stdout in ("", "hello\n", "EDUBOTICS_TOWER_FALL=sideways\n", "EDUBOTICS_TOWER_FALL=\n",
                       None, b"EDUBOTICS_TOWER_FALL=forward"):
            with self.subTest(stdout=stdout):
                self.assertEqual(self._read(stdout=stdout), "unknown")
        self.assertEqual(self._read(stdout="", stderr="EDUBOTICS_TOWER_FALL=forward\n"), "unknown")

    def test_every_exception_is_unknown_and_never_raised(self):
        """wsl_bridge.run raises WSLError when its round trip times out: a hung
        script answers unknown, and unknown asks."""
        for exc in (wsl_bridge.WSLError("WSL command timed out after 15s"),
                    FileNotFoundError("wsl"), OSError("broken pipe"), RuntimeError("anything"),
                    subprocess.TimeoutExpired("wsl", 15)):
            with self.subTest(type(exc).__name__):
                with mock.patch.object(docker_manager.wsl_bridge, "run", side_effect=exc):
                    self.assertEqual(docker_manager.read_tower_fall(), "unknown")


def _stub_scripts():
    return {
        "docker": textwrap.dedent(r"""
            #!/bin/bash
            printf '%s\n' "docker $*" >> "$STUB_DIR/calls.log"
            case "$1" in
              inspect)
                [ "$STUB_ARM_RUNNING" = "error" ] && exit 1
                echo "$STUB_ARM_RUNNING"; exit 0;;
              exec)
                printf '%s' "$5" > "$STUB_DIR/inner.txt"
                case "$STUB_ECHO" in
                  fail) exit 1;;
                  hang) echo $$ > "$STUB_DIR/hang.pid"; exec sleep 30;;
                esac
                printf '%s' "$STUB_ECHO_OUT"; exit 0;;
            esac
            exit 99
        """).lstrip(),
        "ros2": textwrap.dedent(r"""
            #!/bin/bash
            printf '%s\n' "$@" > "$STUB_DIR/ros2_argv.txt"
            printf '%s' "$STUB_ECHO_OUT"
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
        self.addCleanup(self._kill_hung_stub)

    def _kill_hung_stub(self):
        pid_file = self.dir / "hang.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def _env(self, **stub):
        env = {
            "PATH": str(self.dir) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_DIR": str(self.dir),
            "STUB_ARM_RUNNING": "true",
            "STUB_ECHO": "ok",
            "STUB_ECHO_OUT": "",
        }
        env.update(stub)
        return env

    def _run_script(self, **stub):
        result = subprocess.run(
            ["bash"], input=docker_manager.TOWER_STATE_SCRIPT.encode("utf-8"),
            env=self._env(**stub), capture_output=True, timeout=30)
        stdout = result.stdout.decode("utf-8", "replace")
        sentinel_lines = [ln for ln in stdout.splitlines()
                          if ln.startswith(docker_manager.TOWER_FALL_SENTINEL)]
        self.assertEqual(len(sentinel_lines), 1, (stdout, result.stderr))
        self.assertEqual(result.returncode, 0, result.stderr)
        return docker_manager._tower_fall_outcome(stdout)

    def test_the_drivers_own_message_reads_as_its_fall(self):
        payload = _driver_payload()
        for glide in ("clear", "refused", "unjudgeable"):
            for fall in ("forward", "back", "unknown"):
                for part in (None, "column", "head"):
                    with self.subTest(glide=glide, fall=fall, part=part):
                        printed = _as_ros2_topic_echo_prints(payload(glide, fall, part, 123456789))
                        self.assertEqual(self._run_script(STUB_ECHO_OUT=printed), fall)

    def test_a_stopped_arm_container_is_none_and_starts_no_cli(self):
        for running in ("false", "error", ""):
            with self.subTest(running=running):
                self.assertEqual(self._run_script(STUB_ARM_RUNNING=running), "none")
        self.assertFalse((self.dir / "inner.txt").exists())

    def test_no_message_or_a_failing_exec_is_unknown(self):
        """No message within the bound (the topic absent, the CLI slow, the
        server container down): unknown, which asks."""
        self.assertEqual(self._run_script(STUB_ECHO_OUT=""), "unknown")
        self.assertEqual(self._run_script(STUB_ECHO="fail"), "unknown")
        self.assertEqual(self._run_script(STUB_ECHO_OUT="WARNING: topic [/edubotics/tower_state] "
                                                        "does not appear to be published yet\n"),
                         "unknown")

    def test_the_inner_command_reaches_ros2_with_the_exact_argv(self):
        self._run_script(STUB_ECHO_OUT=_as_ros2_topic_echo_prints(
            _driver_payload()("clear", "forward", None, 1)))
        inner = (self.dir / "inner.txt").read_text(encoding="utf-8")
        patched = inner.replace("/opt/ros/jazzy/setup.bash", str(self.dir / "ros_setup.bash"))
        self.assertNotEqual(patched, inner)
        result = subprocess.run(["bash", "-c", patched], env=self._env(), capture_output=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.dir / "ros2_argv.txt").read_text(encoding="utf-8").splitlines(),
            ["topic", "echo", "--once", "--qos-reliability", "reliable", "--qos-durability",
             "transient_local", "/edubotics/tower_state", "std_msgs/msg/String"])
        calls = (self.dir / "calls.log").read_text(encoding="utf-8")
        self.assertIn("timeout -k 1 8 nice -n 19 ros2 topic echo", calls)

    def test_the_inspect_asks_the_arm_and_the_echo_runs_in_the_server(self):
        self._run_script()
        docker_calls = [ln for ln in (self.dir / "calls.log").read_text(
            encoding="utf-8").splitlines() if ln.startswith("docker ")]
        self.assertEqual(len(docker_calls), 2, docker_calls)
        self.assertTrue(docker_calls[0].startswith("docker inspect"), docker_calls)
        self.assertTrue(docker_calls[0].endswith(" open_manipulator"), docker_calls)
        self.assertTrue(docker_calls[1].startswith("docker exec physical_ai_server bash -c "),
                        docker_calls)

    def test_a_hung_script_is_unknown_within_its_bound(self):
        """FAILS FIRST (R4, 56 N-13): the round trip is bounded by
        read_tower_fall's timeout (TOWER_STATE_EXEC_TIMEOUT_S = 15 by default):
        the REAL wsl_bridge.run over a real bash whose docker hangs answers
        unknown when the bound runs out. Run with a 2 s bound to stay fast."""
        real_run = subprocess.run
        env = self._env(STUB_ECHO="hang")
        argv = []

        def as_bash(args, **kw):
            argv.append(list(args))
            return real_run(["bash"], env=env, **kw)

        with mock.patch.object(wsl_bridge.subprocess, "run", as_bash):
            started = time.monotonic()
            fall = docker_manager.read_tower_fall(timeout=2)
            elapsed = time.monotonic() - started
        self.assertEqual(argv, [["wsl", "-d", wsl_bridge.WSL_DISTRO_NAME, "--", "bash"]])
        self.assertEqual(fall, "unknown")
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 6.0)
        self.assertTrue((self.dir / "hang.pid").exists(), "the stub never hung")


class _Recorder:
    """Records every call by name, in order, with where a worker thread and a
    Tk callback begin and end (the markers)."""

    MARKERS = ("thread>", "<thread", "after>", "<after")

    def __init__(self):
        self.calls = []

    def note(self, name):
        return lambda *a, **k: self.calls.append(name)

    def trace(self):
        """Every named call, with where a worker thread and a Tk callback run."""
        return [c for c in self.calls if isinstance(c, str)]

    def names(self):
        return [c for c in self.trace() if c not in self.MARKERS]


class _SyncThread:
    """Runs the target at start(), recording where the worker begins and ends."""

    def __init__(self, rec, target=None, daemon=None):
        self._rec = rec
        self._target = target

    def start(self):
        self._rec.calls.append("thread>")
        self._target()
        self._rec.calls.append("<thread")


def _load(name, ns):
    """The shipped method ``name`` of gui_app's class by its AST span, compiled
    against ``ns`` (importing gui_app pulls in tkinter and pywebview)."""
    src = _GUI_APP_SRC.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name and node.col_offset == 4:
            lines = src.splitlines()[node.lineno - 1:node.end_lineno]
            exec(compile(textwrap.dedent("\n".join(lines)), str(_GUI_APP_SRC), "exec"), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f"{name} not found in gui_app.py — the stop has no pre-check")


class TheStopAsksOnlyOnTheV1UnderTheTower(unittest.TestCase):
    """The shipped „Stoppen" handler and the shipped ``_stop_environment``,
    executed against recording doubles, with the REAL read_tower_fall over a
    patched ``wsl_bridge.run``."""

    STATUS = "Aktiv — Umgebung läuft"

    def _rig(self, *, robot_type=V1, scene_profile=TOWER, cloud_only=False, sentinel=None,
             run_raises=None, answer=None, ask_raises=None):
        rec = _Recorder()
        note = rec.note

        def run(cmd, timeout=None, check=True, **kw):
            rec.calls.append("wsl")
            if run_raises is not None:
                raise run_raises
            return types.SimpleNamespace(stdout=f"EDUBOTICS_TOWER_FALL={sentinel}\n", stderr="",
                                         returncode=0)

        def ask(parent):
            rec.calls.append("ask")
            if ask_raises is not None:
                raise ask_raises
            return answer

        def after(_delay, fn=None, *args):
            rec.calls.append("after>")
            if fn is not None:
                fn(*args)
            rec.calls.append("<after")

        def button(name):
            return types.SimpleNamespace(
                config=lambda **kw: rec.calls.append((name, kw.get("state"))))

        owner = types.SimpleNamespace(
            btn_stop=button("btn_stop"),
            btn_open_browser=button("btn_open_browser"),
            root=types.SimpleNamespace(after=after),
            progress=types.SimpleNamespace(start=lambda *a: None, stop=lambda *a: None),
            status_var=types.SimpleNamespace(get=lambda: self.STATUS),
            _set_status=lambda msg: rec.calls.append(("status", msg)),
            _log=lambda msg: rec.calls.append(("log", msg)),
            _stop_camera_bridge=note("camera_bridge"),
            _stop_camera_health_poll=note("health_poll"),
            _stop_phone_server=note("phone_server"),
            _stop_rs_control_server=note("rs_control"),
            _update_start_button=lambda: None,
            cloud_only=types.SimpleNamespace(get=lambda: cloud_only),
            gpu_available=False,
            running=True,
            _rs_robot_type=robot_type,
            _rs_scene_profile=scene_profile,
            _tower_check_running=False,
        )
        docker = types.SimpleNamespace(
            read_tower_fall=docker_manager.read_tower_fall,
            TOWER_FALL_UNKNOWN=docker_manager.TOWER_FALL_UNKNOWN,
            stop_containers=note("containers"),
            stop_cloud_only=note("containers"),
            stop_keepalive=note("keepalive"),
        )
        ns = {
            "threading": types.SimpleNamespace(
                Thread=lambda target=None, daemon=None: _SyncThread(rec, target, daemon)),
            "tk": types.SimpleNamespace(DISABLED="disabled", NORMAL="normal"),
            "webview_window": types.SimpleNamespace(destroy_all=note("webview")),
            "docker_manager": docker,
            "tower_stop_dialog": tower_stop_dialog,
        }
        owner._stop_environment = types.MethodType(_load("_stop_environment", dict(ns)), owner)
        owner._ask_tower_fall_first = types.MethodType(_load("_ask_tower_fall_first", dict(ns)), owner)
        handler = types.MethodType(_load("_stop_environment_after_tower_check", ns), owner)
        with mock.patch.object(docker_manager.wsl_bridge, "run", run), \
                mock.patch.object(tower_stop_dialog, "ask", ask):
            handler()
        return rec

    def _assert_stopped_as_today(self, rec):
        """The unchanged stop ran once, in VA-07's order."""
        order = rec.names()
        self.assertEqual(order.count("containers"), 1, order)
        containers = order.index("containers")
        for first in ("rs_control", "webview", "health_poll"):
            self.assertLess(order.index(first), containers, order)
        for source in ("camera_bridge", "phone_server"):
            self.assertGreater(order.index(source), containers, order)
        self.assertEqual(order[-1], "keepalive", order)

    def _assert_stopped_nothing(self, rec):
        for name in ("containers", "webview", "camera_bridge", "phone_server", "rs_control",
                     "health_poll", "keepalive"):
            self.assertNotIn(name, rec.names(), rec.calls)

    def test_back_and_unknown_ask_before_anything_stops(self):
        for sentinel in ("back", "unknown"):
            with self.subTest(sentinel=sentinel):
                rec = self._rig(sentinel=sentinel, answer=False)
                self.assertEqual(rec.names().count("wsl"), 1, rec.calls)
                self.assertEqual(rec.names().count("ask"), 1, rec.calls)

    def test_abbrechen_stops_nothing_and_gives_the_buttons_back(self):
        rec = self._rig(sentinel="back", answer=False)
        self._assert_stopped_nothing(rec)
        buttons = [c for c in rec.calls if isinstance(c, tuple) and c[0].startswith("btn_")]
        self.assertEqual(buttons, [("btn_stop", "disabled"), ("btn_open_browser", "disabled"),
                                   ("btn_stop", "normal"), ("btn_open_browser", "normal")])
        self.assertIn(("status", tower_stop_dialog.CHECKING_DE), rec.calls)
        statuses = [c for c in rec.calls if isinstance(c, tuple) and c[0] == "status"]
        self.assertEqual(statuses[-1], ("status", self.STATUS))
        self.assertIn(("log", tower_stop_dialog.CANCELLED_DE), rec.calls)

    def test_trotzdem_stoppen_runs_the_unchanged_stop_after_the_check(self):
        """FAILS FIRST (R4): the read and the answer come strictly before
        anything stops, and then the stop runs exactly as without the tower."""
        for sentinel in ("back", "unknown"):
            with self.subTest(sentinel=sentinel):
                rec = self._rig(sentinel=sentinel, answer=True)
                self._assert_stopped_as_today(rec)
                order = rec.names()
                first_stop = min(order.index(n) for n in ("rs_control", "webview", "health_poll",
                                                          "containers"))
                self.assertLess(order.index("wsl"), first_stop, order)
                self.assertLess(order.index("ask"), first_stop, order)

    def test_the_read_runs_on_a_worker_and_the_dialog_on_the_tk_thread(self):
        rec = self._rig(sentinel="back", answer=False)
        calls = rec.trace()
        self.assertLess(calls.index("thread>"), calls.index("wsl"))
        self.assertLess(calls.index("wsl"), calls.index("after>"))
        self.assertLess(calls.index("after>"), calls.index("ask"))
        self.assertLess(calls.index("ask"), calls.index("<after"))

    def test_an_unknown_answer_says_so_in_the_protokoll(self):
        rec = self._rig(sentinel="unknown", answer=False)
        self.assertIn(("log", tower_stop_dialog.UNKNOWN_DE), rec.calls)
        rec = self._rig(sentinel="back", answer=False)
        self.assertNotIn(("log", tower_stop_dialog.UNKNOWN_DE), rec.calls)

    def test_a_hung_or_failing_read_asks(self):
        """FAILS FIRST (R4): wsl_bridge.run raises WSLError when the 15 s round
        trip runs out; that is unknown, and unknown shows the dialog."""
        for exc in (wsl_bridge.WSLError("WSL command timed out after 15s"), OSError("wsl")):
            with self.subTest(type(exc).__name__):
                rec = self._rig(run_raises=exc, answer=False)
                self.assertEqual(rec.names().count("ask"), 1, rec.calls)
                self._assert_stopped_nothing(rec)

    def test_forward_and_none_stop_at_once(self):
        for sentinel in ("forward", "none"):
            with self.subTest(sentinel=sentinel):
                rec = self._rig(sentinel=sentinel, answer=False)
                self.assertNotIn("ask", rec.names())
                self.assertEqual(rec.names().count("wsl"), 1)
                self._assert_stopped_as_today(rec)
                self.assertLess(rec.names().index("wsl"), rec.names().index("containers"))

    def test_omx_legacy_and_cloud_only_never_reach_wsl(self):
        cases = [(robot_type, scene, False) for robot_type in ROBOT_PROFILES
                 for scene in (TOWER, LEGACY) if (robot_type, scene) != (V1, TOWER)]
        cases.append((V1, TOWER, True))
        self.assertIn(("omx_full", TOWER, False), cases)
        self.assertIn((V1, LEGACY, False), cases)
        for robot_type, scene, cloud_only in cases:
            with self.subTest(robot_type=robot_type, scene=scene, cloud_only=cloud_only):
                rec = self._rig(robot_type=robot_type, scene_profile=scene, cloud_only=cloud_only,
                                sentinel="back", answer=False)
                self.assertNotIn("wsl", rec.names())
                self.assertNotIn("ask", rec.names())
                self._assert_stopped_as_today(rec)

    def test_a_dialog_that_cannot_be_shown_never_blocks_the_stop(self):
        rec = self._rig(sentinel="back", ask_raises=RuntimeError("no Tk"))
        self._assert_stopped_as_today(rec)
        self.assertIn(("log", tower_stop_dialog.DIALOG_FAILED_DE), rec.calls)


class TheStopButtonGoesThroughTheCheck(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding="utf-8"))

    def test_the_stoppen_button_runs_the_check(self):
        commands = []
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Button"):
                kw = {k.arg: k.value for k in node.keywords}
                if isinstance(kw.get("text"), ast.Constant) and kw["text"].value == "Stoppen":
                    commands.append(ast.unparse(kw["command"]))
        self.assertEqual(commands, ["self._stop_environment_after_tower_check"])

    def test_nothing_else_reaches_the_stop_around_the_check(self):
        callers = []

        def walk(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef):
                    walk(child, owner or child.name)
                    continue
                if (isinstance(child, ast.Attribute) and child.attr == "_stop_environment"
                        and isinstance(child.value, ast.Name) and child.value.id == "self"):
                    callers.append(owner)
                walk(child, owner)

        for cls in (n for n in self.tree.body if isinstance(n, ast.ClassDef)):
            walk(cls, None)
        self.assertTrue(callers)
        self.assertEqual(set(callers), {"_stop_environment_after_tower_check"}, callers)


def _self_attribute_owners(tree, attr):
    """The top-level method of every ``self.<attr>`` reference in gui_app's classes (a reference in a closure
    counts for the method that defines it)."""
    owners = []

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                walk(child, owner or child.name)
                continue
            if (isinstance(child, ast.Attribute) and child.attr == attr
                    and isinstance(child.value, ast.Name) and child.value.id == "self"):
                owners.append(owner)
            walk(child, owner)

    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        walk(cls, None)
    return owners


def _wired_close_handler(tree):
    """The method ``self.root.protocol("WM_DELETE_WINDOW", …)`` is given (the window's close button)."""
    handlers = [ast.unparse(node.args[1]) for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "protocol" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "WM_DELETE_WINDOW"
                and ast.unparse(node.func.value) == "self.root"]
    if len(handlers) != 1 or not handlers[0].startswith("self."):
        raise AssertionError(f"the main window's close button is wired to {handlers}")
    return handlers[0][len("self."):]


def _wired_scan_handlers(tree):
    """Every scan entry: what a scan button's ``command=`` names and what ``root.after`` re-runs after the
    elevated arm bind."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "command" and ast.unparse(kw.value).startswith("self._scan_arms"):
                found.append(ast.unparse(kw.value))
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "after" and len(node.args) == 2
                and ast.unparse(node.args[1]).startswith("self._scan_arms")):
            found.append(ast.unparse(node.args[1]))
    return found


class EveryOtherWayToTakeTheTorqueOffGoesThroughTheCheck(unittest.TestCase):
    """X-C4 (verifier C-4): closing the GUI window stops the containers (``_on_close``) and „Arme scannen" stops
    the environment to free the bus (``_scan_arms._do_scan`` → ``ensure_environment_stopped``); both take the
    V1's torque off like „Stoppen". Each is entered through a wrapper that asks the same check first; the
    methods that stop (``_on_close``, ``_scan_arms``) stay as they are."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding="utf-8"))

    def test_the_window_close_button_runs_the_check(self):
        self.assertEqual(_wired_close_handler(self.tree), "_on_close_after_tower_check")
        self.assertEqual(set(_self_attribute_owners(self.tree, "_on_close")), {"_on_close_after_tower_check"})

    def test_every_scan_entry_runs_the_check(self):
        """the two scan buttons, „Erneut scannen" and the re-scan after „Arme freigeben"."""
        entries = _wired_scan_handlers(self.tree)
        self.assertEqual(len(entries), 4, entries)
        self.assertEqual(set(entries), {"self._scan_arms_after_tower_check"}, entries)
        self.assertEqual(set(_self_attribute_owners(self.tree, "_scan_arms")), {"_scan_arms_after_tower_check"})


class _CloseScanRig:
    """The shipped close and scan entries with the shipped ``_on_close``, executed against recording doubles
    and the REAL read_tower_fall over a patched ``wsl_bridge.run``; ``_scan_arms`` itself is a recorder (its
    worker is fenced elsewhere)."""

    STATUS = "Aktiv — Umgebung läuft"
    HELPERS = ("_ask_tower_fall_first", "_on_close_after_tower_check", "_scan_arms_after_tower_check")

    def __init__(self, *, robot_type=V1, scene_profile=TOWER, cloud_only=False, running=True, any_running=True,
                 sentinel=None, answer=None, ask_raises=None, generic_answer=True, scanning=False,
                 blocked=None):
        self.rec = rec = _Recorder()
        self.sentinel = sentinel
        self.answer = answer
        self.ask_raises = ask_raises
        note = rec.note

        def after(_delay, fn=None, *args):
            rec.calls.append("after>")
            if fn is not None:
                fn(*args)
            rec.calls.append("<after")

        def askyesno(*a, **k):
            rec.calls.append("askyesno")
            return generic_answer

        def any_container_running():
            rec.calls.append("ps")
            return any_running

        self.owner = owner = types.SimpleNamespace(
            root=types.SimpleNamespace(after=after, destroy=note("root.destroy")),
            status_var=types.SimpleNamespace(get=lambda: self.STATUS),
            _set_status=lambda msg: rec.calls.append(("status", msg)),
            _log=lambda msg: rec.calls.append(("log", msg)),
            _stop_camera_previews=note("previews"),
            _stop_camera_bridge=note("camera_bridge"),
            _stop_camera_health_poll=note("health_poll"),
            _stop_phone_server=note("phone_server"),
            _stop_rs_control_server=note("rs_control"),
            _scan_arms=note("scan"),
            _scan_blocked_reason=lambda: blocked,
            _scanning=scanning,
            _scan_confirm_open=False,
            _tower_check_running=False,
            cloud_only=types.SimpleNamespace(get=lambda: cloud_only),
            gpu_available=False,
            running=running,
            _rs_robot_type=robot_type,
            _rs_scene_profile=scene_profile,
        )
        docker = types.SimpleNamespace(
            read_tower_fall=docker_manager.read_tower_fall,
            TOWER_FALL_UNKNOWN=docker_manager.TOWER_FALL_UNKNOWN,
            TOWER_FALL_NONE=docker_manager.TOWER_FALL_NONE,
            any_container_running=any_container_running,
            stop_containers=note("containers"),
            stop_cloud_only=note("containers"),
            stop_keepalive=note("keepalive"),
        )
        self.ns = {
            "threading": types.SimpleNamespace(
                Thread=lambda target=None, daemon=None: _SyncThread(rec, target, daemon)),
            "tk": types.SimpleNamespace(DISABLED="disabled", NORMAL="normal"),
            "webview_window": types.SimpleNamespace(destroy_all=note("webview")),
            "docker_manager": docker,
            "tower_stop_dialog": tower_stop_dialog,
            "messagebox": types.SimpleNamespace(askyesno=askyesno),
        }
        src = _GUI_APP_SRC.read_text(encoding="utf-8")
        defined = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
        for name in ("_on_close",) + self.HELPERS:
            if name in defined:
                setattr(owner, name, types.MethodType(_load(name, dict(self.ns)), owner))

    def press(self, name):
        def run(cmd, timeout=None, check=True, **kw):
            self.rec.calls.append("wsl")
            return types.SimpleNamespace(stdout=f"EDUBOTICS_TOWER_FALL={self.sentinel}\n", stderr="",
                                         returncode=0)

        def ask(parent):
            self.rec.calls.append("ask")
            if self.ask_raises is not None:
                raise self.ask_raises
            return self.answer

        with mock.patch.object(docker_manager.wsl_bridge, "run", run), \
                mock.patch.object(tower_stop_dialog, "ask", ask):
            getattr(self.owner, name)()
        return self.rec


class ClosingTheWindowAsksOnlyOnTheV1UnderTheTower(unittest.TestCase):
    """The window's close button, as wired, with the shipped ``_on_close`` behind it."""

    @classmethod
    def setUpClass(cls):
        cls.handler = _wired_close_handler(ast.parse(_GUI_APP_SRC.read_text(encoding="utf-8")))

    def _close(self, **kw):
        return _CloseScanRig(**kw).press(self.handler)

    def test_back_and_unknown_ask_before_anything_stops_and_abbrechen_keeps_it_all_running(self):
        """C-4's repro: the V1 under the tower with the stack up, the limp arm falling back."""
        for sentinel in ("back", "unknown"):
            with self.subTest(sentinel=sentinel):
                rec = self._close(sentinel=sentinel, answer=False)
                names = rec.names()
                self.assertEqual(names.count("ask"), 1, rec.calls)
                for stopped in ("previews", "askyesno", "containers", "webview", "camera_bridge", "phone_server",
                                "rs_control", "keepalive", "root.destroy"):
                    self.assertNotIn(stopped, names, rec.calls)
                statuses = [c for c in rec.calls if isinstance(c, tuple) and c[0] == "status"]
                self.assertEqual(statuses, [("status", tower_stop_dialog.CHECKING_DE),
                                            ("status", _CloseScanRig.STATUS)])
                self.assertIn(("log", tower_stop_dialog.CLOSE_CANCELLED_DE), rec.calls)

    def test_trotzdem_stoppen_runs_the_unchanged_close_after_the_check(self):
        rec = self._close(sentinel="back", answer=True)
        names = rec.names()
        self.assertEqual(names.count("ask"), 1)
        self.assertLess(names.index("wsl"), names.index("ask"))
        self.assertLess(names.index("ask"), names.index("previews"))   # _on_close's first step
        self.assertEqual(names.count("askyesno"), 1)                   # _on_close's own question, unchanged
        containers = names.index("containers")
        for first in ("rs_control", "webview", "health_poll"):
            self.assertLess(names.index(first), containers, names)
        for source in ("camera_bridge", "phone_server"):
            self.assertGreater(names.index(source), containers, names)
        self.assertEqual(names[-1], "root.destroy")

    def test_the_read_runs_on_a_worker_and_the_dialog_on_the_tk_thread(self):
        """the stack probe and the read on a worker (`self.running` False: the probe asks docker)."""
        calls = self._close(running=False, sentinel="back", answer=False).trace()
        self.assertLess(calls.index("thread>"), calls.index("ps"))
        self.assertLess(calls.index("ps"), calls.index("wsl"))
        self.assertLess(calls.index("wsl"), calls.index("after>"))
        self.assertLess(calls.index("after>"), calls.index("ask"))

    def test_forward_or_no_arm_container_closes_as_today(self):
        for sentinel in ("forward", "none"):
            with self.subTest(sentinel=sentinel):
                names = self._close(sentinel=sentinel, answer=False).names()
                self.assertNotIn("ask", names)
                self.assertEqual(names.count("askyesno"), 1)
                self.assertEqual(names[-1], "root.destroy")

    def test_a_stack_that_is_down_never_reaches_wsl(self):
        names = self._close(running=False, any_running=False, sentinel="back", answer=False).names()
        self.assertNotIn("wsl", names)
        self.assertNotIn("ask", names)
        self.assertNotIn("containers", names)
        self.assertEqual(names[-1], "root.destroy")

    def test_omx_legacy_and_cloud_only_close_as_today_and_never_reach_wsl(self):
        cases = [(robot_type, scene, False) for robot_type in ROBOT_PROFILES
                 for scene in (TOWER, LEGACY) if (robot_type, scene) != (V1, TOWER)]
        cases.append((V1, TOWER, True))
        for robot_type, scene, cloud_only in cases:
            with self.subTest(robot_type=robot_type, scene=scene, cloud_only=cloud_only):
                names = self._close(robot_type=robot_type, scene_profile=scene, cloud_only=cloud_only,
                                    sentinel="back", answer=False).names()
                self.assertNotIn("wsl", names)
                self.assertNotIn("ask", names)
                self.assertEqual(names.count("askyesno"), 1)
                self.assertEqual(names[-1], "root.destroy")

    def test_a_dialog_that_cannot_be_shown_never_blocks_the_close(self):
        rec = self._close(sentinel="back", ask_raises=RuntimeError("no Tk"))
        self.assertEqual(rec.names()[-1], "root.destroy")
        self.assertIn(("log", tower_stop_dialog.DIALOG_FAILED_DE), rec.calls)

    def test_a_second_click_while_the_check_runs_is_ignored(self):
        rig = _CloseScanRig(sentinel="back", answer=False)
        rig.owner._tower_check_running = True
        names = rig.press(self.handler).names()
        self.assertEqual(names, [])


class ScanningArmsAsksOnlyOnTheV1UnderTheTower(unittest.TestCase):
    """„Arme scannen" through its entry: the check, then ``_scan_arms`` (which confirms and refuses as before)."""

    def _scan(self, **kw):
        return _CloseScanRig(**kw).press("_scan_arms_after_tower_check")

    def test_back_and_unknown_ask_first_and_abbrechen_scans_nothing(self):
        for sentinel in ("back", "unknown"):
            with self.subTest(sentinel=sentinel):
                rec = self._scan(sentinel=sentinel, answer=False)
                self.assertEqual(rec.names().count("ask"), 1, rec.calls)
                self.assertNotIn("scan", rec.names())
                self.assertIn(("log", tower_stop_dialog.SCAN_CANCELLED_DE), rec.calls)
                statuses = [c for c in rec.calls if isinstance(c, tuple) and c[0] == "status"]
                self.assertEqual(statuses[-1], ("status", _CloseScanRig.STATUS))

    def test_trotzdem_stoppen_scans(self):
        names = self._scan(sentinel="back", answer=True).names()
        self.assertEqual(names.count("scan"), 1)
        self.assertLess(names.index("ask"), names.index("scan"))

    def test_forward_none_or_a_stack_that_is_down_scan_at_once(self):
        for kw in ({"sentinel": "forward"}, {"sentinel": "none"},
                   {"sentinel": "back", "running": False, "any_running": False}):
            with self.subTest(**kw):
                names = self._scan(answer=False, **kw).names()
                self.assertNotIn("ask", names)
                self.assertEqual(names.count("scan"), 1)
        self.assertNotIn("wsl", self._scan(sentinel="back", running=False, any_running=False).names())

    def test_omx_legacy_cloud_only_a_running_or_refused_scan_never_reach_wsl(self):
        cases = [{"robot_type": robot_type, "scene_profile": scene} for robot_type in ROBOT_PROFILES
                 for scene in (TOWER, LEGACY) if (robot_type, scene) != (V1, TOWER)]
        cases += [{"cloud_only": True}, {"scanning": True}, {"blocked": "Die Einrichtung läuft gerade."}]
        for kw in cases:
            with self.subTest(**kw):
                names = self._scan(sentinel="back", answer=False, **kw).names()
                self.assertNotIn("wsl", names)
                self.assertNotIn("ask", names)
                self.assertEqual(names, ["scan"])


class TheDecisions(unittest.TestCase):

    def test_only_the_v1_on_the_tower_applies(self):
        self.assertTrue(tower_stop_dialog.applies(V1, TOWER))
        for robot_type in ROBOT_PROFILES:
            for scene in (TOWER, LEGACY, None, ""):
                if (robot_type, scene) == (V1, TOWER):
                    continue
                with self.subTest(robot_type=robot_type, scene=scene):
                    self.assertFalse(tower_stop_dialog.applies(robot_type, scene))
        self.assertFalse(tower_stop_dialog.applies(None, TOWER))

    def test_the_arm_profile_is_the_drivers(self):
        self.assertEqual(tower_stop_dialog.ARM_PROFILE, _driver_assign("ARM_PROFILE"))
        self.assertIn(tower_stop_dialog.ARM_PROFILE, ROBOT_PROFILES)

    def test_back_and_unknown_ask(self):
        self.assertTrue(tower_stop_dialog.asks_first("back"))
        self.assertTrue(tower_stop_dialog.asks_first("unknown"))
        for fall in ("forward", "none"):
            with self.subTest(fall):
                self.assertFalse(tower_stop_dialog.asks_first(fall))


def _js_strings(path):
    text = path.read_text(encoding="utf-8")
    return {m.group(1): m.group(2)
            for m in re.finditer(r"export const (\w+) =\s*'([^'\n]*)';", text)}


class BothPlatformsSayTheSameThing(unittest.TestCase):
    """The Pi's System tab asks with ``TowerStopDialog`` (WP-HINT); the GUI
    asks with the same title, sentence and buttons, byte for byte."""

    def test_the_texts_are_the_pis(self):
        js = _js_strings(_COPY_JS)
        self.assertEqual(tower_stop_dialog.TITLE_DE, js["TOWER_HINT_STOP_TITLE_DE"])
        self.assertEqual(tower_stop_dialog.TEXT_DE, js["TOWER_HINT_STOP_DE"])
        self.assertEqual(tower_stop_dialog.CANCEL_DE, js["TOWER_CANCEL_DE"])
        self.assertEqual(tower_stop_dialog.STOP_ANYWAY_DE, js["TOWER_STOP_ANYWAY_DE"])
        self.assertEqual(tower_stop_dialog.CANCEL_DE, "Abbrechen")
        self.assertEqual(tower_stop_dialog.STOP_ANYWAY_DE, "Trotzdem stoppen")

    def test_the_guis_own_lines_are_german_with_literal_umlauts(self):
        self.assertIn("geprüft", tower_stop_dialog.CHECKING_DE)
        self.assertIn("fällt", tower_stop_dialog.CHECKING_DE)
        for text in (tower_stop_dialog.CANCELLED_DE, tower_stop_dialog.CLOSE_CANCELLED_DE,
                     tower_stop_dialog.SCAN_CANCELLED_DE):
            self.assertIn("läuft", text)
        self.assertTrue(tower_stop_dialog.UNKNOWN_DE.startswith("[INFO] "))
        self.assertTrue(tower_stop_dialog.DIALOG_FAILED_DE.startswith("[WARNUNG] "))
        for text in (tower_stop_dialog.UNKNOWN_DE, tower_stop_dialog.DIALOG_FAILED_DE):
            self.assertIn("Rückfrage", text)


def _png_header(path):
    data = path.read_bytes()[:33]
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height, data[24], data[25]


class TheFramesAreWpArtsAndShip(unittest.TestCase):

    def test_the_frames_are_wp_arts_renders_byte_for_byte(self):
        on_disk = sorted(p.name for p in _FRAME_DIR.iterdir())
        self.assertEqual(on_disk, sorted(WP_ART_FRAME_SHA256))
        for name, digest in WP_ART_FRAME_SHA256.items():
            with self.subTest(name):
                path = _FRAME_DIR / name
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
                # 320 x 236, 8 bit, RGBA (colour type 6): what WP-ART's meta.json says
                self.assertEqual(_png_header(path), (320, 236, 8, 6))

    def test_the_dialog_shows_every_frame_in_order(self):
        self.assertEqual(list(tower_stop_dialog.FRAME_FILES), list(WP_ART_FRAME_SHA256))
        self.assertEqual(tower_stop_dialog.FRAME_DIR, _FRAME_DIR.name)
        self.assertEqual(tower_stop_dialog.SAFE_FRAME, 23)
        self.assertEqual(len(tower_stop_dialog.FRAME_DELAYS_MS), 24)
        self.assertAlmostEqual(sum(tower_stop_dialog.FRAME_DELAYS_MS), 4800, delta=24)

    def test_in_the_source_tree_the_frames_resolve_under_gui_assets(self):
        with mock.patch.object(sys, "_MEIPASS", None, create=True):
            paths = tower_stop_dialog.frame_paths()
        self.assertEqual([pathlib.Path(p) for p in paths],
                         [_FRAME_DIR / name for name in WP_ART_FRAME_SHA256])
        self.assertTrue(all(os.path.isfile(p) for p in paths))

    def test_in_the_frozen_exe_they_resolve_under_the_bundle(self):
        with tempfile.TemporaryDirectory() as bundle, \
                mock.patch.object(sys, "_MEIPASS", bundle, create=True):
            paths = tower_stop_dialog.frame_paths()
        self.assertEqual(paths, [os.path.join(bundle, "assets", "v1_tower_hint", name)
                                 for name in WP_ART_FRAME_SHA256])

    def test_build_spec_ships_the_whole_assets_directory(self):
        """``('assets', 'assets')`` copies gui/assets into the bundle's
        ``assets/`` and PyInstaller walks a directory entry recursively
        (6.22.3, building/utils.py::format_binaries_and_datas, os.walk), so the
        frames land where frame_paths() looks for them. The entry sits behind a
        guard (not only a .gitkeep): it must hold."""
        tree = ast.parse(_BUILD_SPEC.read_text(encoding="utf-8"))
        analysis = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "Analysis"]
        self.assertEqual(len(analysis), 1)
        datas = next(k.value for k in analysis[0].keywords if k.arg == "datas")
        guarded = [n for n in ast.walk(datas) if isinstance(n, ast.IfExp)
                   and "('assets', 'assets')" in ast.unparse(n.body)]
        self.assertEqual(len(guarded), 1, ast.unparse(datas))
        self.assertEqual(ast.literal_eval(guarded[0].body), [("assets", "assets")])
        gui_os = types.SimpleNamespace(
            path=types.SimpleNamespace(isdir=lambda p: os.path.isdir(_GUI_DIR / p)),
            listdir=lambda p: os.listdir(_GUI_DIR / p))
        self.assertTrue(eval(compile(ast.Expression(guarded[0].test), str(_BUILD_SPEC), "eval"),  # noqa: S307
                             {"os": gui_os}))
        self.assertEqual(_FRAME_DIR.parent, _GUI_DIR / "assets")


class _Widget:
    """A recording stand-in for a tkinter/ttk widget."""

    created = []

    def __init__(self, master=None, **kw):
        self.master = master
        self.kw = dict(kw)
        self.calls = []
        self.protocols = {}
        self.bindings = {}
        self.afters = []
        self.cancelled = []
        self.destroyed = False
        _Widget.created.append(self)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*a, **k):
            self.calls.append(name)
            return 0
        return call

    def configure(self, **kw):
        self.kw.update(kw)

    config = configure

    def protocol(self, name, fn):
        self.protocols[name] = fn

    def bind(self, sequence, fn, add=None):
        self.bindings[sequence] = fn

    def after(self, ms, fn=None, *args):
        self.afters.append((ms, fn))
        return f"after#{len(self.afters)}"

    def after_cancel(self, ident):
        self.cancelled.append(ident)

    def destroy(self):
        self.destroyed = True


def _fake_tkinter(missing=()):
    class TclError(Exception):
        pass

    class PhotoImage:
        def __init__(self, file=None, **kw):
            if os.path.basename(file) in missing or not os.path.isfile(file):
                raise TclError(f"couldn't open {file}")
            self.file = file

    class Toplevel(_Widget):
        pass

    class Frame(_Widget):
        pass

    class Label(_Widget):
        pass

    class Button(_Widget):
        pass

    tk = types.ModuleType("tkinter")
    tk.Toplevel, tk.PhotoImage, tk.TclError = Toplevel, PhotoImage, TclError
    tk.BOTH, tk.LEFT, tk.RIGHT, tk.CENTER, tk.X, tk.W = "both", "left", "right", "center", "x", "w"
    ttk = types.ModuleType("tkinter.ttk")
    ttk.Frame, ttk.Label, ttk.Button = Frame, Label, Button
    tk.ttk = ttk
    return tk, ttk


class TheDialog(unittest.TestCase):
    """``tower_stop_dialog.ask`` against a recording stand-in for tkinter (the
    CI venv has no Tk). ``parent.wait_window`` is where the student acts."""

    def _ask(self, act, *, missing=(), animations=True):
        _Widget.created = []
        tk, ttk = _fake_tkinter(missing)
        seen = {}

        class Root(_Widget):
            def wait_window(self, win):
                seen["win"] = win
                act(win)

        root = Root()
        with mock.patch.dict(sys.modules, {"tkinter": tk, "tkinter.ttk": ttk}), \
                mock.patch.object(tower_stop_dialog, "animations_enabled", lambda: animations):
            answer = tower_stop_dialog.ask(root)
        return answer, seen["win"], [w for w in _Widget.created if w is not root]

    @staticmethod
    def _button(widgets, text):
        found = [w for w in widgets if type(w).__name__ == "Button" and w.kw.get("text") == text]
        assert len(found) == 1, (text, [w.kw for w in widgets])
        return found[0]

    def test_trotzdem_stoppen_answers_true(self):
        answer, win, widgets = self._ask(
            lambda win: self._button(_Widget.created, tower_stop_dialog.STOP_ANYWAY_DE).kw["command"]())
        self.assertIs(answer, True)
        self.assertTrue(win.destroyed)

    def test_abbrechen_the_close_button_and_esc_answer_false(self):
        acts = {
            "Abbrechen": lambda win: self._button(_Widget.created,
                                                  tower_stop_dialog.CANCEL_DE).kw["command"](),
            "WM_DELETE_WINDOW": lambda win: win.protocols["WM_DELETE_WINDOW"](),
            "Escape": lambda win: win.bindings["<Escape>"](None),
            "nothing": lambda win: None,
        }
        for name, act in acts.items():
            with self.subTest(name):
                answer, win, _widgets = self._ask(act)
                self.assertIs(answer, False)

    def test_it_is_modal_german_and_abbrechen_has_the_focus(self):
        _answer, win, widgets = self._ask(lambda win: None)
        self.assertEqual(type(win).__name__, "Toplevel")
        self.assertIn("grab_set", win.calls)
        self.assertIn("transient", win.calls)
        labels = [w.kw.get("text") for w in widgets if type(w).__name__ == "Label"]
        self.assertIn(tower_stop_dialog.TITLE_DE, labels)
        self.assertIn(tower_stop_dialog.TEXT_DE, labels)
        self.assertIn("focus_set", self._button(widgets, tower_stop_dialog.CANCEL_DE).calls)
        self.assertNotIn("focus_set", self._button(widgets, tower_stop_dialog.STOP_ANYWAY_DE).calls)

    def test_it_plays_every_frame_and_stops_when_closed(self):
        def act(win):
            picture = [w for w in _Widget.created if type(w).__name__ == "Label"
                       and "image" in w.kw][0]
            shown = [picture.kw["image"].file]
            for _ in range(len(tower_stop_dialog.FRAME_FILES)):
                _ms, step = win.afters[-1]
                step()
                shown.append(picture.kw["image"].file)
            act.shown = shown
            self._button(_Widget.created, tower_stop_dialog.CANCEL_DE).kw["command"]()

        _answer, win, _widgets = self._ask(act)
        names = [os.path.basename(f) for f in act.shown]
        self.assertEqual(names, list(WP_ART_FRAME_SHA256) + ["fall-00.png"])
        self.assertEqual([ms for ms, _fn in win.afters], list(tower_stop_dialog.FRAME_DELAYS_MS)
                         + [tower_stop_dialog.FRAME_DELAYS_MS[0]])
        self.assertEqual(win.cancelled, [f"after#{len(win.afters)}"])

    def test_without_animations_it_shows_the_safe_frame_still(self):
        _answer, win, widgets = self._ask(lambda win: None, animations=False)
        picture = [w for w in widgets if type(w).__name__ == "Label" and "image" in w.kw][0]
        self.assertEqual(os.path.basename(picture.kw["image"].file), "fall-23.png")
        self.assertEqual(win.afters, [])

    def test_a_frame_that_cannot_be_read_leaves_the_text_and_the_buttons(self):
        answer, win, widgets = self._ask(
            lambda win: self._button(_Widget.created, tower_stop_dialog.STOP_ANYWAY_DE).kw["command"](),
            missing=("fall-07.png",))
        self.assertIs(answer, True)
        self.assertEqual([w for w in widgets if "image" in w.kw], [])
        self.assertEqual(win.afters, [])
        labels = [w.kw.get("text") for w in widgets if type(w).__name__ == "Label"]
        self.assertIn(tower_stop_dialog.TEXT_DE, labels)


class WindowsAnimationSetting(unittest.TestCase):
    """The still picture follows Windows' „Animationen in Windows anzeigen"
    (SPI_GETCLIENTAREAANIMATION), the setting WebView2 reads as
    prefers-reduced-motion on the Pi's page's twin."""

    def _fake_ctypes(self, answer=None, value=1, raises=None):
        class CInt:
            def __init__(self, v=0):
                self.value = v

        def spi(action, ui_param, ref, win_ini):
            self.assertEqual(action, 0x1042)
            if raises is not None:
                raise raises
            ref.value = value
            return answer

        return types.SimpleNamespace(
            c_int=CInt, byref=lambda obj: obj,
            windll=types.SimpleNamespace(user32=types.SimpleNamespace(SystemParametersInfoW=spi)))

    def _enabled(self, fake_ctypes, platform="win32"):
        with mock.patch.object(tower_stop_dialog, "sys", types.SimpleNamespace(platform=platform)), \
                mock.patch.object(tower_stop_dialog, "ctypes", fake_ctypes):
            return tower_stop_dialog.animations_enabled()

    def test_the_windows_setting_decides(self):
        self.assertTrue(self._enabled(self._fake_ctypes(answer=1, value=1)))
        self.assertFalse(self._enabled(self._fake_ctypes(answer=1, value=0)))

    def test_an_unreadable_setting_or_another_os_animates(self):
        self.assertTrue(self._enabled(self._fake_ctypes(answer=0, value=0)))
        self.assertTrue(self._enabled(self._fake_ctypes(raises=OSError("no user32"))))
        self.assertTrue(self._enabled(self._fake_ctypes(answer=1, value=0), platform="linux"))


if __name__ == "__main__":
    unittest.main()
