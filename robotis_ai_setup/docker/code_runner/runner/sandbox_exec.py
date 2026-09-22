"""Exec shim for the JVM processes of a run (runs AS the student, uid 10001).

    python3 -I /opt/edubotics/runner/sandbox_exec.py <language> <argv...>

Lowers this process's own rlimits to the table for ``<language>`` and then
``execv``s ``argv`` in place — the way ``javac`` and ``java`` get the same
bounds ``student_main.py`` applies to itself, with no ``preexec_fn`` in the
multithreaded supervisor. The limits survive the exec (they are process
attributes, and hard == soft), so the JVM can neither raise nor outgrow them.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write('usage: sandbox_exec.py <language> <program> [args...]\n')
        return 2
    spec = importlib.util.spec_from_file_location(
        'runner_limits', os.path.join(_HERE, 'runner_limits.py'))
    limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(limits)
    limits.apply_rlimits(argv[1])
    os.execv(argv[2], argv[2:])
    return 1  # unreachable: execv does not return


if __name__ == '__main__':
    sys.exit(main(sys.argv))
