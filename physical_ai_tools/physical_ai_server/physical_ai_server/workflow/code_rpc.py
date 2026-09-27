#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The data-socket RPC server for text programs — every frame is hostile.

The student process (uid 10001 in the ``code_runner`` container) connects to
``rpc.sock`` in the shared ipc volume and sends ``robot.*`` calls as framed
JSON. Whether the frames come from the shipped stub or from a raw ``socket``
is indistinguishable and treated the same: every frame is validated here —
method, arity, per-parameter kind and range from :mod:`robot_api` — before a
handler is dispatched (A7.3).

**Framing** (one pair of helpers, every caller): ``u32 big-endian length`` +
the object as ``json.dumps(obj, ensure_ascii=False, separators=(',', ':'))``
in UTF-8. The receiver allocates exactly ``length`` bytes and ``recv_into``s
them — there is no accumulation buffer. ``length > max_bytes`` → one German
error frame and the connection closes. Two bounds: ``MAX_FRAME_BYTES`` for
this DATA socket, ``CONTROL_MAX_FRAME_BYTES`` for the supervisor's control
socket, whose ``start`` envelope carries the whole project.

**Every wait is bounded.** ``read_frame`` and ``write_frame`` set the socket
timeout themselves before every ``recv``/``sendall`` (an AST test asserts no
socket op without a preceding ``settimeout`` in its function). A read timeout
is a POLL POINT — ``should_continue()`` is asked and the partial frame is
resumed — not a frame boundary; a send timeout means the client is not
draining its replies and the connection is closed. The hello read is
additionally bounded AS A WHOLE (``deadline_s``): a per-recv timeout alone
lets a one-byte-per-second drip hold a reader thread forever.

**Before the hello, the surface is anonymous and bounded twice.** At most
``CODE_RPC_MAX_PENDING_CONNECTIONS`` connections may sit un-greeted at once,
process-wide; past that a connection is closed at accept, before a thread is
spawned for it (the per-run cap of ``CODE_RPC_MAX_CONNECTIONS_PER_RUN``
applies only after a valid ``__hello``, so it cannot bound this). Each
pending one lives at most ``CODE_RPC_HELLO_TIMEOUT_S``.

**Rate budget (B1/B2), the server half.** Each run has one global bucket
(``MAX_CALLS_PER_S`` / ``BURST``) and one perception bucket
(``PERCEPTION_*``); both SLEEP until a token is available, never drop, never
busy-decode. The global bucket is charged for EVERY decoded frame, before
validation, so an invalid-frame flood is throttled too; the perception bucket
is charged after validation for the rows whose ``budget`` is
``'perception'``. One frame is decoded per loop iteration per connection and
the reader waits for the reply before reading the next, so the decode rate is
bounded by construction (P11: ~200/s against a 350k/s flood).

**One dispatch worker per run.** All data connections of a run (at most
``CODE_RPC_MAX_CONNECTIONS_PER_RUN``) feed ONE worker thread; handlers never
run concurrently with each other (D5). The worker calls
``ctx.wait_if_paused()`` before every handler, then polls
``ctx.should_stop()`` the way ``interpreter._exec_chain`` does before every
statement (a call queued after Stopp never starts a handler), and takes no
lock of its own — the handlers hold ``ctx.motion_lock`` through
``motion._hold_motion_lock`` exactly as a Blockly run does.

**Handles.** A row returning ``'ziel'`` (``find``) hands back ``{"h": n}``;
the Detection lives in ``ctx.rpc_handles`` (an ``OrderedDict`` capped at
``CODE_RPC_MAX_HANDLES``, oldest evicted) and a ``ziel``-kind argument is
resolved through it on the worker. A handle the run does not own is a German
refusal — the ``_is_greifziel`` KIND test in ``motion.py`` then runs
unchanged on the resolved object.

**The ``__`` methods** (validated like any other row): ``__hello`` (the first
frame of every connection; the token is minted per run, handed to the
supervisor, retired the instant the run ends), ``__line`` (status position,
throttled at ``CODE_STATUS_MIN_INTERVAL_S``), ``__paused`` (a breakpoint hit:
emits the locals as ``[VAR:]`` sentinels, sets the pause, and BLOCKS on the
reader thread until the manager resumes — answering ``continue`` / ``step`` /
``stop``), ``__vars`` (the live values the student's own thread sends right
before a robot call and once at the end, owner decision R2-O1: the program's
module-level variables, emitted as ``[VAR:]`` sentinels ONLY for names whose
rendered value changed since this run last showed them and that ``zeige`` has
not claimed; at most one frame per ``VARS_MIN_INTERVAL_S`` is looked at — any
other is answered ``VARS_REPLY_SKIPPED`` at once after the budget charge, so
the runner knows to send again), ``__exit`` (the launcher's exit report, kept
on the session for the program object that owns the run). The shown values
of ONE ``__vars`` / ``__paused`` frame are held to ``SHOWN_FRAME_MAX_NODES`` /
``SHOWN_FRAME_MAX_CHARS`` in TOTAL by :func:`fit_shown_values` before any
rendering — the largest become ``SHOWN_TOO_BIG``, every name still shows (the
caps are derived in ``robot_api`` from the runner's own bounds, so only a
hand-made frame is ever trimmed). ``register_object`` is validated from its
row and registers a per-run type.

**The code-only rows** (``robot_api.CODE_ONLY_METHODS``: ``zeige``) are
public calls with no block and no handler. They are queued to the run's one
worker like a statement (so they wait while paused, are refused after Stopp
and keep program order), and answered there by :meth:`RunSession._code_only`,
which only ever calls ``ctx.log`` — never ``ctx.publisher``, the handler table
or the motion lock (an AST fence in ``test_code_rpc_zeige_vars.py``). ``zeige``
is COALESCED: the latest value per name waits in a per-run map and is emitted
at most ``SHOWN_EMITS_PER_S`` sentinels per second (burst
``SHOWN_EMITS_BURST``) — flushed by the next call, by the worker's idle tick
(``SHOWN_FLUSH_TICK_S``) and, whatever is left, when the run closes, so the
last value of every waiting name arrives. At most ``SHOWN_VAR_NAMES_MAX``
names wait at once; a NEW name past that is dropped and the run's log says so
once (``ZEIGE_TOO_MANY_NAMES_DE``). A name ``zeige`` has shown belongs to
``zeige`` for the rest of the run: ``__vars`` no longer shows a module
variable of the same name (it would flip the panel between the two).
``__vars``, ``zeige`` and a breakpoint's locals share one per-run map of the
last payload per name.

**Replies.** ``{"id", "ok": true, "r"}`` or ``{"id", "ok": false, "k", "e"}``
with a German ``e``. A ``WorkflowError`` from a handler is relayed verbatim
(``k: "robot"``, German by contract); any other exception is ``k:
"internal"`` with :data:`INTERNAL_ERROR_DE` and the traceback logged
server-side only. Nothing raises out of the worker or a reader.

Nothing here reads the environment (no ``EDU…_`` knob: the server package is
scanned for such names and each must be forwarded by a compose). The
constants below are plain module constants.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import secrets
import socket
import stat
import struct
import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Callable

