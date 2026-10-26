"""The launcher of a Python student program (runs AS the student, uid 10001).

    python3 -I -u /opt/edubotics/runner/student_main.py <run_dir> [<bp_fd>]

Isolated mode (``-I``) puts neither the script directory nor the working
directory on ``sys.path``, so the path is assembled here explicitly: the
shipped ``lib/`` (``robot``, ``edubotics_debug``) first, the project second.
The stems ``robot`` and ``edubotics_*`` are refused by the server before a
project ever reaches this container, so a student file cannot shadow either.

In order: lower this process's own rlimits, install the debugger hook, read
the breakpoint channel, connect the RPC client with the run token (the
``__hello`` frame), start the line sampler, wire the live values into the
stub (``robot._rpc.before_call``: sent by the program's own thread right
before each public robot call), run ``main.py`` as ``__main__``, send the
last live values and report ``__exit`` under ONE hold of the stub's RPC lock
(:func:`finish_run`), and terminate with ``os._exit`` — the
launcher owns the process's end, so a non-daemon student thread cannot keep
the run alive after ``main.py`` returned.

The exit report is the ``{kind, file, line, exc_type, name, detail_line}``
dict ``code_rpc._EXIT_INFO_KEYS`` accepts; ``kind`` is one of
``code_errors_de.ERROR_KINDS`` or ``'ok'``. Never a message: the German
sentence is the server's, the raw last traceback line rides ``detail_line``
for the ``[TECHNIK]`` log line (decision A14). Its Java twin is
``java/edubotics/Launcher.java`` (area C); the one sentence both launchers
own, the keyboard refusal :data:`INPUT_REFUSED_DE`, is byte-equal in the two
(tests/test_code_runner_launcher_lockstep.py).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import runpy
import sys
import threading
import traceback
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, 'lib')
ENTRY_FILE = 'main.py'
EXIT_DETAIL_MAX_CHARS = 2000
# input() on the robot: there is no keyboard (N12). Raised as a RobotError, so
# the exit report carries it as kind `robot` with the line of the call.
INPUT_REFUSED_DE = ('Eingaben über die Tastatur gibt es hier nicht — das Programm '
                    'läuft auf dem Roboter.')


def _load_limits():
    spec = importlib.util.spec_from_file_location(
        'runner_limits', os.path.join(_HERE, 'runner_limits.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exc_field(exc: BaseException, base: type, field: str):
    """``exc``'s own ``field`` slot, read through the BASE class's member
    descriptor (``ImportError.__dict__['name_from'].__get__(exc)``) — never
    ``getattr``, which runs a student subclass's property, ``__getattribute__``
    or ``__getattr__`` for that name (fix round 1, R3: one that raised made
    :func:`classify_exception` itself raise, and the report lost its line and
    its ``[TECHNIK]`` line). The descriptor reads the C slot and runs no
    student code; anything that goes wrong answers None."""
    try:
        descriptor = base.__dict__.get(field)
        return None if descriptor is None else descriptor.__get__(exc, base)
    except Exception:  # noqa: BLE001 — no field is no name
        return None


def _exc_text(exc: BaseException, base: type, field: str) -> str:
    """:func:`_exc_field` when it is an EXACT ``str`` (a ``str`` subclass's
    ``__bool__``/``__eq__``/``__str__`` would be student code), else ``''``."""
    value = _exc_field(exc, base, field)
    return value if type(value) is str else ''


def classify_exception(exc: BaseException, project_root: str, robot_module) -> dict:
    """Map a fault to the exit-report dict (pure; tested without a process).

    ``name``, ``name_from`` and ``obj`` are read through the base class's
    member descriptors (:func:`_exc_field`), never ``getattr``: a hostile
    subclass keeps its report (fix round 1, R3)."""
    kind, name = 'other', ''
    if isinstance(exc, IndentationError):
        kind = 'indentation'
    elif isinstance(exc, SyntaxError):
        kind = 'syntax'
    elif isinstance(exc, NameError):
        kind, name = 'name', _exc_text(exc, NameError, 'name')
    elif isinstance(exc, AttributeError) and _exc_field(exc, AttributeError, 'obj') is robot_module:
        kind, name = 'robot_method', _exc_text(exc, AttributeError, 'name')
    elif (isinstance(exc, AttributeError) and _exc_text(exc, AttributeError, 'name')
          and _exc_field(exc, AttributeError, 'obj') is None):
        # `ziel.x` with ziel None: the server words it „Hier ist kein Wert da
        # (None bzw. null)" (code_errors_de, chosen from exc_type + this name).
        name = 'None'
    elif (isinstance(exc, AttributeError)
          and type(_exc_field(exc, AttributeError, 'obj')) is types.ModuleType):
        # `math.sqrtt`, `helfer.nope` (M3): kind `name` with `module.attr` —
        # the server words a dotted name „„sqrtt“ gibt es im Modul „math“
        # nicht" (code_errors_de 'name_in_module'). An EXACT type test, never
        # isinstance (which reads `obj.__class__`, i.e. a student class's own
        # __getattribute__ — review-2 mi-2), and the module's name read from its
        # __dict__, which never runs a module-level __getattr__. A module whose
        # __name__ is not a str stays `other` (review-3 mi-E).
        module = _exc_field(exc, AttributeError, 'obj').__dict__.get('__name__')
        attr = _exc_text(exc, AttributeError, 'name')
        if type(module) is str and module and module != 'robot' and attr:
            kind, name = 'name', f'{module}.{attr}'
    elif isinstance(exc, ImportError):
        # CPython 3.12: `from X import Y` names the MODULE in exc.name and the
        # missing NAME in exc.name_from (M3). `from robot import mvoe_to` is
        # the robot_method sentence; any other module a dotted `name` (a
        # NameError's name never holds a dot, so the dot is the signal); a
        # missing module keeps `import`; a program's own bare ImportError is
        # `other` (no module to name).
        module = _exc_text(exc, ImportError, 'name')
        name_from = _exc_text(exc, ImportError, 'name_from')
        if name_from:
            if module == 'robot':
                kind, name = 'robot_method', name_from
            elif module:
                kind, name = 'name', f'{module}.{name_from}'
            else:
                kind, name = 'name', name_from
        elif module:
            kind, name = 'import', module
    elif isinstance(exc, TypeError):
        kind = 'type'
    elif isinstance(exc, ZeroDivisionError):
        kind = 'zero_division'
    elif isinstance(exc, (IndexError, KeyError)):
        kind = 'index'
    elif isinstance(exc, RecursionError):
        kind = 'recursion'
    elif isinstance(exc, MemoryError):
        kind = 'memory'
    elif robot_module is not None and isinstance(
            exc, getattr(robot_module, 'RobotError', ())):
        kind = 'robot'

    file, line = '', 0
    root = os.path.abspath(project_root) + os.sep
    if isinstance(exc, SyntaxError) and isinstance(exc.filename, str) \
            and exc.filename.startswith(root):
        file = exc.filename[len(root):].replace(os.sep, '/')
        line = int(exc.lineno or 0)
    else:
        tb = exc.__traceback__
        while tb is not None:
            path = tb.tb_frame.f_code.co_filename
            if path.startswith(root):
                file = path[len(root):].replace(os.sep, '/')
                line = int(tb.tb_lineno or 0)
            tb = tb.tb_next

    if kind == 'robot':
        detail = str(exc)
    else:
        try:
            detail = traceback.format_exception_only(exc)[-1].strip()
        except Exception:  # noqa: BLE001
            detail = type(exc).__name__
    return {
        'kind': kind,
        'file': file[:80],
        'line': line,
        'exc_type': type(exc).__name__[:80],
        'name': name[:80],
        'detail_line': detail[:EXIT_DETAIL_MAX_CHARS],
    }


def finish_run(rpc, live, namespaces, info, error_type) -> None:
    """The last live values and ``__exit``, under ONE hold of the stub's RPC
    lock (review round 5, md7).

    ``__exit`` always had to wait for that lock: another thread inside a
    long robot call, or one looping robot calls, holds it (and the lock is
    not fair — ``docs/KNOWN-ISSUES.md``). Taking it ONCE, blocking, for both
    frames therefore adds no wait of its own, and no other thread's call can
    come between them: the last values are never skipped because the lock
    was busy. Round 4 skipped them after one interval and then waited for
    the same lock for ``__exit`` anyway, so a program with a worker thread
    ended showing stale values. The live-values hook is unwired inside the
    same hold. A failing ``__exit`` is written to stderr, never raised."""
    lock = getattr(rpc, '_lock', None)
    with lock if lock is not None else contextlib.nullcontext():
        rpc.before_call = None
        if live is not None:
            live.final(namespaces, (ENTRY_FILE, 0))
        try:
            rpc.call('__exit', [info], 'call')
        except error_type as exc:
            sys.stderr.write(f'__exit: {exc}\n')


def _read_breakpoint_channel(fd: int, hook, max_line: int) -> None:
    """Apply every ``{"lines": {...}}`` line the supervisor writes on the pipe."""
    buf = b''
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        if len(buf) > max_line:
            buf = buf[-max_line:]
        while b'\n' in buf:
            raw, buf = buf.split(b'\n', 1)
            try:
                message = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(message, dict):
                hook.set_breakpoints(message.get('lines'))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write('usage: student_main.py <run_dir> [<bp_fd>]\n')
        return 2
    limits = _load_limits()
    limits.apply_rlimits('python')
    run_dir = os.path.abspath(argv[1])
    bp_fd = int(argv[2]) if len(argv) > 2 else None

    sys.path.insert(0, _LIB)
    sys.path.insert(1, run_dir)
    import edubotics_debug  # noqa: E402 — needs the path above
    import robot  # noqa: E402

    def _no_keyboard(*_args, **_kwargs):
        raise robot.RobotError(INPUT_REFUSED_DE)

    import builtins
    builtins.input = _no_keyboard

    hook = edubotics_debug.Hook(robot._rpc, run_dir, max_locals=limits.PAUSED_MAX_LOCALS)
    hook.install()
    if bp_fd is not None:
        threading.Thread(
            target=_read_breakpoint_channel,
            args=(bp_fd, hook, limits.BREAKPOINT_LINE_MAX_BYTES),
            name='edubotics-breakpoints', daemon=True).start()

    socket_path = os.environ.get('CODE_RPC_SOCKET') or ''
    token = os.environ.get('CODE_RUN_TOKEN') or ''
    connected = False
    try:
        robot._rpc.connect(socket_path, token, project_root=run_dir)
        connected = True
    except robot.RobotError as exc:
        sys.stderr.write(f'{exc}\n')
    live = None
    if connected:
        edubotics_debug.start_line_sampler(robot._rpc, run_dir)
        # `vars(robot)`: a name `from robot import *` bound to the library's
        # own object is not one of the program's variables.
        live = edubotics_debug.LiveValues(robot._rpc, run_dir,
                                          max_vars=limits.PAUSED_MAX_LOCALS,
                                          exclude=vars(robot))
        robot._rpc.before_call = live.before_call

    code = 0
    info: dict | None = {'kind': 'ok'}
    # The program's module dicts at its end, for the last live values:
    # main.py's (or, after an exception, those of its traceback) — every
    # other project module is added from sys.modules at the end.
    namespaces: list = []
    os.chdir(run_dir)
    try:
        namespaces = [runpy.run_path(os.path.join(run_dir, ENTRY_FILE), run_name='__main__')]
    except edubotics_debug.StopRequested:
        info = None
    except SystemExit as exc:
        namespaces = edubotics_debug.project_namespaces(exc.__traceback__, run_dir)
        if exc.code is None:
            code = 0
        elif isinstance(exc.code, int):
            code = exc.code
        else:
            # `sys.exit("Fehler!")`: the text is the program's own output —
            # stdout reaches the Protokoll before the exit sentence, stderr is
            # never shown (C9-b).
            print(exc.code, flush=True)
            code = 1
    except BaseException as exc:  # noqa: BLE001 — every fault is reported
        namespaces = edubotics_debug.project_namespaces(exc.__traceback__, run_dir)
        info = classify_exception(exc, run_dir, robot)
        code = 1
        try:
            traceback.print_exc()
        except Exception:  # noqa: BLE001 — a hostile exception's own attributes (R3)
            sys.stderr.write(f'{type(exc).__name__}\n')
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except OSError:
            pass
        if connected and info is not None:
            if live is not None:
                namespaces = namespaces + edubotics_debug.project_module_namespaces(run_dir)
            try:
                finish_run(robot._rpc, live, namespaces, info, robot.RobotError)
            except Exception as exc:  # noqa: BLE001 — a program that broke the library itself
                sys.stderr.write(f'__exit: {exc!r}\n')
        robot._rpc.close()
    return code


if __name__ == '__main__':
    os._exit(main(sys.argv))
