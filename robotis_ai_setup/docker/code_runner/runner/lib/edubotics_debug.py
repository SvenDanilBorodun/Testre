"""The Python debugger hook and the live values of a student program.

Installed by ``student_main.py`` into the student's own process before
``main.py`` runs. Three things live here:

* :class:`Hook` — a LINE callback that returns ``sys.monitoring.DISABLE`` for
  every location that carries no breakpoint (measured x1.02 against no hook,
  P12 of the spec), and on a hit sends ``__paused(file, line, locals)`` over
  the stub's RPC client and blocks until the robot answers ``continue`` |
  ``step`` | ``stop``. A breakpoint set that changes MID-RUN is applied
  through :meth:`Hook.set_breakpoints`, which ends in
  ``sys.monitoring.restart_events()`` — without that call a location the
  callback has already DISABLEd never fires again (P13: 0 hits without it,
  millions with it), so a breakpoint added while the loop runs would be
  silently dead. It renders on the student's own thread while the program
  stands still, where calling a ``__repr__`` is what every Python debugger
  does.
* :func:`start_line_sampler` — the „where am I" indicator. A daemon thread
  reads the main thread's frame every ``interval_s`` and reports the
  innermost PROJECT line as ``__line`` when it changed and the connection
  lock is free. Never a per-line event (B13). It reads ``f_code``,
  ``f_lineno`` and ``f_back`` of the main thread's frames and nothing else —
  never ``f_locals`` (on CPython 3.12 a cross-thread read re-syncs the frame's
  own ``locals()`` dict, and a student's ``for k in locals():`` raised
  „dictionary changed size during iteration"), never ``f_globals``, never a
  value.
* :class:`LiveValues` — the live values (owner decision R2-O1, 2026-09-27).
  The stub calls :meth:`LiveValues.before_call` on the STUDENT'S OWN thread
  right before every public robot call (``zeige`` included, the library's
  ``__`` calls never), and the launcher calls :meth:`LiveValues.final` once
  before ``__exit``. Each reports the program's MODULE-LEVEL variables
  (:func:`snapshot_vars`) as ``__vars`` — at most once per
  ``LIVE_VALUES_INTERVAL_S``, only when the snapshot changed. The connection
  is the calling thread's own at that moment, so nothing waits for a lock a
  robot call holds (a sampler that waited for a free lock sent nothing while
  a program spent its time in robot calls), and nothing is read from another
  thread. :meth:`LiveValues.final` sends the last values of every project
  module inside the launcher's one hold of the RPC lock, together with
  ``__exit`` (``student_main.finish_run``), so no other thread's call comes
  between them and nothing is skipped; its only wait of its own is the
  server's floor, within one ``LIVE_VALUES_INTERVAL_S``. A failure — a robot that does not know
  ``__vars``, a lost connection, an oversized frame — switches the live
  values off for the rest of the run and never reaches the program: the
  robot call goes ahead unchanged.

  Every live value goes through :func:`safe_render`, which never runs
  student code: EXACT builtin types only (``type(v) is …``, compared by
  identity — never ``isinstance``, which may consult ``__class__``, and never
  ``in`` a tuple, which calls a metaclass ``__eq__``), exact builtin
  containers walked within a node and character budget, a class named through
  ``type``'s own descriptor and only when that name is an exact ``str``. No
  ``repr``, ``str``, ``format``, ``__iter__``, ``__len__``, ``__eq__`` or
  ``__hash__`` of a student object is ever called, and no full ``repr`` of a
  big container is ever built. A file name is looked at only when it is an
  exact ``str`` (a ``co_filename`` can be a subclass).

The bounds are twins of ``physical_ai_server/workflow/robot_api.py`` (the
server derives its per-frame caps from the same numbers, so a frame the
runner renders always fits; ``tests/test_code_runner_limits_lockstep.py``
compares them). Only the main thread is debugged; a line event from any other
thread (the sampler, a student's own thread) is DISABLEd on sight. None of
this is a security boundary — a student can uninstall all of it from inside
their own process (R-3); the server-side budgets and validation are the
boundary.
"""

from __future__ import annotations

import contextlib
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

