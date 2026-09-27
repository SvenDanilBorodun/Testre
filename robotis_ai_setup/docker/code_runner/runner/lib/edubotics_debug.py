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
* :func:`start_line_sampler` — the „where am I" indicator and the live
  variable values. A daemon thread reads the main thread's frame every
  ``interval_s`` and, when the connection lock is free, reports the innermost
  PROJECT line as ``__line`` when it changed and the program's MODULE-LEVEL
  variables as ``__vars`` when their snapshot changed (:func:`snapshot_vars`;
  at most 30). Never a per-line event (B13).

  The sampler is a second thread INSIDE the student's program, so two rules
  bind it (owner decision R-O1, 2026-09-27 — the O3 fallback):

  - It never reads a function frame's ``f_locals``. On CPython 3.12 that read
    re-syncs the frame's own ``locals()`` dict, so a student's
    ``for k in locals():`` raised „dictionary changed size during iteration"
    (the O3 probe had measured values and refcounts, not the student's view
    of ``locals()``). Module globals are read through ``f_globals``, the
    module's own dict, which a read never changes.
  - It never runs student code. :func:`safe_render` renders only EXACT
    builtin types (``type(v) is …``, never ``isinstance``, which may consult
    ``__class__``), walks only exact builtin containers, each value within a
    node and character budget, and names anything else ``<Klasse>`` — the
    name read through ``type``'s own descriptor, so a metaclass's
    ``__name__`` never runs. No ``repr``, ``str``, ``__iter__``, ``__len__``,
    ``__eq__`` or ``__hash__`` of a student object is ever called, and no
    full ``repr`` of a big container is ever built.

  The breakpoint path (:class:`Hook`, ``__paused``) is unchanged: it renders
  on the student's own thread while the program stands still, where calling
  a ``__repr__`` is what every Python debugger does.