from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import (
    _MAX_VAR_PAYLOAD_CHARS,
    _MAX_VAR_PAYLOAD_ITEMS,
    _jsonable,
)
from physical_ai_server.workflow.robot_api import (
    CODE_ONLY_METHODS_BY_NAME,
    CODE_PATH_RE,
    INTERNAL_METHODS_BY_NAME,
    MAX_CODE_FILE_BYTES,
    MAX_CODE_FILES,
    MAX_CODE_PROJECT_BYTES,
    ROBOT_API_BY_NAME,
    RPC_LIMITS,
    SHOWN_FRAME_MAX_CHARS,
    SHOWN_FRAME_MAX_NODES,
    SHOWN_TOO_BIG,
    SHOWN_VAR_NAMES_MAX,
    VARS_MIN_INTERVAL_S,
    VARS_REPLY_SKIPPED,
    ApiCall,
    ApiParam,
)

__all__ = [
    'CODE_PATH_RE', 'MAX_CODE_FILES', 'MAX_CODE_FILE_BYTES', 'MAX_CODE_PROJECT_BYTES',
    'CodeRpcServer', 'RunSession', 'FrameError', 'FrameTooLarge', 'ReadAbandoned',
    'read_frame', 'write_frame', 'validate_value', 'default_handlers',
]

_log = logging.getLogger(__name__)

# ── constants (§3.2) ──────────────────────────────────────────────────────
CODE_RPC_RECV_TIMEOUT_S = 0.25
CODE_RPC_SEND_TIMEOUT_S = 2.0
# The first frame (``__hello``) must arrive within this — a bound on the WHOLE
# frame, not on each recv, so a byte-by-byte drip cannot hold the reader; a
# connection that sits silent is somebody else's, not a run's.
CODE_RPC_HELLO_TIMEOUT_S = 2.0
# Un-greeted connections (accepted, ``__hello`` not yet validated) are capped
# process-wide: past this, a connection is closed at accept, before a thread
# exists for it. The per-run cap below applies only AFTER a valid hello.
CODE_RPC_MAX_PENDING_CONNECTIONS = 8
CODE_RPC_MAX_CONNECTIONS_PER_RUN = 4
CODE_RPC_MAX_HANDLES = 256
# Student-defined Greifobjekt types registered per run (§3.10). Per-run only:
# ctx is rebuilt every start, nothing is persisted, no table (A13).
CODE_MAX_STUDENT_OBJECTS = 8
CODE_STATUS_MIN_INTERVAL_S = 0.1
# Supervisor → server stdout/stderr events are bounded per line and per
# second (the supervisor enforces them; the server sizes its buffers by them).
STDOUT_MAX_LINE_BYTES = 2000
STDOUT_MAX_LINES_PER_S = 20
# How long close_run() waits for the worker after draining it.
CODE_RPC_WORKER_JOIN_S = 1.0

MAX_FRAME_BYTES = RPC_LIMITS.MAX_FRAME_BYTES
CONTROL_MAX_FRAME_BYTES = RPC_LIMITS.CONTROL_MAX_FRAME_BYTES

# The data socket is created by this process (root in its container) and
# must be connectable by the student uid; stale files are unlinked first.
_DATA_SOCKET_MODE = 0o666
_LISTEN_BACKLOG = 16
_ACCEPT_POLL_S = 0.25
_JOB_POLL_S = 0.25
_MAX_LINE_NUMBER = 1_000_000
_MAX_NAME_CHARS = 64
_EXIT_INFO_KEYS = {
    'kind': (str, 32), 'file': (str, 80), 'line': (int, None),
    'exc_type': (str, 80), 'name': (str, 80), 'detail_line': (str, STDOUT_MAX_LINE_BYTES),
}
# The three characters the [VAR:] frame cannot carry in a name (the
# interpreter's _UNSHOWABLE_NAME_CHARS, re-spelled here rather than imported
# from a class attribute).
_UNSHOWABLE_NAME_CHARS = '=[]'
# A shown value (the `value` kind of `zeige`, every entry of `__vars`) is a
# JSON tree bounded in depth and size before it is rendered — walked
# iteratively, so a hostile nesting is a refusal, never a RecursionError. The
# stubs render at most 3 levels, so a real program never meets either bound.
SHOWN_VALUE_MAX_DEPTH = 8
SHOWN_VALUE_MAX_NODES = 5000
# SHOWN_VAR_NAMES_MAX (robot_api): the per-run map of the last payload shown
# per name is bounded; past it a NEW name is still shown by `__vars`, just not
# remembered (so it re-emits every change) — and `zeige` keeps at most that
# many names waiting.
# Flood limits (2026-09-27 review rounds). Every frame is still charged
# against the call budget BEFORE any of this (Rule §2, condition 1); these
# bound what a charged frame may cost after that.
# - `__vars`: the runner sends at most one frame per LIVE_VALUES_INTERVAL_S;
#   one per VARS_MIN_INTERVAL_S (robot_api) is looked at, any other is
#   answered VARS_REPLY_SKIPPED at once.
# - A frame's shown values in TOTAL (every value of a `__vars` / `__paused`
#   dict, or one `zeige` value): SHOWN_FRAME_MAX_NODES / _CHARS (robot_api,
#   derived from the runner's own bounds); fit_shown_values trims to them.
# - `[VAR:]` sentinels from `zeige`, per run: a token bucket; what does not
#   fit waits (latest value per name) for the next call, the worker's idle
#   tick or the end of the run.
SHOWN_EMITS_PER_S = 20
SHOWN_EMITS_BURST = 20
SHOWN_FLUSH_TICK_S = 0.1

# ── German replies ────────────────────────────────────────────────────────
INTERNAL_ERROR_DE = 'Interner Fehler — bitte den Lehrer rufen.'
UNKNOWN_HANDLE_DE = 'Dieses Greifziel gibt es nicht mehr — bitte zuerst „finde“ aufrufen.'
FRAME_TOO_BIG_DE = 'Die Nachricht an den Roboter ist zu groß.'
BAD_FRAME_DE = 'Die Nachricht an den Roboter ist unverständlich.'
BAD_REQUEST_DE = 'Der Aufruf ist unvollständig — Methode und Argumente fehlen.'
RUN_STOPPED_DE = 'Programm wurde gestoppt.'
# `zeige` with more names waiting than SHOWN_VAR_NAMES_MAX: said once per run.
ZEIGE_TOO_MANY_NAMES_DE = ('[WARNUNG] robot.zeige: zu viele verschiedene Namen auf '
                           'einmal — nur {n} werden angezeigt, weitere nicht.')
# A student redefines a type under the same name with different values.
REGISTER_OBJECT_REDEFINED_DE = ('Objekt „{name}“ ist in diesem Programm bereits '
                                'anders definiert.')
REGISTER_OBJECT_TOO_MANY_DE = ('Zu viele eigene Objekte in einem Programm '
                               '(höchstens {n}).')
