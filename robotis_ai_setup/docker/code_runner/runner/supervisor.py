"""The code-runner supervisor: the CONTROL side of a Python/Java student run.

    python3 /opt/edubotics/runner/supervisor.py [<ipc_dir>]

Runs as uid 0 with exactly SETUID, SETGID and KILL (the compose service drops
every other capability, keeps the root filesystem read-only, has no network
and is started under ``init: true`` so this process is never PID 1). Every
student process — the Python launcher, ``javac``, ``java`` — and every helper
that touches the run directory is spawned as uid 10001 with
``start_new_session=True``; the supervisor and the student therefore run as
different uids and in different process groups (decision A7.4).

The protocol on ``runner.sock`` (0600, root only — ``physical_ai_server`` is
the one client). Every frame is ``u32 big-endian length`` + compact UTF-8
JSON with a ``ev`` discriminator, bounded by ``CONTROL_MAX_FRAME_BYTES``; the
``start`` envelope carries the whole project, which is why this bound and not
the DATA bound applies here. One request per connection:

    start {run_id, token, language, files, breakpoints}
        -> started {run_id}          on the SAME connection, then
           stdout {lines} / stderr {lines} / compile_error {file, line, text}
           exited {code}             when the student process ended
        -> busy {run_id}             while another run holds the slot
        -> refused {reason}          an envelope the server should never send
    kill {run_id}                    -> killed {run_id}   (any connection)
    breakpoints {run_id, lines}      -> breakpoints_set {run_id, applied}
    ping                             -> pong

A ``kill``, ``breakpoints`` or ``ping`` frame is also accepted on the
connection that carried ``start``; and that connection's END is itself a
kill: the run lives exactly as long as the connection that started it, so a
server that dies mid-program leaves no student process behind.

The kill sequence — ``os.killpg(SIGKILL)`` on the student's session, a
``/proc`` sweep for every process of the student uid (three rounds), then
``waitpid`` — runs on every kill AND after every ordinary exit, so a
``setsid()`` escapee or a daemon thread's process never outlives its run.

The DATA socket ``rpc.sock`` is not this process's: the server creates it,
the student connects to it. The supervisor only hands its path and the run
token to the student process as ``CODE_RPC_SOCKET`` / ``CODE_RUN_TOKEN`` —
the exact and only two names in that process's environment.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Callable

import runner_limits as L

_log = logging.getLogger('code_runner')

# Socket bounds, the server's shape: a recv slice so a stop flag is polled,
# a send bound so a peer that does not drain cannot pin a thread.
RECV_TIMEOUT_S = 0.25
SEND_TIMEOUT_S = 2.0
# How long ``proc.wait`` may take after SIGKILL before the run is reported
# anyway (a D-state process cannot be reaped; the sweep already ran).
_WAIT_AFTER_KILL_S = 5.0
_LISTEN_BACKLOG = 8
_ACCEPT_POLL_S = 0.5
_COMPILE_OUTPUT_MAX_BYTES = 65536

_LANGUAGES = ('python', 'java')
_ENTRY_FILE = {'python': 'main.py', 'java': 'Main.java'}
_EXT_FOR = {'python': '.py', 'java': '.java'}
_PATH_RE = re.compile(L.CODE_PATH_RE)
_TOKEN_RE = re.compile(L.RUN_TOKEN_RE)
_RUN_ID_MAX = 80
_RUN_DIR_SAFE_RE = re.compile(r'[^A-Za-z0-9_-]')
_JAVAC_ERROR_RE = re.compile(r'^(?P<file>[^\s:]+\.java):(?P<line>\d+): error: (?P<text>.*)$',
                             re.MULTILINE)

# Rule §1: the one line a student may see from this file — sent as a stdout
# event once per run when the per-second line budget drops output.
_STDOUT_DROP_NOTICE_DE = ('Zu viele Ausgabe-Zeilen auf einmal — einige wurden nicht '
                          'weitergegeben. Das Programm läuft normal weiter.')

_STUDENT_MAIN = os.path.join(L.RUNNER_ROOT, 'student_main.py')
_SANDBOX_EXEC = os.path.join(L.RUNNER_ROOT, 'sandbox_exec.py')
_WORK_HELPER = os.path.join(L.RUNNER_ROOT, 'work_helper.py')


# ══════════════════════════════════════════════════════════════════════════
# Framing — the twin of code_rpc.read_frame / write_frame
# ══════════════════════════════════════════════════════════════════════════

class FrameError(Exception):
    """A frame that cannot be read: malformed, over the bound, or abandoned."""


class FrameTooLarge(FrameError):
    """``length`` exceeded the bound of this endpoint."""


class ReadAbandoned(FrameError):
    """``should_continue()`` answered False while waiting for bytes."""


def encode_frame_body(obj) -> bytes:
    """The wire encoding — umlauts raw, no whitespace — as bytes."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _recv_exact(sock: socket.socket, n: int, timeout_s: float,
                should_continue: Callable[[], bool] | None) -> bytes | None:
    """Exactly ``n`` bytes into a fresh buffer; ``None`` on a clean EOF before
    the first byte; a timeout asks ``should_continue`` and resumes the same
    buffer (no callback: the ``socket.timeout`` propagates)."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        sock.settimeout(timeout_s)
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
               timeout_s: float = RECV_TIMEOUT_S,
               should_continue: Callable[[], bool] | None = None) -> dict | None:
    """One framed JSON object, or ``None`` on a clean EOF."""
    header = _recv_exact(sock, 4, timeout_s, should_continue)
    if header is None:
        return None
    (length,) = struct.unpack('>I', header)
    if length > max_bytes:
        raise FrameTooLarge(f'frame of {length} bytes exceeds {max_bytes}')
    body = _recv_exact(sock, length, timeout_s, should_continue) if length else b''
    if body is None:
        raise FrameError('connection closed after the header')
    try:
        obj = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError(f'malformed frame body: {exc}') from None
    if not isinstance(obj, dict):
        raise FrameError('frame body is not a JSON object')
    return obj


def write_frame(sock: socket.socket, obj, max_bytes: int, *,
                timeout_s: float = SEND_TIMEOUT_S) -> None:
    """Frame and send ``obj``; ``FrameTooLarge`` when it exceeds ``max_bytes``
    (nothing is sent); ``socket.timeout`` / ``OSError`` when the peer does
    not drain within ``timeout_s``."""
    data = encode_frame_body(obj)
    if len(data) > max_bytes:
        raise FrameTooLarge(f'frame of {len(data)} bytes exceeds {max_bytes}')
    sock.settimeout(timeout_s)
    sock.sendall(struct.pack('>I', len(data)) + data)


# ══════════════════════════════════════════════════════════════════════════
# The start envelope — validated a second time (the server did already)
# ══════════════════════════════════════════════════════════════════════════

def validate_start(frame: dict) -> tuple[dict | None, str | None]:
    """``(clean_envelope, None)`` or ``(None, reason)``. Every field the
    supervisor acts on is checked here so a malformed envelope can neither
    reach the filesystem nor a process argv."""
    run_id = frame.get('run_id')
    if not isinstance(run_id, str) or not run_id or len(run_id) > _RUN_ID_MAX:
        return None, 'run_id'
    token = frame.get('token')
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None, 'token'
    language = frame.get('language')
    if language not in _LANGUAGES:
        return None, 'language'
    files = frame.get('files')
    if not isinstance(files, dict) or not files or len(files) > L.MAX_CODE_FILES:
        return None, 'files'
    clean: dict[str, str] = {}
    for path, content in files.items():
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
            return None, f'path {path!r}'
        if not isinstance(content, str) or not path.endswith(_EXT_FOR[language]):
            return None, f'file {path!r}'
        if len(content.encode('utf-8')) > L.MAX_CODE_FILE_BYTES:
            return None, f'size {path!r}'
        clean[path] = content
    if _ENTRY_FILE[language] not in clean:
        return None, 'entry'
    if len(json.dumps(clean, ensure_ascii=False).encode('utf-8')) > L.MAX_CODE_PROJECT_BYTES:
        return None, 'project size'
    breakpoints = frame.get('breakpoints')
    if breakpoints is not None and not isinstance(breakpoints, dict):
        return None, 'breakpoints'
    return {
        'run_id': run_id, 'token': token, 'language': language,
        'files': clean, 'breakpoints': breakpoints or {},
    }, None


# ══════════════════════════════════════════════════════════════════════════
# The student uid: spawning, the kill sequence
# ══════════════════════════════════════════════════════════════════════════

def spawn_as_student(argv: list[str], *, env: dict, cwd: str | None,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, pass_fds=()) -> subprocess.Popen:
    """Every process that runs as the student goes through here: uid/gid
    10001 with no supplementary groups, its own session (so ``killpg``
    reaches it and its children), the given environment and nothing else."""
    return subprocess.Popen(
        argv, user=L.STUDENT_UID, group=L.STUDENT_GID, extra_groups=[],
        env=env, cwd=cwd, start_new_session=True, close_fds=True,
        pass_fds=pass_fds, stdin=stdin, stdout=stdout, stderr=stderr)


def student_pids() -> list[int]:
    """Every pid whose real uid is the student's, from ``/proc``."""
    found = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/status', 'rb') as handle:
                for raw in handle:
                    if raw.startswith(b'Uid:'):
                        if int(raw.split()[1]) == L.STUDENT_UID:
                            found.append(int(entry))
                        break
        except (OSError, ValueError, IndexError):
            continue
    return found