Only the main thread is debugged; a line event from any other thread (the
sampler, a student's own thread) is DISABLEd on sight. Neither mechanism is
a security boundary — a student can uninstall both from inside their own
process (R-3); the server-side budgets and validation are the boundary.
"""

from __future__ import annotations

import itertools
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
# A `__vars` frame is trimmed below this many bytes of JSON (the DATA frame
# bound is 64 KiB; the envelope and the position fit in the rest).
VARS_FRAME_BUDGET_BYTES = 48 * 1024


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
        # islice inside one list() call: O(CONTAINER_MAX_ITEMS), not O(len),
        # and one C-level pass (the sampler reads while the program runs).
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(itertools.islice(value, CONTAINER_MAX_ITEMS))
            return [_jsonable(v, depth + 1) for v in items]
        if isinstance(value, dict):
            out = {}
            for k, v in list(itertools.islice(value.items(), CONTAINER_MAX_ITEMS)):
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


def _render_value(value):
    """One value, bounded: the structured rendering when its JSON fits
    ``VALUE_MAX_CHARS``, else the (cut) repr — never an exception."""
    try:
        rendered = _jsonable(value)
        if len(json.dumps(rendered, ensure_ascii=False)) <= VALUE_MAX_CHARS:
            return rendered
    except Exception:  # noqa: BLE001 — a hostile __str__ on a key, a mutation mid-walk
        pass
    try:
        return repr(value)[:VALUE_MAX_CHARS]
    except Exception:  # noqa: BLE001 — a hostile __repr__ must not break a pause
        return '<?>'


# ── the sampler's renderer: exact builtin types only, never student code ──

# Per shown value: nodes visited and string characters copied. A snapshot
# holds at most 30 values, so a whole `__vars` frame stays far below the
# server's per-frame cap (code_rpc.SHOWN_FRAME_MAX_NODES).
SAFE_VALUE_MAX_NODES = 100
SAFE_VALUE_MAX_CHARS = VALUE_MAX_CHARS
# An exact int wider than this is only named: int→str is quadratic and
# refuses past 4300 digits.
SAFE_INT_MAX_BITS = 256
# At most this many module globals are looked at per module per sample.
GLOBALS_SCAN_MAX = 512
_SAFE_SEQUENCES = (list, tuple, set, frozenset)
_SAFE_KEY_SCALARS = (bool, int, float)
_TYPE_NAME = type.__dict__['__name__']


def type_name_of(value) -> str:
    """The class name of ``value`` without running student code: ``type()``
    reads the object's type slot, and ``type``'s own ``__name__`` descriptor
    reads the class's stored name — a metaclass ``__name__`` property is
    never consulted."""
    try:
        return _TYPE_NAME.__get__(type(value), type)
    except Exception:  # noqa: BLE001 — never raise out of the sampler
        return '?'


def safe_render(value, _budget: list | None = None, _depth: int = 0):
    """``value`` as JSON-able builtins, without running student code.

    Exact ``None``/``bool``/``int``/``float``/``str`` are rendered (a string
    cut to the character budget, a non-finite float as ``'nan'``/``'inf'``, an
    int over :data:`SAFE_INT_MAX_BITS` bits as ``'<int>'``); an exact
    ``list``/``tuple``/``set``/``frozenset``/``dict`` is walked up to
    ``CONTAINER_MAX_DEPTH`` levels and ``CONTAINER_MAX_ITEMS`` items, its
    prefix copied in ONE C-level ``islice`` pass (atomic under the GIL); a
    dict entry is kept only when its key is an exact builtin scalar. Anything
    else — a subclass of a builtin included — is ``'<Klasse>'``."""
    budget = _budget if _budget is not None else [SAFE_VALUE_MAX_NODES, SAFE_VALUE_MAX_CHARS]
    if budget[0] <= 0:
        return '…'
    budget[0] -= 1
    t = type(value)
    if value is None or t is bool:
        return value
    if t is int:
        return value if value.bit_length() <= SAFE_INT_MAX_BITS else '<int>'
    if t is float:
        return value if math.isfinite(value) else float.__repr__(value)
    if t is str:
        n = max(0, min(len(value), budget[1]))
        budget[1] -= n
        return value[:n]
    if t in _SAFE_SEQUENCES or t is dict:
        if _depth >= CONTAINER_MAX_DEPTH:
            return f'<{type_name_of(value)}>'
        take = max(0, min(CONTAINER_MAX_ITEMS, budget[0]))
        if t is dict:
            out = {}
            for k, v in list(itertools.islice(value.items(), take)):
                kt = type(k)
                if kt is str:
                    key = k[:VALUE_MAX_CHARS]
                elif k is None or (kt in _SAFE_KEY_SCALARS
                                   and (kt is not int or k.bit_length() <= SAFE_INT_MAX_BITS)):
                    key = repr(k)
                else:
                    continue
                out[key] = safe_render(v, budget, _depth + 1)
            return out
        return [safe_render(v, budget, _depth + 1)
                for v in list(itertools.islice(value, take))]
    return f'<{type_name_of(value)}>'


def _is_skipped_type(value) -> bool:
    """Modules, functions, methods and classes are not variables worth
    showing — decided on ``type(value)`` alone (``issubclass`` against
    builtin types reads the MRO slot; no student code)."""
    try:
        return issubclass(type(value), _SKIPPED_VALUE_TYPES)
    except Exception:  # noqa: BLE001
        return True


def _add_safe_globals(out: dict, namespace, max_locals: int) -> None:
    """Copy showable module globals into ``out`` (the sampler's path)."""
    if type(namespace) is not dict:
        return
    for name, value in list(itertools.islice(namespace.items(), GLOBALS_SCAN_MAX)):
        if len(out) >= max_locals:
            return
        if type(name) is not str or name.startswith('__') or name in out:
            continue
        if _is_skipped_type(value):
            continue
        out[name] = safe_render(value)


def _add_mapping(out: dict, items, max_locals: int) -> None:
    """Copy showable ``(name, value)`` pairs into ``out`` until it holds
    ``max_locals``; a name already in ``out`` keeps its first value."""
    for name, value in items:
        if len(out) >= max_locals:
            return
        if not isinstance(name, str) or name.startswith('__') or name in out:
            continue
        if isinstance(value, _SKIPPED_VALUE_TYPES):
            continue
        out[name] = _render_value(value)


def snapshot_locals(frame, max_locals: int = _DEFAULT_MAX_LOCALS) -> dict:
    """At most ``max_locals`` of the frame's locals, dunders and code objects
    skipped, every value bounded so the whole frame fits the DATA bound."""
    out: dict = {}
    _add_mapping(out, list(frame.f_locals.items()), max_locals)
    return out


def snapshot_vars(frame, project_root: str, max_locals: int = _DEFAULT_MAX_LOCALS) -> dict:
    """The variables a student sees while the program runs, for ``__vars``.

    From ``frame`` (the main thread's current one) the innermost PROJECT
    frame is found; its MODULE's globals come first, then the entry module's
    (``main.py``) when that is another module. Never a function frame's
    ``f_locals`` (see the module docstring). At most ``max_locals``; dunders,
    modules, functions and classes skipped; every value through
    :func:`safe_render`, so no student code runs."""
    root = os.path.abspath(project_root)
    project = []
    while frame is not None:
        if project_relpath(frame.f_code.co_filename, root) is not None:
            project.append(frame)
        frame = frame.f_back
    if not project:
        return {}
    inner, outer = project[0], project[-1]
    out: dict = {}
    _add_safe_globals(out, inner.f_globals, max_locals)
    if outer.f_globals is not inner.f_globals:
        _add_safe_globals(out, outer.f_globals, max_locals)
    return out


def fit_vars(snapshot: dict, budget_bytes: int = VARS_FRAME_BUDGET_BYTES) -> dict:
    """Drop the biggest entries until the snapshot's JSON fits the budget."""
    out = dict(snapshot)

    def size(obj) -> int:
        return len(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    while out and size(out) > budget_bytes:
        del out[max(out, key=lambda k: size(out[k]))]
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
                       interval_s: float = SAMPLER_INTERVAL_S,
                       send_vars: bool = True,
                       stop: threading.Event | None = None) -> threading.Thread:
    """Every ``interval_s``: report the main thread's current project line as
    ``__line`` when it changed, and its variables (:func:`snapshot_vars`, cut
    to the frame budget by :func:`fit_vars`) as ``__vars`` when the snapshot
    changed — both only when the RPC lock is free (a student call in flight
    is never delayed by the indicator). ``stop`` ends the thread (tests)."""
    root = os.path.abspath(project_root)
    main_id = threading.main_thread().ident
    lock = rpc._lock

    def run() -> None:
        last = None
        last_vars = None
        while True:
            if stop is not None:
                if stop.wait(interval_s):
                    return
            else:
                time.sleep(interval_s)
            frame = sys._current_frames().get(main_id)
            if frame is None:
                return
            pos = _innermost_project_position(frame, root)
            if pos is None:
                continue
            new_vars = None
            if send_vars:
                try:
                    snapshot = fit_vars(snapshot_vars(frame, root))
                    key = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
                    if key != last_vars:
                        new_vars = (snapshot, key)
                except Exception:  # noqa: BLE001 — a value the snapshot cannot render
                    new_vars = None
            # Drop the frame now: a held frame keeps the program's objects alive.
            frame = None
            if pos == last and new_vars is None:
                continue
            if not lock.acquire(blocking=False):
                continue
            try:
                if pos != last:
                    rpc.call('__line', [pos[0], pos[1]], 'call')
                    last = pos
                if new_vars is not None:
                    rpc.call('__vars', [pos[0], pos[1], new_vars[0]], 'call')
                    last_vars = new_vars[1]
            except Exception:  # noqa: BLE001 — the indicator never ends a run
                return
            finally:
                lock.release()

    thread = threading.Thread(target=run, name='edubotics-line-sampler', daemon=True)
    thread.start()
    return thread
