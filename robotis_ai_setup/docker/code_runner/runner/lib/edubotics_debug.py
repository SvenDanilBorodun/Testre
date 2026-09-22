"""The Python debugger hook of a student program (PEP 669 ``sys.monitoring``).

Installed by ``student_main.py`` into the student's own process before
``main.py`` runs. Two things live here, both zero-cost on the hot path:

* :class:`Hook` — a LINE callback that returns ``sys.monitoring.DISABLE`` for
  every location that carries no breakpoint (measured x1.02 against no hook,
  P12 of the spec), and on a hit sends ``__paused(file, line, locals)`` over
  the stub's RPC client and blocks until the robot answers ``continue`` |
  ``step`` | ``stop``. A breakpoint set that changes MID-RUN is applied
  through :meth:`Hook.set_breakpoints`, which ends in
  ``sys.monitoring.restart_events()`` — without that call a location the
  callback has already DISABLEd never fires again (P13: 0 hits without it,
  millions with it), so a breakpoint added while the loop runs would be
  silently dead.
* :func:`start_line_sampler` — the „where am I" indicator. A daemon thread
  reads the main thread's frame every ``interval_s`` and reports the
  innermost PROJECT line as ``__line`` when it changed and the connection
  lock is free. Never a per-line event (B13).

Only the main thread is debugged; a line event from any other thread (the
sampler, a student's own thread) is DISABLEd on sight. Neither mechanism is
a security boundary — a student can uninstall both from inside their own
process (R-3); the server-side budgets and validation are the boundary.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import types

# sys.monitoring.DEBUGGER_ID — spelled as the number so this module imports
# on any 3.12+ without touching sys.monitoring before ``install``.
DEBUGGER_ID = 3

# Bounds on the locals snapshot a ``__paused`` frame carries: the server
# truncates again, but the DATA frame is bounded at 64 KiB and this keeps a
# 30-entry snapshot under ~32 KiB.
VALUE_MAX_CHARS = 1000
CONTAINER_MAX_ITEMS = 50
CONTAINER_MAX_DEPTH = 3
_DEFAULT_MAX_LOCALS = 30

SAMPLER_INTERVAL_S = 0.5


class StopRequested(BaseException):
    """The robot answered ``stop`` at a breakpoint: unwind the program.

    A ``BaseException`` so an ordinary ``except Exception:`` in student code
    does not swallow it. The supervisor's out-of-band kill does not depend on
    this reaching the launcher; it is the polite path."""


def _jsonable(value, depth: int = 0):
    """A bounded, JSON-serialisable rendering of one local's value."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return value[:VALUE_MAX_CHARS]
    if depth < CONTAINER_MAX_DEPTH:
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)[:CONTAINER_MAX_ITEMS]
            return [_jsonable(v, depth + 1) for v in items]
        if isinstance(value, dict):
            out = {}
            for k, v in list(value.items())[:CONTAINER_MAX_ITEMS]:
                out[str(k)[:VALUE_MAX_CHARS]] = _jsonable(v, depth + 1)
            return out
    try:
        return repr(value)[:VALUE_MAX_CHARS]
    except Exception:  # noqa: BLE001 — a hostile __repr__ must not break a pause
        return '<?>'


_SKIPPED_VALUE_TYPES = (
    types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
    types.MethodType, type,
)


def snapshot_locals(frame, max_locals: int = _DEFAULT_MAX_LOCALS) -> dict:
    """At most ``max_locals`` of the frame's locals, dunders and code objects
    skipped, every value bounded so the whole frame fits the DATA bound."""
    out: dict = {}
    for name, value in list(frame.f_locals.items()):
        if len(out) >= max_locals:
            break
        if not isinstance(name, str) or name.startswith('__'):
            continue
        if isinstance(value, _SKIPPED_VALUE_TYPES):
            continue
        rendered = _jsonable(value)
        try:
            if len(json.dumps(rendered, ensure_ascii=False)) > VALUE_MAX_CHARS:
                rendered = repr(value)[:VALUE_MAX_CHARS]
        except (TypeError, ValueError):
            rendered = repr(value)[:VALUE_MAX_CHARS]
        out[name] = rendered
    return out


def project_relpath(path: str, project_root: str) -> str | None:
    """``main.py`` / ``pkg/helper.py`` for a file under the project, else None."""
    root = project_root.rstrip('/') + '/'
    if not path.startswith(root):
        return None
    return path[len(root):].replace(os.sep, '/')


