"""The run-directory helper (runs AS the student, uid 10001).

    python3 -I /opt/edubotics/runner/work_helper.py stage  <run_dir>   (files JSON on stdin)
    python3 -I /opt/edubotics/runner/work_helper.py remove <run_dir>

The supervisor is root WITHOUT ``DAC_OVERRIDE``, so it can neither write into
nor unlink inside a directory the student uid owns; this short-lived helper,
spawned with ``Popen(user=10001, group=10001, extra_groups=[])``, is the one
writer under ``/work``. ``stage`` first clears every entry of ``/work`` (one
run at a time, so anything there is a leftover), creates ``<run_dir>``, and
writes the project's files, refusing any path outside the server's
``CODE_PATH_RE`` a second time (the supervisor checked already; this is the
last writer, so it checks again). ``remove`` deletes ``<run_dir>`` and, for
the same reason, every other entry of ``/work``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_limits():
    spec = importlib.util.spec_from_file_location(
        'runner_limits', os.path.join(_HERE, 'runner_limits.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _force_removable(func, path, _exc) -> None:
    """A student may have chmod-ed their own tree; the owner can chmod back."""
    parent = os.path.dirname(path)
    try:
        os.chmod(parent, stat.S_IRWXU)
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass
    try:
        func(path)
    except OSError:
        pass


def clear_work_dir(work_dir: str, keep: str | None = None) -> None:
    try:
        entries = os.listdir(work_dir)
    except OSError:
        return
    for entry in entries:
        path = os.path.join(work_dir, entry)
        if keep is not None and os.path.abspath(path) == os.path.abspath(keep):
            continue
        if os.path.islink(path) or not os.path.isdir(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            shutil.rmtree(path, onexc=_force_removable)


def stage(run_dir: str, files: dict, path_re: str, max_files: int,
          max_file_bytes: int, dir_mode: int, work_dir: str) -> int:
    if not isinstance(files, dict) or not files or len(files) > max_files:
        sys.stderr.write('stage: bad files map\n')
        return 3
    pattern = re.compile(path_re)
    for rel, content in files.items():
        if not isinstance(rel, str) or not pattern.fullmatch(rel) \
                or not isinstance(content, str) \
                or len(content.encode('utf-8')) > max_file_bytes:
            sys.stderr.write(f'stage: refused {rel!r}\n')
            return 3
    clear_work_dir(work_dir)
    os.makedirs(run_dir, mode=dir_mode, exist_ok=False)
    os.chmod(run_dir, dir_mode)
    for rel, content in files.items():
        target = os.path.join(run_dir, rel)
        os.makedirs(os.path.dirname(target), mode=dir_mode, exist_ok=True)
        with open(target, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(content)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ('stage', 'remove'):
        sys.stderr.write('usage: work_helper.py stage|remove <run_dir>\n')
        return 2
    limits = _load_limits()
    limits.apply_rlimits('python')
    run_dir = os.path.abspath(argv[2])
    work_dir = limits.WORK_DIR
    if os.path.dirname(run_dir) != os.path.abspath(work_dir):
        sys.stderr.write(f'{argv[1]}: {run_dir} is not directly under {work_dir}\n')
        return 2
    if argv[1] == 'stage':
        try:
            payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            sys.stderr.write('stage: payload is not JSON\n')
            return 3
        files = payload.get('files') if isinstance(payload, dict) else None
        return stage(run_dir, files, limits.CODE_PATH_RE, limits.MAX_CODE_FILES,
                     limits.MAX_CODE_FILE_BYTES, limits.RUN_DIR_MODE, work_dir)
    clear_work_dir(work_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
