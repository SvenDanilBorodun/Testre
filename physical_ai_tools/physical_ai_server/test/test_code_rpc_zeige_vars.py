#!/usr/bin/env python3
"""``robot.zeige`` and ``__vars`` over the data socket (2026-09-27, owner
decision O3): a text program's variables reach the Debug-Panel as the same
``[VAR:name=json]`` sentinel a Blockly run emits.

These two methods widen the SECOND EXECUTION PATH's surface (CLAUDE.md Rule §2,
"A second execution path drives the arm"), so each of its four conditions is
asserted here for them, over a real unix socket, not argued:

1. every ``zeige`` / ``__vars`` frame is charged against the run's call budget
   BEFORE validation (an invalid one too);
2. stop semantics are untouched: a ``zeige`` queued after Stopp is refused like
   any statement, a paused run holds it, and closing the run drops it;
3. every frame is hostile: method, arity, the name rule and the ``value`` kind
   are validated server-side from the rows, whoever sent the bytes;
4. (the uid split is the compose's and is not touched by this change.)

And the new property of their own: NOTHING they do reaches ``ctx.publisher``,
the handler table or the motion lock — they only ever call ``ctx.log``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import time

import pytest

from physical_ai_server.workflow import code_rpc
from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.code_rpc import CodeRpcServer, validate_value
from physical_ai_server.workflow.workflow_manager import WorkflowContext

from test_code_rpc import _Client, _fake_handlers  # noqa: E402 — the shared harness


def _ctx(**over) -> WorkflowContext:
    published = []
    logs = []
    kw = dict(publisher=lambda pts: published.append(pts),
              log=lambda m: logs.append(m),
              var_lock=threading.RLock(),
              claim_lock=threading.RLock())
    kw.update(over)
    ctx = WorkflowContext(**kw)
    ctx._test_published = published
    ctx._test_logs = logs
    return ctx


def _vars(ctx) -> list[str]:
    return [line for line in ctx._test_logs if line.startswith('[VAR:')]


@pytest.fixture
def server():
    with tempfile.TemporaryDirectory(prefix='code-rpc-zeige-') as d:
        srv = CodeRpcServer(os.path.join(d, 'rpc.sock'))
        # Every handler of the table records — none may ever be reached.
        reached = []

        def _record(block_type):
            return lambda ctx, args: reached.append(block_type)
        srv.handlers = _fake_handlers(**{
            t: _record(t) for t in code_rpc.default_handlers()})
        srv._test_reached = reached
        yield srv
        srv.close()


# ── the shipped bounds (literal pins, test_constant_pins' rule) ────────────

def test_the_shipped_bounds_are_the_numbers_we_chose():
    assert robot_api.PAUSED_MAX_LOCALS == 30
    assert robot_api.SHOWN_VALUE_MAX_CHARS == 2000
    assert code_rpc.SHOWN_VALUE_MAX_DEPTH == 8
    assert code_rpc.SHOWN_VALUE_MAX_NODES == 5000
    assert robot_api.SHOWN_VAR_NAMES_MAX == 256
    assert code_rpc.SHOWN_VAR_NAMES_MAX is robot_api.SHOWN_VAR_NAMES_MAX
    # The flood limits of the 2026-09-27 review rounds (m7, then mi4: the
    # per-frame caps are DERIVED from the runner's own bounds in robot_api —
    # 30 × max(100 live nodes, 1000 // 2 + 1 breakpoint nodes) and
    # 30 × 1000 characters — where they had been picked, 5000 / 48 KiB, and
    # the runner's legal frames broke them).
    assert robot_api.VARS_MIN_INTERVAL_S == 0.4
    assert code_rpc.VARS_MIN_INTERVAL_S is robot_api.VARS_MIN_INTERVAL_S
    assert robot_api.LIVE_VALUES_INTERVAL_S == 0.5
    assert robot_api.LIVE_VALUE_MAX_NODES == 100
    assert robot_api.LIVE_VALUE_MAX_CHARS == 1000
    assert robot_api.PAUSED_VALUE_MAX_CHARS == 1000
    assert robot_api.SHOWN_FRAME_MAX_NODES == 15030
    assert robot_api.SHOWN_FRAME_MAX_CHARS == 30000
    assert code_rpc.SHOWN_FRAME_MAX_NODES is robot_api.SHOWN_FRAME_MAX_NODES
    assert code_rpc.SHOWN_FRAME_MAX_CHARS is robot_api.SHOWN_FRAME_MAX_CHARS
    assert robot_api.SHOWN_TOO_BIG == '<zu groß>'
    assert code_rpc.SHOWN_EMITS_PER_S == 20
    assert code_rpc.SHOWN_EMITS_BURST == 20
    assert code_rpc.SHOWN_FLUSH_TICK_S == 0.1


def test_the_runners_pace_is_slower_than_the_servers_floor():
    """An honest runner is never skipped: it waits LIVE_VALUES_INTERVAL_S
    after a REPLY, the server looks at one frame per VARS_MIN_INTERVAL_S."""
    assert robot_api.LIVE_VALUES_INTERVAL_S > robot_api.VARS_MIN_INTERVAL_S


# ── the `value` kind ───────────────────────────────────────────────────────

def _value_param():
    return robot_api.CODE_ONLY_METHODS_BY_NAME['zeige'].params[1]


def test_value_kind_accepts_every_json_value_within_the_bounds():
    p = _value_param()
    for good in (0, -3, 1.5, 'Hallo', '', True, None, [1, [2, [3]]],
                 {'a': 1, 'b': [1, 2]}, [[[[[[[[1]]]]]]]]):
        assert validate_value(p, good) is None, good


def test_value_kind_refuses_nesting_past_the_depth_bound_and_huge_trees():
    p = _value_param()
    deep = 1
    for _ in range(code_rpc.SHOWN_VALUE_MAX_DEPTH + 1):
        deep = [deep]
    assert validate_value(p, deep)
    wide = [[0] * 1000 for _ in range(code_rpc.SHOWN_VALUE_MAX_NODES // 1000 + 1)]
    assert validate_value(p, wide)
    # The walk is iterative: a nesting far past Python's recursion limit is a
    # refusal, never a RecursionError out of the reader thread.
    very_deep = 1
    for _ in range(5000):
        very_deep = [very_deep]
    assert validate_value(p, very_deep)


# ── zeige ──────────────────────────────────────────────────────────────────

def test_zeige_emits_the_var_sentinel_and_touches_nothing_else(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('zeige', ['punkte', 3]) == {'id': 2, 'ok': True, 'r': None}
    assert c.call('zeige', ['Anzahl Würfel', [1, 2]])['ok'] is True
    assert c.call('zeige', ['text', 'Hallo'])['ok'] is True
    assert _vars(ctx) == ['[VAR:punkte=3]', '[VAR:Anzahl Würfel=[1, 2]]', '[VAR:text="Hallo"]']
    assert ctx._test_published == []
    assert server._test_reached == []
    server.close_run(session)


def test_zeige_emits_every_call_even_an_unchanged_value(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    for _ in range(3):
        assert c.call('zeige', ['n', 1])['ok'] is True
    assert _vars(ctx) == ['[VAR:n=1]'] * 3
    server.close_run(session)


def test_zeige_names_are_validated_from_the_row(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    for bad in ('a=b', 'liste[0]', '   ', '', 'x' * 41, 'a\nb', 5, None):
        r = c.call('zeige', [bad, 1])
        assert r['ok'] is False and r['k'] == 'value', (bad, r)
        assert r['e'].startswith('robot.zeige: ')
    r = c.call('zeige', ['n'])
    assert r['ok'] is False and r['k'] == 'arity'
    deep = 1
    for _ in range(code_rpc.SHOWN_VALUE_MAX_DEPTH + 1):
        deep = [deep]
    r = c.call('zeige', ['n', deep])
    assert r['ok'] is False and r['k'] == 'value'
    assert _vars(ctx) == []
    server.close_run(session)


def test_a_long_value_is_cut_to_the_interpreters_payload_cap(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('zeige', ['lang', 'x' * 30000])['ok'] is True
    [line] = _vars(ctx)
    payload = line[len('[VAR:lang='):-1]
    assert payload.endswith(' …')
    assert len(payload) == robot_api.SHOWN_VALUE_MAX_CHARS + 2
    server.close_run(session)


def test_zeige_is_charged_against_the_call_budget_before_validation(server):
    """Condition 1: an INVALID zeige costs a token too. With the burst spent
    on invalid frames, the next valid one waits for the refill."""
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    burst = robot_api.RPC_LIMITS.BURST
    before = session.frames_decoded
    for _ in range(burst):
        assert c.call('zeige', ['a=b', 1])['ok'] is False
    assert session.frames_decoded - before == burst
    n = 40
    t0 = time.monotonic()
    for _ in range(n):
        assert c.call('zeige', ['n', 1])['ok'] is True
    elapsed = time.monotonic() - t0
    # The hello + the invalid burst emptied the bucket: 40 more frames take
    # at least (40 - slack) / MAX_CALLS_PER_S.
    assert elapsed >= (n - 3) / robot_api.RPC_LIMITS.MAX_CALLS_PER_S
    server.close_run(session)


def test_a_zeige_after_stop_is_refused_like_any_statement(server):
    stopped = {'v': False}
    ctx = _ctx(should_stop=lambda: stopped['v'])
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('zeige', ['n', 1])['ok'] is True
    stopped['v'] = True
    r = c.call('zeige', ['n', 2])
    assert r == {'id': 3, 'ok': False, 'k': 'robot', 'e': code_rpc.RUN_STOPPED_DE}
    assert _vars(ctx) == ['[VAR:n=1]']
    server.close_run(session)


def test_a_zeige_waits_while_the_run_is_paused(server):
    released = threading.Event()
    waits = []

    def wait_if_paused():
        waits.append(time.monotonic())
        released.wait(5.0)
    ctx = _ctx(wait_if_paused=wait_if_paused)
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    reply = {}
    t = threading.Thread(target=lambda: reply.update(c.call('zeige', ['n', 7])), daemon=True)
    t.start()
    time.sleep(0.3)
    assert reply == {} and waits and _vars(ctx) == []
    released.set()
    t.join(5.0)
    assert reply['ok'] is True and _vars(ctx) == ['[VAR:n=7]']
    server.close_run(session)


def test_zeige_carries_its_position_into_the_running_status(server):
    statuses = []
    ctx = _ctx()
    session = server.open_run(ctx, on_block_change=lambda *a: statuses.append(a))
    c = _Client(server.socket_path, session.token)
    c.call('zeige', ['n', 1], f='main.py', l=4)
    assert statuses == [('main.py:L4', 'running', 0.0)]
    server.close_run(session)


# ── __vars ─────────────────────────────────────────────────────────────────

def test_vars_emits_only_the_names_whose_value_changed(server, monkeypatch):
    monkeypatch.setattr(code_rpc, 'VARS_MIN_INTERVAL_S', 0.0)   # not the floor under test
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('__vars', ['main.py', 3, {'a': 1, 'b': [1, 2]}])['ok'] is True
    assert _vars(ctx) == ['[VAR:a=1]', '[VAR:b=[1, 2]]']
    assert c.call('__vars', ['main.py', 4, {'a': 1, 'b': [1, 2, 3]}])['ok'] is True
    assert _vars(ctx)[2:] == ['[VAR:b=[1, 2, 3]]']
    assert c.call('__vars', ['main.py', 5, {'a': 1}])['ok'] is True
    assert len(_vars(ctx)) == 3
    assert ctx._test_published == [] and server._test_reached == []
    server.close_run(session)


def test_a_zeige_and_a_pause_feed_the_same_change_map(server, monkeypatch):
    """A value a breakpoint already showed is not re-emitted by the live
    values; a changed one is. A name zeige showed is zeige's for the rest of
    the run (review round 2, ni3 — `punkte` 6 below was shown before that)."""
    monkeypatch.setattr(code_rpc, 'VARS_MIN_INTERVAL_S', 0.0)   # not the floor under test
    ctx = _ctx()          # default wait_for_resume returns at once
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('zeige', ['punkte', 5])['ok'] is True
    assert c.call('__paused', ['main.py', 2, {'i': 3}])['r'] == 'continue'
    before = len(_vars(ctx))
    assert c.call('__vars', ['main.py', 2, {'punkte': 5, 'i': 3}])['ok'] is True
    assert len(_vars(ctx)) == before
    assert c.call('__vars', ['main.py', 2, {'punkte': 6, 'i': 4}])['ok'] is True
    assert _vars(ctx)[before:] == ['[VAR:i=4]']
    server.close_run(session)


def test_vars_is_validated_from_its_row_and_skips_unshowable_entries(server, monkeypatch):
    monkeypatch.setattr(code_rpc, 'VARS_MIN_INTERVAL_S', 0.0)   # not the floor under test
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    too_many = {f'v{i}': i for i in range(robot_api.PAUSED_MAX_LOCALS + 1)}
    r = c.call('__vars', ['main.py', 1, too_many])
    assert r['ok'] is False and r['k'] == 'value'
    r = c.call('__vars', ['../boese.py', 1, {}])
    assert r['ok'] is False and r['k'] == 'value'
    r = c.call('__vars', ['main.py', 1, []])
    assert r['ok'] is False and r['k'] == 'value'
    assert _vars(ctx) == []
    deep = 1
    for _ in range(50):               # past the depth bound, few nodes
        deep = [deep]
    r = c.call('__vars', ['main.py', 1, {'a=b': 1, 'x' * 65: 1, '  ': 1, 'tief': deep, 'ok': 2}])
    assert r['ok'] is True
    assert _vars(ctx) == ['[VAR:ok=2]']
    # A nesting far past Python's recursion limit: the iterative walks
    # measure it (never a RecursionError out of the reader) and refuse THAT
    # entry by its depth; the rest of the frame still shows (review round 2,
    # mi4: a frame is never dropped whole — it was, before).
    very_deep = 1
    for _ in range(5000):
        very_deep = [very_deep]
    r = c.call('__vars', ['main.py', 1, {'tief': very_deep, 'ok': 3}])
    assert r['ok'] is True
    assert _vars(ctx) == ['[VAR:ok=2]', '[VAR:ok=3]']
    server.close_run(session)


def test_vars_is_charged_against_the_call_budget(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    before = session.frames_decoded
    for i in range(5):
        c.call('__vars', ['main.py', i, {'i': i}])
    assert session.frames_decoded - before == 5
    server.close_run(session)


def test_the_change_map_is_bounded(server, monkeypatch):
    monkeypatch.setattr(code_rpc, 'VARS_MIN_INTERVAL_S', 0.0)   # not the floor under test
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    for start in range(0, code_rpc.SHOWN_VAR_NAMES_MAX + 60, 30):
        c.call('__vars', ['main.py', 1, {f'v{i}': i for i in range(start, start + 30)}])
    assert len(session._var_payloads) <= code_rpc.SHOWN_VAR_NAMES_MAX
    server.close_run(session)


def test_nothing_in_the_zeige_and_vars_path_names_the_publisher_or_the_motion_lock():
    """Structural: the helpers these two methods run are the only new code on
    the reader and the worker, and none of them mentions the arm."""
    import ast
    tree = ast.parse(open(code_rpc.__file__, encoding='utf-8').read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RunSession')
    names = {'_code_only', '_emit_var', '_emit_changed_vars', '_render_shown',
             '_queue_shown', '_flush_shown', '_vars_floor_ok', '_emit_locals'}
    found = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found.add(node.name)
            text = ast.unparse(node)
            for forbidden in ('publisher', 'motion_lock', 'handlers', '_trajectory'):
                assert forbidden not in text, (node.name, forbidden)
    assert found == names


# ── the flood limits (2026-09-27 review round, m7; Rule §2 intact) ─────────

def test_vars_frames_past_the_floor_are_charged_then_dropped(server):
    """The real sampler sends at most 2 frames/s; a flood of `__vars` still
    costs a token each (condition 1) but only one per VARS_MIN_INTERVAL_S is
    looked at — the rest are answered at once, with nothing emitted."""
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    before = session.frames_decoded
    replies = [c.call('__vars', ['main.py', 1, {'i': i}]) for i in range(10)]
    assert session.frames_decoded - before == 10
    assert all(r['ok'] is True for r in replies)
    # The first is looked at; the rest are answered „skipped" — the runner,
    # told so, sends its values again (R2-O1: the send is synchronous).
    assert [r['r'] for r in replies] == [None] + [robot_api.VARS_REPLY_SKIPPED] * 9
    assert _vars(ctx) == ['[VAR:i=0]']
    time.sleep(0.45)
    assert c.call('__vars', ['main.py', 1, {'i': 99}])['r'] is None
    assert _vars(ctx) == ['[VAR:i=0]', '[VAR:i=99]']
    server.close_run(session)


def test_every_vars_frame_takes_a_token_before_anything_else(server):
    """Condition 1 observed at the BUCKET (review round 3, MB2a): the tests
    above count ``frames_decoded``, which the reader bumps whether or not
    it then charges — a reader that let `__vars` through uncharged kept
    them green. Here every token the run's call bucket hands out is
    counted: a looked-at `__vars`, a floor-skipped one and an invalid one
    each take exactly one, and a burst of them delays what follows."""
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)   # the hello is not charged
    taken = []
    real_take = session._calls.take

    def counting_take(stop):
        ok = real_take(stop)
        if ok:
            taken.append(time.monotonic())
        return ok
    session._calls.take = counting_take

    replies = [c.call('__vars', ['main.py', 1, {'i': i}]) for i in range(10)]
    assert [r['r'] for r in replies] == [None] + [robot_api.VARS_REPLY_SKIPPED] * 9
    assert len(taken) == 10, 'a looked-at and a floor-skipped __vars each cost one token'
    time.sleep(robot_api.VARS_MIN_INTERVAL_S + 0.05)       # past the floor: validated
    bad = c.call('__vars', ['main.py', 1])                 # wrong arity: refused
    assert bad['ok'] is False
    assert len(taken) == 11, 'an invalid __vars costs its token before it is refused'

    # The tokens are the run's real ones: with the burst spent on __vars,
    # the next frames wait for the refill.
    burst = robot_api.RPC_LIMITS.BURST
    for _ in range(burst):
        c.call('__vars', ['main.py', 1, {'x': 1}])
    n = 40
    t0 = time.monotonic()
    for _ in range(n):
        c.call('__vars', ['main.py', 1, {'x': 1}])
    elapsed = time.monotonic() - t0
    assert len(taken) == 11 + burst + n
    assert elapsed >= (n - 3) / robot_api.RPC_LIMITS.MAX_CALLS_PER_S
    server.close_run(session)


def test_a_vars_frame_over_the_total_cap_shows_every_name(server):
    """Each value within its own bound, the FRAME over the total: the
    largest values become SHOWN_TOO_BIG until the rest fit — every name
    still shows (review round 2, mi4: the whole frame used to be dropped
    while the runner believed it delivered)."""
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    big = code_rpc.SHOWN_VALUE_MAX_NODES - 1          # each value alone is valid
    wide = {f'v{i}': [0] * big for i in range(5)}
    wide['klein'] = 7
    assert c.call('__vars', ['main.py', 1, wide])['ok'] is True
    shown = dict(line[len('[VAR:'):-1].split('=', 1) for line in _vars(ctx))
    assert set(shown) == set(wide)
    assert shown['klein'] == '7'
    too_big = json.dumps(robot_api.SHOWN_TOO_BIG)   # the [VAR:] payload is JSON
    # 5 × 5000 nodes (a 4999-item list is 5000) + 1: the two largest go.
    assert sum(1 for v in shown.values() if v == too_big) == 2
    server.close_run(session)
    assert not code_rpc.shown_values_exceed([[0] * 10, 'abc'], 100, 100)
    assert code_rpc.shown_values_exceed(['x' * 60, 'y' * 60], 1000, 100)
    assert code_rpc.shown_values_exceed([[0] * 60, [0] * 60], 100, 10_000)


def test_fit_shown_values_is_a_no_op_within_the_caps_and_keeps_the_order():
    items = [('a', [1, 2]), ('b', 'x' * 10)]
    assert code_rpc.fit_shown_values(items) == items
    fitted = code_rpc.fit_shown_values([('a', 'x' * 60), ('b', 'y' * 30), ('c', 1)],
                                       max_nodes=100, max_chars=50)
    assert [n for n, _v in fitted] == ['a', 'b', 'c']
    assert fitted == [('a', robot_api.SHOWN_TOO_BIG), ('b', 'y' * 30), ('c', 1)]


# ── the runner's worst case fits (review round 2, mi4) ─────────────────────

_RUNNER_LIB = pathlib.Path(__file__).resolve().parents[3] / 'robotis_ai_setup' / 'docker' \
    / 'code_runner' / 'runner' / 'lib'


def _runner_debug():
    spec = importlib.util.spec_from_file_location(
        'edubotics_debug_under_test', _RUNNER_LIB / 'edubotics_debug.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worst_case_values():
    return [
        [[[k for k in range(50)] for _ in range(50)] for _ in range(50)],   # the reviewer's cube
        {('k%03d' % j) + 'x' * 995: {('q%03d' % m) + 'y' * 995: m for m in range(2)}
         for j in range(50)},                                                # long keys
        [[0] * 6] * 50,                                                      # the breakpoint grid
        list(range(10_000)),
        'ü' * 100_000,
    ]


def test_a_full_live_frame_the_runner_renders_is_never_trimmed(server):
    """30 values rendered by the RUNNER's safe_render at their worst: the
    server shows every one with its real value (the reviewer's cube once
    rendered to 197 nodes a value, 5910 a frame, over the 5000 cap)."""
    dbg = _runner_debug()
    values = (_worst_case_values() * 6)[:robot_api.PAUSED_MAX_LOCALS]
    snapshot = dbg.fit_vars({f'g{i:02d}': dbg.safe_render(v) for i, v in enumerate(values)})
    assert robot_api.SHOWN_TOO_BIG not in snapshot.values()
    assert not code_rpc.shown_values_exceed(list(snapshot.values()),
                                            code_rpc.SHOWN_FRAME_MAX_NODES,
                                            code_rpc.SHOWN_FRAME_MAX_CHARS)
    items = list(snapshot.items())
    assert code_rpc.fit_shown_values(items) == items


def test_a_breakpoint_shows_every_variable_again(server):
    """Base behaviour, restored (review round 2, mi4): 20 locals of
    `[[0] * 6] * 50` were 0 of 20 shown under the round-1 total cap."""
    dbg = _runner_debug()

    class _Frame:
        f_locals = {f'v{i}': [[0] * 6] * 50 for i in range(20)}
    snapshot = dbg.snapshot_locals(_Frame(), robot_api.PAUSED_MAX_LOCALS)
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('__paused', ['main.py', 3, snapshot])['r'] == 'continue'
    shown = dict(line[len('[VAR:'):-1].split('=', 1) for line in _vars(ctx))
    assert set(shown) == set(snapshot)
    assert all(v.startswith('[[0, 0, 0, 0, 0, 0]') for v in shown.values())
    server.close_run(session)


def test_thirty_worst_case_breakpoint_values_fit_the_caps():
    dbg = _runner_debug()
    values = (_worst_case_values() * 6)[:robot_api.PAUSED_MAX_LOCALS]

    class _Frame:
        f_locals = {f'v{i:02d}': v for i, v in enumerate(values)}
    snapshot = dbg.snapshot_locals(_Frame(), robot_api.PAUSED_MAX_LOCALS)
    assert len(snapshot) == robot_api.PAUSED_MAX_LOCALS
    items = list(snapshot.items())
    assert code_rpc.fit_shown_values(items) == items


# ── zeige: the waiting-names bound and a name it claims (mi6, ni3) ─────────

def test_zeige_says_once_when_too_many_names_wait_and_keeps_the_bound(server, monkeypatch):
    """At most SHOWN_VAR_NAMES_MAX names wait for the emit budget; a NEW name
    past it is dropped — and the run's log says so ONCE in German (review
    round 2, mi6: it was silent, and a test without the bound stayed green)."""
    monkeypatch.setattr(code_rpc, 'SHOWN_EMITS_PER_S', 0.001)
    monkeypatch.setattr(code_rpc, 'SHOWN_EMITS_BURST', 0)
    ctx = _ctx()
    session = server.open_run(ctx)
    for i in range(code_rpc.SHOWN_VAR_NAMES_MAX + 40):
        session._queue_shown(f'n{i}', i)
    assert len(session._shown_pending) <= code_rpc.SHOWN_VAR_NAMES_MAX
    warnings = [line for line in ctx._test_logs if line.startswith('[WARNUNG]')]
    assert warnings == [code_rpc.ZEIGE_TOO_MANY_NAMES_DE.format(n=code_rpc.SHOWN_VAR_NAMES_MAX)]
    assert 'zeige' in warnings[0] and '256' in warnings[0]
    # A name already waiting is still updated while the map is full.
    session._queue_shown('n0', 'neu')
    assert session._shown_pending['n0'] == '"neu"'
    server.close_run(session)
    names = {line[len('[VAR:'):].split('=', 1)[0] for line in _vars(ctx)}
    assert len(names) == code_rpc.SHOWN_VAR_NAMES_MAX


def test_a_name_zeige_shows_is_zeiges_for_the_rest_of_the_run(server, monkeypatch):
    """A module variable and a zeige call with the same name used to flip
    the panel between the two values on every frame (review round 2, ni3)."""
    monkeypatch.setattr(code_rpc, 'VARS_MIN_INTERVAL_S', 0.0)
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('__vars', ['main.py', 1, {'punkte': 3, 'andere': 1}])['ok'] is True
    assert c.call('zeige', ['punkte', 5])['ok'] is True
    assert c.call('__vars', ['main.py', 2, {'punkte': 3, 'andere': 2}])['ok'] is True
    assert c.call('zeige', ['punkte', 5])['ok'] is True
    assert c.call('__vars', ['main.py', 3, {'punkte': 4, 'andere': 2}])['ok'] is True
    assert _vars(ctx) == ['[VAR:punkte=3]', '[VAR:andere=1]', '[VAR:punkte=5]',
                          '[VAR:andere=2]', '[VAR:punkte=5]']
    server.close_run(session)


def test_a_nine_deep_value_is_never_shown(server, monkeypatch):
    """`__vars` validates its entries' VALUES nowhere but in _emit_var's own
    bound: a 9-deep value (json-dumpable, few nodes) must still be refused
    there (n6 — this fails if that check is removed)."""
    monkeypatch.setattr(code_rpc, 'VARS_MIN_INTERVAL_S', 0.0)
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    nine = 1
    for _ in range(9):
        nine = [nine]
    assert c.call('__vars', ['main.py', 1, {'tief': nine, 'ok': 2}])['ok'] is True
    assert _vars(ctx) == ['[VAR:ok=2]']
    server.close_run(session)


def test_zeige_is_coalesced_per_name_and_emitted_at_a_bounded_rate(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    t0 = time.monotonic()
    for i in range(120):
        assert c.call('zeige', ['a' if i % 2 else 'b', i])['ok'] is True
    elapsed = time.monotonic() - t0
    emitted = _vars(ctx)
    allowed = code_rpc.SHOWN_EMITS_BURST + code_rpc.SHOWN_EMITS_PER_S * (elapsed + 0.2) + 2
    assert len(emitted) <= allowed, (len(emitted), elapsed)
    server.close_run(session)
    # The LAST value of every name arrives — at the latest when the run ends.
    last = {}
    for line in _vars(ctx):
        name, payload = line[len('[VAR:'):-1].split('=', 1)
        last[name] = payload
    assert last == {'a': '119', 'b': '118'}


def test_a_coalesced_zeige_is_flushed_on_the_next_tick(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    for i in range(code_rpc.SHOWN_EMITS_BURST + 10):
        assert c.call('zeige', ['n', i])['ok'] is True
    final = f'[VAR:n={code_rpc.SHOWN_EMITS_BURST + 9}]'
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _vars(ctx)[-1:] != [final]:
        time.sleep(0.02)
    assert _vars(ctx)[-1] == final, 'the latest value waited for the run to end'
    server.close_run(session)
