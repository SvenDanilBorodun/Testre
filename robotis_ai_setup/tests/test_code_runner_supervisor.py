"""The code-runner supervisor: the sandbox's own numbers and the protocol
surface that needs no uid switch (deps-free, runs on the host).

``robotis_ai_setup/docker/code_runner/runner/`` is the tree the runner image
ships verbatim. It is outside every compileall root and every CI scan, so the
deps-free suite is its only fence. What a root-less host CAN prove is here:
the AST order of the kill sequence, the two-name student environment, the
rlimit table, that the supervisor never writes under /work itself, the socket
modes, the validation of a ``start`` envelope before anything touches a
filesystem, the CONTROL bound on every control read, the output pump's
bounds, the framing round trip, and the request/reply surface of a real
supervisor on a temp-dir socket (``ping``/``kill``/``refused``/unknown).
What needs uid 10001 and the capabilities — a real run — is the image's
self-test (``runner/selftest.py``, the ``other:runner-sandbox`` gate).
"""

import ast
import importlib.util
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
_SUPERVISOR = _RUNNER / 'supervisor.py'
_LIMITS = _RUNNER / 'runner_limits.py'
_WORK_HELPER = _RUNNER / 'work_helper.py'


def _load(name, path):
    """Load a runner module by path with the runner dir importable (the
    supervisor does ``import runner_limits`` the way the image runs it)."""
    if str(_RUNNER) not in sys.path:
        sys.path.insert(0, str(_RUNNER))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function(tree, name, cls=None):
    for node in ast.walk(tree):
        if cls is not None:
            if isinstance(node, ast.ClassDef) and node.name == cls:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == name:
                        return item
        elif isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{cls + "." if cls else ""}{name} not found')


def _call_names(node):
    """Dotted names of every call in ``node``, in SOURCE order (``ast.walk``
    is breadth-first, which is not an order a statement fence can use)."""
    calls = [sub for sub in ast.walk(node) if isinstance(sub, ast.Call)]
    calls.sort(key=lambda c: (c.lineno, c.col_offset))
    return [ast.unparse(c.func) for c in calls]


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assertTrue(cls, _SUPERVISOR.is_file(), _SUPERVISOR)
        cls.src = _SUPERVISOR.read_text(encoding='utf-8')
        cls.tree = ast.parse(cls.src)
        cls.L = _load('runner_limits', _LIMITS)
        cls.S = _load('supervisor', _SUPERVISOR)


