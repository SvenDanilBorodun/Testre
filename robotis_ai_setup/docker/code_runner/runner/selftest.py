"""The code-runner self-test — run INSIDE the image, as root.

    python3 /opt/edubotics/runner/selftest.py            # every rung (needs the sandbox flags)
    python3 /opt/edubotics/runner/selftest.py --build    # the build-time subset (a, b, c, d)

Each rung prints its own ``OK (<rung>) …`` line and the whole thing ends with
``SELFTEST OK``; any failure ends the process with ``SELFTEST FAILED: …`` and
a non-zero exit. The rungs:

  (a) uid/caps — a process spawned as the student uid cannot signal PID 1,
      cannot signal its parent, cannot ``setuid(0)`` (three EPERMs), and a
      ``setsid()`` escapee that ``killpg`` misses is caught by the /proc sweep.
  (b) debugger — a student loop under ``edubotics_debug``; a breakpoint on
      the loop body added MID-RUN through ``Hook.set_breakpoints`` (the path
      the ``breakpoints`` control message ends in) fires; an unmarked line's
      callback returned ``DISABLE``. Comment out ``restart_events()`` in the
      hook and the hit count is 0.
  (c) rate floor — 400 back-to-back stub calls against a null server take at
      least ``(400 - BURST) / MAX_CALLS_PER_S`` seconds of wall clock.
  (d) the compiled Java tree's ``SmokeMain`` prints ``JAVA SMOKE OK``.
  (e) end to end — the real supervisor over ``runner.sock`` against a fake
      ``physical_ai_server`` on ``rpc.sock``: a clean Python run, a faulting
      one, a misspelled robot method, a Java run, a Java compile error, a
      breakpoint applied mid-run over the control socket, ``busy``, an
      out-of-band ``kill``, and no student process left behind.

The full run needs the compose sandbox (see the Dockerfile header for the
``docker run`` line); ``--build`` needs only root and ``/proc``.
"""

from __future__ import annotations

import os
import runpy
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import runner_limits as L
import supervisor as S

_ROOT = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_ROOT, 'lib')
_TOKEN = '0123456789abcdef0123456789abcdef'


def _ok(rung: str, detail: str) -> None:
    print(f'OK ({rung}) {detail}', flush=True)


def _check(cond, what: str) -> None:
    if not cond:
        print(f'SELFTEST FAILED: {what}', flush=True)
        raise SystemExit(1)


def _alive(pid: int) -> bool:
    """A pid that exists and is not a zombie (a zombie is dead; whether it
    has been reaped depends on who PID 1 is)."""
    try:
        with open(f'/proc/{pid}/status', 'rb') as handle:
            for raw in handle:
                if raw.startswith(b'State:'):
                    return raw.split()[1] not in (b'Z', b'X')
    except OSError:
        return False
    return False


# ── (a) uid/caps ────────────────────────────────────────────────────────────

_PROBE = r'''
import errno, os
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
print(os.getuid(), os.getgid(), attempt(lambda: os.kill(1, 0)),
      attempt(lambda: os.kill(os.getppid(), 0)), attempt(lambda: os.setuid(0)))
'''

_ESCAPEE = r'''
import os, time
pid = os.fork()
if pid == 0:
    os.setsid()
    null = os.open("/dev/null", os.O_RDWR)
    os.dup2(null, 1)
    os.dup2(null, 2)
    time.sleep(60)
    os._exit(0)
print(pid, flush=True)
'''


