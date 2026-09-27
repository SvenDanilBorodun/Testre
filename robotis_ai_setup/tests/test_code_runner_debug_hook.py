"""The Python debugger hook of the code runner (``runner/lib/edubotics_debug.py``).

Two halves. The AST half pins the one call that makes a mid-run breakpoint
work at all: ``Hook.set_breakpoints`` must end in
``sys.monitoring.restart_events()`` (P13: a location the LINE callback has
DISABLEd never fires again without it), and the callback's unmarked branch
must ``return sys.monitoring.DISABLE`` (P12: that is what makes the hook
free). The behavioural half runs the hook on THIS interpreter (3.12+ has
``sys.monitoring``): a loop is started, the breakpoint is added while it
runs, and the hit count is asserted > 0 — and asserted == 0 once
``restart_events`` is replaced by a no-op, which is the red-before-green of
the whole mechanism. Deliberately stdlib-only.
"""

import ast
import importlib.util
import os
import pathlib
import runpy
import sys
import tempfile
import threading
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
_HOOK = _RUNNER / 'lib' / 'edubotics_debug.py'
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
            self.assertLessEqual(len(self.dbg.json.dumps(value)), self.dbg.VALUE_MAX_CHARS + 2)

    def test_non_finite_floats_and_hostile_reprs_do_not_break_a_pause(self):
        class _Hostile:
            def __repr__(self):
                raise RuntimeError('nope')

        self.assertEqual(self.dbg._jsonable(float('inf')), 'inf')
        self.assertEqual(self.dbg._jsonable(_Hostile()), '<?>')


_VARS_SRC = (
    'import time\n'
    'zaehler = 0\n'
    'name = "Robo"\n'
    'def schritt(k):\n'
    '    lokal = k * 2\n'
    '    name = "innen"\n'
    '    ende = time.monotonic() + 0.25\n'
    '    while time.monotonic() < ende:\n'
    '        pass\n'
    '    return lokal\n'
    'for i in range(4):\n'
    '    zaehler += schritt(i)\n'
    'ende = time.monotonic() + 0.4\n'
    'while time.monotonic() < ende:\n'
    '    pass\n'
)


class _RecordingRpc:
    """The stub's RPC surface the sampler uses: `_lock` + `call`."""

    def __init__(self):
        self._lock = threading.RLock()
        self.calls = []

    def call(self, method, args, kind):
        self.calls.append((method, args, kind))
        return None


class VarsSnapshot(unittest.TestCase):
    """2026-09-27, owner decision R-O1 (the O3 fallback): the sampler's
    `__vars` snapshot is MODULE-LEVEL only — the innermost project frame's
    module globals, then the entry module's; never a function frame's
    `f_locals` (reading them from another thread writes the frame's own
    `locals()` dict); at most 30; modules/functions/classes/dunders skipped."""

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
                raise AssertionError('the sampler must never read f_locals')

        snap = self.dbg.snapshot_vars(_Frame(), self.tmp, 30)
        self.assertEqual(len(snap), 30)
        self.assertEqual(list(snap)[:3], ['g0', 'g1', 'g2'])

    def test_a_frame_outside_the_project_has_no_variables(self):
        self.assertEqual(self.dbg.snapshot_vars(sys._getframe(), self.tmp), {})

    def test_fit_vars_trims_the_biggest_entries_until_the_frame_fits(self):
        snap = {f'v{i}': 'ü' * 1000 for i in range(30)}
        snap['klein'] = 1
        fitted = self.dbg.fit_vars(snap, 4000)
        self.assertIn('klein', fitted)
        self.assertLessEqual(
            len(self.dbg.json.dumps(fitted, ensure_ascii=False).encode('utf-8')), 4000)
        self.assertLess(self.dbg.VARS_FRAME_BUDGET_BYTES, 65536)

    def test_a_hostile_repr_inside_a_big_container_does_not_break_the_snapshot(self):
        class _Hostile:
            def __repr__(self):
                raise RuntimeError('nope')

        class _Frame:
            f_locals = {'liste': ['x' * 900, 'y' * 900, _Hostile()]}

        out = self.dbg.snapshot_locals(_Frame(), 30)
        self.assertIn('liste', out)


