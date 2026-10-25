#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The preview is the meaning of a block (Principle 1, semantics round 2026-10-18).

For Blockly's OWN blocks the „Code"-Vorschau shows the output of Blockly's
Python generator. ``blocklyConformance.cases.test.js`` (vitest) builds a corpus
of built-in-block programs with headless Blockly and writes, per program, the
workspace JSON and the preview's Python into
``blockly-conformance-cases.json``. This test executes BOTH: the preview with
CPython, the workspace with ``workflow/interpreter.py``. They must agree — the
same variables, or both raise — except for the deliberate deviations in
``INTENTIONAL``, each with its reason. A listed case that agrees, an entry no
case uses, and a disagreement that is not listed all fail.

A program the interpreter refuses fails loud in German (an
``InterpreterError``): never a raw Python exception, never English.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from physical_ai_server.workflow import interpreter as interp_mod
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import Interpreter, InterpreterError

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE = (_REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components'
            / 'Workshop' / '__tests__' / 'fixtures' / 'blockly-conformance-cases.json')

# The complete list of deliberate deviations from the preview, by number (the
# spec's numbering) → (reason, the corpus cases that exercise it).
INTENTIONAL: dict[int, tuple[str, tuple[str, ...]]] = {
    1: ('„und"/„oder" answer wahr or falsch, never one of their operands '
        '(the preview returns the operand: 3 and "x" is "x").',
        ('and_value', 'or_value')),
    2: ('A list element is numbered from 1. Element 0 and an element past '
        'the end are refused in German; the preview’s L[-1] silently reads '
        'the LAST element.',
        ('getidx_0',)),
    3: ('A power with no real result (a negative base, a fractional exponent) '
        'is refused; the preview hands a complex number to the next block.',
        ('pow_complex',)),
    4: ('„beschränke" with LOW > HIGH swaps the bounds; the preview '
        'answers HIGH.',
        ('constrain_low_gt_high',)),
    5: ('„Zufallszahl von 5 bis 1" swaps the bounds; the preview raises.',
        ('random_from_gt_to',)),
    6: ('„ist leer" of an empty value (a variable that holds nothing) is '
        'wahr; the preview raises on len(None).',
        ('isempty_empty_socket',)),
    7: ('„erstelle Text aus" renders values the way every other student '
        'surface does (student_text): 2.0 reads „2", a list „[1, b]"; the '
        'preview uses str() („2.0", "[1, \'b\']").',
        ('join_float_and_list',)),
    8: ('The loop, list and text caps (MAX_LOOP_ITERATIONS, MAX_LIST_ITEMS, '
        'MAX_LIST_CREATE_ITEMS, MAX_TEXT_CHARS) refuse in German where the '
        'preview would keep going.',
        ('cap_for_loop', 'cap_lists_repeat', 'cap_lists_create_with',
         'cap_text_join')),
}

# Cases whose VALUE is random: only „raises or not" is compared.
_RANDOM_PREFIXES = ('randint_', 'random_')

# English fragments of Python's own exception text that must never reach a
# student through an interpreter refusal.
_PYTHON_ENGLISH = (
    'could not convert', 'invalid literal', 'unsupported operand',
    'object is not', 'has no len', "can't multiply", 'must be real number',
    'not all arguments converted', 'Traceback', 'Error:',
)


def _gdl():
    spec = importlib.util.spec_from_file_location(
        'gdl', _REPO / '.github' / 'scripts' / 'german_detail_lint.py')
    gdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gdl)
    return gdl


def _german_violation(gdl, text: str) -> str | None:
    """german_detail_lint's own rule for one string: a transliteration is
    always a violation; English stopwords are one only when the text carries
    no German marker. Too little text to carry a language is neutral."""
    import re
    if len(re.sub(r'[^A-Za-zäöüßÄÖÜ]', '', text)) < 4:
        return None
    m = gdl.TRANSLITERATIONS.search(text)
    if m:
        return f'TRANSLITERATION {m.group(0)!r}'
    is_german = bool(gdl.GERMAN_CHARS.search(text) or gdl.GERMAN_WORDS.search(text))
    if not is_german and gdl.ENGLISH_WORDS.search(text):
        return 'ENGLISH'
    return None


