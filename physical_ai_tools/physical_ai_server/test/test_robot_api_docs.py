#!/usr/bin/env python3
"""The Methoden column's data (``robot_api_docs.json``) is complete, German
and RUNNABLE.

``workflow/robot_api.py`` carries, beside each public row, the texts and the
example the editor's „Methoden" column shows (``ApiParam.doc_de``,
``ApiCall.returns_de`` / ``doc_de_java`` / ``example`` / ``example_var``) and
the two types a text program meets (``TYPE_DOCS``). Every fence here is about
that column never telling a student something the robot does not do:

- every parameter of every public row is explained in German, without a
  Python-ism a Java student cannot read (``None``, ``True``, ``[x, y, z]``,
  ``snake_case`` names);
- a value row says what comes back, a statement does not;
- a row whose own ``doc_de`` names Python carries a Java sentence
  (``doc_de_java``) and only such a row does;
- every example passes the robot's OWN validator (``code_rpc._validate_call``,
  the trailing defaults appended, a variable argument standing as handle 0),
  every Python example parses (the Java ones are compiled by
  ``robotis_ai_setup/tests/test_robot_api_examples_java.py``);
- the new fields are JSON-only: the five older artifacts render the same bytes
  whether the fields are filled or empty, and ``robot_api.json`` (in the entry
  bundle) carries none of them.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import re
from pathlib import Path

from physical_ai_server.workflow import code_rpc
from physical_ai_server.workflow import robot_api


_REPO = Path(__file__).resolve().parents[3]
_PUBLIC = robot_api.ROBOT_API + robot_api.CODE_ONLY_METHODS
_DOCS = json.loads(robot_api.export_docs_json())

# What a Java student cannot read in a sentence about Python: the three
# Python constants, a Python list literal of a point, a snake_case name.
_PYTHON_ISM = re.compile(
    r'\b(?:None|True|False)\b|\[x, y, z\]|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')
# A `code` span is a name, not prose (the plan's „backtick spans stripped").
_BACKTICK_SPAN = re.compile(r'`[^`]*`')

# The rows whose doc_de names Python — found by running _PYTHON_ISM over the
# table; the list pins it, so a new row that names Python must bring a Java
# sentence (or reword) consciously.
_JAVA_DOC_ROWS = frozenset({
    'move_to', 'pin', 'ziel', 'wait_until_seen', 'wait_until_held', 'find',
    'object_position',
})


def _load_gdl():
    path = _REPO / '.github' / 'scripts' / 'german_detail_lint.py'
    spec = importlib.util.spec_from_file_location('gdl', path)
    gdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gdl)
    return gdl


_GDL = _load_gdl()


def _is_german(s: str) -> bool:
    """The shipped predicate (test_robot_api._is_german), backtick spans
    stripped: a German marker AND no transliteration."""
    text = _BACKTICK_SPAN.sub('', s)
    return bool(_GDL.GERMAN_CHARS.search(text) or _GDL.GERMAN_WORDS.search(text)) \
        and not _GDL.TRANSLITERATIONS.search(text)


def _has_python_ism(s: str) -> bool:
    return bool(_PYTHON_ISM.search(s))


def _example_args(call: robot_api.ApiCall) -> list:
    """The example as the wire would carry it: a variable as handle 0, the
    defaults the stub appends after the given positionals."""
    args = [0 if isinstance(a, robot_api.ExampleRef) else a for a in call.example]
    args += [p.default for p in call.params[len(call.example):]]
    return args


# ── the texts ─────────────────────────────────────────────────────────────

def test_the_public_rows_are_the_33_of_the_column():
    assert len(_PUBLIC) == 33
    assert [m['name'] for m in _DOCS['methods']] == [c.name for c in _PUBLIC]


def test_every_param_is_explained_in_german_without_a_python_ism():
    for call in _PUBLIC:
        for p in call.params:
            assert p.doc_de.strip(), (call.name, p.name)
            assert _is_german(p.doc_de), (call.name, p.name, p.doc_de)
            assert not _has_python_ism(p.doc_de), (call.name, p.name, p.doc_de)


def test_a_value_row_says_what_comes_back_and_a_statement_does_not():
    for call in _PUBLIC:
        if call.table == 'value':
            assert _is_german(call.returns_de), (call.name, call.returns_de)
            assert not _has_python_ism(call.returns_de), (call.name, call.returns_de)
        else:
            assert call.returns_de == '', call.name


def test_a_java_sentence_exactly_where_doc_de_names_python():
    named_python = {c.name for c in _PUBLIC if _has_python_ism(c.doc_de)}
    assert named_python == _JAVA_DOC_ROWS
    for call in _PUBLIC:
        if call.name in _JAVA_DOC_ROWS:
            assert _is_german(call.doc_de_java), (call.name, call.doc_de_java)
            assert not _has_python_ism(call.doc_de_java), (call.name, call.doc_de_java)
        else:
            assert call.doc_de_java == '', call.name


def test_the_python_ism_fence_bites():
    assert _has_python_ism('sonst None.')
    assert _has_python_ism('False, wenn …')
    assert _has_python_ism('ein Punkt [x, y, z] in Metern')
    assert _has_python_ism('für move_to und drop_at')
    assert not _has_python_ism('ein Punkt (x, y, z) in Metern')
    assert not _has_python_ism('false, wenn es nach timeout Sekunden nicht da ist')
    assert not _has_python_ism('für moveTo und dropAt')


def test_internal_rows_carry_no_column_data():
    for call in robot_api.INTERNAL_METHODS:
        assert (call.returns_de, call.doc_de_java, call.example, call.example_var) \
            == ('', '', (), ''), call.name
        assert all(p.doc_de == '' for p in call.params), call.name


# ── the examples ──────────────────────────────────────────────────────────

def test_every_example_has_the_right_number_of_arguments():
    for call in _PUBLIC:
        required = sum(1 for p in call.params if p.required)
        assert required <= len(call.example) <= len(call.params), call.name


def test_every_example_passes_the_robots_own_validator():
    for call in _PUBLIC:
        assert code_rpc._validate_call(call, _example_args(call)) is None, (
            call.name, _example_args(call))


def test_the_validator_check_bites():
    move_to = robot_api.ROBOT_API_BY_NAME['move_to']
    bad = dataclasses.replace(move_to, example=('Ablage!',))
    assert code_rpc._validate_call(bad, _example_args(bad)) is not None
    tone = robot_api.ROBOT_API_BY_NAME['tone']
    bad = dataclasses.replace(tone, example=(40.0, 0.5))
    assert code_rpc._validate_call(bad, _example_args(bad)) is not None


def test_example_var_exactly_on_value_rows_and_an_identifier():
    for call in _PUBLIC:
        if call.table == 'value':
            assert call.example_var and call.example_var.isidentifier(), call.name
        else:
            assert call.example_var == '', call.name


def test_every_python_example_parses_and_every_java_example_is_one_statement():
    for m in _DOCS['methods']:
        ast.parse(m['example_py'])
        assert '\n' not in m['example_py'] and '\n' not in m['example_java'], m['name']
        assert m['example_java'].endswith(';'), m['name']
        assert m['example_py'].startswith(
            f"{m['example']['var']} = robot." if m['example']['var'] else 'robot.'), m['name']


def test_the_example_lines_are_spelled_by_param_kind():
    by = {m['name']: m for m in _DOCS['methods']}
    assert by['tone']['example_py'] == 'robot.tone(440.0, 0.5)'
    assert by['tone']['example']['args'] == [440.0, 0.5]
    assert by['wait']['example_java'] == 'Robot.waitSeconds(1.5);'
    assert by['find']['example_java'] == 'Robot.Greifziel ziel = Robot.find("wuerfel");'
    assert by['object_position']['example']['args'] == [{'ref': 'ziel'}]
    assert by['object_position']['example_py'] == 'position = robot.object_position(ziel)'
    assert by['object_position']['example_java'] == \
        'double[] position = Robot.objectPosition(ziel);'
    assert by['pin']['example_java'] == 'Robot.pin("Ablage", 0.2, 0.0, 0.05);'
    assert by['zeige']['example_py'] == 'robot.zeige("Anzahl", 3)'
    assert by['is_holding']['example'] == {'args': [], 'var': 'gehalten'}
    assert by['home']['example'] == {'args': [], 'var': None}


# ── the signatures ────────────────────────────────────────────────────────

def test_signatures_are_one_python_and_every_java_overload():
    by = {m['name']: m for m in _DOCS['methods']}
    for call in _PUBLIC:
        m = by[call.name]
        assert len(m['signatures_java']) == len(robot_api._java_overload_specs(call))
        assert m['signature_py'].startswith(f'robot.{call.name}(')
        for sig in m['signatures_java']:
            assert f' Robot.{call.java_name}(' in sig, sig
            # The nested class is spelled as a student writes it outside Robot.
            assert not re.search(r'(?<![.\w])Greifziel\b', sig), sig
    assert by['replay']['signature_py'] == 'robot.replay(name, speed=1.0)'
    assert by['replay']['signatures_java'] == [
        'void Robot.replay(String name, double speed)', 'void Robot.replay(String name)']
    assert by['find']['signatures_java'][0] == 'Robot.Greifziel Robot.find(String obj)'


def test_the_overload_specs_are_what_the_stub_renders():
    java = robot_api.render_java_robot()
    for call in _PUBLIC:
        for spec in robot_api._java_overload_specs(call):
            params = ', '.join(f'{t} {n}' for t, n in spec.params)
            assert f'public static {spec.ret_type} {call.java_name}({params}) {{' in java, (
                call.name, params)


# ── the types ─────────────────────────────────────────────────────────────

def test_the_two_types_are_documented_in_german_with_a_parsing_example():
    assert [t['name'] for t in _DOCS['types']] == ['Greifziel', 'Greifobjekt']
    for t in robot_api.TYPE_DOCS:
        assert _is_german(t.doc_de), t.name
        ast.parse(t.example_py)
        assert t.example_java.rstrip().endswith((';', '}')), t.name
    greifobjekt = next(t for t in robot_api.TYPE_DOCS if t.name == 'Greifobjekt')
    # M3: the Java starter imports only edubotics.Robot.
    assert 'new edubotics.Greifobjekt(' in greifobjekt.example_java


# ── JSON-only ─────────────────────────────────────────────────────────────

def _strip(call: robot_api.ApiCall) -> robot_api.ApiCall:
    return dataclasses.replace(
        call, returns_de='', doc_de_java='', example=(), example_var='',
        params=tuple(dataclasses.replace(p, doc_de='') for p in call.params))


def test_the_older_five_artifacts_do_not_read_the_column_data(monkeypatch):
    before = robot_api.render_all()
    monkeypatch.setattr(robot_api, 'ROBOT_API', tuple(_strip(c) for c in robot_api.ROBOT_API))
    monkeypatch.setattr(robot_api, 'CODE_ONLY_METHODS',
                        tuple(_strip(c) for c in robot_api.CODE_ONLY_METHODS))
    after = robot_api.render_all()
    docs = robot_api.GENERATED_PATHS['docs_json']
    for rel in before:
        if rel != docs:
            assert after[rel] == before[rel], rel
    assert after[docs] != before[docs]


def test_robot_api_json_carries_no_column_key():
    doc = json.loads(robot_api.export_json())
    for m in doc['methods'] + doc['internal']:
        assert set(m) == {'name', 'java_name', 'block_type', 'table', 'budget', 'returns',
                          'doc_de', 'params'}, m['name']
        for p in m['params']:
            assert 'doc_de' not in p and 'java_name' not in p, (m['name'], p['name'])
    assert 'types' not in doc
    assert 'export_docs_json' not in robot_api.export_json()


def test_the_docs_json_names_its_generator():
    # The marker the entry-bundle grep of the build looks for (spec §10.1):
    # it must sit in exactly one lazy chunk, never in the entry.
    assert _DOCS['generated_by'] == \
        'physical_ai_server/workflow/robot_api.py::export_docs_json'
