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

"""Run one in-container bench tool from Windows and bring its files home
(dev only; stdlib).

    py container_run.py arrival_rate --out C:\\bench\\b2 [--offset-json F] -- --duration 120
    py container_run.py perception_load --out C:\\bench\\b4 -- --trials 10
    py container_run.py check_log --out C:\\bench\\s1

1. copies ``bench_common.py`` and ``container/<tool>.py`` into
   ``physical_ai_server:/tmp/station_bench/`` (on stdin; no shared mount);
2. runs the tool there with ROS sourced, its output folder
   ``/tmp/station_bench/out/<tool>_<stamp>``, relaying its console here
   (through a pipe: the ``wsl`` client is started without a console window);
3. Ctrl+C sends the tool SIGINT (it writes its summary) and waits, bounded,
   until the tool's process has ended or its summary exists, then
4. copies that folder back into ``--out``.

The wait for the client is in 0.5 s slices: on Windows an untimed
``Popen.wait()`` is ``WaitForSingleObject(INFINITE)``, which Ctrl+C cannot
interrupt, and the ``wsl`` child (no console) never sees Ctrl+C itself.
The copy waits for the CONTAINER, not for the local client: the client can
end before the tool has written its summary.

``--offset-json`` (clock_offset.py's file) is turned into the tool's
``--offset-ns`` / ``--offset-unc-ns``. Nothing here talks to the robot.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_common as B  # noqa: E402
import wsl_docker as W  # noqa: E402

TOOLS = ('arrival_rate', 'perception_load', 'check_log')


def tool_args(tool, container_out, extra, offset=None, run_stamp=None):
    """Pure: the argv the container tool gets."""
    args = ['--out', container_out] + (['--stamp', run_stamp] if run_stamp else [])
    if offset is not None and tool == 'arrival_rate':
        args += ['--offset-ns', str(offset[0]), '--offset-unc-ns', str(offset[1])]
    return args + list(extra)


def build_parser():
    p = argparse.ArgumentParser(description='Run a container bench tool and copy its files back.')
    p.add_argument('tool', choices=TOOLS)
    p.add_argument('--out', required=True, help='Windows folder for the results (created)')
    p.add_argument('--offset-json', default=None, help='clock_offset.py JSON (arrival_rate)')
    p.epilog = 'Everything after a lone -- goes to the tool unchanged.'
    return p


def split_argv(argv):
    """Pure: (this script's words, the tool's words) split at the first ``--``."""
    argv = list(argv)
    if '--' in argv:
        i = argv.index('--')
        return argv[:i], argv[i + 1:]
    return argv, []


# How long the copy waits for the container tool after Ctrl+C (it only has to
# close its CSVs and write the summary), and after a normal end.
DONE_TIMEOUT_S = 60.0
DONE_TIMEOUT_NORMAL_S = 15.0
POLL_S = 0.5
REAP_TIMEOUT_S = 15.0


def wait_sliced(proc, poll_s=POLL_S):
    """``proc.wait()`` in timed slices, so Ctrl+C is seen on Windows."""
    while True:
        try:
            return proc.wait(timeout=poll_s)
        except subprocess.TimeoutExpired:
            continue


def wait_for_tool_end(tool_state, timeout_s, *, poll_s=POLL_S, clock=time.monotonic,
                      sleep=time.sleep, say=print):
    """Poll the CONTAINER until the tool's summary exists or its process is
    gone, at most ``timeout_s``. Returns ``'summary'`` / ``'gone'`` /
    ``'unknown'`` (the container could not be asked) / ``'timeout'`` /
    ``'interrupted'`` (a second Ctrl+C)."""
    deadline = clock() + timeout_s
    try:
        while True:
            state = tool_state()
            if state in ('summary', 'gone'):
                return state
            if state is None:
                say('the container could not be asked whether the tool has finished')
                return 'unknown'
            if clock() >= deadline:
                say(f'the container tool did not finish within {timeout_s:.0f} s; '
                    'copying what is there')
                return 'timeout'
            sleep(poll_s)
    except KeyboardInterrupt:
        say('no longer waiting for the container tool; copying what is there')
        return 'interrupted'


def reap(proc, timeout_s=REAP_TIMEOUT_S, *, poll_s=POLL_S, clock=time.monotonic):
    """The local client's exit code; killed after ``timeout_s`` (sliced waits)."""
    deadline = clock() + timeout_s
    try:
        while clock() < deadline:
            try:
                return proc.wait(timeout=poll_s)
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        pass
    proc.kill()
    try:
        return proc.wait(timeout=poll_s)
    except Exception:  # noqa: BLE001
        return None