class _Ctx:
    """Just enough context for built-in blocks: nothing here moves an arm."""

    def __init__(self):
        self.variables: dict = {}
        self.counters: dict = {}
        self.destinations: dict = {}
        self.logs: list[str] = []
        self.should_stop = lambda: False
        self.log = self.logs.append
        self.publisher = lambda _points: None
        self.ik = None
        self.perception = None
        self.last_arm_joints = None
        self.last_full_joints = [0.0] * 6
        self.z_table = 0.0
        self.motion_lock = threading.RLock()
        self.var_lock = threading.RLock()


def _norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


@contextlib.contextmanager
def _fake_robot_module():
    """The preview starts with `import robot` once it names the robot API; a
    corpus program never calls it, so an empty module is enough."""
    had = 'robot' in sys.modules
    old = sys.modules.get('robot')
    sys.modules['robot'] = types.ModuleType('robot')
    try:
        yield
    finally:
        if had:
            sys.modules['robot'] = old
        else:
            sys.modules.pop('robot', None)


def _run_preview(case) -> tuple[str | None, dict]:
    names = [v['name'] for v in case['ws'].get('variables', [])]
    ns: dict = {'__name__': '__main__'}
    try:
        with _fake_robot_module():
            exec(compile(case['py'], case['name'], 'exec'), ns)  # noqa: S102 — the corpus is ours
    except Exception as e:  # noqa: BLE001 — what the preview raises is the datum
        return type(e).__name__, {}
    return None, {n: _norm(ns.get(n)) for n in names if ns.get(n) is not None}


def _run_interpreter(case) -> tuple[BaseException | None, dict]:
    ctx = _Ctx()
    try:
        Interpreter.from_json(json.dumps(case['ws'])).execute(ctx, lambda *a: None)
    except Exception as e:  # noqa: BLE001
        return e, {}
    return None, {n: _norm(v) for n, v in ctx.variables.items() if v is not None}


def _load_cases():
    data = json.loads(_FIXTURE.read_text(encoding='utf-8'))
    return data['cases']


def _disagreements():
    out = {}
    for case in _load_cases():
        ref_err, ref = _run_preview(case)
        got_err, got = _run_interpreter(case)
        if case['name'].startswith(_RANDOM_PREFIXES):
            same = (ref_err is None) == (got_err is None)
        else:
            same = ((ref_err is None) == (got_err is None)
                    and (ref_err is not None or ref == got))
        if not same:
            out[case['name']] = (ref_err or ref,
                                 f'{type(got_err).__name__}: {got_err}' if got_err else got)
    return out


def test_corpus_matches_the_preview():
    cases = _load_cases()
    assert len(cases) >= 150, 'the corpus lost its programs'
    names = {c['name'] for c in cases}
    listed = {name for _reason, case_names in INTENTIONAL.values() for name in case_names}
    missing = sorted(listed - names)
    assert not missing, f'INTENTIONAL names cases the corpus does not have: {missing}'
    bad = _disagreements()
    unlisted = {n: v for n, v in bad.items() if n not in listed}
    stale = sorted(listed - set(bad))
    unused = sorted(k for k, (_r, case_names) in INTENTIONAL.items()
                    if not any(n in bad for n in case_names))
    lines = [f'  {n}\n      preview    : {p!r}\n      interpreter: {i!r}'
             for n, (p, i) in sorted(unlisted.items())]
    assert not unlisted, ('the interpreter disagrees with the preview:\n'
                          + '\n'.join(lines))
    assert not stale, f'INTENTIONAL cases that now agree with the preview (stale): {stale}'
    assert not unused, f'INTENTIONAL entries no case uses: {unused}'


def test_every_interpreter_refusal_in_the_corpus_is_german():
    gdl = _gdl()
    wrong = []
    for case in _load_cases():
        err, _ = _run_interpreter(case)
        if err is None:
            continue
        text = str(err)
        if not isinstance(err, (InterpreterError, WorkflowError)):
            wrong.append((case['name'], type(err).__name__, text))
            continue
        violation = _german_violation(gdl, text)
        if violation or any(f in text for f in _PYTHON_ENGLISH):
            wrong.append((case['name'], violation or 'python text', text))
    assert not wrong, wrong