class KillSequence(_Base):
    def test_kill_sequence_is_killpg_then_uid_sweep_then_waitpid(self):
        """In this order, always all three (§3.1): a setsid() escapee is what
        the sweep is for, and waitpid last so the sweep runs before we block."""
        fn = _function(self.tree, 'kill_sequence')
        names = _call_names(fn)
        self.assertIn('os.killpg', names)
        self.assertIn('sweep_student_uid', names)
        self.assertIn('proc.wait', names)
        self.assertLess(names.index('os.killpg'), names.index('sweep_student_uid'))
        self.assertLess(names.index('sweep_student_uid'), names.index('proc.wait'))

    def test_the_sweep_signals_every_student_pid_for_three_rounds(self):
        fn = _function(self.tree, 'sweep_student_uid')
        self.assertIn('student_pids', _call_names(fn))
        self.assertIn('os.kill', _call_names(fn))
        self.assertEqual(self.L.KILL_SWEEP_ROUNDS, 3)
        self.assertEqual(self.L.KILL_SWEEP_INTERVAL_S, 0.1)
        self.assertIn('KILL_SWEEP_ROUNDS', ast.unparse(fn))

    def test_the_sweep_reads_the_uid_from_proc(self):
        fn = _function(self.tree, 'student_pids')
        self.assertIn("'/proc'", ast.unparse(fn))
        self.assertIn("b'Uid:'", ast.unparse(fn))

    def test_every_run_end_runs_the_sequence_not_only_a_kill(self):
        """A daemon the program left behind would otherwise survive into the
        next run; ``_execute`` therefore calls kill_sequence after wait."""
        fn = _function(self.tree, '_execute', cls='Run')
        names = _call_names(fn)
        self.assertIn('kill_sequence', names)
        self.assertLess(names.index('proc.wait'), len(names) - 1 - names[::-1].index('kill_sequence'))

    def test_a_kill_after_the_post_exit_sequence_is_a_no_op(self):
        """The uid sweep is GLOBAL. Measured 2026-09-21 in the image: the
        start connection of a finished run closed while its cleanup still
        ran, the connection watcher killed it, and the sweep landed on the
        NEXT run's freshly spawned student (`exited code -9`). Once the run's
        own post-exit sequence has run there is nothing of ours to kill."""
        run = self.S.Run.__new__(self.S.Run)
        run._state_lock = threading.Lock()
        run._kill_lock = threading.Lock()
        run._killed = threading.Event()
        run.process_done = threading.Event()
        run._proc = None
        calls = []
        original = self.S.kill_sequence
        self.S.kill_sequence = lambda proc: calls.append(proc)
        try:
            run.process_done.set()
            run.kill()
            self.assertEqual(calls, [], 'a sweep after process_done hits the next run')
            fresh = self.S.Run.__new__(self.S.Run)
            fresh._state_lock = threading.Lock()
            fresh._kill_lock = threading.Lock()
            fresh._killed = threading.Event()
            fresh.process_done = threading.Event()
            fresh._proc = None
            fresh.kill()
            fresh.kill()
            self.assertEqual(calls, [None], 'a live run is killed exactly once')
        finally:
            self.S.kill_sequence = original

    def test_the_slot_is_released_only_after_an_in_flight_kill_and_the_cleanup(self):
        """execute(): process_done → wait out _kill_lock → cleanup → finished."""
        fn = _function(self.tree, 'execute', cls='Run')
        src = ast.unparse(fn)
        self.assertLess(src.index('self.process_done.set()'), src.index('with self._kill_lock'))
        self.assertLess(src.index('with self._kill_lock'), src.index('self.cleanup()'))
        self.assertLess(src.index('self.cleanup()'), src.index('self.finished.set()'))
        claim = _function(self.tree, '_claim_slot', cls='Supervisor')
        self.assertIn('current.finished.is_set()', ast.unparse(claim))

    def test_the_connection_watcher_kills_only_a_live_process(self):
        fn = _function(self.tree, '_watch_start_connection', cls='Supervisor')
        self.assertIn('if not run.process_done.is_set():', ast.unparse(fn))


