#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The „Code"-Vorschau of a robot block is a call of the REAL text API
(Principle 2 of the 2026-10-18 semantics round: robot_api.py is the only
source for robot blocks; F8).

``pythonCodeGen.api.cases.test.js`` (vitest) renders every block that has a
robot_api row — with every input filled by a valid value of its parameter's
kind, and with every input empty — plus the hats, a broadcast, the Tempo
dropdown and an un-pinned pin, and writes the previews into
``preview-api-cases.json``. Judged here with the server's own table:

* the whole preview parses and imports ``robot`` first;
* every ``robot.X(…)`` names a public row, with an arity and keywords that
  row accepts;
* every LITERAL argument of a filled case passes ``code_rpc.validate_value``
  for its parameter (an empty input renders as ``None``, which a non-nullable
  kind refuses, so the empty cases are only parsed and must name their row);
* a hat is a ``def when_…():`` and a broadcast calls a function that the
  module defines.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from physical_ai_server.workflow import code_rpc, robot_api

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE = (_REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components'
            / 'Workshop' / '__tests__' / 'fixtures' / 'preview-api-cases.json')

_PUBLIC = {c.name: c for c in robot_api.ROBOT_API + tuple(robot_api.CODE_ONLY_METHODS)}


def _cases():
    return json.loads(_FIXTURE.read_text(encoding='utf-8'))['cases']


def _robot_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'robot'):
            yield node


_NO_LITERAL = object()


def _literal(node: ast.AST):
    """The value of a literal argument, or ``_NO_LITERAL`` for an expression
    (a nested robot call, a variable)."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return _NO_LITERAL


def _bind(row, call: ast.Call) -> tuple[list[str], dict]:
    """Bind the call's arguments to the row's parameters like Python would:
    ``(errors, {param name: arg node})``."""
    errors: list[str] = []
    names = [p.name for p in row.params]
    bound: dict = {}
    if len(call.args) > len(names):
        errors.append(f'robot.{row.name}: {len(call.args)} positional arguments, '
                      f'the row takes {len(names)}')
    for p_name, arg in zip(names, call.args):
        bound[p_name] = arg
    for kw in call.keywords:
        if kw.arg not in names:
            errors.append(f'robot.{row.name}: no parameter {kw.arg}=')
        elif kw.arg in bound:
            errors.append(f'robot.{row.name}: {kw.arg} given twice')
        else:
            bound[kw.arg] = kw.value
    for p in row.params:
        if p.required and p.name not in bound:
            errors.append(f'robot.{row.name}: the required {p.name} is missing')
    return errors, bound


def _check(case) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(case['py'])
    except SyntaxError as e:
        return [f'SyntaxError: {e}']
    first = tree.body[0] if tree.body else None
    if not (isinstance(first, ast.Import) and [a.name for a in first.names] == ['robot']):
        errors.append('the preview does not start with `import robot`')
    called = set()
    for call in _robot_calls(tree):
        row = _PUBLIC.get(call.func.attr)
        if row is None:
            errors.append(f'robot.{call.func.attr}: no such public method')
            continue
        called.add(row.name)
        bind_errors, bound = _bind(row, call)
        errors.extend(bind_errors)
        if case['kind'] != 'filled':
            continue
        for p in row.params:
            if p.name not in bound:
                continue
            value = _literal(bound[p.name])
            if value is _NO_LITERAL:
                continue
            reason = code_rpc.validate_value(p, value)
            if reason is not None:
                errors.append(f'robot.{row.name}({p.name}={value!r}): {reason}')
    if case.get('row') and case['row'] not in called:
        errors.append(f'the preview never calls robot.{case["row"]}')
    return errors


def test_every_robot_call_is_a_valid_row_call():
    cases = _cases()
    rows_with_block = {c.name for c in robot_api.ROBOT_API if c.block_type}
    filled = {c['row'] for c in cases if c['kind'] == 'filled' and c.get('row')}
    empty = {c['row'] for c in cases if c['kind'] == 'empty' and c.get('row')}
    assert filled == rows_with_block, sorted(rows_with_block ^ filled)
    assert empty == rows_with_block, sorted(rows_with_block ^ empty)
    wrong = {c['name']: e for c in cases if (e := _check(c))}
    assert not wrong, json.dumps(wrong, ensure_ascii=False, indent=1)


def test_the_hats_are_functions_and_a_broadcast_calls_them():
    by_name = {c['name']: c for c in _cases()}
    tree = ast.parse(by_name['hats_and_broadcast']['py'])
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert all(name.startswith('when_') for name in defined), defined
    # Two hats on one message, one object hat, one counter hat.
    assert len(defined) == 4, defined
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    start_hats = {name for name in defined if 'start' in name}
    assert len(start_hats) == 2 and start_hats <= called, (start_hats, called)
    assert called <= defined, called - defined
    # Every hat says, in German, that it runs by itself.
    src = by_name['hats_and_broadcast']['py']
    assert src.count('# Läuft von selbst') == 4, src
    # A broadcast nobody waits for calls nothing and still parses.
    lonely = ast.parse(by_name['broadcast_without_hat']['py'])
    assert not [n for n in ast.walk(lonely) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)]


def test_tempo_and_an_unpinned_pin_are_end_of_line_comments():
    by_name = {c['name']: c for c in _cases()}
    tempo = by_name['tempo']['py']
    assert "robot.move_to(robot.ziel('Ablage'))  # mit Tempo „schnell“" in tempo, tempo
    assert 'robot.lift()  # mit Tempo „langsam“' in tempo, tempo
    pin = by_name['pin_unpinned']['py']
    assert "robot.pin('B', None, None, None)  # nicht gepinnt" in pin, pin
    filled = by_name['edubotics_destination_pin:filled']['py']
    assert "robot.pin('Ablage', 0.182, -0.05, 0.03)" in filled, filled
    assert 'nicht gepinnt' not in filled


def test_the_empty_program_is_just_the_import():
    by_name = {c['name']: c for c in _cases()}
    assert by_name['empty_program']['py'] == 'import robot\n'