@unittest.skipUnless(hasattr(sys, 'monitoring'), 'needs sys.monitoring (3.12+)')
class VarsSampler(unittest.TestCase):
    def setUp(self):
        self.dbg = _load_hook()
        self.tmp = tempfile.mkdtemp(prefix='edu-vars-run-')
        self.path = os.path.join(self.tmp, 'main.py')
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write(_VARS_SRC)
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()

    def _run(self, rpc, **kw):
        thread = self.dbg.start_line_sampler(rpc, self.tmp, interval_s=0.05,
                                             stop=self.stop, **kw)
        runpy.run_path(self.path, run_name='__main__')
        self.stop.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())

    def test_vars_are_sent_only_when_they_changed(self):
        rpc = _RecordingRpc()
        self._run(rpc)
        sent = [args for method, args, _kind in rpc.calls if method == '__vars']
        self.assertGreater(len(sent), 2)
        snapshots = [self.dbg.json.dumps(a[2], sort_keys=True) for a in sent]
        for before, after in zip(snapshots, snapshots[1:]):
            self.assertNotEqual(before, after)
        self.assertTrue(all(a[0] == 'main.py' and isinstance(a[1], int) for a in sent))
        self.assertTrue(all(len(a[2]) <= 30 for a in sent))
        seen = {k: v for a in sent for k, v in a[2].items()}
        self.assertIn('zaehler', seen)
        self.assertNotIn('lokal', seen)             # R-O1: module level only
        self.assertTrue(all(kind == 'call' for _m, _a, kind in rpc.calls))
        # __line keeps working beside it.
        self.assertTrue(any(m == '__line' for m, _a, _k in rpc.calls))

    def test_nothing_is_sent_while_the_rpc_lock_is_held(self):
        rpc = _RecordingRpc()
        with rpc._lock:                 # a student call "in flight" (this thread)
            self._run(rpc)
        self.assertEqual(rpc.calls, [])

    def test_vars_can_be_switched_off(self):
        rpc = _RecordingRpc()
        self._run(rpc, send_vars=False)
        self.assertFalse(any(m == '__vars' for m, _a, _k in rpc.calls))
        self.assertTrue(any(m == '__line' for m, _a, _k in rpc.calls))


_NEVER_RUN_SRC = (
    'import time\n'
    'class _Meta(type):\n'
    '    zaehler = 0\n'
    '    @property\n'
    '    def __name__(cls):\n'
    '        _Meta.zaehler += 1\n'
    '        return "Falsch"\n'
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
    'ding = Laut()\n'
    'liste = [1, Laut(), "a"]\n'
    'zuordnung = {"a": 1, 2: "b", Laut(): 3, True: None}\n'
    'text = MeinText("hallo")\n'
    'eigen = MeineListe([1, 2])\n'
    'riesig = 2 ** 5000\n'
    'def arbeite():\n'
    '    lokal = Laut()\n'
    '    ende = time.monotonic() + 0.4\n'
    '    while time.monotonic() < ende:\n'
    '        pass\n'
    '    return lokal\n'
    'arbeite()\n'
    'ende = time.monotonic() + 0.3\n'
    'while time.monotonic() < ende:\n'
    '    pass\n'
    'ERGEBNIS = (Laut.aufrufe, _Meta.zaehler)\n'
)

_LOCALS_LOOP_SRC = (
    'import time\n'
    'def f():\n'
    '    a = 1\n'
    '    b = 2\n'
    '    seen = []\n'
    '    for k in locals():\n'
    '        seen.append(k)\n'
    '        time.sleep(0.1)\n'
    '    return seen\n'
    'try:\n'
    '    ERGEBNIS = ("ok", f())\n'
    'except RuntimeError as exc:\n'
    '    ERGEBNIS = ("RuntimeError", str(exc))\n'
)