class StudentProcessContract(_Base):
    def test_student_env_is_exactly_two_names(self):
        self.assertEqual(self.L.STUDENT_ENV_NAMES, ('CODE_RPC_SOCKET', 'CODE_RUN_TOKEN'))
        fn = _function(self.tree, '_student_env', cls='Run')
        ret = [n for n in ast.walk(fn) if isinstance(n, ast.Return)][0]
        self.assertIsInstance(ret.value, ast.Dict)
        keys = sorted(k.value for k in ret.value.keys)
        self.assertEqual(keys, sorted(self.L.STUDENT_ENV_NAMES))

    def test_helpers_and_javac_get_an_empty_environment(self):
        for name, cls in (('run_helper', None), ('_compile_java', 'Run')):
            fn = _function(self.tree, name, cls=cls)
            envs = [ast.unparse(kw.value) for n in ast.walk(fn) if isinstance(n, ast.Call)
                    for kw in n.keywords if kw.arg == 'env']
            self.assertEqual(envs, ['{}'], f'{name}: {envs}')

    def test_every_student_spawn_goes_through_spawn_as_student(self):
        """One function owns uid/gid/session/close_fds; nothing else in the
        supervisor constructs a Popen."""
        popens = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == 'subprocess.Popen']
        self.assertEqual(len(popens), 1)
        fn = _function(self.tree, 'spawn_as_student')
        kws = {kw.arg: ast.unparse(kw.value) for n in ast.walk(fn)
               if isinstance(n, ast.Call) and ast.unparse(n.func) == 'subprocess.Popen'
               for kw in n.keywords}
        self.assertEqual(kws['user'], 'L.STUDENT_UID')
        self.assertEqual(kws['group'], 'L.STUDENT_GID')
        self.assertEqual(kws['extra_groups'], '[]')
        self.assertEqual(kws['start_new_session'], 'True')
        self.assertEqual(kws['close_fds'], 'True')
        self.assertNotIn('preexec_fn', kws)

    def test_the_supervisor_uses_no_preexec_fn_anywhere(self):
        # CPython documents preexec_fn as unsafe with threads; the limits are
        # applied by the student-side processes themselves (apply_rlimits).
        self.assertNotIn('preexec_fn', self.src)

    def test_rlimits_table(self):
        mib = 1024 * 1024
        self.assertEqual(self.L.RLIMIT_AS_BYTES, {'python': 512 * mib, 'java': 1024 * mib})
        self.assertEqual(self.L.RLIMIT_NPROC, 32)
        self.assertEqual(self.L.RLIMIT_FSIZE_BYTES, 32 * mib)
        self.assertEqual(self.L.RLIMIT_NOFILE, 64)
        self.assertEqual(self.L.RLIMIT_CORE, 0)

    def test_apply_rlimits_sets_soft_equal_to_hard_for_every_row(self):
        tree = ast.parse(_LIMITS.read_text(encoding='utf-8'))
        fn = _function(tree, 'apply_rlimits')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == 'resource.setrlimit']
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0].args[1]), '(value, value)')
        rows = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                and ast.unparse(n).startswith('resource.RLIMIT_')]
        self.assertEqual(sorted(r.attr for r in rows),
                         ['RLIMIT_AS', 'RLIMIT_CORE', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE',
                          'RLIMIT_NPROC'])

    def test_every_student_side_entry_point_applies_the_limits_first(self):
        """student_main, sandbox_exec and work_helper each lower their own
        limits before any student byte or any filesystem write."""
        for name in ('student_main.py', 'sandbox_exec.py', 'work_helper.py'):
            tree = ast.parse((_RUNNER / name).read_text(encoding='utf-8'))
            fn = _function(tree, 'main')
            names = _call_names(fn)
            self.assertIn('limits.apply_rlimits', names, name)
            first_write = next((i for i, n in enumerate(names)
                                if n in ('runpy.run_path', 'os.execv', 'stage',
                                         'clear_work_dir')), None)
            self.assertIsNotNone(first_write, name)
            self.assertLess(names.index('limits.apply_rlimits'), first_write, name)

    def test_interpreters_are_invoked_by_absolute_path(self):
        # The student process has no PATH (two env names only).
        for value in (self.L.PYTHON_BIN, self.L.JAVA_BIN, self.L.JAVAC_BIN):
            self.assertTrue(value.startswith('/'), value)
        self.assertEqual(self.L.JAVA_BIN, '/opt/java/openjdk/bin/java')

    def test_the_jvm_reservations_are_capped_so_a_bounded_address_space_works(self):
        # Measured 2026-09-21 on eclipse-temurin:21-jdk-noble arm64: without
        # these three caps the JVM refuses every RLIMIT_AS under ~1.3 GiB.
        flags = self.L.JAVA_FLAGS
        for needed in ('-Xmx256m', '-Xss1m', '-XX:CompressedClassSpaceSize=64m',
                       '-XX:ReservedCodeCacheSize=32m', '-XX:MaxMetaspaceSize=96m',
                       '-XX:+UseSerialGC'):
            self.assertIn(needed, flags)


