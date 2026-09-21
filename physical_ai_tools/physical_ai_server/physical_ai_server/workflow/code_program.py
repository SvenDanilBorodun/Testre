#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The text-program driver: the ``WorkflowManager``-side of a Python/Java run.

``CodeProgram`` is the code-run twin of :class:`~workflow.interpreter.Interpreter`
— it implements exactly the two methods ``WorkflowManager`` calls on a program,
``split_roots()`` (``([], [])`` — a code program has no Blockly roots) and
``execute(ctx, on_block_change)``, plus ``request_stop()``. The manager routes a
``/workflow/start`` payload whose ``language`` is in :data:`CODE_LANGUAGES` here
instead of through the interpreter.

Two sockets, two roles (§3.2):

* the DATA socket ``rpc.sock`` (owned by ``physical_ai_server`` via
  :class:`~workflow.code_rpc.CodeRpcServer`) carries the student's ``robot.*``
  calls; ``CodeProgram`` opens a run on it (``open_run`` mints the per-run token
  and starts the single dispatch worker).
* the CONTROL socket ``runner.sock`` (owned by the supervisor, ``0600`` so only
  ``physical_ai_server`` reaches it) carries ``start`` / ``kill`` /
  ``breakpoints`` / ``ping``; the supervisor answers ``started`` / ``killed`` /
  ``stdout`` / ``stderr`` / ``compile_error`` / ``exited``. Every control frame
  carries an ``ev`` discriminator and rides the same ``read_frame`` /
  ``write_frame`` helpers under ``CONTROL_MAX_FRAME_BYTES``.

**Stop travels out-of-band (A7.2).** ``request_stop`` — called by
``WorkflowManager.stop()`` BEFORE its joins, and by ``execute`` itself when
``ctx.should_stop()`` flips or ``CODE_RUN_MAX_S`` passes — opens a NEW control
connection and sends ``kill``; it never rides the data socket and never depends
on the student process answering. Every wait in ``execute`` is timeout-bounded,
so ``_run`` reaches its ``finally`` within :data:`CODE_STOP_DEADLINE_S` of the
stop event whatever the student process, the supervisor or the socket does.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any, Callable

from physical_ai_server.workflow import code_errors_de
from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.code_rpc import (
    CODE_RPC_RECV_TIMEOUT_S,
    CODE_RPC_SEND_TIMEOUT_S,
    CODE_RPC_WORKER_JOIN_S,
    CONTROL_MAX_FRAME_BYTES,
    FrameError,
    FrameTooLarge,
    ReadAbandoned,
    read_frame,
    write_frame,
)
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import InterpreterError

# ── constants (§3.3) ──────────────────────────────────────────────────────
# The languages a code payload may declare; the manager routes on this and
# robot_profiles.capabilities_json advertises the same pair.
CODE_LANGUAGES = ('python', 'java')

# The bound on the control handshake: waiting for ``started`` after ``start``,
# and for the ``killed`` ack after ``kill``.
CODE_CONTROL_TIMEOUT_S = 2.0
# The join execute performs on the dispatch worker via close_run — the SAME
# number code_rpc.close_run uses, so the third term of the deadline below is the
# join that actually happens (aliased, never re-chosen).
CODE_WORKER_JOIN_S = CODE_RPC_WORKER_JOIN_S
# The hard cap on one run's wall clock.
CODE_RUN_MAX_S = 600.0
# DERIVED, never a literal (m1 of the review): _run reaches its finally within
# this of the stop event — at most one recv timeout to notice the flag, at most
# one unanswered kill, at most one worker join.
CODE_STOP_DEADLINE_S = (CODE_RPC_RECV_TIMEOUT_S + CODE_CONTROL_TIMEOUT_S
                        + CODE_WORKER_JOIN_S)
# Scheduler slack the stop test allows on top of the derived deadline.
_STOP_TEST_SLACK_S = 0.25

# Entry file per language (required at the project root).
_ENTRY_FILE = {'python': 'main.py', 'java': 'Main.java'}
_EXT_FOR = {'python': '.py', 'java': '.java'}

# Import the code-project caps from the ONE source (robot_api) so a project's
# on-wire bytes are the bytes the cap counted.
MAX_CODE_FILES = robot_api.MAX_CODE_FILES
MAX_CODE_FILE_BYTES = robot_api.MAX_CODE_FILE_BYTES
MAX_CODE_PROJECT_BYTES = robot_api.MAX_CODE_PROJECT_BYTES
_PATH_RE = re.compile(robot_api.CODE_PATH_RE)

# Reserved file stems a student may not use (they would shadow the shipped stub
# module / the reserved namespace on sys.path).
_RESERVED_STEMS = ('robot',)
_RESERVED_PREFIX = 'edubotics'

