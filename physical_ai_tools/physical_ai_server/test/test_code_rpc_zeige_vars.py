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

import os
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
    assert code_rpc.SHOWN_VAR_NAMES_MAX == 256


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

def test_vars_emits_only_the_names_whose_value_changed(server):
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


def test_a_zeige_and_a_pause_feed_the_same_change_map(server):
    """A value zeige or a breakpoint already showed is not re-emitted by the
    sampler; a changed one is."""
    ctx = _ctx()          # default wait_for_resume returns at once
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('zeige', ['punkte', 5])['ok'] is True
    assert c.call('__paused', ['main.py', 2, {'i': 3}])['r'] == 'continue'
    before = len(_vars(ctx))
    assert c.call('__vars', ['main.py', 2, {'punkte': 5, 'i': 3}])['ok'] is True
    assert len(_vars(ctx)) == before
    assert c.call('__vars', ['main.py', 2, {'punkte': 6, 'i': 3}])['ok'] is True
    assert _vars(ctx)[-1] == '[VAR:punkte=6]'
    server.close_run(session)


def test_vars_is_validated_from_its_row_and_skips_unshowable_entries(server):
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
    for _ in range(5000):
        deep = [deep]
    r = c.call('__vars', ['main.py', 1, {'a=b': 1, 'x' * 65: 1, '  ': 1, 'tief': deep, 'ok': 2}])
    assert r['ok'] is True
    assert _vars(ctx) == ['[VAR:ok=2]']
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


def test_the_change_map_is_bounded(server):
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
    names = {'_code_only', '_emit_var', '_emit_changed_vars'}
    found = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found.add(node.name)
            text = ast.unparse(node)
            for forbidden in ('publisher', 'motion_lock', 'handlers', '_trajectory'):
                assert forbidden not in text, (node.name, forbidden)
    assert found == names