class FilesystemDiscipline(_Base):
    def test_supervisor_never_writes_under_work_itself(self):
        """Sources are written and the run dir removed by the uid-10001 helper
        (root without DAC_OVERRIDE cannot unlink in a student-owned dir)."""
        writers = ('open', 'os.makedirs', 'os.mkdir', 'os.remove', 'os.unlink',
                   'os.rmdir', 'shutil.rmtree', 'os.rename', 'os.replace')
        calls = [ast.unparse(n.func) for n in ast.walk(self.tree) if isinstance(n, ast.Call)]
        # The two sanctioned writes are the control socket's own file and
        # /proc/<pid>/status reads; both live outside /work.
        offending = [c for c in calls if c in writers]
        self.assertEqual(sorted(set(offending)), ['open', 'os.unlink'])
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == 'open':
                self.assertIn('/proc/', ast.unparse(node.args[0]))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.unlink':
                self.assertEqual(ast.unparse(node.args[0]), 'self.socket_path')
        # WORK_DIR reaches a helper's cwd and the run dir's name — never a write.
        self.assertIn('L.WORK_DIR', self.src)
        self.assertNotIn("open(os.path.join(L.WORK_DIR", self.src)

    def test_the_work_helper_is_the_one_writer_and_checks_paths_first(self):
        tree = ast.parse(_WORK_HELPER.read_text(encoding='utf-8'))
        fn = _function(tree, 'stage')
        names = _call_names(fn)
        self.assertIn('pattern.fullmatch', names)
        self.assertLess(names.index('pattern.fullmatch'), names.index('clear_work_dir'))
        self.assertLess(names.index('pattern.fullmatch'), names.index('os.makedirs'))

    def test_control_socket_mode_is_0600_and_data_socket_0666(self):
        self.assertEqual(self.L.CONTROL_SOCKET_MODE, 0o600)
        self.assertEqual(self.L.DATA_SOCKET_MODE, 0o666)
        fn = _function(self.tree, 'bind', cls='Supervisor')
        chmods = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == 'os.chmod']
        self.assertEqual(chmods, ['os.chmod(self.socket_path, L.CONTROL_SOCKET_MODE)'])
        names = _call_names(fn)
        self.assertLess(names.index('os.unlink'), names.index('listener.bind'), 'stale unlink first')
        self.assertLess(names.index('listener.bind'), names.index('os.chmod'))
        self.assertLess(names.index('os.chmod'), names.index('listener.listen'))

    def test_the_supervisor_never_touches_the_data_socket_file(self):
        # rpc.sock is the server's; the supervisor only hands its PATH on.
        self.assertNotIn('DATA_SOCKET_MODE', self.src)
        self.assertNotIn('DATA_SOCKET_NAME', self.src.replace(
            "os.path.join(ipc_dir, L.DATA_SOCKET_NAME)", ''))

    def test_paths_are_validated_before_any_write(self):
        """CODE_PATH_RE twin: validate_start refuses before a Run exists."""
        fn = _function(self.tree, '_handle_start', cls='Supervisor')
        names = _call_names(fn)
        self.assertLess(names.index('validate_start'), names.index('Run'))
        good = {'run_id': 'w1', 'token': '0' * 32, 'language': 'python',
                'files': {'main.py': 'print(1)\n'}, 'breakpoints': {}}
        self.assertIsNone(self.S.validate_start(good)[1])
        for bad_path in ('../main.py', '/main.py', 'a b.py', 'main.txt', 'x/../main.py',
                         'a/b/c/d/main.py', 'main.py\n', '1main.py'):
            files = {'main.py': '', bad_path: ''}
            clean, why = self.S.validate_start({**good, 'files': files})
            self.assertIsNone(clean, bad_path)
            self.assertIsNotNone(why, bad_path)

    def test_validate_start_refuses_every_other_bad_field(self):
        good = {'run_id': 'w1', 'token': '0' * 32, 'language': 'java',
                'files': {'Main.java': 'class Main {}\n'}, 'breakpoints': {}}
        self.assertIsNone(self.S.validate_start(good)[1])
        cases = {
            'run_id': {**good, 'run_id': ''},
            'long run_id': {**good, 'run_id': 'x' * 81},
            'token': {**good, 'token': 'zz'},
            'language': {**good, 'language': 'ruby'},
            'no files': {**good, 'files': {}},
            'too many': {**good, 'files': {f'F{i}.java': '' for i in range(33)}},
            'entry': {**good, 'files': {'Other.java': ''}},
            'ext': {**good, 'files': {'Main.java': '', 'x.py': ''}},
            'size': {**good, 'files': {'Main.java': 'x' * (self.L.MAX_CODE_FILE_BYTES + 1)}},
            'breakpoints': {**good, 'breakpoints': [1]},
        }
        for label, frame in cases.items():
            clean, why = self.S.validate_start(frame)
            self.assertIsNone(clean, label)
            self.assertTrue(why, label)

    def test_a_project_at_the_cap_passes_and_one_byte_over_is_refused(self):
        base = {'run_id': 'w1', 'token': '0' * 32, 'language': 'python', 'breakpoints': {}}
        # Umlaut filler: 60 000 bytes per file on the wire with the umlaut raw
        # (under the per-file cap); two of them leave the pad for main.py
        # under the per-file cap too.
        filler = 'ä' * 30000
        files = {'main.py': '', 'f0.py': filler, 'f1.py': filler}
        clean, why = self.S.validate_start({**base, 'files': files})
        self.assertIsNone(why)
        size = len(self.S.json.dumps(clean['files'], ensure_ascii=False).encode('utf-8'))
        pad = self.L.MAX_CODE_PROJECT_BYTES - size
        files['main.py'] = 'x' * pad
        clean, why = self.S.validate_start({**base, 'files': files})
        self.assertIsNone(why, 'exactly at the cap must pass')
        files['main.py'] = 'x' * (pad + 1)
        clean, why = self.S.validate_start({**base, 'files': files})
        self.assertEqual(why, 'project size')