def rung_uid_caps(build: bool) -> None:
    if not build:
        _check(os.getpid() != 1, 'the self-test runs as PID 1 — no init in front of it')
    _check(os.getuid() == 0, 'the self-test must run as uid 0')
    proc = S.spawn_as_student([L.PYTHON_BIN, '-I', '-c', _PROBE], env={}, cwd='/',
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(timeout=30)
    fields = out.decode().split()
    _check(len(fields) == 5, f'probe output {out!r} {err!r}')
    uid, gid, kill_init, kill_parent, setuid = fields
    _check(uid == str(L.STUDENT_UID) and gid == str(L.STUDENT_GID),
           f'student process is uid {uid} gid {gid}')
    _check((kill_init, kill_parent, setuid) == ('EPERM', 'EPERM', 'EPERM'),
           f'expected three EPERMs, got kill(1)={kill_init} kill(parent)={kill_parent} '
           f'setuid(0)={setuid}')

    proc = S.spawn_as_student([L.PYTHON_BIN, '-I', '-c', _ESCAPEE], env={}, cwd='/',
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = proc.communicate(timeout=30)
    escapee = int(out.decode().split()[0])
    _check(_alive(escapee), 'the setsid escapee is not running')
    try:
        os.killpg(proc.pid, 9)
    except OSError:
        pass
    time.sleep(0.1)
    _check(_alive(escapee), 'killpg alone reached the setsid escapee — the sweep is untested')
    signalled = S.sweep_student_uid()
    _check(not _alive(escapee), 'the /proc uid sweep did not kill the setsid escapee')
    _check(not any(_alive(p) for p in S.student_pids()), 'a student-uid process survived the sweep')
    _ok('a', f'uid/caps: student uid {uid}, kill(1)/kill(parent)/setuid(0) all EPERM; '
             f'setsid escapee {escapee} survived killpg and died to the sweep ({signalled} signalled)')


# ── (b) debugger ────────────────────────────────────────────────────────────

_LOOP_SRC = (
    'import time\n'
    'deadline = time.monotonic() + 1.5\n'
    'x = 0\n'
    'while time.monotonic() < deadline:\n'
    '    x += 1\n'
)
_LOOP_BODY_LINE = 5


class _FakeRpc:
    def __init__(self) -> None:
        self.paused = 0
        self._lock = threading.RLock()

    def call(self, method, args, kind):
        if method == '__paused':
            self.paused += 1
            return 'continue'
        return None


def rung_debugger() -> None:
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import edubotics_debug
    tmp = tempfile.mkdtemp(prefix='edu-dbg-')
    path = os.path.join(tmp, 'loop.py')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(_LOOP_SRC)
    rpc = _FakeRpc()
    hook = edubotics_debug.Hook(rpc, tmp)
    disabled_lines: list[int] = []
    inner = hook.on_line

    def spy(code, line):
        result = inner(code, line)
        if code.co_filename == path and line != _LOOP_BODY_LINE \
                and result is sys.monitoring.DISABLE:
            disabled_lines.append(line)
        return result

    hook.on_line = spy
    hook.install()
    timer = threading.Timer(0.4, hook.set_breakpoints, ({'loop.py': [_LOOP_BODY_LINE]},))
    timer.start()
    try:
        runpy.run_path(path, run_name='__main__')
    finally:
        timer.cancel()
        hook.uninstall()
    _check(rpc.paused > 0, 'a breakpoint added mid-run never fired (restart_events missing?)')
    _check(disabled_lines, "an unmarked line's callback did not return DISABLE")
    _ok('b', f'debugger: {rpc.paused} hits on line {_LOOP_BODY_LINE} after a mid-run '
             f'breakpoint; unmarked lines {sorted(set(disabled_lines))} returned DISABLE')


# ── (c) rate floor ──────────────────────────────────────────────────────────

def _null_server(path: str, ready: threading.Event, stop: threading.Event) -> None:
    """Accepts, replies ``ok`` to every frame, never initiates anything."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    listener.settimeout(0.2)
    ready.set()

    def serve(conn: socket.socket) -> None:
        try:
            while not stop.is_set():
                try:
                    frame = S.read_frame(conn, L.MAX_FRAME_BYTES, timeout_s=0.2,
                                         should_continue=lambda: not stop.is_set())
                except S.FrameError:
                    return
                if frame is None:
                    return
                S.write_frame(conn, {'id': frame.get('id'), 'ok': True, 'r': None},
                              L.MAX_FRAME_BYTES)
        except OSError:
            pass
        finally:
            conn.close()

    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        threading.Thread(target=serve, args=(conn,), daemon=True).start()
    listener.close()


def rung_rate_floor() -> None:
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import robot
    tmp = tempfile.mkdtemp(prefix='edu-rate-')
    path = os.path.join(tmp, 'null.sock')
    ready, stop = threading.Event(), threading.Event()
    threading.Thread(target=_null_server, args=(path, ready, stop), daemon=True).start()
    _check(ready.wait(5), 'null server did not come up')
    rpc = robot._Rpc()
    robot._rpc = rpc
    calls = 400
    floor = (calls - L.BURST) / L.MAX_CALLS_PER_S
    t0 = time.monotonic()
    rpc.connect(path, _TOKEN)
    for _ in range(calls):
        robot.home()
    elapsed = time.monotonic() - t0
    rpc.close()
    stop.set()
    _check(elapsed >= floor, f'{calls} calls took {elapsed:.3f} s, under the floor {floor:.2f} s')
    _ok('c', f'rate floor: {calls} stub calls took {elapsed:.3f} s (floor {floor:.2f} s)')


# ── (d) java smoke ──────────────────────────────────────────────────────────

def rung_java_smoke() -> None:
    proc = subprocess.run([L.JAVA_BIN, *L.JAVA_FLAGS, '-cp', L.JAVA_CLASSES_DIR,
                           'edubotics.SmokeMain'], capture_output=True, timeout=120)
    out = proc.stdout.decode('utf-8', 'replace')
    _check(proc.returncode == 0 and 'JAVA SMOKE OK' in out,
           f'SmokeMain rc={proc.returncode} out={out!r} err={proc.stderr[-500:]!r}')
    _ok('d', 'java: SmokeMain against the compiled tree printed JAVA SMOKE OK')


# ── (e) end to end ──────────────────────────────────────────────────────────

class _FakeServer:
    """The DATA side: rpc.sock, 0666, the ``__hello`` gate, ``ok`` to all."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.seen: list = []
        self.exit_infos: list = []
        self.paused = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(path)
        os.chmod(path, L.DATA_SOCKET_MODE)
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        greeted = False
        try:
            while not self._stop.is_set():
                try:
                    frame = S.read_frame(conn, L.MAX_FRAME_BYTES, timeout_s=0.2,
                                         should_continue=lambda: not self._stop.is_set())
                except S.FrameError:
                    return
                if frame is None:
                    return
                method, args = frame.get('m'), frame.get('a')
                if not greeted:
                    if method != '__hello' or args != [_TOKEN]:
                        return
                    greeted = True
                with self._lock:
                    self.seen.append(method)
                    if method == '__exit' and args:
                        self.exit_infos.append(args[0])
                    if method == '__paused':
                        self.paused += 1
                result = 'continue' if method == '__paused' else None
                S.write_frame(conn, {'id': frame.get('id'), 'ok': True, 'r': result},
                              L.MAX_FRAME_BYTES)
        except OSError:
            pass
        finally:
            conn.close()

    def wait_for(self, predicate, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if predicate():
                    return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self._stop.set()
        self._listener.close()


def _control(path: str, frame: dict) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5.0)
    conn.connect(path)
    S.write_frame(conn, frame, L.CONTROL_MAX_FRAME_BYTES)
    return conn


def _events(conn: socket.socket, until: set, timeout_s: float) -> list:
    out = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            frame = S.read_frame(conn, L.CONTROL_MAX_FRAME_BYTES, timeout_s=0.25,
                                 should_continue=lambda: time.monotonic() < deadline)
        except S.ReadAbandoned:
            break
        if frame is None:
            break
        out.append(frame)
        if frame.get('ev') in until:
            break
    return out


def _start(path: str, run_id: str, language: str, files: dict, breakpoints=None):
    return _control(path, {'ev': 'start', 'run_id': run_id, 'token': _TOKEN,
                           'language': language, 'files': files,
                           'breakpoints': breakpoints or {}})


def _lines(events: list, kind: str) -> list:
    return [line for ev in events if ev.get('ev') == kind for line in ev.get('lines', [])]


def rung_end_to_end() -> None:
    ipc = tempfile.mkdtemp(prefix='edu-ipc-', dir=L.IPC_DIR if os.path.isdir(L.IPC_DIR) else None)
    os.chmod(ipc, 0o755)   # the student uid must traverse it to reach rpc.sock
    server = _FakeServer(os.path.join(ipc, L.DATA_SOCKET_NAME))
    sup = subprocess.Popen([L.PYTHON_BIN, os.path.join(_ROOT, 'supervisor.py'), ipc],
                           stderr=subprocess.PIPE)
    control = os.path.join(ipc, L.CONTROL_SOCKET_NAME)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not os.path.exists(control):
        time.sleep(0.05)
    _check(os.path.exists(control), 'runner.sock never appeared')
    _check(stat.S_IMODE(os.stat(control).st_mode) == L.CONTROL_SOCKET_MODE,
           f'runner.sock mode {oct(stat.S_IMODE(os.stat(control).st_mode))}')
    _check(stat.S_IMODE(os.stat(server.path).st_mode) == L.DATA_SOCKET_MODE, 'rpc.sock mode')
    _check(sup.pid != 1 and sup.pid != os.getpid(), 'supervisor pid')
    try:
        # ping
        conn = _control(control, {'ev': 'ping'})
        _check(_events(conn, {'pong'}, 5)[-1].get('ev') == 'pong', 'ping → pong')
        conn.close()

        # 1. a clean Python run
        conn = _start(control, 'run-1', 'python',
                      {'main.py': 'import robot\nrobot.home()\nprint("hallo ü")\n'})
        events = _events(conn, {'exited', 'compile_error', 'busy', 'refused'}, 30)
        conn.close()
        kinds = [ev.get('ev') for ev in events]
        _check(kinds and kinds[0] == 'started', f'python: first event {kinds}')
        _check(kinds[-1] == 'exited' and events[-1].get('code') == 0, f'python: {events}')
        _check('hallo ü' in _lines(events, 'stdout'), f'python stdout: {events}')
        _check(server.wait_for(lambda: server.exit_infos and server.exit_infos[-1].get('kind') == 'ok', 5),
               f'python __exit: {server.exit_infos}')
        _check('home' in server.seen and server.seen[0] == '__hello', f'server saw {server.seen}')

        # 2. a faulting Python run — the classified __exit, then a non-zero code
        conn = _start(control, 'run-2', 'python', {'main.py': 'x = 1 / 0\n'})
        events = _events(conn, {'exited'}, 30)
        conn.close()
        _check(events[-1].get('ev') == 'exited' and events[-1].get('code') == 1, f'fault: {events}')
        info = server.exit_infos[-1]
        _check(info.get('kind') == 'zero_division' and info.get('file') == 'main.py'
               and info.get('line') == 1 and 'ZeroDivisionError' in info.get('detail_line', ''),
               f'fault __exit: {info}')

        # 3. a misspelled robot method
        conn = _start(control, 'run-3', 'python', {'main.py': 'import robot\nrobot.hoome()\n'})
        events = _events(conn, {'exited'}, 30)
        conn.close()
        info = server.exit_infos[-1]
        _check(info.get('kind') == 'robot_method' and info.get('name') == 'hoome'
               and info.get('line') == 2,
               f'robot_method __exit: {info}; stderr={_lines(events, "stderr")}; events={events}')

        # 4. a Java run
        conn = _start(control, 'run-4', 'java', {'Main.java': (
            'import edubotics.Robot;\n'
            'public class Main {\n'
            '    public static void main(String[] args) {\n'
            '        Robot.home();\n'
            '        System.out.println("hallo ü");\n'
            '    }\n'
            '}\n')})
        events = _events(conn, {'exited', 'compile_error'}, 120)
        conn.close()
        kinds = [ev.get('ev') for ev in events]
        _check(kinds and kinds[0] == 'started' and kinds[-1] == 'exited'
               and events[-1].get('code') == 0, f'java: {events}')
        _check('hallo ü' in _lines(events, 'stdout'), f'java stdout: {events}')

        # 5. a Java compile error
        conn = _start(control, 'run-5', 'java', {'Main.java': (
            'public class Main { public static void main(String[] a) { int x = ; } }\n')})
        events = _events(conn, {'exited', 'compile_error'}, 120)
        conn.close()
        _check(events[-1].get('ev') == 'compile_error' and events[-1].get('file') == 'Main.java'
               and events[-1].get('line') == 1 and events[-1].get('text'), f'javac: {events}')

        # 6. a breakpoint applied mid-run over the control socket, busy, then kill
        paused_before = server.paused
        conn = _start(control, 'run-6', 'python', {'main.py': 'i = 0\nwhile True:\n    i += 1\n'})
        first = _events(conn, {'started', 'busy', 'refused'}, 10)
        _check(first and first[-1].get('ev') == 'started', f'loop start: {first}')
        second = _start(control, 'run-7', 'python', {'main.py': 'pass\n'})
        _check(_events(second, {'busy', 'started'}, 5)[-1].get('ev') == 'busy', 'second start not busy')
        second.close()
        bp = _control(control, {'ev': 'breakpoints', 'run_id': 'run-6', 'lines': {'main.py': [3]}})
        ack = _events(bp, {'breakpoints_set'}, 5)
        bp.close()
        _check(ack and ack[-1].get('applied') is True, f'breakpoints ack: {ack}')
        _check(server.wait_for(lambda: server.paused > paused_before, 10),
               'a breakpoint applied over the control socket never paused the loop')
        t0 = time.monotonic()
        killer = _control(control, {'ev': 'kill', 'run_id': 'run-6'})
        _check(_events(killer, {'killed'}, 5)[-1].get('ev') == 'killed', 'kill → killed')
        killer.close()
        tail = _events(conn, {'exited'}, 10)
        conn.close()
        kill_s = time.monotonic() - t0
        _check(tail and tail[-1].get('ev') == 'exited' and tail[-1].get('code') != 0,
               f'kill: {tail}')
        _check(not any(_alive(p) for p in S.student_pids()), 'a student process survived the kill')
        # `exited` precedes the remove helper; the slot is released after it.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and os.listdir(L.WORK_DIR):
            time.sleep(0.05)
        _check(not os.listdir(L.WORK_DIR), f'/work not cleared: {os.listdir(L.WORK_DIR)}')

        # 7. a run whose start connection is DROPPED is killed too
        conn = _start(control, 'run-8', 'python', {'main.py': 'while True:\n    pass\n'})
        _check(_events(conn, {'started'}, 10)[-1].get('ev') == 'started', 'drop: start')
        conn.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(_alive(p) for p in S.student_pids()):
            time.sleep(0.1)
        _check(not any(_alive(p) for p in S.student_pids()),
               'a run outlived the connection that started it')
    finally:
        sup.terminate()
        try:
            sup.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sup.kill()
        server.close()
    _ok('e', f'end to end: python ok/fault/robot_method, java ok/compile_error, '
             f'mid-run breakpoint paused {server.paused - paused_before}x, busy answered, '
             f'kill acked and exited in {kill_s:.2f} s, dropped connection killed the run')


def main(argv: list[str]) -> int:
    build = '--build' in argv
    rung_uid_caps(build)
    rung_debugger()
    rung_rate_floor()
    rung_java_smoke()
    if not build:
        rung_end_to_end()
    print('SELFTEST OK', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