# ── twins of robot_api (the lockstep test compares them) ─────────────────
PAUSED_MAX_LOCALS = 30
# A breakpoint value: its JSON text at most this long, else its cut repr.
PAUSED_VALUE_MAX_CHARS = 1000
# A live value: JSON nodes (a cut container's '…' included) and characters
# of strings and dict keys, counted the way the server counts them.
LIVE_VALUE_MAX_NODES = 100
LIVE_VALUE_MAX_CHARS = 1000
SHOWN_TOO_BIG = '<zu groß>'
LIVE_VALUES_INTERVAL_S = 0.5
VARS_REPLY_SKIPPED = 'skipped'
# The robot shows no name longer than this (code_rpc._MAX_NAME_CHARS).
SHOWN_NAME_MAX_CHARS = 64

CONTAINER_MAX_ITEMS = 50
CONTAINER_MAX_DEPTH = 3
SAMPLER_INTERVAL_S = 0.5
# A `__vars` / `__paused` frame is trimmed below this many bytes of JSON (the
# DATA frame bound is 64 KiB; the envelope and the position fit in the rest).
VARS_FRAME_BUDGET_BYTES = 48 * 1024
# An exact int wider than this is only named: int→str is quadratic and
# refuses past 4300 digits.
SAFE_INT_MAX_BITS = 256
# At most this many module globals are looked at per module per snapshot.
GLOBALS_SCAN_MAX = 512
# A class name is shown up to this many characters.
_CLASS_NAME_MAX_CHARS = 60
_CUT = '…'
_TYPE_NAME = type.__dict__['__name__']
_MISSING = object()


class StopRequested(BaseException):
    """The robot answered ``stop`` at a breakpoint: unwind the program.

    A ``BaseException`` so an ordinary ``except Exception:`` in student code
    does not swallow it. The supervisor's out-of-band kill does not depend on
    this reaching the launcher; it is the polite path."""


# ── the breakpoint path's renderer (the stopped student thread) ───────────