class ControlBound(_Base):
    def test_supervisor_reads_control_frames_with_the_control_bound_and_data_never(self):
        reads = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == 'read_frame']
        writes = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == 'write_frame']
        self.assertGreaterEqual(len(reads), 2)
        self.assertGreaterEqual(len(writes), 2)
        for call in reads:
            self.assertEqual(ast.unparse(call.args[1]), 'L.CONTROL_MAX_FRAME_BYTES',
                             ast.unparse(call))
        for call in writes:
            self.assertEqual(ast.unparse(call.args[2]), 'L.CONTROL_MAX_FRAME_BYTES',
                             ast.unparse(call))
        names = {ast.unparse(n) for n in ast.walk(self.tree)
                 if isinstance(n, (ast.Name, ast.Attribute))}
        self.assertNotIn('L.MAX_FRAME_BYTES', names)
        self.assertNotIn('MAX_FRAME_BYTES', names)

    def test_control_max_frame_bytes_fits_a_project_at_the_cap_plus_the_envelope(self):
        self.assertGreaterEqual(self.L.CONTROL_MAX_FRAME_BYTES,
                                self.L.MAX_CODE_PROJECT_BYTES + 8192)

    def test_every_recv_and_sendall_carries_a_timeout(self):
        # Mirror of code_rpc's discipline: settimeout precedes each socket op.
        for name in ('_recv_exact', 'write_frame'):
            fn = _function(self.tree, name)
            names = _call_names(fn)
            first_op = next(i for i, n in enumerate(names)
                            if n.endswith('.recv_into') or n.endswith('.sendall'))
            self.assertIn('sock.settimeout', names[:first_op], name)

    def test_the_first_control_frame_is_bounded_in_time(self):
        fn = _function(self.tree, '_serve_connection', cls='Supervisor')
        self.assertIn('L.CONTROL_FIRST_FRAME_S', ast.unparse(fn))


