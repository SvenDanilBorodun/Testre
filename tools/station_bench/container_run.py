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
   ``/tmp/station_bench/out/<tool>_<stamp>``, streaming its console here;
3. Ctrl+C sends the tool SIGINT (it writes its summary), then
4. copies that folder back into ``--out``.

``--offset-json`` (clock_offset.py's file) is turned into the tool's
``--offset-ns`` / ``--offset-unc-ns``. Nothing here talks to the robot.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

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
    proc = subprocess.Popen(W.bash_stdin_argv(distro), stdin=subprocess.PIPE,
                            creationflags=W._CREATE_NO_WINDOW)
    proc.stdin.write(W.python_tool_script(
        target, tool_args(args.tool, container_out, extra, offset, run_stamp), pid_file))
    proc.stdin.close()
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        print('stopping the container tool …')
        W.interrupt(pid_file, distro)
        try:
            rc = proc.wait(timeout=60)
        except Exception:  # noqa: BLE001
            proc.kill()
            rc = None
    local = pathlib.Path(args.out) / f'{args.tool}_{run_stamp}'
    try:
        names = W.pull_dir(container_out, str(local), distro)
        print(f'copied {len(names)} file(s) to {local}')
    except RuntimeError as e:
        print(f'nothing copied back: {e}')
    return rc or 0


if __name__ == '__main__':
    sys.exit(main())