def test_a_math_block_with_text_refuses_with_the_number_sentence():
    """The wrapper case of the spec: „runde" of a text never shows Python's
    „could not convert …" / „invalid literal …"."""
    ws = {
        'blocks': {'languageVersion': 0, 'blocks': [{
            'type': 'variables_set', 'id': 's', 'fields': {'VAR': {'id': 'v_r'}},
            'inputs': {'VALUE': {'block': {
                'type': 'math_round', 'id': 'm', 'fields': {'OP': 'ROUND'},
                'inputs': {'NUM': {'block': {'type': 'text', 'id': 't',
                                             'fields': {'TEXT': 'abc'}}}},
            }}},
        }]},
        'variables': [{'name': 'r', 'id': 'v_r'}],
    }
    with pytest.raises(InterpreterError) as ei:
        Interpreter.from_json(json.dumps(ws)).execute(_Ctx(), lambda *a: None)
    text = str(ei.value)
    assert 'could not convert' not in text and 'invalid literal' not in text, text
    assert 'Zahl' in text and 'abc' in text, text


def test_an_unexpected_error_inside_a_value_block_names_the_block_in_german():
    """The generic wrapper never appends Python's text: an overflow inside
    „runde" (round(inf)) reads as the block's German label."""
    ws = {
        'blocks': {'languageVersion': 0, 'blocks': [{
            'type': 'variables_set', 'id': 's', 'fields': {'VAR': {'id': 'v_r'}},
            'inputs': {'VALUE': {'block': {
                'type': 'math_round', 'id': 'm', 'fields': {'OP': 'ROUND'},
                'inputs': {'NUM': {'block': {
                    'type': 'math_arithmetic', 'id': 'a', 'fields': {'OP': 'MULTIPLY'},
                    'inputs': {
                        'A': {'block': {'type': 'math_number', 'id': 'n1', 'fields': {'NUM': 1e308}}},
                        'B': {'block': {'type': 'math_number', 'id': 'n2', 'fields': {'NUM': 10}}},
                    },
                }}},
            }}},
        }]},
        'variables': [{'name': 'r', 'id': 'v_r'}],
    }
    with pytest.raises(InterpreterError) as ei:
        Interpreter.from_json(json.dumps(ws)).execute(_Ctx(), lambda *a: None)
    text = str(ei.value)
    assert text.startswith('Der Block „'), text
    assert 'konnte nicht ausgewertet werden' in text, text
    assert 'infinity' not in text and 'Overflow' not in text and 'math_round' not in text, text


def _raise_messages(tree: ast.AST):
    """Every message literal / f-string passed to InterpreterError or
    WorkflowError, with the names of exceptions caught by an `except … as`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', None)
        if name not in ('InterpreterError', 'WorkflowError') or not node.args:
            continue
        yield node.args[0]


def _caught_names(tree: ast.AST) -> set[str]:
    return {h.name for h in ast.walk(tree)
            if isinstance(h, ast.ExceptHandler) and h.name}


def _text_of(node: ast.AST) -> str:
    return ' '.join(n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))


def test_every_interpreter_raise_is_german():
    gdl = _gdl()
    source = Path(interp_mod.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    caught = _caught_names(tree)
    wrong = []
    n = 0
    for msg in _raise_messages(tree):
        n += 1
        text = _text_of(msg).strip()
        violation = _german_violation(gdl, text)
        if violation:
            wrong.append((msg.lineno, violation, text))
        # Never Python's own text: no f-string slot that formats a caught
        # exception (`{e}`), however German the rest of the sentence is.
        for v in ast.walk(msg):
            if (isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)
                    and v.value.id in caught):
                wrong.append((msg.lineno, f'formats the caught exception {{{v.value.id}}}', text))
        if any(f in text for f in _PYTHON_ENGLISH):
            wrong.append((msg.lineno, 'python text', text))
    assert n >= 30, f'the walk found only {n} raise messages'
    assert not wrong, wrong