class Hook:
    """The LINE-event debugger of one student process."""

    def __init__(self, rpc, project_root: str, *,
                 max_locals: int = _DEFAULT_MAX_LOCALS) -> None:
        self._rpc = rpc
        self._root = os.path.abspath(project_root)
        self._max_locals = max_locals
        self._breakpoints: dict[str, frozenset] = {}
        self._step_armed = False
        self._in_hook = False
        self._main_thread = threading.main_thread()
        self.hits = 0

    # ── lifecycle ───────────────────────────────────────────────────────────
    def install(self) -> None:
        mon = sys.monitoring
        mon.use_tool_id(DEBUGGER_ID, 'edubotics')
        mon.register_callback(DEBUGGER_ID, mon.events.LINE, self.on_line)
        mon.set_events(DEBUGGER_ID, mon.events.LINE)

    def uninstall(self) -> None:
        mon = sys.monitoring
        mon.set_events(DEBUGGER_ID, mon.events.NO_EVENTS)
        mon.register_callback(DEBUGGER_ID, mon.events.LINE, None)
        mon.free_tool_id(DEBUGGER_ID)

    # ── the breakpoint set (the `breakpoints` control message ends here) ───
    def set_breakpoints(self, lines_by_file) -> None:
        """Replace the breakpoint set — ``{"main.py": [12, 40]}`` — and wake
        every location the callback has DISABLEd so a new breakpoint fires
        on the very next pass through its line."""
        table: dict[str, frozenset] = {}
        if isinstance(lines_by_file, dict):
            for rel, lines in lines_by_file.items():
                if not isinstance(rel, str) or not isinstance(lines, list):
                    continue
                path = os.path.normpath(os.path.join(self._root, rel))
                if not path.startswith(self._root + os.sep):
                    continue
                ints = frozenset(
                    n for n in lines
                    if isinstance(n, int) and not isinstance(n, bool) and n > 0)
                if ints:
                    table[path] = ints
        self._breakpoints = table
        sys.monitoring.restart_events()

    # ── the LINE callback ───────────────────────────────────────────────────
    def on_line(self, code, line):
        if self._in_hook or threading.current_thread() is not self._main_thread:
            return sys.monitoring.DISABLE
        marks = self._breakpoints.get(code.co_filename)
        hit = marks is not None and line in marks
        if not hit and not self._step_armed:
            # An unmarked line: never call us here again until restart_events.
            return sys.monitoring.DISABLE
        if not hit and not code.co_filename.startswith(self._root + os.sep):
            # Stepping through library code on the way to the next project
            # line: keep the events on, do not stop here.
            return None
        self._pause(code, line, sys._getframe(1))
        # A location that paused stays enabled (a breakpoint in a loop fires
        # on every pass); a step location is DISABLEd on its next pass above.
        return None

    def _pause(self, code, line, frame) -> None:
        rel = project_relpath(code.co_filename, self._root) or os.path.basename(
            code.co_filename)
        self._step_armed = False
        self._in_hook = True
        try:
            self.hits += 1
            reply = self._rpc.call(
                '__paused', [rel, int(line), snapshot_locals(frame, self._max_locals)],
                'call')
        finally:
            self._in_hook = False
        if reply == 'stop':
            raise StopRequested()
        if reply == 'step':
            self._step_armed = True
            sys.monitoring.restart_events()


# ── the „where am I" sampler ────────────────────────────────────────────────

def _innermost_project_position(frame, project_root: str):
    while frame is not None:
        rel = project_relpath(frame.f_code.co_filename, project_root)
        if rel is not None:
            return rel, int(frame.f_lineno or 0)
        frame = frame.f_back
    return None


def start_line_sampler(rpc, project_root: str, *,
                       interval_s: float = SAMPLER_INTERVAL_S) -> threading.Thread:
    """Report the main thread's current project line as ``__line`` every
    ``interval_s`` when it changed, only when the RPC lock is free (a student
    call in flight is never delayed by the indicator)."""
    root = os.path.abspath(project_root)
    main_id = threading.main_thread().ident
    lock = rpc._lock

    def run() -> None:
        last = None
        while True:
            time.sleep(interval_s)
            frame = sys._current_frames().get(main_id)
            if frame is None:
                return
            pos = _innermost_project_position(frame, root)
            if pos is None or pos == last:
                continue
            if not lock.acquire(blocking=False):
                continue
            try:
                rpc.call('__line', [pos[0], pos[1]], 'call')
                last = pos
            except Exception:  # noqa: BLE001 — the indicator never ends a run
                return
            finally:
                lock.release()

    thread = threading.Thread(target=run, name='edubotics-line-sampler', daemon=True)
    thread.start()
    return thread
