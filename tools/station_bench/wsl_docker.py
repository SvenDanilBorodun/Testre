#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Windows-side plumbing for the station bench tools (dev only, stdlib).

Everything reaches the container the way the GUI does: ``wsl -d EduBotics --
docker …`` (no Docker Desktop, no ``/mnt/c``: files go in on stdin and come
back as a tar stream on stdout). The argv builders are pure and unit-tested;
the runners only wrap ``subprocess``.

Read-only towards the robot: the only ROS calls these helpers make are
``status`` queries; the tools that use them never call ``enter``/``leave``,
never command motion and never switch torque.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
GUI_DIR = REPO / 'robotis_ai_setup' / 'gui'

SERVER_CONTAINER = 'physical_ai_server'
ROS_SETUP = 'source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash'
CONTAINER_DIR = '/tmp/station_bench'
CONTAINER_OUT = CONTAINER_DIR + '/out'
BRIDGE_URL = 'http://127.0.0.1:8769'
# The Origin the student page sends; the bridge's Origin check accepts it
# (roboter_studio_control._origin_allowed: localhost, http/https, port 80/none).
PAGE_ORIGIN = 'http://localhost'

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0


def gui_import_path() -> str:
    """Put ``robotis_ai_setup/gui`` on sys.path so ``from app import …``
    imports the GUI's OWN modules from this checkout (never a copy)."""
    p = str(GUI_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


def distro_name() -> str:
    gui_import_path()
    try:
        from app import constants  # noqa: PLC0415
        return constants.WSL_DISTRO_NAME
    except Exception:  # noqa: BLE001 — the default the GUI uses
        return 'EduBotics'


# ── argv builders (pure) ─────────────────────────────────────────────────────
# ``wsl -d X -- cmd …`` hands the rest of the line to the distro's default
# shell, so every argv word here is PLAIN (no quote, $, |, ;, &, >, spaces):
# anything that needs a shell travels as a script on stdin to ``bash -s``,
# the way the GUI's wsl_bridge.run does.
_PLAIN = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./=:,+@%')


def is_plain(word: str) -> bool:
    return bool(word) and all(c in _PLAIN for c in word)


def wsl_argv(args, distro='EduBotics'):
    bad = [a for a in args if not is_plain(a)]
    if bad:
        raise ValueError(f'not a plain argv word for wsl: {bad!r}')
    return ['wsl', '-d', distro, '--'] + list(args)


def docker_exec_argv(args, *, stdin=False, distro='EduBotics', container=SERVER_CONTAINER):
    """``docker exec [-i] <container> <args…>`` through wsl (plain words)."""
    return wsl_argv(['docker', 'exec'] + (['-i'] if stdin else []) + [container] + list(args),
                    distro)


def bash_stdin_argv(distro='EduBotics', container=SERVER_CONTAINER):
    """``docker exec -i <container> bash -s``: the script comes on stdin."""
    return docker_exec_argv(['bash', '-s'], stdin=True, distro=distro, container=container)


def sh_quote(s: str) -> str:
    s = str(s)
    if is_plain(s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def ros_script(command_line: str) -> bytes:
    """A bash script (stdin) that sources ROS and the workspace, then runs."""
    return (f'{ROS_SETUP}\n{command_line}\n').encode('utf-8')


def service_status_script(service: str, srv_type: str) -> bytes:
    """The read-only ``status`` query, exactly the owner's CLI form."""
    return ros_script(f'ros2 service call {service} {srv_type} "{{command: status}}"')


def python_tool_script(target: str, args, pid_file: str) -> bytes:
    """Source ROS, record the pid, then ``exec`` python so the pid is python's
    (``container_run`` / ``ContainerStream.stop`` send it SIGINT)."""
    quoted = ' '.join(sh_quote(a) for a in args)
    return ros_script(f'echo $$ > {pid_file}\nexec python3 -u {target} {quoted}')


# ── runners ──────────────────────────────────────────────────────────────────
def run(argv, *, input_bytes=None, timeout=60):
    return subprocess.run(argv, input=input_bytes, capture_output=True, timeout=timeout,
                          creationflags=_CREATE_NO_WINDOW)


def run_bash(script: bytes, *, distro='EduBotics', timeout=60):
    return run(bash_stdin_argv(distro), input_bytes=script.replace(b'\r\n', b'\n'),
               timeout=timeout)


def push_file(local_path, container_path, distro='EduBotics'):
    """Copy a file in on stdin (``dd``): no shared mount, no shell."""
    data = pathlib.Path(local_path).read_bytes()
    d = container_path.rsplit('/', 1)[0]
    r = run(docker_exec_argv(['mkdir', '-p', d], distro=distro), timeout=60)
    if r.returncode == 0:
        r = run(docker_exec_argv(['dd', f'of={container_path}', 'status=none'], stdin=True,
                                 distro=distro), input_bytes=data, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'push {local_path} -> {container_path} failed: '
                           f'{r.stderr.decode("utf-8", "replace").strip()}')


def pull_dir(container_dir, local_dir, distro='EduBotics'):
    """Copy every file of a container folder back, one ``base64`` per file
    (text through the wsl relay, no shared mount). Returns the file names."""
    import base64  # noqa: PLC0415
    r = run(docker_exec_argv(['ls', '-1', container_dir], distro=distro), timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'list {container_dir} failed: '
                           f'{r.stderr.decode("utf-8", "replace").strip()}')
    os.makedirs(local_dir, exist_ok=True)
    names = []
    for name in r.stdout.decode('utf-8', 'replace').split():
        if not is_plain(name) or name.startswith('.') or '/' in name:
            continue
        f = run(docker_exec_argv(['base64', f'{container_dir}/{name}'], distro=distro),
                timeout=300)
        if f.returncode != 0:
            continue
        with open(os.path.join(local_dir, name), 'wb') as fh:
            fh.write(base64.b64decode(f.stdout))
        names.append(name)
    return names


def session_state_via_cli(distro='EduBotics', timeout=60):
    """``(state, raw_text)`` of ``/station/session status`` through the ros2
    CLI (read-only). state is None when the call failed or the image has no
    station service."""
    from bench_common import parse_ros2_response_field  # noqa: PLC0415
    r = run_bash(service_status_script('/station/session',
                                       'physical_ai_interfaces/srv/StationSession'),
                 distro=distro, timeout=timeout)
    text = r.stdout.decode('utf-8', 'replace')
    return parse_ros2_response_field(text, 'state'), text


# ── the GUI's control bridge (127.0.0.1:8769) ────────────────────────────────
def bridge_get(path='/station/status', timeout=1.0):
    """GET a bridge endpoint; ``(status_code, json_or_None)``; (None, None)
    when the bridge does not answer (GUI closed or not a station rig)."""
    import json  # noqa: PLC0415
    req = urllib.request.Request(BRIDGE_URL + path, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception:  # noqa: BLE001
        return None, None


def bridge_stream(on: bool, timeout=2.0):
    """POST /station/stream ``{"on": …}`` with the page's Origin."""
    import json  # noqa: PLC0415
    body = json.dumps({'on': bool(on)}).encode('utf-8')
    req = urllib.request.Request(BRIDGE_URL + '/station/stream', data=body, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'Origin': PAGE_ORIGIN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception:  # noqa: BLE001
        return None, None


class ContainerStream:
    """A long-lived container process whose stdout is read line by line.

    The script is pushed into the container first (with bench_common next to
    it) and run with ``python3 -u``; ``stop()`` sends SIGINT through its pid
    file so the tool writes its summary, then reaps the wsl client."""

    def __init__(self, script, args, distro='EduBotics'):
        self.script = pathlib.Path(script)
        self.args = [str(a) for a in args]
        self.distro = distro
        self.name = self.script.stem
        self.pid_file = f'{CONTAINER_DIR}/{self.name}.pid'
        self.proc = None

    def start(self):
        push_file(pathlib.Path(__file__).with_name('bench_common.py'),
                  f'{CONTAINER_DIR}/bench_common.py', self.distro)
        target = f'{CONTAINER_DIR}/{self.script.name}'
        push_file(self.script, target, self.distro)
        self.proc = subprocess.Popen(bash_stdin_argv(self.distro), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=_CREATE_NO_WINDOW)
        self.proc.stdin.write(python_tool_script(target, self.args, self.pid_file))
        self.proc.stdin.close()
        return self.proc

    def lines(self):
        for raw in iter(self.proc.stdout.readline, b''):
            yield raw.decode('utf-8', 'replace').rstrip('\r\n')

    def stop(self, timeout=20):
        if self.proc is None:
            return
        interrupt(self.pid_file, self.distro)
        try:
            self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def interrupt(pid_file, distro='EduBotics'):
    """SIGINT the container tool whose pid is in ``pid_file`` (best effort)."""
    try:
        run_bash(f'kill -INT "$(cat {pid_file})" 2>/dev/null; true\n'.encode('utf-8'),
                 distro=distro, timeout=30)
    except Exception:  # noqa: BLE001
        pass