# ── German refusals for from_payload (student-facing, Rule §1) ─────────────
_NO_FILES_DE = 'Das Programm enthält keine Dateien.'
_BAD_LANGUAGE_DE = 'Die Sprache des Programms ist unbekannt.'
_TOO_MANY_FILES_DE = 'Das Programm hat zu viele Dateien (höchstens {n}).'
_FILE_TOO_BIG_DE = 'Die Datei „{path}“ ist zu groß (höchstens {kib} KiB).'
_PROJECT_TOO_BIG_DE = 'Das Programm ist insgesamt zu groß (höchstens {kib} KiB).'
_BAD_PATH_DE = 'Der Dateiname „{path}“ ist nicht erlaubt.'
_RESERVED_DE = 'Der Dateiname „{path}“ ist reserviert.'
_WRONG_EXT_DE = 'Die Datei „{path}“ passt nicht zur Sprache {language}.'
_MISSING_ENTRY_DE = 'Die Startdatei „{entry}“ fehlt.'


def language_of(workflow_json: str) -> str:
    """The declared code language of a payload, or ``''``.

    Never raises: a malformed payload answers ``''`` and the caller then takes
    the Blockly path, which reports the JSON error loudly (unchanged)."""
    try:
        data = json.loads(workflow_json)
    except (ValueError, TypeError):
        return ''
    if not isinstance(data, dict):
        return ''
    language = data.get('language')
    return language if language in CODE_LANGUAGES else ''


def _breakpoints_by_file(block_ids) -> dict[str, list[int]]:
    """Convert the manager's ``{"<file>:L<line>", …}`` breakpoint id set to the
    ``{"main.py": [12, 40]}`` shape the control ``start`` envelope carries."""
    out: dict[str, list[int]] = {}
    for bid in (block_ids or ()):
        if not isinstance(bid, str):
            continue
        m = re.match(r'^(.+):L(\d+)$', bid)
        if not m:
            continue
        out.setdefault(m.group(1), []).append(int(m.group(2)))
    return out