def supervise(proc, *, interrupt, tool_state, poll_s=POLL_S, done_timeout_s=DONE_TIMEOUT_S,
              clock=time.monotonic, sleep=time.sleep, say=print):
    """Wait for the container tool; on Ctrl+C stop it. Returns
    ``(rc, end)``: the client's exit code and how the tool's end was seen
    (``wait_for_tool_end``). Only after this returns may the folder be copied."""
    rc = None
    try:
        rc = wait_sliced(proc, poll_s)
        end = wait_for_tool_end(tool_state, min(done_timeout_s, DONE_TIMEOUT_NORMAL_S),
                                poll_s=poll_s, clock=clock, sleep=sleep, say=say)
    except KeyboardInterrupt:
        say('stopping the container tool …')
        interrupt()
        end = wait_for_tool_end(tool_state, done_timeout_s, poll_s=poll_s, clock=clock,
                                sleep=sleep, say=say)
        rc = reap(proc, poll_s=poll_s, clock=clock)
    return rc, end


def relay(src, out=None):
    """Copy the tool's console (bytes, line by line) to ours; never raises."""
    for raw in iter(src.readline, b''):
        text = raw.decode('utf-8', 'replace')
        dst = out or sys.stdout
        try:
            dst.write(text)
        except UnicodeEncodeError:
            enc = getattr(dst, 'encoding', None) or 'ascii'
            dst.write(text.encode(enc, 'replace').decode(enc, 'replace'))
        except Exception:  # noqa: BLE001
            return
        try:
            dst.flush()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    mine, extra = split_argv(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(mine)
    offset = None
    if args.offset_json:
        offset = B.parse_offset_json(pathlib.Path(args.offset_json).read_text(encoding='utf-8'))
    distro = W.distro_name()
    run_stamp = B.stamp()
    container_out = f'{W.CONTAINER_OUT}/{args.tool}_{run_stamp}'
    script = HERE / 'container' / f'{args.tool}.py'
    W.push_file(HERE / 'bench_common.py', f'{W.CONTAINER_DIR}/bench_common.py', distro)
    target = f'{W.CONTAINER_DIR}/{script.name}'
    W.push_file(script, target, distro)
    pid_file = f'{W.CONTAINER_DIR}/{args.tool}.pid'
    # stdout through a pipe and relayed: the wsl client has no console window
    # (CREATE_NO_WINDOW), so its output is shown by us, never left to chance.
    proc = subprocess.Popen(W.bash_stdin_argv(distro), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=W._CREATE_NO_WINDOW)
    relay_thread = threading.Thread(target=relay, args=(proc.stdout, sys.stdout), daemon=True)
    relay_thread.start()
    proc.stdin.write(W.python_tool_script(
        target, tool_args(args.tool, container_out, extra, offset, run_stamp), pid_file))
    proc.stdin.close()
    rc, end = supervise(proc, interrupt=lambda: W.interrupt(pid_file, distro),
                        tool_state=lambda: W.container_tool_state(pid_file, container_out,
                                                                  distro))
    relay_thread.join(timeout=2.0)
    if end not in ('summary',):
        print(f'note: the container tool ended without a summary seen ({end})')
    local = pathlib.Path(args.out) / f'{args.tool}_{run_stamp}'
    try:
        names = W.pull_dir(container_out, str(local), distro)
        print(f'copied {len(names)} file(s) to {local}')
    except RuntimeError as e:
        print(f'nothing copied back: {e}')
    return rc or 0


if __name__ == '__main__':
    sys.exit(main())
