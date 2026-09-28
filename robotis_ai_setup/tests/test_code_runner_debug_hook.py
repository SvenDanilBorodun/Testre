"""The Python debugger hook and the live values of the code runner
(``runner/lib/edubotics_debug.py``).

Three halves. The AST half pins the one call that makes a mid-run breakpoint
work at all: ``Hook.set_breakpoints`` must end in
``sys.monitoring.restart_events()`` (P13: a location the LINE callback has
DISABLEd never fires again without it), and the callback's unmarked branch
must ``return sys.monitoring.DISABLE`` (P12: that is what makes the hook
free). The behavioural half runs the hook on THIS interpreter (3.12+ has
``sys.monitoring``): a loop is started, the breakpoint is added while it
runs, and the hit count is asserted > 0 — and asserted == 0 once
``restart_events`` is replaced by a no-op, which is the red-before-green of
the whole mechanism.

The live-values half (owner decision R2-O1, 2026-09-27) runs a student
program through the REAL generated stub (``runner/lib/robot.py``) against a
fake robot on a real unix socket: the values are sent by the program's own
thread right before its robot calls and once at the end, never by the line
sampler, never by running student code, and a robot that refuses them costs
the program nothing. The sampler/``locals()`` fence only has teeth on
CPython 3.12 (PEP 667 made 3.13+ immune), which is the version CI and the
runner image use. Deliberately stdlib-only.
"""

import ast
import importlib.util
import json
import os
import pathlib
import runpy
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
_HOOK = _RUNNER / 'lib' / 'edubotics_debug.py'
_STUB = _RUNNER / 'lib' / 'robot.py'
_STUDENT_MAIN = _RUNNER / 'student_main.py'

_LOOP_SRC = (
    'import time\n'
    'deadline = time.monotonic() + 0.6\n'
    'x = 0\n'
    'while time.monotonic() < deadline:\n'
    '    x += 1\n'
)
_BODY_LINE = 5
_TEST_LINE = 4