def sweep_student_uid() -> int:
    """SIGKILL every process of the student uid, ``KILL_SWEEP_ROUNDS`` times
    ``KILL_SWEEP_INTERVAL_S`` apart — what catches a ``setsid()`` escapee that
    ``killpg`` cannot see. Returns how many pids were signalled."""
    signalled = 0
    for _ in range(L.KILL_SWEEP_ROUNDS):
        for pid in student_pids():
            try:
                os.kill(pid, signal.SIGKILL)
                signalled += 1
            except OSError:
                pass
        time.sleep(L.KILL_SWEEP_INTERVAL_S)
    return signalled


def kill_sequence(proc: subprocess.Popen | None) -> None:
    """In this order, always all three: killpg → uid sweep → waitpid."""
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    sweep_student_uid()
    if proc is not None:
        try:
            proc.wait(timeout=_WAIT_AFTER_KILL_S)
        except subprocess.TimeoutExpired:
            _log.error('student process %d did not exit after SIGKILL', proc.pid)


def run_helper(action: str, run_dir: str, payload: bytes | None = None) -> tuple[int, str]:
    """``work_helper.py`` as the student uid; ``(returncode, stderr)``."""
    proc = spawn_as_student(
        [L.PYTHON_BIN, '-I', _WORK_HELPER, action, run_dir],
        env={}, cwd=L.WORK_DIR,
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    try:
        _, err = proc.communicate(payload, timeout=L.HELPER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_sequence(proc)
        return 1, 'helper timed out'
    return proc.returncode, err.decode('utf-8', 'replace')[:2000]


# ══════════════════════════════════════════════════════════════════════════
# The output pump — bounded lines, a per-second budget, one notice on drop
# ══════════════════════════════════════════════════════════════════════════

class _LinePump(threading.Thread):
    """Reads one pipe, splits it into lines, bounds every line, and hands
    batches to ``emit``. A line over ``STDOUT_MAX_LINE_BYTES`` is cut there
    and the rest of it dropped; beyond ``STDOUT_MAX_LINES_PER_S`` lines in
    one second the surplus is dropped and ``on_drop`` called once."""

    def __init__(self, stream, emit: Callable[[list[str]], None],
                 on_drop: Callable[[], None]) -> None:
        super().__init__(name='pump', daemon=True)
        # The Popen keeps the pipe object alive; this thread reads its fd
        # and closes it at EOF (a second close on a closed pipe is a no-op).
        self._stream = stream
        self._emit = emit
        self._on_drop = on_drop
        self._partial = bytearray()
        self._overflowed = False
        self._window_start = time.monotonic()
        self._window_count = 0

    def run(self) -> None:
        fd = self._stream.fileno()
        try:
            while True:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self._feed(chunk)
            if self._partial:
                self._deliver([self._finish_line()])
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def _feed(self, chunk: bytes) -> None:
        lines: list[str] = []
        start = 0
        while True:
            nl = chunk.find(b'\n', start)
            if nl < 0:
                self._append(chunk[start:])
                break
            self._append(chunk[start:nl])
            lines.append(self._finish_line())
            start = nl + 1
        if lines:
            self._deliver(lines)

    def _append(self, data: bytes) -> None:
        if self._overflowed:
            return
        room = L.STDOUT_MAX_LINE_BYTES - len(self._partial)
        if len(data) > room:
            self._partial += data[:room]
            self._overflowed = True
        else:
            self._partial += data

    def _finish_line(self) -> str:
        text = bytes(self._partial).decode('utf-8', 'replace').rstrip('\r')
        if self._overflowed:
            text += ' …'
        self._partial = bytearray()
        self._overflowed = False
        return text

    def _deliver(self, lines: list[str]) -> None:
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._window_count = 0
        room = L.STDOUT_MAX_LINES_PER_S - self._window_count
        if room <= 0:
            self._on_drop()
            return
        kept = lines[:room]
        self._window_count += len(kept)
        if len(kept) < len(lines):
            self._on_drop()
        self._emit(kept)


# ══════════════════════════════════════════════════════════════════════════
# One run
# ══════════════════════════════════════════════════════════════════════════

class Run:
    """The lifetime of one ``start``: stage → (compile) → spawn → pump →
    exited → clean up. Owned by the connection thread that carried ``start``;
    ``kill`` and ``set_breakpoints`` may be called from any thread."""

    def __init__(self, conn: socket.socket, envelope: dict, ipc_dir: str) -> None:
        self.conn = conn
        self.run_id: str = envelope['run_id']
        self.token: str = envelope['token']
        self.language: str = envelope['language']
        self.files: dict[str, str] = envelope['files']
        self.breakpoints: dict = envelope['breakpoints']
        self._data_socket = os.path.join(ipc_dir, L.DATA_SOCKET_NAME)
        safe = _RUN_DIR_SAFE_RE.sub('_', self.run_id)[:64]
        self.run_dir = os.path.join(L.WORK_DIR, f'{safe}-{secrets.token_hex(4)}')
        self._proc: subprocess.Popen | None = None
        self._bp_write_fd: int | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Held while a kill sequence runs; execute() takes it before releasing
        # the slot, so a sweep of THIS run can never land on the next one.
        self._kill_lock = threading.Lock()
        self._killed = threading.Event()
        # Set once the student process is gone AND its post-exit sequence has
        # run: from then on kill() is a no-op (a sweep now could only hit a
        # successor), and the slot is released after cleanup.
        self.process_done = threading.Event()
        self.finished = threading.Event()
        self._drop_notified = False
        self.exit_code: int | None = None

    # ── events to the server ────────────────────────────────────────────────
    def send(self, event: dict) -> None:
        event.setdefault('run_id', self.run_id)
        with self._send_lock:
            try:
                write_frame(self.conn, event, L.CONTROL_MAX_FRAME_BYTES)
            except (OSError, FrameError):
                # The server closed the start connection (its stop path does
                # that before the out-of-band kill); nothing to tell it.
                pass

    def _emit_lines(self, kind: str) -> Callable[[list[str]], None]:
        def emit(lines: list[str]) -> None:
            self.send({'ev': kind, 'lines': lines})
        return emit

    def _on_drop(self) -> None:
        with self._state_lock:
            if self._drop_notified:
                return
            self._drop_notified = True
        self.send({'ev': 'stdout', 'lines': [_STDOUT_DROP_NOTICE_DE]})

    # ── the control surface ─────────────────────────────────────────────────
    def kill(self) -> None:
        """Idempotent; safe from any thread. The student process is gone
        when this returns (or a D-state pid has been logged). A no-op once
        the run's own post-exit sequence has run — the uid sweep is global,
        and a late kill would hit the NEXT run's process."""
        with self._state_lock:
            if self._killed.is_set() or self.process_done.is_set():
                return
            self._killed.set()
            proc = self._proc
        with self._kill_lock:
            kill_sequence(proc)

    def set_breakpoints(self, lines) -> bool:
        with self._state_lock:
            fd = self._bp_write_fd
        if fd is None or not isinstance(lines, dict):
            return False
        try:
            os.write(fd, encode_frame_body({'lines': lines})[:L.BREAKPOINT_LINE_MAX_BYTES - 1] + b'\n')
        except OSError:
            return False
        return True

    # ── the lifetime ────────────────────────────────────────────────────────
    def execute(self) -> None:
        """Run, wait out any in-flight kill, clean up, then release the slot
        — in that order, so neither this run's sweep nor its remove can
        overlap the next ``start``."""
        try:
            self._execute()
        finally:
            self.process_done.set()
            with self._kill_lock:
                pass
            try:
                self.cleanup()
            finally:
                self.finished.set()

    def _execute(self) -> None:
        rc, err = run_helper('stage', self.run_dir,
                             encode_frame_body({'files': self.files}))
        if rc != 0:
            _log.error('stage failed for %s: rc=%d %s', self.run_id, rc, err.strip())
            self.send({'ev': 'refused', 'reason': 'stage'})
            return
        if self._killed.is_set():
            # The kill landed while staging: nothing was spawned, nothing will be.
            self.send({'ev': 'exited', 'code': -signal.SIGKILL})
            return
        self.send({'ev': 'started'})
        try:
            if self.language == 'java':
                if not self._compile_java():
                    return
                proc = self._spawn_java()
            else:
                proc = self._spawn_python()
        except OSError as exc:
            _log.error('spawn failed for %s: %s', self.run_id, exc)
            self.send({'ev': 'exited', 'code': 1})
            return
        with self._state_lock:
            self._proc = proc
            already_killed = self._killed.is_set()
        if already_killed:
            # The kill landed between the check above and the spawn.
            with self._kill_lock:
                kill_sequence(proc)
        pumps = [
            _LinePump(proc.stdout, self._emit_lines('stdout'), self._on_drop),
            _LinePump(proc.stderr, self._emit_lines('stderr'), self._on_drop),
        ]
        for pump in pumps:
            pump.start()
        proc.wait()
        self.exit_code = proc.returncode
        # Every exit ends with the full sequence: a daemon the program left
        # behind would otherwise survive into the next run.
        with self._kill_lock:
            kill_sequence(proc)
        self.process_done.set()
        for pump in pumps:
            pump.join(timeout=2.0)
        self._close_breakpoint_channel()
        self.send({'ev': 'exited', 'code': self.exit_code})

    def cleanup(self) -> None:
        self._close_breakpoint_channel()
        rc, err = run_helper('remove', self.run_dir)
        if rc != 0:
            _log.warning('remove failed for %s: rc=%d %s', self.run_id, rc, err.strip())

    def _close_breakpoint_channel(self) -> None:
        with self._state_lock:
            fd, self._bp_write_fd = self._bp_write_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _student_env(self) -> dict:
        return {'CODE_RPC_SOCKET': self._data_socket, 'CODE_RUN_TOKEN': self.token}

    def _spawn_python(self) -> subprocess.Popen:
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            proc = spawn_as_student(
                [L.PYTHON_BIN, '-I', '-u', _STUDENT_MAIN, self.run_dir, str(read_fd)],
                env=self._student_env(), cwd=self.run_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(read_fd,))
        except OSError:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        with self._state_lock:
            self._bp_write_fd = write_fd
        if self.breakpoints:
            self.set_breakpoints(self.breakpoints)
        return proc

    def _java_sources(self) -> list[str]:
        return sorted(path for path in self.files if path.endswith('.java'))

    def _compile_java(self) -> bool:
        argv = [L.PYTHON_BIN, '-I', _SANDBOX_EXEC, 'java', L.JAVAC_BIN,
                *(f'-J{flag}' for flag in L.JAVA_FLAGS),
                '-encoding', 'UTF-8', '-d', os.path.join(self.run_dir, 'out'),
                '-cp', L.JAVA_CLASSES_DIR, *self._java_sources()]
        proc = spawn_as_student(argv, env={}, cwd=self.run_dir,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with self._state_lock:
            self._proc = proc
        try:
            out, _ = proc.communicate(timeout=L.COMPILE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            kill_sequence(proc)
            self.send({'ev': 'compile_error', 'file': _ENTRY_FILE['java'], 'line': 0,
                       'text': 'javac timed out'})
            return False
        with self._state_lock:
            self._proc = None
            killed = self._killed.is_set()
        if killed:
            # The server's kill landed during the compile; it has already
            # closed its side, there is nobody to report to.
            return False
        if proc.returncode == 0:
            return True
        text = out[:_COMPILE_OUTPUT_MAX_BYTES].decode('utf-8', 'replace')
        match = _JAVAC_ERROR_RE.search(text)
        if match:
            report = {'file': match.group('file')[:80], 'line': int(match.group('line')),
                      'text': match.group('text')[:L.STDOUT_MAX_LINE_BYTES]}
        else:
            first = next((line for line in text.splitlines() if line.strip()), '')
            report = {'file': '', 'line': 0, 'text': first[:L.STDOUT_MAX_LINE_BYTES]}
        self.send({'ev': 'compile_error', **report})
        return False

    def _spawn_java(self) -> subprocess.Popen:
        classpath = os.path.join(self.run_dir, 'out') + ':' + L.JAVA_CLASSES_DIR
        argv = [L.PYTHON_BIN, '-I', _SANDBOX_EXEC, 'java', L.JAVA_BIN,
                *L.JAVA_FLAGS, '-cp', classpath, 'Main']
        return spawn_as_student(argv, env=self._student_env(), cwd=self.run_dir,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)


# ══════════════════════════════════════════════════════════════════════════
# The supervisor
# ══════════════════════════════════════════════════════════════════════════

class Supervisor:
    def __init__(self, ipc_dir: str = L.IPC_DIR) -> None:
        self.ipc_dir = ipc_dir
        self.socket_path = os.path.join(ipc_dir, L.CONTROL_SOCKET_NAME)
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._run_lock = threading.Lock()
        self._run: Run | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def bind(self) -> None:
        if not os.path.isdir(self.ipc_dir):
            raise RuntimeError(f'ipc directory {self.ipc_dir} does not exist')
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, L.CONTROL_SOCKET_MODE)
        listener.listen(_LISTEN_BACKLOG)
        listener.settimeout(_ACCEPT_POLL_S)
        self._listener = listener
        _log.info('control socket %s (mode %o)', self.socket_path, L.CONTROL_SOCKET_MODE)

    def serve_forever(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_connection, args=(conn,),
                             name='control-conn', daemon=True).start()

    def shutdown(self) -> None:
        self._stopping.set()
        with self._run_lock:
            run = self._run
        if run is not None:
            run.kill()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    # ── one control connection ──────────────────────────────────────────────
    def _serve_connection(self, conn: socket.socket) -> None:
        try:
            try:
                frame = read_frame(conn, L.CONTROL_MAX_FRAME_BYTES,
                                   timeout_s=L.CONTROL_FIRST_FRAME_S)
            except (FrameError, socket.timeout, OSError) as exc:
                _log.warning('control connection dropped: %s', exc)
                return
            if frame is None:
                return
            if frame.get('ev') == 'start':
                self._handle_start(conn, frame)
            else:
                self._handle_request(conn, frame)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_request(self, conn: socket.socket, frame: dict) -> None:
        ev = frame.get('ev')
        run_id = frame.get('run_id')
        if ev == 'ping':
            self._reply(conn, {'ev': 'pong'})
        elif ev == 'kill':
            with self._run_lock:
                target = self._run
            if target is not None:
                target.kill()
            self._reply(conn, {'ev': 'killed', 'run_id': run_id})
        elif ev == 'breakpoints':
            with self._run_lock:
                target = self._run
            applied = target is not None and target.set_breakpoints(frame.get('lines'))
            self._reply(conn, {'ev': 'breakpoints_set', 'run_id': run_id, 'applied': applied})
        else:
            _log.warning('unknown control event %r', ev)
            self._reply(conn, {'ev': 'refused', 'reason': 'unknown event'})

    @staticmethod
    def _reply(conn: socket.socket, obj: dict) -> None:
        try:
            write_frame(conn, obj, L.CONTROL_MAX_FRAME_BYTES)
        except (OSError, FrameError):
            pass

    def _handle_start(self, conn: socket.socket, frame: dict) -> None:
        envelope, reason = validate_start(frame)
        if envelope is None:
            _log.error('refused start: %s', reason)
            self._reply(conn, {'ev': 'refused', 'reason': reason})
            return
        run = Run(conn, envelope, self.ipc_dir)
        if not self._claim_slot(run):
            self._reply(conn, {'ev': 'busy', 'run_id': run.run_id})
            return
        _log.info('run %s (%s) started', run.run_id, run.language)
        watcher = threading.Thread(target=self._watch_start_connection, args=(conn, run),
                                   name='start-conn-watch', daemon=True)
        watcher.start()
        try:
            run.execute()
        finally:
            with self._run_lock:
                if self._run is run:
                    self._run = None
            _log.info('run %s ended (code %s)', run.run_id, run.exit_code)

    def _claim_slot(self, run: Run) -> bool:
        deadline = time.monotonic() + L.BUSY_WAIT_S
        while True:
            with self._run_lock:
                current = self._run
                if current is None or current.finished.is_set():
                    self._run = run
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _watch_start_connection(self, conn: socket.socket, run: Run) -> None:
        """The run lives as long as the connection that started it: more
        control frames are served from it, and its end (EOF, reset, garbage)
        kills the run."""
        while not run.finished.is_set():
            try:
                frame = read_frame(conn, L.CONTROL_MAX_FRAME_BYTES,
                                   should_continue=lambda: not run.finished.is_set())
            except ReadAbandoned:
                return
            except (FrameError, socket.timeout, OSError):
                break
            if frame is None:
                break
            self._handle_request(conn, frame)
        if not run.process_done.is_set():
            _log.info('start connection of %s ended — killing the run', run.run_id)
            run.kill()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(levelname)s %(message)s')
    supervisor = Supervisor(argv[1] if len(argv) > 1 else L.IPC_DIR)
    try:
        supervisor.bind()
    except (OSError, RuntimeError) as exc:
        _log.error('cannot bind the control socket: %s', exc)
        return 1

    def _terminate(signum, _frame) -> None:
        _log.info('signal %d — shutting down', signum)
        supervisor.shutdown()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    _log.info('supervisor pid %d uid %d', os.getpid(), os.getuid())
    supervisor.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