class FramingRoundTrip(_Base):
    def test_a_frame_survives_a_socketpair_with_the_umlaut_raw(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.S.write_frame(a, {'ev': 'stdout', 'lines': ['hallo ü']}, self.L.CONTROL_MAX_FRAME_BYTES)
        self.assertEqual(self.S.read_frame(b, self.L.CONTROL_MAX_FRAME_BYTES),
                         {'ev': 'stdout', 'lines': ['hallo ü']})
        self.assertEqual(self.S.encode_frame_body({'a': 'ü'}), '{"a":"ü"}'.encode('utf-8'))

    def test_a_frame_over_the_bound_is_refused_before_its_body_is_read(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        with self.assertRaises(self.S.FrameTooLarge):
            self.S.write_frame(a, {'x': 'y' * 100}, 50)
        a.sendall((300).to_bytes(4, 'big'))
        with self.assertRaises(self.S.FrameTooLarge):
            self.S.read_frame(b, 100)

    def test_a_clean_eof_is_None_and_a_torn_frame_is_an_error(self):
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        a.close()
        self.assertIsNone(self.S.read_frame(b, 100))
        c, d = socket.socketpair()
        self.addCleanup(d.close)
        c.sendall((10).to_bytes(4, 'big') + b'abc')
        c.close()
        with self.assertRaises(self.S.FrameError):
            self.S.read_frame(d, 100)

    def test_a_non_object_body_is_an_error(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        body = b'[1,2]'
        a.sendall(len(body).to_bytes(4, 'big') + body)
        with self.assertRaises(self.S.FrameError):
            self.S.read_frame(b, 100)


class OutputPump(_Base):
    """The pump is driven with bytes directly — no process, no uid."""

    def _pump(self):
        emitted, drops = [], []
        pump = self.S._LinePump(None, emitted.extend, lambda: drops.append(1))
        return pump, emitted, drops

    def test_lines_are_split_and_decoded_with_replacement(self):
        pump, emitted, _ = self._pump()
        pump._feed('hallo ü\r\nzwei\n'.encode('utf-8') + b'\xff\n')
        self.assertEqual(emitted, ['hallo ü', 'zwei', '�'])

    def test_a_partial_line_is_kept_across_chunks(self):
        pump, emitted, _ = self._pump()
        pump._feed(b'hal')
        pump._feed(b'lo\n')
        self.assertEqual(emitted, ['hallo'])

    def test_an_overlong_line_is_cut_at_the_bound_and_the_rest_dropped(self):
        pump, emitted, _ = self._pump()
        pump._feed(b'a' * (self.L.STDOUT_MAX_LINE_BYTES + 5000) + b'\nnext\n')
        self.assertEqual(len(emitted), 2)
        self.assertTrue(emitted[0].endswith(' …'))
        self.assertLessEqual(len(emitted[0].encode('utf-8')), self.L.STDOUT_MAX_LINE_BYTES + 4)
        self.assertEqual(emitted[1], 'next')

    def test_the_per_second_budget_drops_the_surplus_and_reports_once(self):
        pump, emitted, drops = self._pump()
        many = b''.join(b'%d\n' % i for i in range(self.L.STDOUT_MAX_LINES_PER_S + 15))
        pump._feed(many)
        self.assertEqual(len(emitted), self.L.STDOUT_MAX_LINES_PER_S)
        self.assertEqual(len(drops), 1)
        pump._feed(b'more\n')
        self.assertEqual(len(emitted), self.L.STDOUT_MAX_LINES_PER_S)
        self.assertEqual(len(drops), 2, 'a full window drops every later line and says so')

    def test_the_drop_notice_is_german(self):
        import importlib.util as iu
        path = _REPO_ROOT / '.github' / 'scripts' / 'german_detail_lint.py'
        spec = iu.spec_from_file_location('gdl', path)
        gdl = iu.module_from_spec(spec)
        spec.loader.exec_module(gdl)
        notice = self.S._STDOUT_DROP_NOTICE_DE
        self.assertTrue(gdl.GERMAN_CHARS.search(notice) or gdl.GERMAN_WORDS.search(notice))
        self.assertIsNone(gdl.TRANSLITERATIONS.search(notice))

    def test_the_run_reports_the_notice_once_per_run(self):
        run = self.S.Run.__new__(self.S.Run)
        run._state_lock = threading.Lock()
        run._drop_notified = False
        run.run_id = 'r'
        sent = []
        run.send = sent.append
        run._on_drop()
        run._on_drop()
        self.assertEqual(sent, [{'ev': 'stdout', 'lines': [self.S._STDOUT_DROP_NOTICE_DE]}])


class JavacErrors(_Base):
    def test_the_first_javac_error_line_is_parsed_into_file_line_text(self):
        text = ('Main.java:3: error: \';\' expected\n'
                '        int x = ;\n'
                '                ^\n'
                'pkg/Helper.java:7: error: cannot find symbol\n'
                '2 errors\n')
        match = self.S._JAVAC_ERROR_RE.search(text)
        self.assertEqual((match.group('file'), int(match.group('line')), match.group('text')),
                         ('Main.java', 3, "';' expected"))


class ControlSurface(_Base):
    """A real supervisor on a temp-dir socket; no run (that needs uid 10001)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='edu-sup-')
        self.sup = self.S.Supervisor(self.tmp)
        self.sup.bind()
        self.thread = threading.Thread(target=self.sup.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.sup.shutdown)

    def _ask(self, frame, timeout=5.0):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(self.sup.socket_path)
        self.addCleanup(conn.close)
        self.S.write_frame(conn, frame, self.L.CONTROL_MAX_FRAME_BYTES)
        return self.S.read_frame(conn, self.L.CONTROL_MAX_FRAME_BYTES, timeout_s=timeout)

    def test_the_socket_file_carries_the_control_mode(self):
        self.assertEqual(os.stat(self.sup.socket_path).st_mode & 0o777, 0o600)

    def test_ping_answers_pong(self):
        self.assertEqual(self._ask({'ev': 'ping'}), {'ev': 'pong'})

    def test_kill_with_no_run_is_acknowledged(self):
        self.assertEqual(self._ask({'ev': 'kill', 'run_id': 'x'}), {'ev': 'killed', 'run_id': 'x'})

    def test_breakpoints_with_no_run_are_not_applied(self):
        self.assertEqual(self._ask({'ev': 'breakpoints', 'run_id': 'x', 'lines': {}}),
                         {'ev': 'breakpoints_set', 'run_id': 'x', 'applied': False})

    def test_a_malformed_start_is_refused_and_touches_nothing(self):
        reply = self._ask({'ev': 'start', 'run_id': 'x', 'token': 'bad',
                           'language': 'python', 'files': {'main.py': ''}})
        self.assertEqual(reply, {'ev': 'refused', 'reason': 'token'})
        self.assertEqual(os.listdir(self.tmp), [self.L.CONTROL_SOCKET_NAME])

    def test_an_unknown_event_is_refused(self):
        self.assertEqual(self._ask({'ev': 'dance'})['ev'], 'refused')

    def test_a_silent_connection_is_dropped_after_the_first_frame_bound(self):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self.L.CONTROL_FIRST_FRAME_S + 3)
        conn.connect(self.sup.socket_path)
        self.addCleanup(conn.close)
        t0 = time.monotonic()
        self.assertEqual(conn.recv(1), b'')
        self.assertLess(time.monotonic() - t0, self.L.CONTROL_FIRST_FRAME_S + 2)

    def test_a_stale_socket_file_is_replaced_on_bind(self):
        other = self.S.Supervisor(self.tmp)
        self.sup.shutdown()
        open(other.socket_path, 'w').close()
        other.bind()
        self.addCleanup(other.shutdown)
        self.assertTrue(os.path.exists(other.socket_path))


if __name__ == '__main__':
    unittest.main()