def _load_hook():
    spec = importlib.util.spec_from_file_location('edubotics_debug', _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_student_main():
    """The launcher as a module (it imports only the stdlib at module level)."""
    spec = importlib.util.spec_from_file_location('edubotics_student_main', _STUDENT_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_stub():
    """A fresh copy of the generated stub module (its own `_rpc`)."""
    spec = importlib.util.spec_from_file_location('robot', _STUB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _method(tree, cls, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f'{cls}.{name} not found')


def _set_breakpoints_restarts_and_unmarked_returns_DISABLE(src: str) -> bool:
    """The checker the AST test applies to the shipped source AND to a
    mutated copy, so the test proves it can fail."""
    tree = ast.parse(src)
    setter = _method(tree, 'Hook', 'set_breakpoints')
    calls = [ast.unparse(n.func) for n in ast.walk(setter) if isinstance(n, ast.Call)]
    if 'sys.monitoring.restart_events' not in calls:
        return False
    on_line = _method(tree, 'Hook', 'on_line')
    returns = [ast.unparse(n.value) for n in ast.walk(on_line)
               if isinstance(n, ast.Return) and n.value is not None]
    return 'sys.monitoring.DISABLE' in returns


def _cost(value):
    """(nodes, chars) of a JSON tree, counted the way the server counts
    them (code_rpc.shown_values_exceed: every node, the characters of every
    string and every dict key)."""
    nodes, chars, stack = 0, 0, [value]
    while stack:
        item = stack.pop()
        nodes += 1
        if isinstance(item, str):
            chars += len(item)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            for k, v in item.items():
                chars += len(k)
                stack.append(v)
    return nodes, chars


class _FakeRpc:
    def __init__(self, replies=None):
        self.pauses = []
        self.replies = list(replies or [])
        self._lock = threading.RLock()

    def call(self, method, args, kind):
        if method == '__paused':
            self.pauses.append((args[0], args[1], args[2]))
            return self.replies.pop(0) if self.replies else 'continue'
        return None


class _RecordingRpc:
    """The stub's RPC surface the sampler uses: `_lock` + `call`."""

    def __init__(self):
        self._lock = threading.RLock()
        self.calls = []

    def call(self, method, args, kind):
        self.calls.append((method, args, kind))
        return None


class _FakeRobot:
    """The robot's data socket, faked: a unix socket that records every
    frame (monotonic time, method, args) and answers by `policy`; a method in
    `slow` takes that long, as a real motion does."""

    def __init__(self, path, policy=None, slow=None):
        self.path = path
        self.frames = []
        self.policy = policy or (lambda m, a: {'ok': True, 'r': None})
        self.slow = slow or {}
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn, n):
        buf = b''
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve(self, conn):
        with conn:
            while True:
                head = self._recv(conn, 4)
                if head is None:
                    return
                body = json.loads(self._recv(conn, struct.unpack('>I', head)[0]).decode('utf-8'))
                method, args = body.get('m'), body.get('a')
                self.frames.append((time.monotonic(), method, args))
                if method in self.slow:
                    time.sleep(self.slow[method])
                reply = dict(self.policy(method, args))
                reply['id'] = body.get('id')
                data = json.dumps(reply, ensure_ascii=False).encode('utf-8')
                conn.sendall(struct.pack('>I', len(data)) + data)

    def close(self):
        self._sock.close()

    def of(self, method):
        return [(t, a) for t, m, a in self.frames if m == method]


@unittest.skipUnless(hasattr(sys, 'monitoring'), 'needs sys.monitoring (3.12+)')
class HookBehaviour(unittest.TestCase):
    def setUp(self):
        self.dbg = _load_hook()
        self.tmp = tempfile.mkdtemp(prefix='edu-hook-')
        self.path = os.path.join(self.tmp, 'loop.py')
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write(_LOOP_SRC)
        self.hook = None

    def tearDown(self):
        if self.hook is not None:
            try:
                self.hook.uninstall()
            except Exception:  # noqa: BLE001 — teardown must not mask a failure
                pass

    def _run(self, rpc, add_after_s=0.15, lines=None):
        self.hook = self.dbg.Hook(rpc, self.tmp)
        self.hook.install()
        timer = threading.Timer(add_after_s, self.hook.set_breakpoints,
                                ({'loop.py': lines or [_BODY_LINE]},))
        timer.start()
        try:
            runpy.run_path(self.path, run_name='__main__')
        finally:
            timer.cancel()
            self.hook.uninstall()
            self.hook = None

    def test_a_breakpoint_added_mid_run_fires(self):
        rpc = _FakeRpc()
        self._run(rpc)
        self.assertGreater(len(rpc.pauses), 0)
        self.assertTrue(all(p[0] == 'loop.py' and p[1] == _BODY_LINE for p in rpc.pauses))

    def test_without_restart_events_the_same_breakpoint_never_fires(self):
        """The red half: DISABLEd locations stay dead without restart_events."""
        rpc = _FakeRpc()
        real = sys.monitoring.restart_events
        sys.monitoring.restart_events = lambda: None
        try:
            self._run(rpc)
        finally:
            sys.monitoring.restart_events = real
        self.assertEqual(len(rpc.pauses), 0)

    def test_the_locals_snapshot_rides_the_pause(self):
        rpc = _FakeRpc()
        self._run(rpc)
        snapshot = rpc.pauses[0][2]
        self.assertIn('x', snapshot)
        self.assertIsInstance(snapshot['x'], int)
        self.assertNotIn('__name__', snapshot)
        self.assertNotIn('time', snapshot, 'modules are skipped')

    def test_step_pauses_on_the_very_next_project_line(self):
        rpc = _FakeRpc(replies=['step', 'continue'])
        self._run(rpc)
        self.assertGreaterEqual(len(rpc.pauses), 2)
        self.assertEqual([p[1] for p in rpc.pauses[:2]], [_BODY_LINE, _TEST_LINE])

    def test_stop_unwinds_the_program(self):
        rpc = _FakeRpc(replies=['stop'])
        with self.assertRaises(self.dbg.StopRequested):
            self._run(rpc)
        self.assertEqual(len(rpc.pauses), 1)

    def test_only_the_main_thread_is_debugged(self):
        rpc = _FakeRpc()
        self.hook = self.dbg.Hook(rpc, self.tmp)
        self.hook.install()
        self.hook.set_breakpoints({'loop.py': [_BODY_LINE]})
        worker = threading.Thread(target=runpy.run_path, args=(self.path,),
                                  kwargs={'run_name': '__main__'})
        worker.start()
        worker.join()
        self.assertEqual(rpc.pauses, [])

    def test_a_breakpoint_outside_the_project_is_ignored(self):
        self.hook = self.dbg.Hook(_FakeRpc(), self.tmp)
        self.hook.set_breakpoints({'../etc/passwd': [1], 'loop.py': [0, -1, True, 'x']})
        self.assertEqual(self.hook._breakpoints, {})


class SnapshotBounds(unittest.TestCase):
    def setUp(self):
        self.dbg = _load_hook()

    def test_at_most_max_locals_and_every_value_bounded(self):
        frame_locals = {f'v{i}': i for i in range(40)}
        frame_locals['big'] = 'x' * 5000
        frame_locals['nested'] = [[list(range(200))] * 5] * 5
        frame_locals['__dunder__'] = 1

        class _Frame:
            f_locals = frame_locals

        out = self.dbg.snapshot_locals(_Frame(), 30)
        self.assertEqual(len(out), 30)
        self.assertNotIn('__dunder__', out)
        for value in out.values():
            self.assertLessEqual(len(self.dbg.json.dumps(value)),
                                 self.dbg.PAUSED_VALUE_MAX_CHARS + 2)

    def test_non_finite_floats_and_hostile_reprs_do_not_break_a_pause(self):
        class _Hostile:
            def __repr__(self):
                raise RuntimeError('nope')

        self.assertEqual(self.dbg._jsonable(float('inf')), 'inf')
        self.assertEqual(self.dbg._jsonable(_Hostile()), '<?>')

    def test_a_hostile_repr_inside_a_big_container_does_not_break_the_snapshot(self):
        """Restored (review round 3, MB2h; removed with the sampler in round
        2). A list too big for its structured rendering falls back to its
        cut repr — which calls every item's __repr__ — on the student's own
        stopped thread at a breakpoint. A raising one must cost the value,
        never the pause."""
        class _Hostile:
            def __repr__(self):
                raise RuntimeError('nope')

        class _Frame:
            f_locals = {'liste': ['x' * 900, 'y' * 900, _Hostile()], 'klein': 1}

        out = self.dbg.snapshot_locals(_Frame(), 30)
        self.assertEqual(out['liste'], '<?>')
        self.assertEqual(out['klein'], 1)

    def test_a_breakpoint_frame_always_fits_and_every_name_still_shows(self):
        """30 locals of 999 emoji each are 30 000 characters but ~120 KB of
        UTF-8: the `__paused` frame used to exceed MAX_FRAME_BYTES, and the
        stub raised „Der Aufruf ist zu groß" INTO the student's program at
        the breakpoint. The biggest now become SHOWN_TOO_BIG."""
        class _Frame:
            f_locals = {f'v{i}': '\U0001F600' * 999 for i in range(30)}

        out = self.dbg.snapshot_locals(_Frame(), 30)
        self.assertEqual(set(out), {f'v{i}' for i in range(30)})
        frame = json.dumps({'id': 1, 'm': '__paused', 'a': ['main.py', 3, out]},
                           ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.assertLess(len(frame), 65536)
        self.assertIn(self.dbg.SHOWN_TOO_BIG, out.values())


class VarsSnapshot(unittest.TestCase):
    """The module-level snapshot the live values send (owner decisions R-O1 +
    R2-O1): the innermost project frame's module globals, then the entry
    module's; never a function frame's `f_locals`; at most 30;
    modules/functions/classes/dunders skipped."""

    def setUp(self):
        self.dbg = _load_hook()
        self.tmp = tempfile.mkdtemp(prefix='edu-vars-')
        with open(os.path.join(self.tmp, 'helfer.py'), 'w', encoding='utf-8') as handle:
            handle.write('import sys\nbasis = 5\ndef tief():\n    wert = 9\n    return sys._getframe()\n')
        with open(os.path.join(self.tmp, 'main.py'), 'w', encoding='utf-8') as handle:
            handle.write('import sys\nimport helfer\nzaehler = 3\nname = "Robo"\n'
                         'def f():\n    lokal = 1\n    name = "innen"\n'
                         '    return sys._getframe(), helfer.tief()\n'
                         'class Kiste:\n    pass\n'
                         'FRAMES = f()\n')
        sys.path.insert(0, self.tmp)
        self.ns = runpy.run_path(os.path.join(self.tmp, 'main.py'), run_name='__main__')

    def tearDown(self):
        sys.path.remove(self.tmp)
        sys.modules.pop('helfer', None)

    def test_a_function_frame_shows_its_module_globals_and_never_its_locals(self):
        f_frame = self.ns['FRAMES'][0]
        snap = self.dbg.snapshot_vars(f_frame, self.tmp)
        self.assertNotIn('lokal', snap)
        self.assertEqual(snap['name'], 'Robo')           # the GLOBAL, not the local
        self.assertEqual(snap['zaehler'], 3)
        for skipped in ('sys', 'helfer', 'f', 'Kiste', '__name__'):
            self.assertNotIn(skipped, snap)

    def test_a_helper_module_frame_shows_its_module_and_the_entry_module(self):
        tief_frame = self.ns['FRAMES'][1]
        tief_frame.f_back  # noqa: B018 — a returned frame has no caller chain
        snap = self.dbg.snapshot_vars(tief_frame, self.tmp)
        self.assertNotIn('wert', snap)
        self.assertEqual(snap['basis'], 5)

    def test_at_most_max_locals_and_f_locals_is_never_read(self):
        class _Code:
            co_filename = os.path.join(self.tmp, 'main.py')
            co_name = 'g'

        class _Frame:
            f_code = _Code()
            f_globals = {f'g{i}': i for i in range(40)}
            f_back = None

            @property
            def f_locals(self):
                raise AssertionError('the live values must never read f_locals')

        snap = self.dbg.snapshot_vars(_Frame(), self.tmp, 30)
        self.assertEqual(len(snap), 30)
        self.assertEqual(list(snap)[:3], ['g0', 'g1', 'g2'])

    def test_a_frame_outside_the_project_has_no_variables(self):
        self.assertEqual(self.dbg.snapshot_vars(sys._getframe(), self.tmp), {})

    def test_fit_vars_cuts_the_biggest_values_until_the_frame_fits_and_keeps_every_name(self):
        snap = {f'v{i}': 'ü' * 1000 for i in range(30)}
        snap['klein'] = 1
        fitted = self.dbg.fit_vars(snap, 4000)
        self.assertEqual(set(fitted), set(snap), 'a name vanished')
        self.assertEqual(fitted['klein'], 1)
        self.assertLessEqual(
            len(json.dumps(fitted, ensure_ascii=False).encode('utf-8')), 4000)
        self.assertLess(self.dbg.VARS_FRAME_BUDGET_BYTES, 65536)

    def test_fit_vars_measures_each_entry_once(self):
        """Review round 2 (mi5): the old loop re-serialised the whole snapshot
        per dropped entry — O(n²), 0.2 s on the reviewer's 3.5 MB snapshot."""
        snap = {f'v{i}': 'x' * 5000 for i in range(30)}
        real = self.dbg.json.dumps
        calls = []

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        self.dbg.json.dumps = counting
        try:
            self.dbg.fit_vars(snap, 4000)
        finally:
            self.dbg.json.dumps = real
        self.assertLessEqual(len(calls), 2 * len(snap) + 2)

    def test_the_program_namespaces_at_the_end_come_from_the_traceback(self):
        path = os.path.join(self.tmp, 'boom.py')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('import helfer\npunkte = 7\ndef f():\n    raise ValueError(1)\nf()\n')
        try:
            runpy.run_path(path, run_name='__main__')
        except ValueError as exc:
            spaces = self.dbg.project_namespaces(exc.__traceback__, self.tmp)
        self.assertEqual(len(spaces), 2)
        self.assertEqual(self.dbg.snapshot_namespaces(spaces)['punkte'], 7)
        self.assertEqual(self.dbg.project_namespaces(None, self.tmp), [])


class SafeRendererBounds(unittest.TestCase):
    """What a live value may cost: counted the way the server counts it, so
    the runner's worst case provably fits the server's per-frame caps (review
    round 2, mi4/mi5: 197 emitted nodes against a 100-node budget made the
    server drop whole frames; unbudgeted dict keys made a 3.5 MB snapshot)."""

    def setUp(self):
        self.dbg = _load_hook()

    def _shapes(self):
        return {
            'cube': [[[k for k in range(50)] for _ in range(50)] for _ in range(50)],
            'grid': [[0.5] * 50 for _ in range(50)],
            'wide': list(range(10_000)),
            'long_keys': {('k%03d' % j) + 'x' * 995: {('q%03d' % m) + 'y' * 995: m
                                                       for m in range(2)} for j in range(50)},
            'deep': [[[[1]]]],
            'mixed': {'a': [1, {'b': ['x' * 900, 2.5, float('nan')]}], 2: None, True: 's'},
            'text': 'ä' * 5_000_000,
            'sets': {frozenset({1, 2}), (1, 2)},
            'ints': [2 ** 300, -1, 2 ** 255],
            'empty': [[], {}, ()],
        }

    def test_every_rendered_value_stays_within_both_budgets(self):
        for name, value in self._shapes().items():
            nodes, chars = _cost(self.dbg.safe_render(value))
            self.assertLessEqual(nodes, self.dbg.LIVE_VALUE_MAX_NODES, name)
            self.assertLessEqual(chars, self.dbg.LIVE_VALUE_MAX_CHARS, name)

    def test_a_cut_container_says_so(self):
        out = self.dbg.safe_render(list(range(10_000)))
        self.assertEqual(out[-1], '…')
        self.assertEqual(out[:3], [0, 1, 2])
        self.assertEqual(self.dbg.safe_render([1, 2, 3]), [1, 2, 3])

    def test_the_renderer_walks_three_levels_and_names_the_rest(self):
        render = self.dbg.safe_render
        self.assertEqual(render([[[[1]]]]), [[['<list>']]])
        self.assertEqual(render(float('nan')), 'nan')
        self.assertEqual(render(float('inf')), 'inf')
        self.assertIs(render(True), True)
        # A dict whose entry cannot be shown is not shown whole: it says so
        # (review round 5, nd6 — it used to read as the empty dict below).
        self.assertEqual(render({(1, 2): 'tupel-schlüssel', 'k': 1}), {'k': 1, '…': None})
        self.assertEqual(render({(1, 2): 'nur ein Tupel'}), {'…': None})
        self.assertEqual(render({}), {})
        self.assertEqual(render(2 ** 300), '<int>')

    def test_a_lone_surrogate_never_makes_the_frame_unsendable(self):
        out = self.dbg.safe_render({'\ud800k': ['a\udfffb']})
        json.dumps(out, ensure_ascii=False).encode('utf-8')   # raises on a surrogate
        self.assertEqual(out, {'?k': ['a?b']})

    def test_thirty_worst_case_values_fit_the_frame_caps_the_server_derives(self):
        """robot_api derives SHOWN_FRAME_MAX_NODES / _CHARS from the same
        numbers (PAUSED_MAX_LOCALS × the per-value bounds): 30 values rendered
        at their worst never reach them — asserted here on the runner's side,
        the arithmetic on the server's (test_code_rpc_zeige_vars)."""
        values = [self.dbg.safe_render(v) for v in list(self._shapes().values()) * 3]
        nodes = sum(_cost(v)[0] for v in values)
        chars = sum(_cost(v)[1] for v in values)
        self.assertLessEqual(nodes, self.dbg.PAUSED_MAX_LOCALS * self.dbg.LIVE_VALUE_MAX_NODES)
        self.assertLessEqual(chars, self.dbg.PAUSED_MAX_LOCALS * self.dbg.LIVE_VALUE_MAX_CHARS)


_NEVER_RUN_SRC = (
    'import robot\n'
    'class _Meta(type):\n'
    '    zaehler = 0\n'
    '    @property\n'
    '    def __name__(cls):\n'
    '        _Meta.zaehler += 1\n'
    '        return "Falsch"\n'
    '    def __eq__(cls, other):\n'
    '        _Meta.zaehler += 1\n'
    '        return False\n'
    '    __hash__ = type.__hash__\n'
    'class Laut(metaclass=_Meta):\n'
    '    aufrufe = 0\n'
    '    def _merk(self):\n'
    '        Laut.aufrufe += 1\n'
    '    def __repr__(self):\n'
    '        self._merk()\n'
    '        return "Laut"\n'
    '    def __str__(self):\n'
    '        self._merk()\n'
    '        return "Laut"\n'
    '    def __format__(self, spec):\n'
    '        self._merk()\n'
    '        return "Laut"\n'
    '    def __iter__(self):\n'
    '        self._merk()\n'
    '        return iter(())\n'
    '    def __len__(self):\n'
    '        self._merk()\n'
    '        return 0\n'
    '    def __eq__(self, other):\n'
    '        self._merk()\n'
    '        return False\n'
    '    def __hash__(self):\n'
    '        return 7\n'
    '    def __getattr__(self, name):\n'
    '        self._merk()\n'
    '        raise AttributeError(name)\n'
    'class MeinText(str):\n'
    '    def __repr__(self):\n'
    '        Laut.aufrufe += 1\n'
    '        return "x"\n'
    'class MeineListe(list):\n'
    '    def __iter__(self):\n'
    '        Laut.aufrufe += 1\n'
    '        return iter([])\n'
    'class Ding:\n'
    '    pass\n'
    'class _Name(str):\n'
    '    def __format__(self, spec):\n'
    '        Laut.aufrufe += 1\n'
    '        return "X"\n'
    '    def __add__(self, other):\n'
    '        Laut.aufrufe += 1\n'
    '        return "X"\n'
    'Ding.__name__ = _Name("Ding")\n'
    'ding = Laut()\n'
    'klasse_als_wert = Laut\n'
    'benannt = Ding()\n'
    'liste = [1, Laut(), "a"]\n'
    'zuordnung = {"a": 1, 2: "b", Laut(): 3, True: None}\n'
    'text = MeinText("hallo")\n'
    'eigen = MeineListe([1, 2])\n'
    'riesig = 2 ** 5000\n'
    'def arbeite():\n'
    '    lokal = Laut()\n'
    '    robot.move_to("A")\n'
    '    robot.move_to("B")\n'
    '    return lokal\n'
    'arbeite()\n'
    'robot.home()\n'
    'ERGEBNIS = (Laut.aufrufe, _Meta.zaehler)\n'
)

_LOCALS_LOOP_SRC = (
    'import robot\n'
    'def f():\n'
    '    a = 1\n'
    '    b = 2\n'
    '    seen = []\n'
    '    for k in locals():\n'
    '        seen.append(k)\n'
    '        robot.move_to("A")\n'
    '    return seen\n'
    'try:\n'
    '    ERGEBNIS = ("ok", f())\n'
    'except RuntimeError as exc:\n'
    '    ERGEBNIS = ("RuntimeError", str(exc))\n'
)


class _StubRun:
    """A student program run through the REAL stub against a fake robot,
    with the live values wired exactly as student_main wires them."""

    def __init__(self, test, src, *, policy=None, slow=None, live=True,
                 sampler_interval_s=None, interval_s=None):
        self.dbg = _load_hook()
        self.tmp = tempfile.mkdtemp(prefix='edu-live-')
        with open(os.path.join(self.tmp, 'main.py'), 'w', encoding='utf-8') as handle:
            handle.write(src)
        self.robot_srv = _FakeRobot(os.path.join(self.tmp, 'rpc.sock'), policy, slow)
        test.addCleanup(self.robot_srv.close)
        self.stub = _load_stub()
        self.stub._rpc.connect(self.robot_srv.path, 'a' * 32, project_root=self.tmp)
        test.addCleanup(self.stub._rpc.close)
        self.stop = threading.Event()
        test.addCleanup(self.stop.set)
        if sampler_interval_s is not None:
            self.dbg.start_line_sampler(self.stub._rpc, self.tmp,
                                        interval_s=sampler_interval_s, stop=self.stop)
        self.live = None
        if live:
            kw = {} if interval_s is None else {'interval_s': interval_s}
            self.live = self.dbg.LiveValues(self.stub._rpc, self.tmp,
                                            exclude=vars(self.stub), **kw)
            self.stub._rpc.before_call = self.live.before_call

    def run(self):
        saved = sys.modules.get('robot')
        sys.modules['robot'] = self.stub
        try:
            ns = runpy.run_path(os.path.join(self.tmp, 'main.py'), run_name='__main__')
        finally:
            if saved is None:
                sys.modules.pop('robot', None)
            else:
                sys.modules['robot'] = saved
        if self.live is not None:
            self.stub._rpc.before_call = None
            self.live.final([ns], ('main.py', 0))
        self.stop.set()
        return ns


_BUSY_SRC = (
    'import robot\n'
    'punkte = 0\n'
    'for i in range(6):\n'
    '    robot.move_to("A")\n'
    '    punkte += 1\n'
)


class LiveValuesThroughTheStub(unittest.TestCase):
    """Owner decision R2-O1. Review round 2 (MA2) measured the sampler design
    sending ZERO `__vars` frames in 4 s of a program that spends its time in
    robot calls (the sampler waited for a lock every call holds), and the
    last values were never sent at all."""

    def test_values_arrive_between_robot_calls_at_most_twice_a_second_and_the_last_at_the_end(self):
        run = _StubRun(self, _BUSY_SRC, slow={'move_to': 0.3})
        run.run()
        frames = run.robot_srv.frames
        sent = run.robot_srv.of('__vars')
        values = [a[2]['punkte'] for _t, a in sent]
        self.assertEqual(values, sorted(set(values)), 'values must only grow')
        self.assertEqual(values[-1], 6, 'the last value arrives at the end')
        last_move = max(i for i, f in enumerate(frames) if f[1] == 'move_to')
        between = [i for i, f in enumerate(frames) if f[1] == '__vars' and i < last_move]
        self.assertGreaterEqual(len(between), 3, 'values arrive between the calls')
        times = [t for t, _a in sent]
        for before, after in zip(times, times[1:]):
            self.assertGreaterEqual(after - before, 0.45, 'at most 2 frames per second')
        moves = run.robot_srv.of('move_to')
        self.assertEqual([a for _t, a in moves], [['A']] * 6, 'the robot calls are unchanged')

    def test_the_line_sampler_never_sends_values(self):
        run = _StubRun(self, _BUSY_SRC, slow={'move_to': 0.05}, live=False,
                       sampler_interval_s=0.01)
        run.run()
        self.assertEqual(run.robot_srv.of('__vars'), [])

    def test_a_refused_vars_switches_the_values_off_and_nothing_else(self):
        """A robot that does not know `__vars` (a runner newer than the
        server — review round 2, mi8): no exception in the program, the robot
        calls unchanged, one attempt only, and the line highlight goes on."""
        src = ('import robot, time\npunkte = 0\nfor i in range(6):\n'
               '    robot.move_to("A")\n    punkte += 1\n'
               '    ende = time.monotonic() + 0.1\n'
               '    while time.monotonic() < ende:\n'
               '        pass\n'
               'ERGEBNIS = punkte\n')

        def refuse_vars(method, _args):
            if method == '__vars':
                return {'ok': False, 'k': 'method', 'e': 'robot.__vars gibt es nicht.'}
            return {'ok': True, 'r': None}
        run = _StubRun(self, src, policy=refuse_vars, sampler_interval_s=0.02)
        ns = run.run()
        self.assertEqual(ns['ERGEBNIS'], 6)
        self.assertFalse(run.live.enabled)
        methods = [m for _t, m, _a in run.robot_srv.frames]
        self.assertEqual(methods.count('__vars'), 1)
        self.assertEqual(methods.count('move_to'), 6)
        first_refusal = methods.index('__vars')
        self.assertIn('__line', methods[first_refusal:], 'the highlight must go on')

    def test_a_skipped_frame_is_sent_again(self):
        """The server's floor answers VARS_REPLY_SKIPPED without looking: the
        runner, told so, sends the same values at its next chance."""
        answers = iter([{'ok': True, 'r': 'skipped'}])

        def policy(method, _args):
            if method == '__vars':
                return next(answers, {'ok': True, 'r': None})
            return {'ok': True, 'r': None}
        src = ('import robot\npunkte = 1\ndef f():\n    for _ in range(4):\n'
               '        robot.move_to("A")\nf()\n')
        run = _StubRun(self, src, policy=policy, slow={'move_to': 0.3})
        run.run()
        payloads = [a[2] for _t, a in run.robot_srv.of('__vars')]
        self.assertEqual(payloads, [{'punkte': 1}, {'punkte': 1}])

    def test_no_student_method_runs_and_every_other_value_is_its_class_name(self):
        run = _StubRun(self, _NEVER_RUN_SRC, interval_s=0.0)
        ns = run.run()
        self.assertEqual(ns['ERGEBNIS'], (0, 0), 'the live values ran student code')
        sent = [a[2] for _t, a in run.robot_srv.of('__vars')]
        self.assertTrue(sent)
        last = sent[-1]
        self.assertEqual(last['ding'], '<Laut>')          # the real name, not the metaclass's
        self.assertEqual(last['benannt'], '<?>')          # a str-subclass name is not asked
        self.assertEqual(last['liste'], [1, '<Laut>', 'a'])
        # `Laut()` as a key is not shown, so the dict ends in '…' (nd6).
        self.assertEqual(last['zuordnung'], {'a': 1, '2': 'b', 'True': None, '…': None})
        self.assertEqual(last['text'], '<MeinText>')
        self.assertEqual(last['eigen'], '<MeineListe>')
        self.assertEqual(last['riesig'], '<int>')
        self.assertNotIn('klasse_als_wert', last)          # a class is not a value
        self.assertNotIn('lokal', {k for snap in sent for k in snap})

    def test_a_file_name_that_is_a_str_subclass_is_never_asked(self):
        """Review round 2 (mi1): a code object compiled with a `str`
        subclass as its file name — project_relpath called its startswith."""
        src = ('import robot, os\n'
               'SEITE = []\n'
               'class S(str):\n'
               '    def startswith(self, *a):\n'
               '        SEITE.append("startswith")\n'
               '        return str.startswith(self, *a)\n'
               '    def __getitem__(self, k):\n'
               '        SEITE.append("getitem")\n'
               '        return str.__getitem__(self, k)\n'
               'fake = S(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake.py"))\n'
               'ns = {"robot": robot}\n'
               'exec(compile("def ruf():\\n    robot.move_to(\\"A\\")\\n", fake, "exec"), ns)\n'
               'ns["ruf"]()\n'
               'ns["ruf"]()\n'
               'ERGEBNIS = list(SEITE)\n')
        run = _StubRun(self, src, interval_s=0.0, sampler_interval_s=0.01)
        ns = run.run()
        self.assertEqual(ns['ERGEBNIS'], [])

    def test_iterating_locals_in_a_function_is_unaffected(self):
        run = _StubRun(self, _LOCALS_LOOP_SRC, interval_s=0.0, sampler_interval_s=0.005)
        ns = run.run()
        self.assertEqual(ns['ERGEBNIS'], ('ok', ['a', 'b', 'seen']))
        without = _StubRun(self, _LOCALS_LOOP_SRC, live=False)
        self.assertEqual(without.run()['ERGEBNIS'], ns['ERGEBNIS'])

    def test_the_robot_librarys_own_names_are_not_the_programs(self):
        """`from robot import *` binds MAX_CALLS_PER_S, BURST, … (and
        `annotations`) in main.py: the very objects of the library, under the
        same names — not variables of the program (review round 2, ni2)."""
        src = 'from robot import *\npunkte = 1\nBURST = 7\nmove_to("A")\n'
        run = _StubRun(self, src, interval_s=0.0)
        run.run()
        last = run.robot_srv.of('__vars')[-1][1][2]
        self.assertEqual(last, {'punkte': 1, 'BURST': 7})

    def test_the_final_values_arrive_even_without_a_robot_call(self):
        run = _StubRun(self, 'punkte = 41\npunkte += 1\n')
        run.run()
        self.assertEqual([a[2] for _t, a in run.robot_srv.of('__vars')], [{'punkte': 42}])

    def test_thirty_long_texts_still_show_every_name_before_the_call(self):
        """Review round 3 (MB2e): `before_call` trims its snapshot with
        fit_vars. Thirty globals of 1000 „€" are ~90 KB of UTF-8 — past the
        stub's 64 KiB frame bound — so without the trim the stub refuses the
        frame, the values switch off for the run, and not one arrives. The
        frame that goes out BEFORE the robot call must carry all thirty
        names, the largest as SHOWN_TOO_BIG."""
        names = [f'text{i:02d}' for i in range(30)]
        src = 'import robot\n' + ''.join(f'{n} = "€" * 1000\n' for n in names) + 'robot.move_to("A")\n'
        run = _StubRun(self, src)
        run.run()
        frames = run.robot_srv.frames
        first_vars = next(i for i, f in enumerate(frames) if f[1] == '__vars')
        first_move = next(i for i, f in enumerate(frames) if f[1] == 'move_to')
        self.assertLess(first_vars, first_move, 'the values ride BEFORE the robot call')
        shown = frames[first_vars][2][2]
        self.assertEqual(sorted(shown), names, 'every name still shows')
        self.assertIn(run.dbg.SHOWN_TOO_BIG, shown.values())
        self.assertIn('€' * 1000, shown.values(), 'what fits is shown whole')
        self.assertTrue(run.live.enabled)

    def test_another_threads_check_in_flight_neither_holds_the_end_nor_loses_the_last_values(self):
        """Review round 5 (md7). Round 3 (nb7) made `final` wait at most one
        interval for another thread's check and then SKIP the last values —
        which were lost whenever a worker thread was busy. A check can send
        only under the stub's RPC lock, which the launcher holds across
        `final` and `__exit`, so `final` no longer waits for it at all."""
        dbg = _load_hook()
        rpc = _RecordingRpc()
        tmp = tempfile.mkdtemp(prefix='edu-final-')
        live = dbg.LiveValues(rpc, tmp, interval_s=0.3)
        held = threading.Event()
        release = threading.Event()

        def other_thread_mid_check():
            with live._busy:
                held.set()
                release.wait(5.0)
        worker = threading.Thread(target=other_thread_mid_check, daemon=True)
        worker.start()
        self.assertTrue(held.wait(2.0))
        t0 = time.monotonic()
        live.final([{'punkte': 3}], ('main.py', 0))
        elapsed = time.monotonic() - t0
        release.set()
        worker.join(2.0)
        self.assertLess(elapsed, 0.2, 'no wait for the other check')
        self.assertEqual([c[1][2] for c in rpc.calls if c[0] == '__vars'], [{'punkte': 3}])

    def test_the_final_send_waits_out_the_servers_floor_after_a_frame_that_just_went_out(self):
        """A `__vars` frame that went out less than one interval ago is waited
        out (within that interval), so the server's floor looks at the last
        values instead of answering `skipped`."""
        dbg = _load_hook()
        rpc = _RecordingRpc()
        live = dbg.LiveValues(rpc, tempfile.mkdtemp(prefix='edu-final-'), interval_s=0.4)
        live._last_send = time.monotonic()
        t0 = time.monotonic()
        live.final([{'punkte': 3}], ('main.py', 0))
        elapsed = time.monotonic() - t0
        self.assertGreater(elapsed, 0.3, 'the floor is waited out')
        self.assertLess(elapsed, 0.4 + 0.15, 'at most one interval')
        self.assertEqual([c[1][2] for c in rpc.calls if c[0] == '__vars'], [{'punkte': 3}])
        # No frame went out lately: nothing to wait for.
        rpc.calls.clear()
        live._last_send = float('-inf')
        t0 = time.monotonic()
        live.final([{'punkte': 4}], ('main.py', 0))
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertEqual([c[1][2] for c in rpc.calls if c[0] == '__vars'], [{'punkte': 4}])


class TheLastValuesAndExitShareOneHoldOfTheRpcLock(unittest.TestCase):
    """Review round 5 (md7). Round 4 (mc3) bounded `final`'s wait for the
    stub's RPC lock and SKIPPED the last values when another thread held it
    — while `__exit` then waited for the same lock without a bound anyway,
    so a program with a worker thread ended showing stale values. The
    launcher now takes the lock ONCE (the wait `__exit` always had) and
    sends both frames inside it (`student_main.finish_run`): the values
    arrive, nothing comes between them, and the end waits no longer than
    `__exit` alone did."""

    class _DepthLock:
        """An RLock that knows how deep its owner holds it."""

        def __init__(self):
            self._lock = threading.RLock()
            self.depth = 0

        def acquire(self, blocking=True, timeout=-1):
            ok = self._lock.acquire(blocking, timeout)
            if ok:
                self.depth += 1
            return ok

        def release(self):
            self.depth -= 1
            self._lock.release()

        __enter__ = acquire

        def __exit__(self, *exc):
            self.release()

    class _LockingRpc:
        """The stub's surface as `call` really uses it: under `_lock`; each
        call records how deep the caller held the lock."""

        def __init__(self, lock=None):
            self._lock = lock if lock is not None else threading.RLock()
            self.before_call = object()
            self.calls = []

        def call(self, method, args, kind):
            with self._lock:
                depth = getattr(self._lock, 'depth', None)
                self.calls.append((method, args, kind, depth))
            return None

    def test_both_frames_go_out_inside_one_outer_hold(self):
        dbg = _load_hook()
        sm = _load_student_main()
        rpc = self._LockingRpc(self._DepthLock())
        live = dbg.LiveValues(rpc, tempfile.mkdtemp(prefix='edu-final-'), interval_s=0.3)
        sm.finish_run(rpc, live, [{'punkte': 3}], {'kind': 'ok'}, RuntimeError)
        self.assertEqual([m for m, *_ in rpc.calls], ['__vars', '__exit'])
        # Both inside the launcher's OUTER hold (depth ≥ 2 at the call): the
        # lock was never released between them, so no other call came between.
        self.assertTrue(all(d >= 2 for *_x, d in rpc.calls), rpc.calls)
        self.assertEqual(rpc.calls[0][1][2], {'punkte': 3})
        self.assertIsNone(rpc.before_call, 'the hook is unwired inside the same hold')

    def test_a_thread_inside_a_long_call_delays_the_end_only_as_long_as_exit_waited_and_the_values_arrive(self):
        dbg = _load_hook()
        sm = _load_student_main()
        rpc = self._LockingRpc()
        live = dbg.LiveValues(rpc, tempfile.mkdtemp(prefix='edu-final-'), interval_s=0.3)
        held = threading.Event()
        done = []

        def long_robot_call():
            with rpc._lock:
                held.set()
                time.sleep(0.6)
                done.append(time.monotonic())
        worker = threading.Thread(target=long_robot_call, daemon=True)
        worker.start()
        self.assertTrue(held.wait(2.0))
        sm.finish_run(rpc, live, [{'punkte': 3}], {'kind': 'ok'}, RuntimeError)
        ended = time.monotonic()
        worker.join(2.0)
        self.assertEqual([m for m, *_ in rpc.calls], ['__vars', '__exit'])
        self.assertEqual(rpc.calls[0][1][2], {'punkte': 3}, 'the last values arrive')
        # The end is the long call's end plus the two frames — the wait
        # `__exit` always had, and nothing on top of it.
        self.assertLess(ended - done[0], 0.15)

    def test_a_free_lock_still_sends_the_last_values(self):
        dbg = _load_hook()
        rpc = self._LockingRpc()
        live = dbg.LiveValues(rpc, tempfile.mkdtemp(prefix='edu-final-'), interval_s=0.3)
        live.final([{'punkte': 3}], ('main.py', 0))
        self.assertEqual([c[1][2] for c in rpc.calls if c[0] == '__vars'], [{'punkte': 3}])

    def test_a_failing_exit_is_written_never_raised(self):
        sm = _load_student_main()

        class _Refusing(self._LockingRpc):
            def call(self, method, args, kind):
                raise RuntimeError('weg')
        rpc = _Refusing()
        sm.finish_run(rpc, None, [], {'kind': 'ok'}, RuntimeError)
        self.assertIsNone(rpc.before_call)


class TheLastValuesComeFromEveryProjectModule(unittest.TestCase):
    """Review round 5 (md7, 5-B n3): the last values were read from
    `main.py`'s module alone, so a value a helper module kept never
    arrived."""

    def setUp(self):
        self.dbg = _load_hook()
        self.tmp = tempfile.mkdtemp(prefix='edu-mods-')

    def _module(self, name, path, **values):
        import types
        mod = types.ModuleType(name)
        mod.__file__ = path
        for k, v in values.items():
            setattr(mod, k, v)
        return mod

    def test_project_modules_are_found_sorted_and_nothing_else(self):
        helper = self._module('helfer', os.path.join(self.tmp, 'helfer.py'), modulwert=3)
        deep = self._module('pkg.werte', os.path.join(self.tmp, 'pkg', 'werte.py'), tief=1)
        outside = self._module('fremd', '/usr/lib/python3/fremd.py', nein=1)
        nofile = self._module('ohne', None)
        del nofile.__dict__['__file__']
        mods = {'pkg.werte': deep, 'fremd': outside, 'helfer': helper, 'ohne': nofile, 'sys': sys}
        spaces = self.dbg.project_module_namespaces(self.tmp, mods)
        self.assertEqual(spaces, [vars(helper), vars(deep)])
        snap = self.dbg.snapshot_namespaces([{'haupt': 2}] + spaces)
        self.assertEqual(snap, {'haupt': 2, 'modulwert': 3, 'tief': 1})

    def test_no_student_code_runs_while_the_modules_are_read(self):
        import types
        calls = []

        class Swapped(types.ModuleType):
            def __getattribute__(self, name):
                calls.append(name)
                return types.ModuleType.__getattribute__(self, name)
        swapped = self._module('getauscht', os.path.join(self.tmp, 'getauscht.py'), x=1)
        swapped.__class__ = Swapped

        target = hash('__file__')

        class Colliding:
            def __hash__(self):
                return target

            def __eq__(self, other):
                calls.append('eq')
                return False
        odd = types.ModuleType('kollision')
        odd.__dict__.clear()
        odd.__dict__[Colliding()] = 1
        # Inserting `__file__` after the colliding key compares the two once
        # (here, on the test's own account); a LOOKUP would do so again.
        odd.__dict__['__file__'] = os.path.join(self.tmp, 'kollision.py')
        odd.__dict__['wert'] = 5
        calls.clear()

        class Mods(dict):
            def items(self):
                calls.append('items')
                return dict.items(self)
        spaces = self.dbg.project_module_namespaces(self.tmp, {'getauscht': swapped, 'kollision': odd})
        self.assertEqual(spaces, [vars(odd)], 'the swapped module is skipped, unread')
        self.assertEqual(self.dbg.project_module_namespaces(self.tmp, Mods(kollision=odd)), [])
        self.assertEqual(calls, [])

    def test_the_real_sys_modules_is_read_by_default(self):
        mod = self._module('edu_test_helfer_md7', os.path.join(self.tmp, 'helfer.py'), w=1)
        sys.modules['edu_test_helfer_md7'] = mod
        self.addCleanup(sys.modules.pop, 'edu_test_helfer_md7', None)
        self.assertEqual(self.dbg.project_module_namespaces(self.tmp), [vars(mod)])


@unittest.skipUnless(hasattr(sys, 'monitoring'), 'needs sys.monitoring (3.12+)')
class TheLauncherEndsARunWithTheLastValues(unittest.TestCase):
    """The launcher itself, run as the runner runs it (`python3 -I -u
    student_main.py <run_dir>`) against a fake robot on a real socket — with
    its own rlimits switched off, which a test host cannot take. A worker
    thread loops robot calls while `main.py` ends, and a helper module keeps
    a value: the last values of both modules arrive, and `__exit` is the
    very next frame after them (review round 5, md7)."""

    def _runner_copy(self):
        import shutil
        dst = pathlib.Path(tempfile.mkdtemp(prefix='edu-runner-'))
        shutil.copy(_STUDENT_MAIN, dst / 'student_main.py')
        shutil.copytree(_RUNNER / 'lib', dst / 'lib',
                        ignore=shutil.ignore_patterns('__pycache__'))
        limits = (_RUNNER / 'runner_limits.py').read_text(encoding='utf-8')
        head = 'def apply_rlimits(language: str) -> None:\n'
        self.assertIn(head, limits)
        limits = limits.replace(head, head + '    return  # a test host keeps its limits\n', 1)
        (dst / 'runner_limits.py').write_text(limits, encoding='utf-8')
        return dst

    def test_a_worker_thread_and_a_helper_module(self):
        runner = self._runner_copy()
        run_dir = tempfile.mkdtemp(prefix='edu-run-')
        with open(os.path.join(run_dir, 'main.py'), 'w', encoding='utf-8') as handle:
            handle.write('import robot, threading, time, helfer\n'
                         'wert = 0\n'
                         'def arbeiter():\n'
                         '    for _ in range(40):\n'
                         '        robot.wait(0.02)\n'
                         'threading.Thread(target=arbeiter, daemon=True).start()\n'
                         'helfer.lauf()\n'
                         'time.sleep(0.2)\n'
                         'wert = -1\n')
        with open(os.path.join(run_dir, 'helfer.py'), 'w', encoding='utf-8') as handle:
            handle.write('import robot\n'
                         'modulwert = 0\n'
                         'def lauf():\n'
                         '    global modulwert\n'
                         '    for i in range(3):\n'
                         '        modulwert = i\n'
                         '        robot.wait(0.05)\n'
                         '    modulwert = 99\n')
        sock_dir = tempfile.mkdtemp(prefix='edu-sock-')
        robot_srv = _FakeRobot(os.path.join(sock_dir, 'rpc.sock'), slow={'wait': 0.02})
        self.addCleanup(robot_srv.close)
        env = dict(os.environ, CODE_RPC_SOCKET=robot_srv.path, CODE_RUN_TOKEN='a' * 32)
        import subprocess
        proc = subprocess.run([sys.executable, '-I', '-u', str(runner / 'student_main.py'), run_dir],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        methods = [m for _t, m, _a in robot_srv.frames]
        # (The worker's call waiting for the lock may still reach the socket
        # after __exit; the robot has retired the run's token by then.)
        self.assertEqual(methods.count('__exit'), 1)
        end = methods.index('__exit')
        self.assertEqual(methods[end - 1], '__vars', 'the last values come right before __exit')
        last = robot_srv.frames[end - 1][2][2]
        self.assertEqual(last.get('wert'), -1, 'main.py\'s last value')
        self.assertEqual(last.get('modulwert'), 99, 'the helper module\'s last value')


class SamplerSendsOnlyTheLine(unittest.TestCase):
    """R2-O1: the background sampler is back to its v2.22.0 job, the `__line`
    highlight. With no cross-thread value reads left, what keeps it that way
    is this fence: the sampler's functions never name a frame's variables."""

    _SAMPLER_FUNCS = ('start_line_sampler', '_innermost_project_position', 'project_relpath')

    def _sampler_source_names(self, src):
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self._SAMPLER_FUNCS:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute):
                        names.add(sub.attr)
                    elif isinstance(sub, ast.Name):
                        names.add(sub.id)
        return names

    def test_the_sampler_never_touches_a_frames_variables(self):
        src = _HOOK.read_text(encoding='utf-8')
        names = self._sampler_source_names(src)
        for forbidden in ('f_locals', 'f_globals', 'snapshot_vars', 'safe_render',
                          'LiveValues', '__vars'):
            self.assertNotIn(forbidden, names)
        # The fence can fail: the round-1 sampler read f_globals.
        mutated = src.replace('pos = _innermost_project_position(frame, root)',
                              'pos = _innermost_project_position(frame, root)\n'
                              '            _g = frame.f_globals', 1)
        self.assertNotEqual(mutated, src)
        self.assertIn('f_globals', self._sampler_source_names(mutated))

    def test_the_sampler_sends_line_only(self):
        dbg = _load_hook()
        tmp = tempfile.mkdtemp(prefix='edu-line-')
        path = os.path.join(tmp, 'main.py')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('import time\nzaehler = 0\nende = time.monotonic() + 0.3\n'
                         'while time.monotonic() < ende:\n    zaehler += 1\n')
        rpc = _RecordingRpc()
        stop = threading.Event()
        thread = dbg.start_line_sampler(rpc, tmp, interval_s=0.02, stop=stop)
        runpy.run_path(path, run_name='__main__')
        stop.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(rpc.calls)
        self.assertEqual({m for m, _a, _k in rpc.calls}, {'__line'})

    def test_nothing_is_sent_while_the_rpc_lock_is_held(self):
        dbg = _load_hook()
        tmp = tempfile.mkdtemp(prefix='edu-line-')
        path = os.path.join(tmp, 'main.py')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('import time\nende = time.monotonic() + 0.2\n'
                         'while time.monotonic() < ende:\n    pass\n')
        rpc = _RecordingRpc()
        stop = threading.Event()
        with rpc._lock:                 # a student call "in flight" (this thread)
            thread = dbg.start_line_sampler(rpc, tmp, interval_s=0.02, stop=stop)
            runpy.run_path(path, run_name='__main__')
            stop.set()
            thread.join(2.0)
        self.assertEqual(rpc.calls, [])


class HookSource(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_HOOK.is_file(), _HOOK)
        self.src = _HOOK.read_text(encoding='utf-8')

    def test_breakpoint_update_path_calls_restart_events_and_unmarked_lines_return_DISABLE(self):
        self.assertTrue(_set_breakpoints_restarts_and_unmarked_returns_DISABLE(self.src))
        # The checker can fail: a copy with the restart line removed.
        mutated = '\n'.join(line for line in self.src.splitlines()
                            if 'sys.monitoring.restart_events()' not in line
                            or 'step_armed' in line)
        self.assertIn('restart_events', mutated, 'the step path keeps its call')
        self.assertFalse(_set_breakpoints_restarts_and_unmarked_returns_DISABLE(mutated))

    def test_the_hook_is_installed_by_the_launcher_before_main_py_runs(self):
        tree = ast.parse(_STUDENT_MAIN.read_text(encoding='utf-8'))
        main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'main')
        names = [ast.unparse(n.func) for n in ast.walk(main) if isinstance(n, ast.Call)]
        self.assertIn('hook.install', names)
        self.assertIn('runpy.run_path', names)
        self.assertLess(names.index('hook.install'), names.index('runpy.run_path'))

    def test_the_launcher_wires_the_live_values_before_main_and_sends_the_last_before_exit(self):
        src = _STUDENT_MAIN.read_text(encoding='utf-8')
        tree = ast.parse(src)
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == 'main')
        order = []
        for node in ast.walk(main):
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == \
                    'robot._rpc.before_call' and ast.unparse(node.value) == 'live.before_call':
                order.append(('wire', node.lineno))
            if isinstance(node, ast.Call):
                text = ast.unparse(node.func)
                if text == 'runpy.run_path':
                    order.append(('run', node.lineno))
                if text == 'edubotics_debug.project_module_namespaces':
                    order.append(('modules', node.lineno))
                if text == 'finish_run':
                    order.append(('finish', node.lineno))
        kinds = [k for k, _line in sorted(order, key=lambda kv: kv[1])]
        self.assertEqual(kinds, ['wire', 'run', 'modules', 'finish'])
        # finish_run: ONE `with` over the stub's lock holds the unwiring, the
        # last values and __exit, in that order (review round 5, md7).
        finish = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == 'finish_run')
        withs = [n for n in finish.body if isinstance(n, ast.With)]
        self.assertEqual(len(withs), 1)
        self.assertIn('lock', ast.unparse(withs[0].items[0].context_expr))
        inner = []
        for node in ast.walk(withs[0]):
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == 'rpc.before_call':
                inner.append(('unwire', node.lineno))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == 'live.final':
                inner.append(('final', node.lineno))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == 'rpc.call' and \
                    node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == '__exit':
                inner.append(('exit', node.lineno))
        self.assertEqual([k for k, _l in sorted(inner, key=lambda kv: kv[1])], ['unwire', 'final', 'exit'])

    def test_the_tool_id_is_the_debugger_slot(self):
        self.assertIn('DEBUGGER_ID = 3', self.src)


if __name__ == '__main__':
    unittest.main()
