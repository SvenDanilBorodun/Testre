#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""AST fences on ``physical_ai_server.py.__init__`` (M7 / R2-1 of the review).

``physical_ai_server.py`` imports rclpy and cannot be imported in the deps-free
or the lerobot pytest env, so these read the source and parse it — the repo's
compare-against-an-artifact idiom (test_robot_profiles / test_sim_node_wiring),
chosen over ``git show`` because ci.yml::python-tests checks out shallow (P35),
so base 7efaeb4 is not reachable from that job and ci.yml may not be edited.
"""

from __future__ import annotations

import ast
from pathlib import Path

_NODE = (Path(__file__).resolve().parents[1]
         / 'physical_ai_server' / 'physical_ai_server.py')
_SRC = _NODE.read_text(encoding='utf-8')
_ARBITER_FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'arbiter_methods_base.py'


def _class(name: str) -> ast.ClassDef:
    tree = ast.parse(_SRC)
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _is_self_call(stmt: ast.AST, method: str) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == method
            and isinstance(stmt.value.func.value, ast.Name)
            and stmt.value.func.value.id == 'self')


def test_init_robot_profile_is_still_the_last_statement_of_init():
    """The degraded-boot contract: _init_robot_profile() stays LAST, and the one
    new statement is `try: self._init_code_rpc()` immediately before it."""
    init = _method(_class('PhysicalAIServer'), '__init__')
    assert _is_self_call(init.body[-1], '_init_robot_profile'), (
        'the last statement of __init__ is no longer self._init_robot_profile()')
    prev = init.body[-2]
    assert isinstance(prev, ast.Try) and prev.body, (
        'the statement before _init_robot_profile() is not a try block')
    assert _is_self_call(prev.body[0], '_init_code_rpc'), (
        'the new statement before _init_robot_profile() is not '
        'self._init_code_rpc() inside its own try')


def test_only_one_init_code_rpc_call_in_init():
    init = _method(_class('PhysicalAIServer'), '__init__')
    calls = [n for n in ast.walk(init)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == '_init_code_rpc']
    assert len(calls) == 1, f'expected one _init_code_rpc() call, found {len(calls)}'


# ── R2-1: the four arbiter methods are AST-identical to base 7efaeb4 ─────────
# The manual-mode arbiter + on_workflow claim/release must stay byte-identical
# to base. DEVIATION FROM THE SPEC (recorded): the spec pins sha256 of
# ``ast.dump`` of each method to the P34 literals — but ``ast.dump`` output is
# NOT stable across Python minors (proven: the same method hashes to
# 8a6a708f… under 3.14 and cf413059… under 3.12), and this suite runs under
# BOTH the 3.11 CI job (ci.yml::python-tests) and the 3.12 local pytest, so no
# single pinned digest passes everywhere. Instead: compare ``ast.dump(live)``
# to ``ast.dump(base)`` where BASE is the verbatim base source
# (fixtures/arbiter_methods_base.py) parsed by the SAME running interpreter —
# the version cancels, and it still fails on any change (proven by inserting a
# statement into a scratch copy). NOT a ``git diff -U0`` hunk grep (git's
# funcname heuristic names the column-0 class line, never a method at indent 4);
# NOT a ``git show`` at test time (python-tests is a shallow checkout, P35).
_ARBITER_NAMES = ('_assert_no_other_active', 'workflow_start_callback',
                  '_launch_workflow', '_on_workflow_finished')


def test_the_four_arbiter_methods_are_ast_identical_to_base():
    live = _class('PhysicalAIServer')
    base = next(n for n in ast.parse(_ARBITER_FIXTURE.read_text(encoding='utf-8')).body
                if isinstance(n, ast.ClassDef) and n.name == '_Base')
    base_fns = {n.name: n for n in base.body if isinstance(n, ast.FunctionDef)}
    for name in _ARBITER_NAMES:
        assert name in base_fns, f'{name} missing from the base fixture'
        assert ast.dump(_method(live, name)) == ast.dump(base_fns[name]), (
            f'{name} changed — the manual-mode arbiter / on_workflow claim must '
            f'stay byte-identical to base 7efaeb4')