def _jsonable(value, depth: int = 0):
    """A bounded, JSON-serialisable rendering of one local's value."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return value[:PAUSED_VALUE_MAX_CHARS]
    if depth < CONTAINER_MAX_DEPTH:
        # islice inside one list() call: O(CONTAINER_MAX_ITEMS), not O(len).
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(itertools.islice(value, CONTAINER_MAX_ITEMS))
            return [_jsonable(v, depth + 1) for v in items]
        if isinstance(value, dict):
            out = {}
            for k, v in list(itertools.islice(value.items(), CONTAINER_MAX_ITEMS)):
                out[str(k)[:PAUSED_VALUE_MAX_CHARS]] = _jsonable(v, depth + 1)
            return out
    try:
        return repr(value)[:PAUSED_VALUE_MAX_CHARS]
    except Exception:  # noqa: BLE001 — a hostile __repr__ must not break a pause
        return '<?>'


_SKIPPED_VALUE_TYPES = (
    types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
    types.MethodType, type,
)


def _render_value(value):
    """One value, bounded: the structured rendering when its JSON fits
    ``PAUSED_VALUE_MAX_CHARS``, else the (cut) repr — never an exception."""
    try:
        rendered = _jsonable(value)
        if len(json.dumps(rendered, ensure_ascii=False)) <= PAUSED_VALUE_MAX_CHARS:
            return rendered
    except Exception:  # noqa: BLE001 — a hostile __str__ on a key, a mutation mid-walk
        pass
    try:
        return repr(value)[:PAUSED_VALUE_MAX_CHARS]
    except Exception:  # noqa: BLE001 — a hostile __repr__ must not break a pause
        return '<?>'


# ── the live values' renderer: exact builtin types only, never student code ──

def type_name_of(value) -> str:
    """The class name of ``value`` without running student code: ``type()``
    reads the object's type slot, and ``type``'s own ``__name__`` descriptor
    reads the class's stored name — a metaclass ``__name__`` property is
    never consulted. A stored name that is not an exact ``str`` (a class can
    be given a ``str`` subclass with its own ``__format__``) is ``'?'``."""
    try:
        name = _TYPE_NAME.__get__(type(value), type)
    except Exception:  # noqa: BLE001 — never raise out of a snapshot
        return '?'
    return name if type(name) is str else '?'


def _utf8_safe(text: str) -> str:
    """``text`` (an exact str) with every lone surrogate replaced by ``?``
    (same length): one would make the whole frame unencodable."""
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        return text.encode('utf-8', 'replace').decode('utf-8')
    return text


def _take_text(text: str, budget: list) -> str:
    """``text`` (an exact str) cut to the character budget and charged to it."""
    n = max(0, min(len(text), budget[1]))
    budget[1] -= n
    return _utf8_safe(text[:n])


def _is_exact_sequence(t) -> bool:
    return t is list or t is tuple or t is set or t is frozenset


def _key_text(k):
    """A dict key as its JSON key text, or None when it is not an exact
    builtin scalar (then the entry is not shown)."""
    kt = type(k)
    if kt is str:
        return k
    if k is None or kt is bool or kt is float:
        return repr(k)
    if kt is int:
        return repr(k) if k.bit_length() <= SAFE_INT_MAX_BITS else None
    return None


def safe_render(value, _budget: list | None = None, _depth: int = 0):
    """``value`` as JSON-able builtins, without running student code.

    Exact ``None``/``bool``/``int``/``float``/``str`` are rendered (a string
    cut to the character budget, a non-finite float as ``'nan'``/``'inf'``, an
    int over :data:`SAFE_INT_MAX_BITS` bits as ``'<int>'``); an exact
    ``list``/``tuple``/``set``/``frozenset``/``dict`` is walked up to
    ``CONTAINER_MAX_DEPTH`` levels and ``CONTAINER_MAX_ITEMS`` items, its
    prefix copied in ONE C-level ``islice`` pass; a dict entry is kept only
    when its key is an exact builtin scalar, and its key text is charged to
    the character budget (a dict with a skipped entry is not shown whole, so
    it ends in the ``'…'`` too — never an empty ``{}``). Anything else — a subclass of a builtin included —
    is ``'<Klasse>'``. A container that could not be shown whole ends in one
    ``'…'``. The result holds at most :data:`LIVE_VALUE_MAX_NODES` nodes and
    :data:`LIVE_VALUE_MAX_CHARS` characters of strings and keys, both counted
    the way the server counts them — every node it emits was charged first."""
    budget = _budget if _budget is not None else [LIVE_VALUE_MAX_NODES, LIVE_VALUE_MAX_CHARS]
    budget[0] -= 1                       # this node; the caller made sure it fits
    t = type(value)
    if value is None or t is bool:
        return value
    if t is int:
        if value.bit_length() <= SAFE_INT_MAX_BITS:
            return value
        return _take_text('<int>', budget)
    if t is float:
        return value if math.isfinite(value) else _take_text(float.__repr__(value), budget)
    if t is str:
        return _take_text(value, budget)
    if _is_exact_sequence(t) or t is dict:
        if _depth >= CONTAINER_MAX_DEPTH:
            return _class_label(value, budget)
        total = len(value)               # an exact builtin's own length slot
        if t is dict:
            items = list(itertools.islice(value.items(), CONTAINER_MAX_ITEMS))
            out: dict | list = {}
        else:
            items = list(itertools.islice(value, CONTAINER_MAX_ITEMS))
            out = []
        cut = total > len(items)
        for i, item in enumerate(items):
            # Keep one node for the closing '…' while anything is left over.
            if budget[0] < (2 if (i + 1 < len(items) or cut) else 1):
                cut = True
                break
            if t is dict:
                key = _key_text(item[0])
                if key is None:
                    # Not a shown key: the entry is skipped, and the dict is
                    # then not shown whole — it says so with the '…' below,
                    # never as an empty `{}` (review round 5, nd6).
                    cut = True
                    continue
                if budget[1] < len(key):
                    cut = True
                    break
                budget[1] -= len(key)
                out[_utf8_safe(key)] = safe_render(item[1], budget, _depth + 1)
            else:
                out.append(safe_render(item, budget, _depth + 1))
        if cut and budget[0] >= 1 and budget[1] >= 1:
            if t is dict:
                if _CUT not in out:          # the key '…' costs one character
                    budget[0] -= 1
                    budget[1] -= 1
                    out[_CUT] = None
            else:
                budget[0] -= 1
                budget[1] -= 1
                out.append(_CUT)
        return out
    return _class_label(value, budget)


def _class_label(value, budget: list) -> str:
    return _take_text('<' + type_name_of(value)[:_CLASS_NAME_MAX_CHARS] + '>', budget)


def _is_skipped_type(value) -> bool:
    """Modules, functions, methods and classes are not variables worth
    showing — decided on ``type(value)`` alone (``issubclass`` against
    builtin types reads the MRO slot; no student code)."""
    try:
        return issubclass(type(value), _SKIPPED_VALUE_TYPES)
    except Exception:  # noqa: BLE001
        return True


def _add_safe_globals(out: dict, namespace, max_vars: int, exclude=None) -> None:
    """Copy showable module globals into ``out`` (the live values' path).
    A name bound to the very object ``exclude`` (the robot module's
    namespace) holds under the same name is the library's, not the
    program's: ``from robot import *`` brings in ``MAX_CALLS_PER_S`` and its
    friends, and they are not shown."""
    if type(namespace) is not dict:
        return
    for name, value in list(itertools.islice(namespace.items(), GLOBALS_SCAN_MAX)):
        if len(out) >= max_vars:
            return
        if (type(name) is not str or name.startswith('__') or name in out
                or len(name) > SHOWN_NAME_MAX_CHARS):
            continue
        if exclude is not None and exclude.get(name, _MISSING) is value:
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
        if (not isinstance(name, str) or name.startswith('__') or name in out
                or len(name) > SHOWN_NAME_MAX_CHARS):
            continue
        if isinstance(value, _SKIPPED_VALUE_TYPES):
            continue
        out[name] = _render_value(value)


def snapshot_locals(frame, max_locals: int = PAUSED_MAX_LOCALS) -> dict:
    """At most ``max_locals`` of the frame's locals, dunders and code objects
    skipped, every value bounded, and the whole trimmed (:func:`fit_vars`)
    so the frame fits the DATA bound — every name still shows."""
    out: dict = {}
    _add_mapping(out, list(frame.f_locals.items()), max_locals)
    return fit_vars(out)


def snapshot_vars(frame, project_root: str, max_locals: int = PAUSED_MAX_LOCALS,
                  exclude=None) -> dict:
    """The variables a student sees while the program runs, for ``__vars``.

    From ``frame`` (a frame of the CALLING thread) the innermost PROJECT
    frame is found; its MODULE's globals come first, then the outermost
    project frame's module (``main.py`` on the main thread) when that is
    another module. Never a function frame's ``f_locals``. At most
    ``max_locals``; dunders, modules, functions, classes and the robot
    library's own names (``exclude``) skipped; every value through
    :func:`safe_render`, so no student code runs."""
    root = os.path.abspath(project_root)
    project = []
    while frame is not None:
        if project_relpath(frame.f_code.co_filename, root) is not None:
            project.append(frame)
        frame = frame.f_back
    if not project:
        return {}
    return snapshot_namespaces([project[0].f_globals, project[-1].f_globals],
                               max_locals, exclude)


def snapshot_namespaces(namespaces, max_locals: int = PAUSED_MAX_LOCALS,
                        exclude=None) -> dict:
    """The shown module-level variables of ``namespaces`` (module dicts,
    first wins; one dict listed twice is read once)."""
    out: dict = {}
    seen = []
    for ns in namespaces:
        if any(ns is s for s in seen):
            continue
        seen.append(ns)
        _add_safe_globals(out, ns, max_locals, exclude)
    return out


def project_namespaces(tb, project_root: str) -> list:
    """The module dicts of a finished program's last position, from its
    traceback (an exception or ``sys.exit``): the innermost project frame's
    module first, then the outermost's. Empty when no project frame is on it
    (a syntax error in ``main.py``)."""
    root = os.path.abspath(project_root)
    frames = []
    while tb is not None:
        frame = tb.tb_frame
        if project_relpath(frame.f_code.co_filename, root) is not None:
            frames.append(frame)
        tb = tb.tb_next
    if not frames:
        return []
    return [frames[-1].f_globals, frames[0].f_globals]


# At most this many entries of ``sys.modules`` are looked at for the last
# values, and this many of a module's first dict entries for its ``__file__``
# (set when the module is created, so among the first few).
MODULES_SCAN_MAX = 4096
_MODULE_HEAD_SCAN = 16
# ``ModuleType``'s own ``__dict__`` slot: a module is read through it, never
# through an attribute lookup a swapped ``__class__`` could answer.
_MODULE_DICT = types.ModuleType.__dict__['__dict__'].__get__


def project_module_namespaces(project_root: str, modules=None) -> list:
    """The module dicts of every PROJECT module the program imported
    (``helfer.py``, ``pkg/werte.py`` …), sorted by module name — for the last
    values, which used to come from ``main.py`` alone, so a value a helper
    module kept never arrived (review round 5, md7).

    Read without running student code: ``modules`` (``sys.modules``) only
    when it is an exact ``dict``; a module only when its type IS
    ``types.ModuleType`` (a module whose ``__class__`` was swapped is
    skipped), its dict through ``ModuleType``'s own slot; its ``__file__``
    found by walking the dict's first entries for an exact ``str`` key (a
    lookup could call a colliding key's ``__eq__``) and used only when it is
    an exact ``str`` under the project. ``main.py`` itself is not among them
    (``runpy`` ran it as a temporary ``__main__``): the caller passes its
    dict first."""
    mods = sys.modules if modules is None else modules
    if type(mods) is not dict:
        return []
    root = os.path.abspath(project_root)
    found = []
    for name, mod in list(itertools.islice(dict.items(mods), MODULES_SCAN_MAX)):
        if type(name) is not str or type(mod) is not types.ModuleType:
            continue
        try:
            ns = _MODULE_DICT(mod)
        except Exception:  # noqa: BLE001
            continue
        if type(ns) is not dict:
            continue
        path = None
        for key, value in itertools.islice(dict.items(ns), _MODULE_HEAD_SCAN):
            if type(key) is str and key == '__file__':
                path = value
                break
        if project_relpath(path, root) is None:
            continue
        found.append((name, ns))
    found.sort(key=lambda item: item[0])
    return [ns for _name, ns in found]


def fit_vars(snapshot: dict, budget_bytes: int = VARS_FRAME_BUDGET_BYTES) -> dict:
    """Replace the biggest values with :data:`SHOWN_TOO_BIG` until the
    snapshot's JSON fits the budget — every name stays. Each entry is
    measured ONCE (json.dumps per entry, then arithmetic), so the cost is
    linear in the snapshot, never quadratic."""
    out = dict(snapshot)

    def size(obj) -> int:
        return len(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    sizes = {k: size(v) for k, v in out.items()}
    # `{"k": v, …}` with json.dumps' default separators: an upper bound on
    # the compact frame the stub sends.
    total = 2 + sum(size(k) + 2 + sizes[k] for k in out) + 2 * max(0, len(out) - 1)
    if total <= budget_bytes:
        return out
    placeholder = size(SHOWN_TOO_BIG)
    for k in sorted(out, key=lambda name: sizes[name], reverse=True):
        if total <= budget_bytes:
            break
        if sizes[k] <= placeholder:
            break
        total -= sizes[k] - placeholder
        out[k] = SHOWN_TOO_BIG
    return out


def project_relpath(path, project_root: str) -> str | None:
    """``main.py`` / ``pkg/helper.py`` for a file under the project, else
    None. Only an exact ``str`` is looked at: a ``co_filename`` that is a
    ``str`` subclass would run its own ``startswith``."""
    if type(path) is not str:
        return None
    root = project_root.rstrip('/') + '/'
    if not path.startswith(root):
        return None
    return path[len(root):].replace(os.sep, '/')


class Hook:
    """The LINE-event debugger of one student process."""

    def __init__(self, rpc, project_root: str, *,
                 max_locals: int = PAUSED_MAX_LOCALS) -> None:
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
                       stop: threading.Event | None = None) -> threading.Thread:
    """Report the main thread's current project line as ``__line`` every
    ``interval_s`` when it changed, only when the RPC lock is free (a student
    call in flight is never delayed by the indicator). It reads the main
    thread's frames' ``f_code``, ``f_lineno`` and ``f_back`` — never a
    variable (the live values are :class:`LiveValues`', sent from the
    student's own thread). ``stop`` ends the thread (tests)."""
    root = os.path.abspath(project_root)
    main_id = threading.main_thread().ident
    lock = rpc._lock

    def run() -> None:
        last = None
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
            # Drop the frame now: a held frame keeps the program's objects alive.
            frame = None
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


# ── the live values (owner decision R2-O1) ────────────────────────────────────

class LiveValues:
    """The program's module-level variables, sent by the program's own thread.

    Wired by the launcher as the stub's ``_Rpc.before_call``: right before a
    public robot call, :meth:`before_call` looks at the variables at most once
    per ``interval_s`` (measured from the previous check's END, so the
    server's ``VARS_MIN_INTERVAL_S`` floor never skips an honest frame) and
    sends ``__vars`` only when the snapshot changed; :meth:`final` sends the
    last values once before ``__exit``. Any failure switches it off for the
    rest of the run and is never raised (``enabled`` tells, for tests)."""

    def __init__(self, rpc, project_root: str, *,
                 max_vars: int = PAUSED_MAX_LOCALS, exclude=None,
                 interval_s: float = LIVE_VALUES_INTERVAL_S,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        self._rpc = rpc
        self._root = os.path.abspath(project_root)
        self._max = max_vars
        self._exclude = exclude if type(exclude) is dict else None
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._busy = threading.Lock()
        self._next_check = float('-inf')
        # When the last ``__vars`` frame was handed to the stub: the
        # server's floor counts from there (set BEFORE the call, so a thread
        # that has since released the RPC lock has always set it).
        self._last_send = float('-inf')
        self._sent_key = None
        self._position = None
        self.enabled = True
        self.sent = 0

    def before_call(self) -> None:
        """The stub's hook: on the calling thread, before a public call."""
        if not self.enabled or self._clock() < self._next_check:
            return
        if not self._busy.acquire(blocking=False):
            return
        try:
            frame = sys._getframe(1)
            pos = _innermost_project_position(frame, self._root)
            if pos is not None:
                self._position = pos
                fitted = fit_vars(snapshot_vars(frame, self._root, self._max, self._exclude))
                frame = None
                self._send_if_changed(pos, fitted)
        except Exception:  # noqa: BLE001 — the live values never break a call
            self.enabled = False
        finally:
            frame = None
            self._next_check = self._clock() + self._interval
            self._busy.release()

    def final(self, namespaces, fallback_position) -> None:
        """The last values, once, right before ``__exit``. Never raises.

        The launcher calls this INSIDE its one hold of the stub's RPC lock
        and sends ``__exit`` in the same hold (``student_main.finish_run``;
        review round 5, md7), so nothing here waits for a lock or skips
        because one was busy: another thread's check can SEND only under that
        lock, and after ``__exit`` it never will. (Round 4 skipped the final
        send when the lock was not free within one interval — and ``__exit``
        then waited for the same lock without a bound anyway, so a program
        with a worker thread ended showing stale values.) Called on its own,
        the RPC lock is taken here — re-entrantly, and blocking, which is the
        wait ``__exit`` has anyway.

        ``namespaces`` are module dicts, first wins: the launcher passes
        ``main.py``'s, then every project module's
        (:func:`project_module_namespaces`). The one wait of its own: when a
        ``__vars`` frame went out less than ``interval_s`` ago, the rest of
        that interval is waited out (at most ``interval_s``), so the
        server's floor looks at this frame instead of answering
        ``skipped``."""
        if not self.enabled:
            return
        rpc_lock = getattr(self._rpc, '_lock', None)
        try:
            with rpc_lock if rpc_lock is not None else contextlib.nullcontext():
                fitted = fit_vars(snapshot_namespaces(namespaces, self._max, self._exclude))
                if self._key(fitted) == self._sent_key:
                    return
                wait = min(self._last_send + self._interval - self._clock(), self._interval)
                if wait > 0:
                    self._sleep(wait)
                self._send_if_changed(self._position or fallback_position, fitted)
        except Exception:  # noqa: BLE001 — the end of a run is never a crash
            self.enabled = False

    @staticmethod
    def _key(fitted: dict) -> str:
        return json.dumps(fitted, sort_keys=True, ensure_ascii=False)

    def _send_if_changed(self, pos, fitted: dict) -> None:
        key = self._key(fitted)
        if key == self._sent_key:
            return
        self._last_send = self._clock()
        reply = self._rpc.call('__vars', [pos[0], pos[1], fitted], 'call')
        self.sent += 1
        # The robot's floor answered without looking: send it again next time.
        if reply != VARS_REPLY_SKIPPED:
            self._sent_key = key