class CodeProgram:
    """One text-program run. Built by :meth:`from_payload` in
    ``WorkflowManager.start`` and driven by :meth:`execute` on the manager's
    daemon thread."""

    def __init__(self, language: str, files: dict[str, str], workflow_id: str,
                 code_rpc: Any, *, breakpoints: dict | None = None,
                 control_path: str | None = None) -> None:
        self.language = language
        self.files = files
        self.run_id = workflow_id
        self.breakpoints = breakpoints or {}
        self._code_rpc = code_rpc
        self._control_path = control_path or self._derive_control_path(code_rpc)
        self._session = None
        self._stop_requested = threading.Event()
        self._exited = threading.Event()
        # Registration hook so WorkflowManager._prev_run_threads_alive can see
        # the dispatch worker outlive _run; set by the manager after construction.
        self._register_thread: Callable[[threading.Thread], None] | None = None

    # ── the two methods WorkflowManager calls on a program ──────────────────
    @property
    def roots(self) -> list:
        """No Blockly roots — the manager's _diagnose_events walk is a no-op."""
        return []

    def split_roots(self):
        return [], []

    def execute(self, ctx, on_block_change) -> None:
        """Open the run, hand the project to the supervisor, and drive the
        control connection until the program exits, is stopped, or the run
        deadline passes. Raises a German :class:`WorkflowError` on a faulted
        run (``_run`` then reports phase ``error``); returns cleanly on a clean
        exit and on a stop (``_run`` reports ``finished`` / ``stopped``)."""
        if self._code_rpc is None:
            raise WorkflowError(code_errors_de.sentence('runner_down'))
        session = self._code_rpc.open_run(ctx, on_block_change)
        self._session = session
        self._register_worker(session)
        try:
            self._drive(ctx, on_block_change, session)
        finally:
            try:
                self._code_rpc.close_run(session)
            except Exception:  # noqa: BLE001 — teardown never raises
                pass

    def request_stop(self) -> None:
        """Send ``kill`` out-of-band on a NEW control connection (never the data
        socket), waiting at most ``CODE_CONTROL_TIMEOUT_S`` for the ``killed``
        ack. Idempotent and safe from any thread — the first caller does the
        work, a second returns at once."""
        if self._stop_requested.is_set():
            return
        self._stop_requested.set()
        control = self._connect_control()
        if control is None:
            return
        try:
            write_frame(control, {'ev': 'kill', 'run_id': self.run_id},
                        CONTROL_MAX_FRAME_BYTES, timeout_s=CODE_RPC_SEND_TIMEOUT_S)
            self._await_event(control, {'killed'}, CODE_CONTROL_TIMEOUT_S)
        except (FrameError, socket.timeout, OSError):
            pass
        finally:
            try:
                control.close()
            except OSError:
                pass

    # ── build (validate the project up front — before any socket) ───────────
    @classmethod
    def from_payload(cls, workflow_json: str, workflow_id: str, code_rpc: Any, *,
                     breakpoints: dict | None = None,
                     control_path: str | None = None) -> 'CodeProgram':
        """Validate ``files`` against §3.7's caps and build the program. Raises
        :class:`InterpreterError` (German) on any refusal so
        ``WorkflowManager.start``'s existing ``except InterpreterError`` reports
        it — including a runner that is not up (``code_rpc is None``)."""
        if code_rpc is None:
            raise InterpreterError(code_errors_de.sentence('runner_down'))
        try:
            data = json.loads(workflow_json)
        except (ValueError, TypeError) as e:
            raise InterpreterError(f'Workflow-JSON konnte nicht gelesen werden: {e}')
        language = data.get('language') if isinstance(data, dict) else None
        if language not in CODE_LANGUAGES:
            raise InterpreterError(_BAD_LANGUAGE_DE)
        files = data.get('files') if isinstance(data, dict) else None
        if not isinstance(files, dict) or not files:
            raise InterpreterError(_NO_FILES_DE)
        if len(files) > MAX_CODE_FILES:
            raise InterpreterError(_TOO_MANY_FILES_DE.format(n=MAX_CODE_FILES))
        ext = _EXT_FOR[language]
        entry = _ENTRY_FILE[language]
        clean: dict[str, str] = {}
        for path, content in files.items():
            if not isinstance(path, str) or not isinstance(content, str):
                raise InterpreterError(_BAD_PATH_DE.format(path=path))
            if not _PATH_RE.fullmatch(path):
                raise InterpreterError(_BAD_PATH_DE.format(path=path))
            stem = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
            if stem in _RESERVED_STEMS or stem.lower().startswith(_RESERVED_PREFIX):
                raise InterpreterError(_RESERVED_DE.format(path=path))
            if not path.endswith(ext):
                raise InterpreterError(_WRONG_EXT_DE.format(path=path, language=language))
            if len(content.encode('utf-8')) > MAX_CODE_FILE_BYTES:
                raise InterpreterError(_FILE_TOO_BIG_DE.format(
                    path=path, kib=MAX_CODE_FILE_BYTES // 1024))
            clean[path] = content
        if entry not in clean:
            raise InterpreterError(_MISSING_ENTRY_DE.format(entry=entry))
        project_bytes = len(json.dumps(clean, ensure_ascii=False).encode('utf-8'))
        if project_bytes > MAX_CODE_PROJECT_BYTES:
            raise InterpreterError(_PROJECT_TOO_BIG_DE.format(
                kib=MAX_CODE_PROJECT_BYTES // 1024))
        return cls(language, clean, workflow_id, code_rpc,
                   breakpoints=breakpoints, control_path=control_path)

    # ── internals ───────────────────────────────────────────────────────────
    @staticmethod
    def _derive_control_path(code_rpc: Any) -> str | None:
        path = getattr(code_rpc, 'socket_path', None)
        if not isinstance(path, str):
            return None
        import os
        return os.path.join(os.path.dirname(path), 'runner.sock')

    def _register_worker(self, session) -> None:
        worker = getattr(session, '_worker', None)
        if isinstance(worker, threading.Thread) and self._register_thread is not None:
            try:
                self._register_thread(worker)
            except Exception:  # noqa: BLE001 — registration never breaks a run
                pass

    def _connect_control(self) -> socket.socket | None:
        if not self._control_path:
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(CODE_CONTROL_TIMEOUT_S)
            sock.connect(self._control_path)
            return sock
        except OSError:
            return None

    def _await_event(self, control: socket.socket, names: set[str],
                     timeout_s: float) -> dict | None:
        """Read control frames until one whose ``ev`` is in ``names`` arrives,
        or the timeout passes. Bounded as a whole by ``timeout_s``."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                frame = read_frame(control, CONTROL_MAX_FRAME_BYTES,
                                   timeout_s=CODE_RPC_RECV_TIMEOUT_S)
            except socket.timeout:
                continue
            except (FrameError, OSError):
                return None
            if frame is None:
                return None
            if frame.get('ev') in names:
                return frame
        return None

    def _drive(self, ctx, on_block_change, session) -> None:
        control = self._connect_control()
        if control is None:
            raise WorkflowError(code_errors_de.sentence('runner_down'))
        try:
            write_frame(control, {
                'ev': 'start',
                'run_id': self.run_id,
                'token': session.token,
                'language': self.language,
                'files': self.files,
                'breakpoints': self.breakpoints,
            }, CONTROL_MAX_FRAME_BYTES, timeout_s=CODE_RPC_SEND_TIMEOUT_S)
            started = self._await_event(control, {'started', 'busy'},
                                        CODE_CONTROL_TIMEOUT_S)
            if started is None:
                raise WorkflowError(code_errors_de.sentence('runner_down'))
            if started.get('ev') == 'busy':
                raise WorkflowError(code_errors_de.sentence('busy'))
            outcome, exited, compile_err = self._event_loop(
                control, ctx, on_block_change)
        finally:
            try:
                control.close()
            except OSError:
                pass
        if outcome == 'stopped':
            self.request_stop()          # kill out-of-band; idempotent
            return
        if outcome == 'deadline':
            self.request_stop()
            self._raise_error(ctx, on_block_change, session, {'kind': 'timeout'})
        if compile_err is not None:
            self._raise_error(ctx, on_block_change, session, {
                'kind': 'compile',
                'file': compile_err.get('file'),
                'line': compile_err.get('line'),
                'detail_line': compile_err.get('text'),
            })
        info = getattr(session, 'exit_info', None) or {}
        kind = info.get('kind')
        if kind in code_errors_de.ERROR_KINDS:
            self._raise_error(ctx, on_block_change, session, info)
        code = exited.get('code') if isinstance(exited, dict) else 0
        if isinstance(code, int) and code != 0:
            self._raise_error(ctx, on_block_change, session,
                              {'kind': 'other', 'exc_type': f'Code {code}'})
        # A clean exit — _run reports 'finished'.

    def _event_loop(self, control, ctx, on_block_change):
        """Handle control events until ``exited`` / ``compile_error`` / stop /
        deadline. Returns ``(outcome, exited_frame, compile_error_frame)``.
        Every read is bounded; ``ctx.should_stop()`` and the deadline are polled
        between recv timeouts through ``should_continue``."""
        deadline = time.monotonic() + CODE_RUN_MAX_S
        exited = None
        compile_err = None
        while True:
            try:
                frame = read_frame(
                    control, CONTROL_MAX_FRAME_BYTES,
                    timeout_s=CODE_RPC_RECV_TIMEOUT_S,
                    should_continue=lambda: (not ctx.should_stop()
                                             and time.monotonic() < deadline))
            except ReadAbandoned:
                return ('stopped' if ctx.should_stop() else 'deadline'), exited, compile_err
            except (FrameError, socket.timeout, OSError):
                return 'eof', exited, compile_err
            if frame is None:
                return 'eof', exited, compile_err
            ev = frame.get('ev')
            if ev == 'stdout':
                self._emit_stdout(ctx, frame)
            elif ev == 'stderr':
                # stderr rides the [TECHNIK] line at exit; nothing to show live.
                pass
            elif ev == 'compile_error':
                return 'compile', exited, frame
            elif ev == 'exited':
                self._exited.set()
                return 'exited', frame, compile_err
            # 'started' or an unknown ev: ignore and keep reading.

    @staticmethod
    def _emit_stdout(ctx, frame) -> None:
        from physical_ai_server.workflow.handlers.output import emit_student_text
        lines = frame.get('lines')
        if isinstance(lines, str):
            lines = [lines]
        if not isinstance(lines, list):
            return
        for line in lines:
            if isinstance(line, str):
                try:
                    emit_student_text(ctx, line, 'ausgabe')
                except Exception:  # noqa: BLE001 — an output line never breaks a run
                    pass

    def _raise_error(self, ctx, on_block_change, session, info: dict) -> None:
        """Map the fault to a German sentence, emit the line highlight + the
        raw ``[TECHNIK]`` line, and raise so ``_run`` reports phase ``error``."""
        kind = info.get('kind') or 'other'
        file = str(info.get('file') or '')
        line = info.get('line') if isinstance(info.get('line'), int) else 0
        name = str(info.get('name') or '')
        exc_type = str(info.get('exc_type') or '')
        suggestion = robot_api.suggest(name) if kind == 'robot_method' else None
        relayed = str(info.get('detail_line') or '') if kind == 'robot' else ''
        text = code_errors_de.sentence(
            kind, file=file, line=line, name=name, suggestion=suggestion,
            relayed=relayed, exc_type=exc_type)
        if file and line:
            try:
                on_block_change(f'{file}:L{line}', 'error', 1.0)
            except Exception:  # noqa: BLE001 — status is observability
                pass
        detail = info.get('detail_line')
        if isinstance(detail, str) and detail and kind != 'robot':
            try:
                ctx.log(f'[TECHNIK] {detail}')
            except Exception:  # noqa: BLE001
                pass
        raise WorkflowError(text)