@unittest.skipUnless(hasattr(sys, 'monitoring'), 'needs sys.monitoring (3.12+)')
class SamplerNeverRunsStudentCode(unittest.TestCase):
    """2026-09-27 review round (M1/m6, owner decision R-O1). The sampler is a
    second thread inside the student's process: it must render only EXACT
    builtin values, walk only exact builtin containers within a budget, name
    everything else `<Klasse>` without asking the class, and never touch a
    function frame's locals. Each test is the reviewers' repro."""

    def setUp(self):
        self.dbg = _load_hook()
        self.tmp = tempfile.mkdtemp(prefix='edu-never-')
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()

    def _run(self, src, interval_s=0.02):
        path = os.path.join(self.tmp, 'main.py')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(src)
        rpc = _RecordingRpc()
        thread = self.dbg.start_line_sampler(rpc, self.tmp, interval_s=interval_s, stop=self.stop)
        ns = runpy.run_path(path, run_name='__main__')
        self.stop.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        return ns, [args[2] for method, args, _k in rpc.calls if method == '__vars']

    def test_no_student_method_runs_and_every_other_value_is_its_class_name(self):
        ns, sent = self._run(_NEVER_RUN_SRC)
        self.assertEqual(ns['ERGEBNIS'], (0, 0), 'the sampler ran student code')
        self.assertTrue(sent)
        last = sent[-1]
        self.assertEqual(last['ding'], '<Laut>')          # the real name, not the metaclass's
        self.assertEqual(last['liste'], [1, '<Laut>', 'a'])
        self.assertEqual(last['zuordnung'], {'a': 1, '2': 'b', 'True': None})
        self.assertEqual(last['text'], '<MeinText>')
        self.assertEqual(last['eigen'], '<MeineListe>')
        self.assertEqual(last['riesig'], '<int>')
        self.assertNotIn('lokal', {k for snap in sent for k in snap})

    def test_iterating_locals_in_a_function_is_unaffected_by_the_sampler(self):
        ns, _sent = self._run(_LOCALS_LOOP_SRC, interval_s=0.01)
        self.assertEqual(ns['ERGEBNIS'], ('ok', ['a', 'b', 'seen']))

    def test_a_huge_global_costs_no_full_repr(self):
        import tracemalloc

        class _Code:
            co_filename = os.path.join(self.tmp, 'main.py')
            co_name = '<module>'

        class _Frame:
            f_code = _Code()
            f_back = None
            f_globals = {
                'text': 'ä' * 5_000_000,
                'tabelle': {i: 'x' * 60 for i in range(200_000)},
                'liste': ['y' * 80] * 200_000,
            }

        tracemalloc.start()
        try:
            snap = self.dbg.snapshot_vars(_Frame(), self.tmp)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1_000_000, f'peak {peak} bytes: a full repr was built')
        self.assertEqual(set(snap), {'text', 'tabelle', 'liste'})
        self.assertLessEqual(len(snap['text']), self.dbg.VALUE_MAX_CHARS)
        # Per value: the character budget plus a few separator/key bytes per
        # visited node — independent of how big the global is.
        per_value = self.dbg.SAFE_VALUE_MAX_CHARS + 12 * self.dbg.SAFE_VALUE_MAX_NODES
        self.assertLessEqual(len(self.dbg.json.dumps(snap, ensure_ascii=False)), 3 * per_value)

    def test_the_renderer_is_bounded_by_nodes_and_depth(self):
        render = self.dbg.safe_render
        # CONTAINER_MAX_DEPTH (3) levels are walked; the 4th is only named.
        self.assertEqual(render([[[[1]]]]), [[['<list>']]])
        out = render([list(range(1000))] * 1000)
        count = len(self.dbg.json.dumps(out))
        self.assertLess(count, 2000)
        self.assertEqual(render(float('nan')), 'nan')
        self.assertEqual(render(float('inf')), 'inf')
        self.assertIs(render(True), True)
        self.assertEqual(render({(1, 2): 'tupel-schlüssel', 'k': 1}), {'k': 1})


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

    def test_the_tool_id_is_the_debugger_slot(self):
        self.assertIn('DEBUGGER_ID = 3', self.src)


if __name__ == '__main__':
    unittest.main()
