#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""A „Funktion" call has its own frame (M1, owner decision OQ-4 A), and deep
recursion says so in German (B6).

Every call of a Blockly function binds its parameters in a frame of ITS OWN
thread (``Interpreter._call_procedure``): inside a body a name is that call's
parameter or else the global — exactly the preview's ``global`` declarations. A
hat and the main stack calling one function therefore never see each other's
arguments, a global of the same name as a parameter is never touched, and a
nested call reads the global, not its caller's parameter.

The interleavings are FORCED with ``threading.Event`` s, never a ``Barrier`` or
a sleep: with a Barrier the last thread through keeps the GIL, finishes and
restores before the first one reads again, so the race never shows (review-2
MJ-2).

The recursion pins run every program in a FRESH thread, as the robot runs a
program in its own thread (in pytest's main thread the limits are lower).
"""

from __future__ import annotations

import json
import threading

from physical_ai_server.workflow import handlers
from physical_ai_server.workflow import interpreter as interp_mod
from physical_ai_server.workflow.interpreter import Interpreter, InterpreterError

_TOO_DEEP_SENTENCE = ('Das Programm ist zu tief verschachtelt — eine Funktion ruft '
                      'sich zu oft selbst auf, oder Blöcke stecken zu tief ineinander.')


class _Ctx:
    def __init__(self):
        self.variables: dict = {}
        self.counters: dict = {}
        self.logs: list = []
        self.log = self.logs.append
        self.should_stop = lambda: False
        self.var_lock = threading.RLock()
        self.motion_lock = threading.RLock()


def _v(name):
    return {'id': 'v_' + name}


def _vget(name):
    return {'type': 'variables_get', 'fields': {'VAR': _v(name)}}


def _vset(name, value):
    return {'type': 'variables_set', 'fields': {'VAR': _v(name)},
            'inputs': {'VALUE': {'block': value}}}


def _text(s):
    return {'type': 'text', 'fields': {'TEXT': s}}


def _num(x):
    return {'type': 'math_number', 'fields': {'NUM': x}}


def _arith(op, a, b):
    return {'type': 'math_arithmetic', 'fields': {'OP': op},
            'inputs': {'A': {'block': a}, 'B': {'block': b}}}


def _compare(op, a, b):
    return {'type': 'logic_compare', 'fields': {'OP': op},
            'inputs': {'A': {'block': a}, 'B': {'block': b}}}


def _log(value):
    return {'type': 'edubotics_log', 'inputs': {'MESSAGE': {'block': value}}}


def _chain(*blocks):
    for a, b in zip(blocks, blocks[1:]):
        a['next'] = {'block': b}
    return blocks[0]


def _call(name, args, params, *, value=False):
    return {'type': 'procedures_callreturn' if value else 'procedures_callnoreturn',
            'extraState': {'name': name, 'params': list(params)},
            'inputs': {f'ARG{k}': {'block': a} for k, a in enumerate(args)}}


def _pdef(name, params, stack=None, ret=None):
    b = {'type': 'procedures_defreturn' if ret is not None else 'procedures_defnoreturn',
         'fields': {'NAME': name},
         'extraState': {'params': [{'name': p, 'id': 'v_' + p} for p in params]},
         'inputs': {}}
    if stack is not None:
        b['inputs']['STACK'] = {'block': stack}
    if ret is not None:
        b['inputs']['RETURN'] = {'block': ret}
    return b


def _ws(blocks, names):
    return json.dumps({'blocks': {'languageVersion': 0, 'blocks': blocks},
                       'variables': [{'name': n, 'id': 'v_' + n} for n in names]})


def _noop(*_a):
    return None


def _run_threads(targets):
    """Run ``{tag: callable}`` each in its own thread; return ``{tag: result or
    exception}``."""
    out: dict = {}

    def wrap(tag, fn):
        try:
            out[tag] = fn()
        except BaseException as e:  # noqa: BLE001 — the datum
            out[tag] = e

    threads = [threading.Thread(target=wrap, args=(tag, fn), name=tag)
               for tag, fn in targets.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
        assert not t.is_alive(), f'thread {t.name} did not end'
    return out


# ── M1: concurrent calls of one function ──────────────────────────────────


def test_two_threads_calling_one_function_each_see_their_own_argument(monkeypatch):
    """fahre(ziel) logs its argument twice. A logs once, then waits until B has
    logged once; B enters its call only after A's first log and waits after its
    first log until A has logged twice. On 47aea6fa A read B's argument and B's
    binding was popped by A's restore: {'A': ['Start', 'Ablage'], 'B': ['Ablage', None]}."""
    a_logged_once = threading.Event()
    b_logged_once = threading.Event()
    a_logged_twice = threading.Event()
    seen = {'A': [], 'B': []}

    def probe(ctx, args):
        me = threading.current_thread().name
        seen[me].append(args.get('message'))
        if me == 'A' and len(seen['A']) == 1:
            a_logged_once.set()
            assert b_logged_once.wait(5)
        elif me == 'A' and len(seen['A']) == 2:
            a_logged_twice.set()
        elif me == 'B' and len(seen['B']) == 1:
            b_logged_once.set()
            assert a_logged_twice.wait(5)

    monkeypatch.setitem(handlers.STATEMENT_HANDLERS, 'edubotics_log', probe)
    interp = Interpreter.from_json(_ws(
        [_pdef('fahre', ['ziel'], _chain(_log(_vget('ziel')), _log(_vget('ziel'))))],
        ['ziel']))
    ctx = _Ctx()

    def a():
        interp._exec_chain(_call('fahre', [_text('Start')], ['ziel']), ctx, _noop)

    def b():
        assert a_logged_once.wait(5)
        interp._exec_chain(_call('fahre', [_text('Ablage')], ['ziel']), ctx, _noop)

    out = _run_threads({'A': a, 'B': b})
    assert out == {'A': None, 'B': None}, out
    assert seen == {'A': ['Start', 'Start'], 'B': ['Ablage', 'Ablage']}, seen
    assert 'ziel' not in ctx.variables


def test_a_call_never_touches_a_global_of_the_same_name(monkeypatch):
    """While B is parked inside fahre('Ablage'), the main thread reads the
    global „ziel": it is still the global's value (47aea6fa: 'Ablage', the
    call had overwritten it for its whole duration)."""
    b_inside = threading.Event()
    main_read = threading.Event()
    seen = []

    def probe(ctx, args):
        seen.append(args.get('message'))
        if len(seen) == 1:
            b_inside.set()
            assert main_read.wait(5)

    monkeypatch.setitem(handlers.STATEMENT_HANDLERS, 'edubotics_log', probe)
    interp = Interpreter.from_json(_ws(
        [_pdef('fahre', ['ziel'], _chain(_log(_vget('ziel')), _log(_vget('ziel'))))],
        ['ziel']))
    ctx = _Ctx()
    ctx.variables['ziel'] = 'Global'
    read = {}

    def b():
        interp._exec_chain(_call('fahre', [_text('Ablage')], ['ziel']), ctx, _noop)

    def main():
        assert b_inside.wait(5)
        try:
            read['ziel'] = interp._eval_value(_vget('ziel'), ctx)
            read['dict'] = ctx.variables.get('ziel')
        finally:
            main_read.set()

    out = _run_threads({'B': b, 'main': main})
    assert out == {'B': None, 'main': None}, out
    assert read == {'ziel': 'Global', 'dict': 'Global'}, read
    assert seen == ['Ablage', 'Ablage'], seen
    assert ctx.variables['ziel'] == 'Global'


def _summe_ws():
    """summe(n): log n; gib 0 zurück, falls n ≤ 0; sonst n + summe(n − 1)."""
    ifret = {'type': 'procedures_ifreturn', 'extraState': '<mutation value="1"></mutation>',
             'inputs': {'CONDITION': {'block': _compare('LTE', _vget('n'), _num(0))},
                        'VALUE': {'block': _num(0)}}}
    rec = _arith('ADD', _vget('n'),
                 _call('summe', [_arith('MINUS', _vget('n'), _num(1))], ['n'], value=True))
    return _ws([_pdef('summe', ['n'], _chain(_log(_vget('n')), ifret), rec)], ['n'])


def test_concurrent_recursion_keeps_each_threads_sum(monkeypatch):
    """summe(6) in A and summe(10) in B, B entering while A is parked at its
    first level: 21 and 55 (47aea6fa: A read B's n and summed 55, then popped
    the binding B still needed, so B's next read was empty)."""
    a_logged_once = threading.Event()
    b_logged_once = threading.Event()
    a_done = threading.Event()
    counts = {'A': 0, 'B': 0}

    def probe(ctx, args):
        me = threading.current_thread().name
        counts[me] += 1
        if me == 'A' and counts['A'] == 1:
            a_logged_once.set()
            assert b_logged_once.wait(5)
        elif me == 'B' and counts['B'] == 1:
            b_logged_once.set()
            assert a_done.wait(5)

    monkeypatch.setitem(handlers.STATEMENT_HANDLERS, 'edubotics_log', probe)
    interp = Interpreter.from_json(_summe_ws())
    ctx = _Ctx()
    # What execute()/execute_chain() bind; a value call goes through it.
    ctx.call_procedure = lambda name, args: interp._call_procedure(name, args, ctx, _noop)

    def a():
        try:
            return interp._call_procedure('summe', [6.0], ctx, _noop)
        finally:
            a_done.set()

    def b():
        assert a_logged_once.wait(5)
        return interp._call_procedure('summe', [10.0], ctx, _noop)

    out = _run_threads({'A': a, 'B': b})
    assert out == {'A': 21.0, 'B': 55.0}, out


def test_a_nested_call_reads_the_global_not_its_callers_parameter(monkeypatch):
    """OQ-4 A, lexical scope: innen() inside aussen(ziel) reads the GLOBAL
    „ziel", as the preview's ``global ziel`` in innen does (47aea6fa: the
    caller's argument)."""
    seen = []
    monkeypatch.setitem(handlers.STATEMENT_HANDLERS, 'edubotics_log',
                        lambda ctx, args: seen.append(args.get('message')))
    innen = _pdef('innen', [], _log(_vget('ziel')))
    aussen = _pdef('aussen', ['ziel'], _chain(_log(_vget('ziel')), _call('innen', [], [])))
    main = _chain(_vset('ziel', _text('GLOBAL')),
                  _call('aussen', [_text('ARG')], ['ziel']))
    interp = Interpreter.from_json(_ws([innen, aussen, main], ['ziel']))
    ctx = _Ctx()
    interp.execute(ctx, _noop)
    assert seen == ['ARG', 'GLOBAL'], seen
    assert ctx.variables['ziel'] == 'GLOBAL'


def test_a_parameter_write_stays_in_the_call():
    """„setze ziel" on a parameter writes the call's own binding: the global of
    the same name keeps its value (the preview's parameter is a local)."""
    body = _vset('ziel', _text('IN DER FUNKTION'))
    main = _chain(_vset('ziel', _text('GLOBAL')),
                  _call('f', [_text('ARG')], ['ziel']))
    interp = Interpreter.from_json(_ws([_pdef('f', ['ziel'], body), main], ['ziel']))
    ctx = _Ctx()
    interp.execute(ctx, _noop)
    assert ctx.variables['ziel'] == 'GLOBAL'


# ── B6: recursion depth (pins) and the German sentence ────────────────────


def _recursion_ws(n, value_call):
    """f(n): falls n > 0, rufe f(n − 1) (as a statement, or inside „setze r")."""
    call = _call('f', [_arith('MINUS', _vget('n'), _num(1))], ['n'], value=value_call)
    body_call = _vset('r', call) if value_call else call
    iff = {'type': 'controls_if',
           'inputs': {'IF0': {'block': _compare('GT', _vget('n'), _num(0))},
                      'DO0': {'block': body_call}}}
    d = _pdef('f', ['n'], iff, _num(1) if value_call else None)
    main = _call('f', [_num(n)], ['n'])
    return _ws([d, main], ['n', 'r'])


def _tief_ws(n):
    """The review corpus' `tief`: gib 1 + tief(a − 1) zurück — the recursive
    call INSIDE a math socket, the shape a socket helper's extra frame shows
    up in."""
    ifret = {'type': 'procedures_ifreturn', 'extraState': '<mutation value="1"></mutation>',
             'inputs': {'CONDITION': {'block': _compare('LTE', _vget('a'), _num(0))},
                        'VALUE': {'block': _num(0)}}}
    rec = _arith('ADD', _num(1),
                 _call('tief', [_arith('MINUS', _vget('a'), _num(1))], ['a'], value=True))
    d = _pdef('tief', ['a'], ifret, rec)
    main = _vset('r', _call('tief', [_num(n)], ['a'], value=True))
    return _ws([d, main], ['a', 'r'])


def _run_in_fresh_thread(ws):
    out = {}

    def run():
        try:
            ctx = _Ctx()
            Interpreter.from_json(ws).execute(ctx, _noop)
            out['err'] = None
            out['r'] = ctx.variables.get('r')
        except Exception as e:  # noqa: BLE001 — the datum
            out['err'] = e

    t = threading.Thread(target=run)
    t.start()
    t.join(60)
    assert not t.is_alive()
    return out


def test_statement_recursion_reaches_135_levels():
    """PIN: measured 140 levels in a fresh thread (47aea6fa and with frames)."""
    out = _run_in_fresh_thread(_recursion_ws(135, value_call=False))
    assert out['err'] is None, out


def test_value_recursion_reaches_95_levels():
    """PIN: measured 98 levels in a fresh thread."""
    out = _run_in_fresh_thread(_recursion_ws(95, value_call=True))
    assert out['err'] is None, out


def test_tief_recursion_inside_a_math_socket_reaches_155_levels():
    """THE FRAME PIN: measured 163 levels; a socket helper that evaluated the
    socket itself (one extra Python frame per math socket) drops it to 140, so
    this fails exactly when the evaluation chain grows."""
    out = _run_in_fresh_thread(_tief_ws(155))
    assert out['err'] is None, out
    assert out['r'] == 155.0


def test_far_too_deep_recursion_says_so_in_german():
    """Depth 2000 runs out of Python's stack: the student reads why, not „Der
    Block „Funktionsaufruf" konnte nicht ausgewertet werden."."""
    out = _run_in_fresh_thread(_recursion_ws(2000, value_call=True))
    err = out['err']
    assert isinstance(err, InterpreterError), out
    assert str(err) == _TOO_DEEP_SENTENCE, str(err)
    assert getattr(interp_mod, '_TOO_DEEP_DE', None) == _TOO_DEEP_SENTENCE