_UNKNOWN_METHOD_DE = 'robot.{name} gibt es nicht.'
_UNKNOWN_METHOD_SUGGEST_DE = 'robot.{name} gibt es nicht. Meintest du robot.{suggestion}?'
_ARITY_DE = 'robot.{name} erwartet {expected} Angabe(n), erhält aber {got}.'
_VALUE_FRAME = 'robot.{name}: {reason}'
_KIND_NUMBER_DE = '„{p}“ muss eine Zahl sein.'
_KIND_INTEGER_DE = '„{p}“ muss eine ganze Zahl sein.'
_KIND_TEXT_DE = '„{p}“ muss ein Text sein.'
_KIND_TEXT_OR_NUMBER_DE = '„{p}“ muss ein Text oder eine Zahl sein.'
_KIND_BOOL_DE = '„{p}“ muss True oder False sein.'
_KIND_POINT_DE = ('„{p}“ muss ein Punkt [x, y, z] in Metern sein, jeder Wert '
                  'höchstens 1 m vom Roboter entfernt.')
_KIND_TARGET_DE = ('„{p}“ muss ein Punkt [x, y, z] in Metern oder der Name eines '
                   'gemerkten Ziels sein.')
_KIND_ZIEL_DE = '„{p}“ muss ein Greifziel von robot.find sein.'
_KIND_TAGIDS_DE = '„{p}“ muss eine Liste mit 1 bis 16 Marker-IDs (0 bis 586) sein.'
_KIND_OBJ_DE = ('„{p}“ muss ein Objekt-Name aus Buchstaben, Ziffern und Unterstrichen '
                'sein (höchstens 24 Zeichen).')
_KIND_DICT_DE = '„{p}“ muss eine Zuordnung mit höchstens {n} Einträgen sein.'
_KIND_VALUE_DE = '„{p}“ ist zu tief verschachtelt oder zu groß zum Anzeigen.'
_RANGE_DE = '„{p}“ muss zwischen {lo} und {hi} liegen.'
_RANGE_OPEN_DE = '„{p}“ muss größer als {lo} und höchstens {hi} sein.'
_TOO_LONG_DE = '„{p}“ ist zu lang (höchstens {n} Zeichen).'
_BAD_CHARS_DE = '„{p}“ enthält ein Zeichen, das hier nicht erlaubt ist.'
_EMPTY_DE = '„{p}“ darf nicht leer sein.'

_TAG_ID_MAX = 586          # tag36h11's id space
_TAG_IDS_MAX = 16
_POINT_ABS_MAX_M = 1.0
_TARGET_NAME_RE = re.compile(robot_api.DESTINATION_NAME_RE)
_TARGET_NAME_MAX = 40
_PATH_RE = re.compile(CODE_PATH_RE)
_pattern_cache: dict[str, re.Pattern] = {}


# ══════════════════════════════════════════════════════════════════════════
# Framing
# ══════════════════════════════════════════════════════════════════════════

class FrameError(Exception):
    """A frame that cannot be read: malformed, or abandoned by the caller."""


class FrameTooLarge(FrameError):
    """``length`` exceeded the bound of this endpoint."""

    def __init__(self, length: int, max_bytes: int) -> None:
        super().__init__(f'frame of {length} bytes exceeds {max_bytes}')
        self.length = length
        self.max_bytes = max_bytes


class ReadAbandoned(FrameError):
    """``should_continue()`` answered False while waiting for bytes."""


def encode_frame_body(obj: Any) -> bytes:
    """The wire encoding — umlauts raw, no whitespace — as bytes."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _recv_exact(sock: socket.socket, n: int, timeout_s: float,
                should_continue: Callable[[], bool] | None,
                deadline: float | None) -> bytes | None:
    """Exactly ``n`` bytes into a fresh buffer; ``None`` on a clean EOF before
    the first byte. A timeout asks ``should_continue`` and resumes the same
    buffer, so a slow sender never loses a partial frame; with no callback the
    ``socket.timeout`` propagates. ``deadline`` (a ``time.monotonic()`` value)
    bounds the whole read: each recv waits at most what is left of it, and
    once nothing is left ``socket.timeout`` is raised whatever the callback
    says — a byte-by-byte drip cannot hold the reader."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        wait = timeout_s
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0.0:
                raise socket.timeout('frame deadline passed')
            wait = min(timeout_s, left)
        sock.settimeout(wait)
        try:
            k = sock.recv_into(view[got:], n - got)
        except socket.timeout:
            if should_continue is None:
                raise
            if not should_continue():
                raise ReadAbandoned('reader asked to stop') from None
            continue
        if k == 0:
            if got == 0:
                return None
            raise FrameError('connection closed mid-frame')
        got += k
    return bytes(buf)


def read_frame(sock: socket.socket, max_bytes: int, *,
               timeout_s: float = CODE_RPC_RECV_TIMEOUT_S,
               should_continue: Callable[[], bool] | None = None,
               deadline_s: float | None = None) -> dict | None:
    """One framed JSON object, or ``None`` on a clean EOF.

    Raises :class:`FrameTooLarge` when the announced length exceeds
    ``max_bytes`` (nothing of the body is read), :class:`FrameError` on a body
    that is not a JSON object, :class:`ReadAbandoned` when ``should_continue``
    says so, ``socket.timeout`` when there is no callback or when
    ``deadline_s`` — a bound on the WHOLE frame, header and body, measured
    from this call — has elapsed."""
    deadline = None if deadline_s is None else time.monotonic() + deadline_s
    header = _recv_exact(sock, 4, timeout_s, should_continue, deadline)
    if header is None:
        return None
    (length,) = struct.unpack('>I', header)
    if length > max_bytes:
        raise FrameTooLarge(length, max_bytes)
    body = _recv_exact(sock, length, timeout_s, should_continue, deadline) if length else b''
    if body is None:
        raise FrameError('connection closed after the header')
    try:
        obj = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError(f'malformed frame body: {exc}') from None
    if not isinstance(obj, dict):
        raise FrameError('frame body is not a JSON object')
    return obj


def write_frame(sock: socket.socket, obj: Any, max_bytes: int, *,
                timeout_s: float = CODE_RPC_SEND_TIMEOUT_S) -> None:
    """Frame and send ``obj``; :class:`FrameTooLarge` when it exceeds
    ``max_bytes`` (nothing is sent); ``socket.timeout`` / ``OSError`` when the
    peer does not drain within ``timeout_s``."""
    data = encode_frame_body(obj)
    if len(data) > max_bytes:
        raise FrameTooLarge(len(data), max_bytes)
    sock.settimeout(timeout_s)
    sock.sendall(struct.pack('>I', len(data)) + data)


# ══════════════════════════════════════════════════════════════════════════
# Validation (from the table)
# ══════════════════════════════════════════════════════════════════════════

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _range_error(p: ApiParam, v: float) -> str | None:
    lo, hi = p.lo, p.hi
    if lo is not None and (v < lo or (p.lo_open and v <= lo)):
        return (_RANGE_OPEN_DE if p.lo_open else _RANGE_DE).format(p=p.name, lo=lo, hi=hi)
    if hi is not None and v > hi:
        return (_RANGE_OPEN_DE if p.lo_open else _RANGE_DE).format(p=p.name, lo=lo, hi=hi)
    return None


def _text_error(p: ApiParam, v: str, *, pattern: str | None, max_len: int | None,
                allow_empty: bool = False) -> str | None:
    if not v and not allow_empty:
        return _EMPTY_DE.format(p=p.name)
    if max_len is not None and len(v) > max_len:
        return _TOO_LONG_DE.format(p=p.name, n=max_len)
    if any(ord(c) < 0x20 or c == '\x7f' for c in v):
        return _BAD_CHARS_DE.format(p=p.name)
    if pattern is not None:
        rx = _pattern_cache.get(pattern)
        if rx is None:
            rx = _pattern_cache[pattern] = re.compile(pattern)
        if not rx.fullmatch(v):
            return _BAD_CHARS_DE.format(p=p.name)
    return None


def _point_error(p: ApiParam, v: Any, sentence: str) -> str | None:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        return sentence.format(p=p.name)
    for c in v:
        if not _is_number(c) or abs(c) > _POINT_ABS_MAX_M:
            return sentence.format(p=p.name)
    return None


def shown_value_is_bad(v: Any) -> bool:
    """True when ``v`` is not a JSON tree within ``SHOWN_VALUE_MAX_DEPTH`` /
    ``SHOWN_VALUE_MAX_NODES`` (an explicit stack: no recursion)."""
    stack = [(v, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > SHOWN_VALUE_MAX_NODES:
            return True
        if isinstance(item, (list, tuple)):
            if depth >= SHOWN_VALUE_MAX_DEPTH:
                return True
            stack.extend((x, depth + 1) for x in item)
        elif isinstance(item, dict):
            if depth >= SHOWN_VALUE_MAX_DEPTH:
                return True
            stack.extend((x, depth + 1) for x in item.values())
        elif not (item is None or isinstance(item, (bool, int, float, str))):
            return True
    return False


def _shown_cost(value: Any, max_nodes: int, max_chars: int) -> tuple[int, int]:
    """``(nodes, chars)`` of one JSON tree — counted like
    :func:`shown_values_exceed` and iteratively, stopping once either count
    passes its bound (the caller only needs to know it is too big)."""
    stack = [value]
    nodes = 0
    chars = 0
    while stack and nodes <= max_nodes and chars <= max_chars:
        item = stack.pop()
        nodes += 1
        if isinstance(item, str):
            chars += len(item)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, dict):
            for k, v in item.items():
                if isinstance(k, str):
                    chars += len(k)
                stack.append(v)
    return nodes, chars


def fit_shown_values(items: list, max_nodes: int = SHOWN_FRAME_MAX_NODES,
                     max_chars: int = SHOWN_FRAME_MAX_CHARS) -> list:
    """``[(name, value)]`` whose values TOGETHER fit the frame caps: the
    largest values are replaced by ``SHOWN_TOO_BIG`` until they do, so every
    name still shows (a frame is never dropped whole — review round 2, mi4).
    Each value is measured once, bounded by the caps: linear in the frame."""
    costs = [_shown_cost(v, max_nodes, max_chars) for _n, v in items]
    total_nodes = sum(n for n, _c in costs)
    total_chars = sum(c for _n, c in costs)
    if total_nodes <= max_nodes and total_chars <= max_chars:
        return list(items)
    out = list(items)
    order = sorted(range(len(out)), reverse=True,
                   key=lambda i: max(costs[i][0] / max_nodes, costs[i][1] / max_chars))
    for i in order:
        if total_nodes <= max_nodes and total_chars <= max_chars:
            break
        total_nodes += 1 - costs[i][0]
        total_chars += len(SHOWN_TOO_BIG) - costs[i][1]
        out[i] = (out[i][0], SHOWN_TOO_BIG)
    return out


def shown_values_exceed(values: Any, max_nodes: int, max_chars: int) -> bool:
    """True when the JSON trees in ``values`` together visit more than
    ``max_nodes`` nodes or hold more than ``max_chars`` string characters
    (dict keys included). Iterative, and it stops at the first bound it
    crosses — the cheap gate in front of any rendering."""
    stack = list(values)
    nodes = 0
    chars = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            return True
        if isinstance(item, str):
            chars += len(item)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, dict):
            for k, v in item.items():
                if isinstance(k, str):
                    chars += len(k)
                stack.append(v)
        if chars > max_chars:
            return True
    return False


class _RateGate:
    """A token bucket that never sleeps: ``try_take`` answers now."""

    def __init__(self, rate_per_s: float, burst: float) -> None:
        self._rate = float(rate_per_s)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()

    def try_take(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


def validate_value(p: ApiParam, v: Any) -> str | None:
    """The German reason ``v`` is not a valid ``p``, or ``None`` when it is."""
    if v is None and p.nullable:
        return None
    kind = p.kind
    if kind == 'float':
        if not _is_number(v):
            return _KIND_NUMBER_DE.format(p=p.name)
        return _range_error(p, float(v))
    if kind == 'int':
        if isinstance(v, bool) or not _is_number(v) or float(v) != int(v):
            return _KIND_INTEGER_DE.format(p=p.name)
        return _range_error(p, int(v))
    if kind == 'str':
        if not isinstance(v, str):
            return _KIND_TEXT_DE.format(p=p.name)
        return _text_error(p, v, pattern=p.pattern, max_len=p.max_len)
    if kind == 'text':
        if isinstance(v, str):
            return _text_error(p, v, pattern=None, max_len=p.max_len, allow_empty=True)
        if isinstance(v, bool) or _is_number(v):
            return None
        return _KIND_TEXT_OR_NUMBER_DE.format(p=p.name)
    if kind == 'bool':
        return None if isinstance(v, bool) else _KIND_BOOL_DE.format(p=p.name)
    if kind == 'point':
        return _point_error(p, v, _KIND_POINT_DE)
    if kind == 'target':
        if isinstance(v, str):
            err = _text_error(p, v, pattern=robot_api.DESTINATION_NAME_RE,
                              max_len=_TARGET_NAME_MAX)
            return _KIND_TARGET_DE.format(p=p.name) if err else None
        return _point_error(p, v, _KIND_TARGET_DE)
    if kind == 'ziel':
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return _KIND_ZIEL_DE.format(p=p.name)
        return None
    if kind == 'tagids':
        if (not isinstance(v, list) or not 1 <= len(v) <= _TAG_IDS_MAX
                or any(isinstance(t, bool) or not isinstance(t, int)
                       or not 0 <= t <= _TAG_ID_MAX for t in v)):
            return _KIND_TAGIDS_DE.format(p=p.name)
        return None
    if kind == 'obj':
        if not isinstance(v, str):
            return _KIND_OBJ_DE.format(p=p.name)
        err = _text_error(p, v, pattern=p.pattern or robot_api.TYPE_NAME_RE,
                          max_len=p.max_len or 24)
        return _KIND_OBJ_DE.format(p=p.name) if err else None
    if kind == 'dict':
        cap = p.max_len if p.max_len is not None else 0
        if (not isinstance(v, dict) or len(v) > cap
                or any(not isinstance(k, str) for k in v)):
            return _KIND_DICT_DE.format(p=p.name, n=cap)
        return None
    if kind == 'value':
        return _KIND_VALUE_DE.format(p=p.name) if shown_value_is_bad(v) else None
    return INTERNAL_ERROR_DE


def _validate_call(call: ApiCall, args: list) -> tuple[str, str] | None:
    """``(kind, sentence)`` of the first refusal, or ``None``."""
    if len(args) != len(call.params):
        return 'arity', _ARITY_DE.format(name=call.name, expected=len(call.params),
                                         got=len(args))
    for p, v in zip(call.params, args):
        reason = validate_value(p, v)
        if reason is not None:
            return 'value', _VALUE_FRAME.format(name=call.name, reason=reason)
    return None


def _lookup(method: str) -> ApiCall | None:
    return (ROBOT_API_BY_NAME.get(method) or INTERNAL_METHODS_BY_NAME.get(method)
            or CODE_ONLY_METHODS_BY_NAME.get(method))


def _unknown_method_de(name: str) -> str:
    suggestion = robot_api.suggest(name)
    if suggestion:
        return _UNKNOWN_METHOD_SUGGEST_DE.format(name=name, suggestion=suggestion)
    return _UNKNOWN_METHOD_DE.format(name=name)


def _request_id(frame: dict) -> int | None:
    rid = frame.get('id')
    return rid if isinstance(rid, int) and not isinstance(rid, bool) else None


def _position_of(frame: dict) -> tuple[str | None, int | None]:
    """The caller's ``f``/``l``, only when both are well-formed."""
    f, l = frame.get('f'), frame.get('l')
    if (isinstance(f, str) and _PATH_RE.fullmatch(f)
            and isinstance(l, int) and not isinstance(l, bool)
            and 0 <= l <= _MAX_LINE_NUMBER):
        return f, l
    return None, None


def _ok(rid: int | None, result: Any) -> dict:
    return {'id': rid, 'ok': True, 'r': result}


def _err(rid: int | None, kind: str, sentence: str) -> dict:
    return {'id': rid, 'ok': False, 'k': kind, 'e': sentence}


def default_handlers() -> dict[str, Callable]:
    """block type → handler, the real dispatch tables merged."""
    table: dict[str, Callable] = dict(STATEMENT_HANDLERS)
    table.update(VALUE_EVALUATORS)
    return table


# ══════════════════════════════════════════════════════════════════════════
# Token bucket
# ══════════════════════════════════════════════════════════════════════════

class _SleepingBucket:
    """Sleeps until a token is available; never drops."""

    def __init__(self, rate_per_s: float, burst: float) -> None:
        self._rate = float(rate_per_s)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, stop: threading.Event) -> bool:
        """True once a token was taken; False when ``stop`` was set first."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self._rate
            if stop.wait(min(wait, _JOB_POLL_S)):
                return False


# ══════════════════════════════════════════════════════════════════════════
# Run session — one per workflow run, one dispatch worker
# ══════════════════════════════════════════════════════════════════════════

class _Job:
    __slots__ = ('call', 'args', 'rid', 'file', 'line', 'done', 'reply')

    def __init__(self, call: ApiCall, args: list, rid: int | None,
                 file: str | None, line: int | None) -> None:
        self.call = call
        self.args = args
        self.rid = rid
        self.file = file
        self.line = line
        self.done = threading.Event()
        self.reply: dict | None = None


class RunSession:
    """The server-side state of one run: its token, its context, its
    connections, its buckets, its handle table and its dispatch worker."""

    def __init__(self, server: 'CodeRpcServer', ctx: Any,
                 on_block_change: Callable[[str, str, float], None] | None) -> None:
        self._server = server
        self.ctx = ctx
        self.token = secrets.token_hex(16)
        self.on_block_change = on_block_change or (lambda _id, _phase, _progress: None)
        self.closed = threading.Event()
        self.frames_decoded = 0
        self.exit_info: dict | None = None
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._queue: queue.Queue = queue.Queue()
        self._calls = _SleepingBucket(RPC_LIMITS.MAX_CALLS_PER_S, RPC_LIMITS.BURST)
        self._perception = _SleepingBucket(RPC_LIMITS.PERCEPTION_MAX_PER_S,
                                           RPC_LIMITS.PERCEPTION_BURST)
        handles = getattr(ctx, 'rpc_handles', None)
        if not isinstance(handles, OrderedDict):
            handles = OrderedDict()
            try:
                ctx.rpc_handles = handles
            except Exception:  # noqa: BLE001 — a stub ctx without the field
                pass
        self._handles: OrderedDict = handles
        self._next_handle = 0
        # name -> the last [VAR:] payload this run showed (zeige, __vars, a
        # breakpoint's locals). Its own lock: the reader (__vars, __paused)
        # and the worker (zeige) both write it.
        self._var_lock = threading.Lock()
        self._var_payloads: dict[str, str] = {}
        # zeige's coalescing: name -> the latest payload not yet emitted, and
        # the per-run emit budget (both under _var_lock).
        self._shown_pending: dict[str, str] = {}
        self._shown_gate = _RateGate(SHOWN_EMITS_PER_S, SHOWN_EMITS_BURST)
        # The names zeige has shown (bounded): `__vars` leaves them alone.
        self._zeige_names: set[str] = set()
        self._zeige_overflow_said = False
        # __vars' floor (reader thread only).
        self._vars_last_accept = float('-inf')
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name=f'code-rpc-worker-{self.token[:8]}')
        self._worker.start()

    # ── connections ──────────────────────────────────────────────────────
    def attach(self, conn: socket.socket) -> bool:
        with self._lock:
            if self.closed.is_set() or len(self._conns) >= CODE_RPC_MAX_CONNECTIONS_PER_RUN:
                return False
            self._conns.append(conn)
            return True

    def detach(self, conn: socket.socket) -> None:
        with self._lock:
            if conn in self._conns:
                self._conns.remove(conn)

    def worker_alive(self) -> bool:
        return self._worker.is_alive()

    def close(self) -> None:
        """Retire the token, drop every connection, drain and join the worker."""
        self.closed.set()
        self._server._forget(self)
        with self._lock:
            conns, self._conns = list(self._conns), []
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self._queue.put(None)
        self._worker.join(CODE_RPC_WORKER_JOIN_S)
        # Whatever zeige still holds is the last value of those names: it
        # arrives before the run's final status, never dropped.
        self._flush_shown(force=True)

    # ── the reader's half: validate, budget, hand to the worker ──────────
    def charge_call(self) -> bool:
        return self._calls.take(self.closed)

    def handle(self, frame: dict) -> dict | None:
        """One request → its reply, or ``None`` when the run closed meanwhile."""
        rid = _request_id(frame)
        method, args = frame.get('m'), frame.get('a')
        if not isinstance(method, str) or not isinstance(args, list):
            return _err(rid, 'protocol', BAD_REQUEST_DE)
        # Already charged (the reader charges every decoded frame first). A
        # `__vars` inside the floor is answered at once: nothing validated,
        # rendered or emitted — and the runner, told so, sends again.
        if method == '__vars' and not self._vars_floor_ok():
            return _ok(rid, VARS_REPLY_SKIPPED)
        call = _lookup(method)
        if call is None:
            return _err(rid, 'method', _unknown_method_de(method))
        refusal = _validate_call(call, args)
        if refusal is not None:
            return _err(rid, *refusal)
        file, line = _position_of(frame)
        if call.budget == 'perception' and not self._perception.take(self.closed):
            return None
        if call.table == 'internal':
            return self._internal(call, args, rid)
        job = _Job(call, args, rid, file, line)
        self._queue.put(job)
        while not job.done.wait(_JOB_POLL_S):
            if self.closed.is_set():
                return None
        return job.reply

    # ── the __ methods (reader thread) ───────────────────────────────────
    def _internal(self, call: ApiCall, args: list, rid: int | None) -> dict:
        name = call.name
        if name == '__hello':
            return _ok(rid, None)
        if name == '__line':
            self._emit_running(args[0], args[1])
            return _ok(rid, None)
        if name == '__vars':
            self._emit_changed_vars(args[2])
            return _ok(rid, None)
        if name == '__exit':
            self.exit_info = self._clean_exit_info(args[0])
            return _ok(rid, None)
        if name == '__paused':
            return _ok(rid, self._paused(args[0], args[1], args[2]))
        if name == 'register_object':
            return self._register_object(args, rid)
        return _err(rid, 'internal', INTERNAL_ERROR_DE)

    @staticmethod
    def _clean_exit_info(info: dict) -> dict:
        out: dict[str, Any] = {}
        for key, (typ, cap) in _EXIT_INFO_KEYS.items():
            value = info.get(key)
            if typ is int:
                if isinstance(value, int) and not isinstance(value, bool):
                    out[key] = value
            elif isinstance(value, str):
                out[key] = value[:cap]
        return out

    def _emit_running(self, file: str | None, line: int | None) -> None:
        """``on_block_change("<file>:L<line>", 'running', 0.0)`` at most every
        ``CODE_STATUS_MIN_INTERVAL_S`` (B13: never per line)."""
        if file is None or line is None:
            return
        now = time.monotonic()
        with self._lock:
            last = float(getattr(self.ctx, 'code_status_last_emit', 0.0) or 0.0)
            if now - last < CODE_STATUS_MIN_INTERVAL_S:
                return
            try:
                self.ctx.code_status_last_emit = now
            except Exception:  # noqa: BLE001 — a stub ctx without the field
                pass
        try:
            self.on_block_change(f'{file}:L{line}', 'running', 0.0)
        except Exception:  # noqa: BLE001 — status is observability, never control
            _log.debug('on_block_change raised', exc_info=True)

    def _paused(self, file: str, line: int, local_vars: dict) -> str:
        """A breakpoint hit: mirror ``Interpreter._pause_for_breakpoint`` —
        emit the paused status and the locals, set the pause, wait for the
        manager, and tell the hook what the student pressed."""
        ctx = self.ctx
        block_id = f'{file}:L{line}'
        try:
            self.on_block_change(block_id, 'paused', 0.0)
            ctx.log(f'⏸ Haltepunkt erreicht: {block_id}')
        except Exception:  # noqa: BLE001
            _log.debug('paused status raised', exc_info=True)
        self._emit_locals(local_vars)
        set_paused = getattr(ctx, 'set_paused', None)
        if callable(set_paused):
            set_paused(True)
        wait = getattr(ctx, 'wait_for_resume', None)
        if callable(wait):
            wait()
        elif callable(set_paused):
            set_paused(False)
        if ctx.should_stop():
            return 'stop'
        is_paused = getattr(ctx, 'is_paused', None)
        return 'step' if callable(is_paused) and is_paused() else 'continue'

    def _vars_floor_ok(self) -> bool:
        """True for a `__vars` frame at least ``VARS_MIN_INTERVAL_S`` after the
        last one this run looked at (reader thread; a plain timestamp)."""
        now = time.monotonic()
        if now - self._vars_last_accept < VARS_MIN_INTERVAL_S:
            return False
        self._vars_last_accept = now
        return True

    def _emit_locals(self, local_vars: dict) -> None:
        """A breakpoint's locals: every one shown (and remembered) — a value
        the frame has no room for as ``SHOWN_TOO_BIG``."""
        items = list(local_vars.items())[:robot_api.PAUSED_MAX_LOCALS]
        for name, value in fit_shown_values(items):
            self._emit_var(name, value, force=True)

    def _emit_changed_vars(self, local_vars: dict) -> None:
        """``__vars``: only the names whose rendered value changed and that
        ``zeige`` has not claimed; a value the frame has no room for as
        ``SHOWN_TOO_BIG``."""
        items = list(local_vars.items())[:robot_api.PAUSED_MAX_LOCALS]
        with self._var_lock:
            claimed = set(self._zeige_names)
        items = [(n, v) for n, v in items if n not in claimed]
        for name, value in fit_shown_values(items):
            self._emit_var(name, value, force=False)

    @staticmethod
    def _render_shown(name: Any, value: Any) -> str | None:
        """The ``[VAR:]`` payload of ``name``/``value``, rendered and capped
        exactly like ``interpreter._set_variable``'s, or ``None`` for an
        unshowable name (empty, only whitespace, over 64 characters, a
        control character or the frame's own ``=[]``) or an out-of-bounds
        value (``shown_value_is_bad``: depth and nodes)."""
        if (not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME_CHARS
                or any(c in name for c in _UNSHOWABLE_NAME_CHARS)
                or any(ord(c) < 0x20 or c == '\x7f' for c in name)):
            return None
        if shown_value_is_bad(value):
            return None
        try:
            payload = json.dumps(_jsonable(value, _MAX_VAR_PAYLOAD_ITEMS))
        except Exception:  # noqa: BLE001 — observability never breaks a run
            return None
        if len(payload) > _MAX_VAR_PAYLOAD_CHARS:
            payload = payload[:_MAX_VAR_PAYLOAD_CHARS] + ' …'
        return payload

    def _queue_shown(self, name: Any, value: Any) -> None:
        """``zeige``: keep the latest payload per name, then emit what the
        per-run budget allows now. A value over the frame caps is shown as
        ``SHOWN_TOO_BIG``; a NEW name while ``SHOWN_VAR_NAMES_MAX`` others
        wait is dropped, and the run's log says so once."""
        [(name, value)] = fit_shown_values([(name, value)])
        payload = self._render_shown(name, value)
        if payload is None:
            return
        overflow = False
        with self._var_lock:
            if name in self._zeige_names or len(self._zeige_names) < SHOWN_VAR_NAMES_MAX:
                self._zeige_names.add(name)
            self._shown_pending.pop(name, None)
            if len(self._shown_pending) < SHOWN_VAR_NAMES_MAX:
                self._shown_pending[name] = payload
            elif not self._zeige_overflow_said:
                self._zeige_overflow_said = overflow = True
        if overflow:
            try:
                self.ctx.log(ZEIGE_TOO_MANY_NAMES_DE.format(n=SHOWN_VAR_NAMES_MAX))
            except Exception:  # noqa: BLE001 — observability never breaks a run
                pass
        self._flush_shown()

    def _flush_shown(self, *, force: bool = False) -> None:
        """Emit pending ``zeige`` payloads, oldest first, while the budget
        lasts (``force``: all of them — the end of the run). Only
        ``ctx.log``; nothing raises out of it."""
        lines = []
        with self._var_lock:
            while self._shown_pending and (force or self._shown_gate.try_take()):
                name = next(iter(self._shown_pending))
                payload = self._shown_pending.pop(name)
                if name in self._var_payloads or len(self._var_payloads) < SHOWN_VAR_NAMES_MAX:
                    self._var_payloads[name] = payload
                lines.append(f'[VAR:{name}={payload}]')
        for line in lines:
            try:
                self.ctx.log(line)
            except Exception:  # noqa: BLE001 — observability never breaks a run
                pass

    def _emit_var(self, name: Any, value: Any, *, force: bool) -> None:
        """One ``[VAR:name=json]`` sentinel now (``__vars``, a breakpoint's
        locals): :meth:`_render_shown`'s payload, skipped when that refuses.
        ``force=False`` skips a name whose payload this run already showed.
        Only ``ctx.log`` is called — this is observability, never control —
        and nothing raises out of it."""
        payload = self._render_shown(name, value)
        if payload is None:
            return
        with self._var_lock:
            if not force and self._var_payloads.get(name) == payload:
                return
            if name in self._var_payloads or len(self._var_payloads) < SHOWN_VAR_NAMES_MAX:
                self._var_payloads[name] = payload
        try:
            self.ctx.log(f'[VAR:{name}={payload}]')
        except Exception:  # noqa: BLE001 — observability never breaks a run
            pass

    # ── the code-only rows (the worker, after the pause/stop gate) ──────────
    def _code_only(self, call: ApiCall, args: list, rid: int | None) -> dict:
        """``zeige(name, wert)``: show the value in the Variablen panel. The
        args are validated against the row already. Nothing else is touched."""
        if call.name == 'zeige':
            self._queue_shown(args[0], args[1])
            return _ok(rid, None)
        return _err(rid, 'internal', INTERNAL_ERROR_DE)

    # ── register_object (§3.10, A13) — the reader thread, perception budget ──
    def _register_object(self, args: list, rid: int | None) -> dict:
        """Register a student-defined Greifobjekt type for THIS run. args are
        already validated against the row (name/label/tag_ids/hoehe_m/greiftiefe_m
        + the two nullable trailing floats). Builds the merged catalog from the
        ctx PROFILE's own fixed set + the previously registered student types +
        the new one, re-runs ``parse_catalog`` under the profile's gripper band,
        and — only on success — rebinds ``ctx.object_catalog`` atomically and
        records the entry in ``ctx.student_objects``. Any ``ObjectCatalogError``
        (a tag-id collision with the built-in type, an out-of-band close, a
        grasp depth over the object height) is the German refusal verbatim."""
        from physical_ai_server.workflow import object_catalog as _oc
        name, label, tag_ids, hoehe_m, greiftiefe_m, close, anfahr = args
        entry: dict[str, Any] = {
            'label_de': label,
            'tag_ids': [int(t) for t in tag_ids],
            'object_height_m': float(hoehe_m),
            'grasp_depth_m': float(greiftiefe_m),
        }
        base_dict, band = self._profile_catalog()
        if close is None:
            # A missing close defaults to the profile's built-in wuerfel close
            # (OMX −0.5, edu6 1.0, edu1 0.10) — never OMX unconditionally (§3.10).
            try:
                close = base_dict['types']['wuerfel']['gripper_close_rad']
            except Exception:  # noqa: BLE001 — a profile without wuerfel
                close = -0.5
        entry['gripper_close_rad'] = float(close)
        if anfahr is not None:
            entry['approach_clear_m'] = float(anfahr)
        with self._lock:
            student = getattr(self.ctx, 'student_objects', None)
            if not isinstance(student, dict):
                student = {}
                try:
                    self.ctx.student_objects = student
                except Exception:  # noqa: BLE001 — a stub ctx without the field
                    pass
            existing = student.get(name)
            if existing is not None:
                if existing == entry:
                    return _ok(rid, None)          # identical re-definition: no-op
                return _err(rid, 'robot',
                            REGISTER_OBJECT_REDEFINED_DE.format(name=name))
            if len(student) >= CODE_MAX_STUDENT_OBJECTS:
                return _err(rid, 'robot',
                            REGISTER_OBJECT_TOO_MANY_DE.format(n=CODE_MAX_STUDENT_OBJECTS))
            # Merge: the profile's own base + every prior student type + the new
            # one. A fresh dict, so the module constant is never mutated.
            merged = {'types': dict(base_dict.get('types', {}))}
            merged['types'].update(student)
            merged['types'][name] = entry
            try:
                catalog = _oc.parse_catalog(merged, gripper_close_range=band)
            except _oc.ObjectCatalogError as exc:
                return _err(rid, 'robot', str(exc))
            # Success: rebind atomically and record (only now).
            self.ctx.object_catalog = catalog
            student[name] = entry
        return _ok(rid, None)

    def _profile_catalog(self):
        """The ctx profile's built-in catalog dict + gripper band, from
        ``_CATALOG_BY_PROFILE`` (never ``_FIXED_CATALOG`` unconditionally). The
        arm is identified the way every handler does — through
        ``motion._profile_for_ctx`` (the solver's backend) — since the ctx does
        not carry the profile id. An unresolved profile (both OMX, no solver)
        gets the OMX set with its negative-close rule (band ``None``)."""
        from physical_ai_server.workflow import object_catalog as _oc
        pid = None
        try:
            from physical_ai_server.workflow.handlers.motion import _profile_for_ctx
            prof = _profile_for_ctx(self.ctx)
            pid = getattr(prof, 'profile_id', None)
        except Exception:  # noqa: BLE001 — identity lookup never raises here
            pid = None
        entry = _oc._CATALOG_BY_PROFILE.get((pid or '').strip())
        if entry is not None:
            return entry[0], entry[1]
        return _oc._FIXED_CATALOG, None

    # ── the worker ───────────────────────────────────────────────────────
    def _worker_loop(self) -> None:
        while True:
            if self._shown_pending:
                # A coalesced zeige is waiting: tick while idle so its latest
                # value arrives without the next call.
                try:
                    job = self._queue.get(timeout=SHOWN_FLUSH_TICK_S)
                except queue.Empty:
                    self._flush_shown()
                    continue
            else:
                job = self._queue.get()
            if job is None:
                break
            try:
                self._execute(job)
            finally:
                job.done.set()

    def _execute(self, job: _Job) -> None:
        rid = job.rid
        if self.closed.is_set():
            job.reply = _err(rid, 'robot', RUN_STOPPED_DE)
            return
        try:
            self._emit_running(job.file, job.line)
            self.ctx.wait_if_paused()
            # The Blockly contract (interpreter._exec_chain polls should_stop
            # before every statement) holds here too: a call queued after
            # Stopp — including one a stop released from its pause — never
            # starts a handler in the window before close_run.
            if self.closed.is_set() or self.ctx.should_stop():
                job.reply = _err(rid, 'robot', RUN_STOPPED_DE)
                return
            if job.call.table == 'code':
                job.reply = self._code_only(job.call, job.args, rid)
                return
            kwargs: dict[str, Any] = {}
            for p, v in zip(job.call.params, job.args):
                if p.kind == 'ziel':
                    v = self._handles.get(v)
                    if v is None:
                        job.reply = _err(rid, 'handle', UNKNOWN_HANDLE_DE)
                        return
                kwargs[p.arg_key] = v
            handler = self._server.handlers.get(job.call.block_type)
            if handler is None:
                job.reply = _err(rid, 'internal', INTERNAL_ERROR_DE)
                return
            result = handler(self.ctx, kwargs)
            job.reply = _ok(rid, self._to_wire(job.call, result))
        except WorkflowError as exc:
            job.reply = _err(rid, 'robot', str(exc))
        except Exception:  # noqa: BLE001 — nothing raises out of the worker
            _log.error('robot.%s raised:\n%s', job.call.name, traceback.format_exc())
            job.reply = _err(rid, 'internal', INTERNAL_ERROR_DE)

    def _new_handle(self, obj: Any) -> int:
        h = self._next_handle
        self._next_handle += 1
        self._handles[h] = obj
        while len(self._handles) > CODE_RPC_MAX_HANDLES:
            self._handles.popitem(last=False)
        return h

    def _to_wire(self, call: ApiCall, result: Any) -> Any:
        kind = call.returns
        if kind == 'none':
            return None
        if kind == 'ziel':
            return None if result is None else {'h': self._new_handle(result)}
        if kind == 'point':
            if result is None:
                return None
            if isinstance(result, dict):
                return [float(result['x']), float(result['y']), float(result['z'])]
            return [float(result[0]), float(result[1]), float(result[2])]
        if kind == 'bool':
            return bool(result)
        if kind == 'int':
            return int(result)
        if kind == 'name':
            return str(result)
        return result


# ══════════════════════════════════════════════════════════════════════════
# The listener
# ══════════════════════════════════════════════════════════════════════════

class CodeRpcServer:
    """Binds ``rpc.sock``, accepts data connections, routes each to the run
    whose token it greets with. Constructed once per node lifetime; a run is
    opened with :meth:`open_run` and closed with :meth:`close_run`."""

    def __init__(self, socket_path: str, *,
                 handlers: dict[str, Callable] | None = None) -> None:
        self.socket_path = socket_path
        self.handlers: dict[str, Callable] = handlers if handlers is not None else default_handlers()
        self._sessions: dict[str, RunSession] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        # Connections accepted but not yet greeted (under _lock).
        self._pending = 0
        try:
            if stat.S_ISSOCK(os.lstat(socket_path).st_mode) or os.path.isfile(socket_path):
                os.unlink(socket_path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(socket_path)
        os.chmod(socket_path, _DATA_SOCKET_MODE)
        self._sock.listen(_LISTEN_BACKLOG)
        self._sock.settimeout(_ACCEPT_POLL_S)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True,
                                               name='code-rpc-accept')
        self._accept_thread.start()

    # ── runs ─────────────────────────────────────────────────────────────
    def open_run(self, ctx: Any,
                 on_block_change: Callable[[str, str, float], None] | None = None) -> RunSession:
        session = RunSession(self, ctx, on_block_change)
        with self._lock:
            self._sessions[session.token] = session
        return session

    def close_run(self, session: RunSession) -> None:
        session.close()

    def _forget(self, session: RunSession) -> None:
        with self._lock:
            self._sessions.pop(session.token, None)

    def _session_for(self, token: Any) -> RunSession | None:
        if not isinstance(token, str):
            return None
        with self._lock:
            session = self._sessions.get(token)
        if session is None or session.closed.is_set():
            return None
        return session

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close()
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        self._accept_thread.join(2 * _ACCEPT_POLL_S + 0.5)

    # ── connections ──────────────────────────────────────────────────────
    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed.is_set():
                    return
                time.sleep(_ACCEPT_POLL_S)
                continue
            with self._lock:
                admitted = self._pending < CODE_RPC_MAX_PENDING_CONNECTIONS
                if admitted:
                    self._pending += 1
            if not admitted:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True,
                             name='code-rpc-conn').start()

    def _release_pending(self) -> None:
        with self._lock:
            self._pending -= 1

    def _serve(self, conn: socket.socket) -> None:
        """The hello gate, then the run's frame loop. The connection counts as
        pending until its ``__hello`` has been validated AND attached; the
        hello read is bounded as a whole by ``CODE_RPC_HELLO_TIMEOUT_S``."""
        session: RunSession | None = None
        pending = True
        try:
            try:
                hello = read_frame(conn, MAX_FRAME_BYTES, timeout_s=CODE_RPC_HELLO_TIMEOUT_S,
                                   deadline_s=CODE_RPC_HELLO_TIMEOUT_S)
            except (FrameError, socket.timeout, OSError):
                return
            if hello is None or hello.get('m') != '__hello':
                return
            args = hello.get('a')
            call = INTERNAL_METHODS_BY_NAME['__hello']
            if not isinstance(args, list) or _validate_call(call, args) is not None:
                return
            session = self._session_for(args[0])
            if session is None or not session.attach(conn):
                return
            self._release_pending()
            pending = False
            self._write_reply(conn, _ok(_request_id(hello), None))
            self._serve_run(conn, session)
        finally:
            if pending:
                self._release_pending()
            if session is not None:
                session.detach(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _serve_run(self, conn: socket.socket, session: RunSession) -> None:
        """One frame per iteration; the reply is written before the next read."""
        while not session.closed.is_set():
            try:
                frame = read_frame(conn, MAX_FRAME_BYTES, timeout_s=CODE_RPC_RECV_TIMEOUT_S,
                                   should_continue=lambda: not session.closed.is_set())
            except FrameTooLarge:
                self._write_reply(conn, _err(None, 'protocol', FRAME_TOO_BIG_DE))
                return
            except ReadAbandoned:
                return
            except FrameError:
                self._write_reply(conn, _err(None, 'protocol', BAD_FRAME_DE))
                return
            except (socket.timeout, OSError):
                return
            if frame is None:
                return
            with session._lock:
                session.frames_decoded += 1
            if not session.charge_call():
                return
            reply = session.handle(frame)
            if reply is None or not self._write_reply(conn, reply):
                return

    @staticmethod
    def _write_reply(conn: socket.socket, reply: dict) -> bool:
        try:
            write_frame(conn, reply, MAX_FRAME_BYTES, timeout_s=CODE_RPC_SEND_TIMEOUT_S)
            return True
        except (FrameTooLarge, socket.timeout, OSError):
            return False
