#!/usr/bin/env python3
"""The interpreter must agree with what Blockly actually SERIALIZES and RUNS.

Every shape asserted here was captured from the shipped Blockly 12.5.1
(`physical_ai_manager/node_modules/blockly`) by building the block headlessly and
reading `serialization.workspaces.save()`; every semantic was captured from the
same block's `pythonGenerator` output. That is the point of the file: the bugs it
guards were all "the runtime invented a shape the editor never produces", and
they are invisible to any test that hand-writes the payload it wishes for.

The captured shapes, verbatim:

  procedures_callnoreturn -> {"type":…, "extraState":{"name":"verdopple",
                              "params":["n"]}}          # NO "fields" key at all
  procedures_defnoreturn  -> {"fields":{"NAME":"verdopple"},
                              "extraState":{"params":[{"name":"n","id":"…"}]}}
  variables_get/set       -> {"fields":{"VAR":{"id":"…"}}}   # NO "name"
  workspace               -> {"blocks":…, "variables":[{"name":"n","id":"…"}]}
  a disabled block        -> {"disabledReasons":["manually_disabled"]}
  controls_if 2+else      -> {"extraState":{"elseIfCount":2,"hasElse":true}}
  lists_getIndex REMOVE   -> {"extraState":{"isStatement":true},
                              "fields":{"MODE":"REMOVE",…}}  # a STATEMENT
  variable flyout         -> [variables_set, math_change, variables_get]

And the generated Python that fixes the list semantics:

  getIndex GET_REMOVE FROM_START at=2 -> L.pop(1)
  getIndex REMOVE     LAST            -> L.pop()
  setIndex INSERT     LAST            -> L.append(v)
  setIndex INSERT     FROM_START at=2 -> L.insert(1, v)
  math_change                         -> x = (x if isinstance(x, Number) else 0) + d
"""

from __future__ import annotations

import ast
import inspect
import json
import threading
import time

import pytest

from physical_ai_server.workflow.interpreter import (
    Interpreter,
    InterpreterError,
    MAX_LIST_CREATE_ITEMS,
    MAX_LIST_ITEMS,
    MAX_TEXT_CHARS,
    _BUILTIN_VALUE_TYPES,
)
from physical_ai_server.workflow.handlers.motion import WorkflowError


# ── harness ──────────────────────────────────────────────────────────────────

class _Ctx:
    """Minimal WorkflowContext stand-in: enough for the pure block semantics."""

    def __init__(self):
        self.variables: dict = {}
        self.counters: dict = {}
        self.destinations: dict = {}
        self.logs: list[str] = []
        self.should_stop = lambda: False
        self.log = self.logs.append
        self.publisher = lambda _p: None
        self.ik = None
        self.perception = None
        self.last_arm_joints = None
        self.last_full_joints = [0.0] * 6
        self.z_table = 0.0
        self.motion_lock = threading.RLock()
        self.var_lock = threading.RLock()


def _ws(blocks: list[dict], **siblings) -> str:
    payload = {'blocks': {'languageVersion': 0, 'blocks': blocks}}
    payload.update(siblings)
    return json.dumps(payload)


def _run(blocks: list[dict], ctx: _Ctx | None = None, **siblings) -> _Ctx:
    ctx = ctx or _Ctx()
    Interpreter.from_json(_ws(blocks, **siblings)).execute(ctx, lambda *_a: None)
    return ctx


def _set_text(name: str, value: str) -> dict:
    return {'type': 'variables_set', 'fields': {'VAR': name},
            'inputs': {'VALUE': {'block': {'type': 'text',
                                           'fields': {'TEXT': value}}}}}


def _num(value) -> dict:
    return {'type': 'math_number', 'fields': {'NUM': value}}


def _list_of(*values) -> dict:
    return {'type': 'lists_create_with',
            'inputs': {f'ADD{i}': {'block': _num(v)}
                       for i, v in enumerate(values)}}


TRUE = {'type': 'logic_boolean', 'fields': {'BOOL': 'TRUE'}}
FALSE = {'type': 'logic_boolean', 'fields': {'BOOL': 'FALSE'}}


# ── RS-01 / RS-23 — the Funktionen category ──────────────────────────────────

def _procedure_program(def_type: str, call_block: dict) -> list[dict]:
    """A def whose body copies its parameter into `ergebnis`, plus `call_block`.

    Every variable reference uses the id-only shape Blockly really writes.
    """
    body = {'type': 'variables_set', 'fields': {'VAR': {'id': 'out-id'}},
            'inputs': {'VALUE': {'block': {'type': 'variables_get',
                                           'fields': {'VAR': {'id': 'p-id'}}}}}}
    return [
        {'type': def_type, 'id': 'def1', 'fields': {'NAME': 'verdopple'},
         'extraState': {'params': [{'name': 'n', 'id': 'p-id'}]},
         'inputs': {'STACK': {'block': body}}},
        call_block,
    ]


def test_a_procedure_call_reads_its_name_from_extrastate():
    """RS-01. The call block has NO `fields` key — NAME is a non-serializable
    FieldLabel — so reading fields.NAME aborted every run with
    „Funktionsaufruf ohne Namen." and the whole category was unusable."""
    call = {'type': 'procedures_callnoreturn', 'id': 'c1',
            'extraState': {'name': 'verdopple', 'params': ['n']},
            'inputs': {'ARG0': {'block': _num(21)}}}
    ctx = _run(_procedure_program('procedures_defnoreturn', call),
               variables=[{'name': 'n', 'id': 'p-id'},
                          {'name': 'ergebnis', 'id': 'out-id'}])
    assert ctx.variables['ergebnis'] == 21.0


def test_a_parameterised_procedure_returns_the_right_value():
    """RS-01 + RS-23 together. Fixing the call-name lookup alone leaves the
    parameter unbound (the caller writes by NAME, the body reads by ID), so the
    procedure would run and return None. This is the end-to-end guard."""
    ret = {'type': 'math_arithmetic', 'fields': {'OP': 'ADD'},
           'inputs': {'A': {'block': {'type': 'variables_get',
                                      'fields': {'VAR': {'id': 'p-id'}}}},
                      'B': {'block': _num(1)}}}
    program = [
        {'type': 'procedures_defreturn', 'fields': {'NAME': 'plus1'},
         'extraState': {'params': [{'name': 'n', 'id': 'p-id'}]},
         'inputs': {'RETURN': {'block': ret}}},
        {'type': 'variables_set', 'fields': {'VAR': {'id': 'out-id'}},
         'inputs': {'VALUE': {'block': {
             'type': 'procedures_callreturn',
             'extraState': {'name': 'plus1', 'params': ['n']},
             'inputs': {'ARG0': {'block': _num(41)}}}}}},
    ]
    ctx = _run(program, variables=[{'name': 'n', 'id': 'p-id'},
                                   {'name': 'ergebnis', 'id': 'out-id'}])
    assert ctx.variables['ergebnis'] == 42.0, (
        'a parameterised procedure must return f(arg), not None')


def test_a_procedure_parameter_binds_without_a_variables_table():
    """The def's own extraState carries {name, id} for each param, so the
    binding must survive a payload that was trimmed of its variables table."""
    call = {'type': 'procedures_callnoreturn',
            'extraState': {'name': 'verdopple', 'params': ['n']},
            'inputs': {'ARG0': {'block': _num(7)}}}
    ctx = _run(_procedure_program('procedures_defnoreturn', call))
    # The body's own output variable has no entry in any table here, so it keeps
    # its raw id as the key — but it holds the ARGUMENT, which is the point: the
    # param id resolved to 'n' through the def's extraState, so the body's read
    # found what the caller wrote.
    assert ctx.variables['out-id'] == 7.0
    # And the param itself is restored (popped) on the way out, so it does not
    # leak into the caller's scope.
    assert 'n' not in ctx.variables


def test_a_call_with_no_resolvable_name_fails_loud_in_both_forms():
    """The returning form used to `return None` silently."""
    for btype, wrapper in (
        ('procedures_callnoreturn', lambda b: b),
        ('procedures_callreturn',
         lambda b: {'type': 'variables_set', 'fields': {'VAR': 'x'},
                    'inputs': {'VALUE': {'block': b}}}),
    ):
        with pytest.raises(InterpreterError) as exc:
            _run([wrapper({'type': btype, 'extraState': {'params': []}})])
        assert 'Funktionsaufruf ohne Namen' in str(exc.value)


def test_a_hand_written_fields_name_still_works():
    """The fields.NAME fallback stays, for imported / hand-written JSON."""
    call = {'type': 'procedures_callnoreturn', 'fields': {'NAME': 'verdopple'},
            'inputs': {'ARG0': {'block': _num(3)}}}
    ctx = _run(_procedure_program('procedures_defnoreturn', call),
               variables=[{'name': 'n', 'id': 'p-id'},
                          {'name': 'ergebnis', 'id': 'out-id'}])
    assert ctx.variables['ergebnis'] == 3.0


# ── RS-23 / RS-49 — variables are keyed by the student's own name ────────────

def test_a_variable_is_keyed_by_its_human_name_not_its_blockly_id():
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': {'id': 'v-id'}},
                 'inputs': {'VALUE': {'block': _num(7)}}}],
               variables=[{'name': 'zaehler', 'id': 'v-id'}])
    assert ctx.variables == {'zaehler': 7.0}


def test_the_variable_inspector_sentinel_carries_the_human_name():
    """RS-49. The React inspector requires a readable name; a 20-char Blockly id
    (they contain `(`, `[`, `~`, backticks …) is dropped by every shape of that
    guard, so the panel was dead for every student variable."""
    ctx = _run([{'type': 'variables_set',
                 'fields': {'VAR': {'id': 'Xo~ikga(Yr[.Te]B4`=s'}},
                 'inputs': {'VALUE': {'block': _num(7)}}}],
               variables=[{'name': 'zaehler', 'id': 'Xo~ikga(Yr[.Te]B4`=s'}])
    assert ctx.logs == ['[VAR:zaehler=7.0]']


def test_an_unknown_variable_id_still_gets_a_consistent_key():
    """Fail-safe: no variables table at all must not drop the write."""
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': {'id': 'orphan'}},
                 'inputs': {'VALUE': {'block': _num(1)}}}])
    assert ctx.variables == {'orphan': 1.0}


# ── RS-05 — disabled blocks ──────────────────────────────────────────────────

@pytest.mark.parametrize('flag', [
    {'disabledReasons': ['manually_disabled']},   # Blockly >= 11 / what we save
    {'enabled': False},                           # legacy saved workflows
])
def test_a_disabled_block_does_not_run(flag):
    ctx = _run([dict(_set_text('ran', 'JA'), **flag)])
    assert ctx.variables == {}, 'a greyed-out block must not execute'


def test_a_disabled_block_does_not_run_its_inner_content():
    ctx = _run([{'type': 'controls_if',
                 'disabledReasons': ['manually_disabled'],
                 'inputs': {'IF0': {'block': TRUE},
                            'DO0': {'block': _set_text('inner', 'JA')}}}])
    assert ctx.variables == {}


def test_a_disabled_block_still_carries_its_enabled_next_chain():
    """The `next` chain is NOT skipped — a disabled block can carry enabled
    ones below it, and dropping them would silently delete the rest of the
    program."""
    ctx = _run([dict(_set_text('a', '1'),
                     disabledReasons=['manually_disabled'],
                     next={'block': _set_text('b', '2')})])
    assert ctx.variables == {'b': '2'}


def test_a_disabled_value_block_reads_as_an_empty_socket():
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
                 'inputs': {'VALUE': {'block': dict(
                     _num(5), disabledReasons=['manually_disabled'])}}}])
    assert ctx.variables == {'x': None}


def test_a_disabled_hat_spawns_no_handler():
    interp = Interpreter.from_json(_ws([
        {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'go'},
         'disabledReasons': ['manually_disabled']},
        {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'los'}},
    ]))
    _main, hats = interp.split_roots()
    assert len(hats) == 1
    assert hats[0]['fields']['EVENT_NAME'] == 'los'


def test_a_top_level_ifreturn_gives_a_german_message_not_a_traceback():
    """RS-05's sharp edge / RS-56A. Blockly auto-disables an unparented
    procedures_ifreturn only in the RENDERED editor; the internal
    _ProcedureReturn used to escape to _run's catch-all and put
    traceback.format_exc() into the student-facing Protokoll."""
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'procedures_ifreturn',
               'inputs': {'CONDITION': {'block': TRUE}}}])
    message = str(exc.value)
    assert 'gib zurück' in message and 'Funktion' in message
    assert 'Traceback' not in message


# ── RS-10 — a parked value block is a no-op, not a fatal error ───────────────

@pytest.mark.parametrize('btype', [
    'math_arithmetic', 'logic_compare', 'text', 'variables_get',
    'lists_create_with', 'math_number', 'logic_boolean', 'text_join',
])
def test_a_parked_value_block_at_the_top_level_is_ignored(btype):
    """It has no previousStatement, so Blockly can only ever leave it lying at
    the top level, where the Code panel renders it harmlessly. It used to abort
    the whole program with the raw English type id."""
    ctx = _run([{'type': btype}, _set_text('after', 'JA')])
    assert ctx.variables == {'after': 'JA'}, (
        'a parked value block must not stop the program that follows it')


def test_a_genuinely_unknown_block_still_fails_loud():
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'edubotics_not_a_real_block'}])
    assert 'Unbekannter Block-Typ' in str(exc.value)


# ── RS-15 — controls_if with empty condition sockets ────────────────────────

def test_an_empty_if_socket_is_false_and_does_not_truncate_the_clause_scan():
    ctx = _run([{'type': 'controls_if',
                 'extraState': {'elseIfCount': 1, 'hasElse': True},
                 'inputs': {'IF1': {'block': TRUE},
                            'DO0': {'block': _set_text('r', 'DO0')},
                            'DO1': {'block': _set_text('r', 'DO1')},
                            'ELSE': {'block': _set_text('r', 'ELSE')}}}])
    assert ctx.variables['r'] == 'DO1', 'ran the else branch past a hole'


def test_an_empty_first_socket_with_no_else_still_reaches_the_true_clause():
    ctx = _run([{'type': 'controls_if', 'extraState': {'elseIfCount': 1},
                 'inputs': {'IF1': {'block': TRUE},
                            'DO1': {'block': _set_text('r', 'DO1')}}}])
    assert ctx.variables['r'] == 'DO1', 'ran nothing at all'


def test_a_hole_in_the_middle_does_not_skip_the_later_true_clause():
    ctx = _run([{'type': 'controls_if',
                 'extraState': {'elseIfCount': 2, 'hasElse': True},
                 'inputs': {'IF0': {'block': FALSE},
                            'IF2': {'block': TRUE},
                            'DO2': {'block': _set_text('r', 'DO2')},
                            'ELSE': {'block': _set_text('r', 'ELSE')}}}])
    assert ctx.variables['r'] == 'DO2'


def test_a_crafted_elseifcount_is_never_read(monkeypatch):
    """`extraState.elseIfCount` is not consulted AT ALL any more.

    It rides the untrusted /workflow/start payload and used to bound a
    `range()`, so it needed a clamp. But an index in `range(declared + 1)` that
    is absent from `inputs` always evaluates to False and can never change an
    outcome — verified over 1296 configurations, 0 differences — so its only
    effect was iteration COST. Not reading it removes the DoS surface instead
    of capping it.

    This REPLACES `test_a_crafted_elseifcount_cannot_pin_the_thread`, which the
    change makes vacuous: it asserted an outcome the clamp already guaranteed,
    and it would exhaust memory (not fail) if anyone re-introduced the
    `range(declared + 1)` scan without a clamp.
    """
    looked_up: list[str] = []
    original = Interpreter._get_input_block

    def counting(block, name):
        looked_up.append(name)
        return original(block, name)

    monkeypatch.setattr(Interpreter, '_get_input_block', staticmethod(counting))

    started = time.perf_counter()
    ctx = _run([{'type': 'controls_if',
                 'extraState': {'elseIfCount': 5000},
                 'inputs': {'ELSE': {'block': _set_text('r', 'ELSE')}}}])
    elapsed = time.perf_counter() - started

    assert ctx.variables['r'] == 'ELSE'
    assert elapsed < 1.0, f'the declared count was walked ({elapsed:.3f}s)'
    if_lookups = [n for n in looked_up if n.startswith('IF')]
    assert if_lookups == [], (
        f'elseIfCount was read after all: {len(if_lookups)} IF* lookups')


def test_a_hole_in_the_clause_sequence_is_still_scanned():
    """RS-15 must not regress now that `elseIfCount` is gone: the scan is over
    the IFk keys the payload actually CONTAINS, so a gap in the sequence is
    still reached. Carries no extraState at all, which is exactly what Blockly
    writes for a block whose clauses were built and then partly emptied."""
    ctx = _run([{'type': 'controls_if',
                 'inputs': {'IF0': {'block': FALSE},
                            'IF2': {'block': TRUE},
                            'DO2': {'block': _set_text('r', 'DO2')},
                            'ELSE': {'block': _set_text('r', 'ELSE')}}}])
    assert ctx.variables['r'] == 'DO2'


def test_the_else_branch_still_runs_when_every_clause_is_false():
    ctx = _run([{'type': 'controls_if',
                 'extraState': {'elseIfCount': 1, 'hasElse': True},
                 'inputs': {'IF0': {'block': FALSE}, 'IF1': {'block': FALSE},
                            'ELSE': {'block': _set_text('r', 'ELSE')}}}])
    assert ctx.variables['r'] == 'ELSE'


# ── RS-25 — math_change („ändere x um 1") ───────────────────────────────────

def test_math_change_adds_to_the_variable():
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
                 'inputs': {'VALUE': {'block': _num(5)}}},
                {'type': 'math_change', 'fields': {'VAR': 'x'},
                 'inputs': {'DELTA': {'block': _num(3)}}}])
    assert ctx.variables['x'] == 8.0


def test_math_change_treats_a_non_number_as_zero_like_blockly_does():
    """Blockly emits `x = (x if isinstance(x, Number) else 0) + delta`."""
    ctx = _run([_set_text('x', 'hallo'),
                {'type': 'math_change', 'fields': {'VAR': 'x'},
                 'inputs': {'DELTA': {'block': _num(3)}}}])
    assert ctx.variables['x'] == 3.0


def test_math_change_on_a_variable_that_does_not_exist_yet_starts_at_zero():
    ctx = _run([{'type': 'math_change', 'fields': {'VAR': 'neu'},
                 'inputs': {'DELTA': {'block': _num(2)}}}])
    assert ctx.variables['neu'] == 2.0


def test_math_change_resolves_the_variable_id_like_every_other_block():
    ctx = _run([{'type': 'math_change', 'fields': {'VAR': {'id': 'v-id'}},
                 'inputs': {'DELTA': {'block': _num(4)}}}],
               variables=[{'name': 'zaehler', 'id': 'v-id'}])
    assert ctx.variables == {'zaehler': 4.0}


# ── RS-43 — the list blocks must match Blockly's generated Python ───────────

def test_lists_getindex_remove_is_a_statement_and_removes():
    """MODE=REMOVE drops the output connection and gains previous/next, so it
    arrives as a STATEMENT — where it used to be an unknown block type."""
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': _list_of(1, 2, 3)}}},
        {'type': 'lists_getIndex', 'extraState': {'isStatement': True},
         'fields': {'MODE': 'REMOVE', 'WHERE': 'FROM_START'},
         'inputs': {'VALUE': {'block': {'type': 'variables_get',
                                        'fields': {'VAR': 'L'}}},
                    'AT': {'block': _num(2)}}},
    ])
    assert ctx.variables['L'] == [1.0, 3.0], 'Blockly generates L.pop(1)'


def test_lists_getindex_get_remove_actually_removes():
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': _list_of(1, 2, 3)}}},
        {'type': 'variables_set', 'fields': {'VAR': 'got'},
         'inputs': {'VALUE': {'block': {
             'type': 'lists_getIndex',
             'fields': {'MODE': 'GET_REMOVE', 'WHERE': 'FROM_START'},
             'inputs': {'VALUE': {'block': {'type': 'variables_get',
                                            'fields': {'VAR': 'L'}}},
                        'AT': {'block': _num(2)}}}}}},
    ])
    assert ctx.variables['got'] == 2.0
    assert ctx.variables['L'] == [1.0, 3.0], 'Blockly generates L.pop(1)'


def test_plain_get_does_not_mutate():
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': _list_of(1, 2, 3)}}},
        {'type': 'variables_set', 'fields': {'VAR': 'got'},
         'inputs': {'VALUE': {'block': {
             'type': 'lists_getIndex',
             'fields': {'MODE': 'GET', 'WHERE': 'LAST'},
             'inputs': {'VALUE': {'block': {'type': 'variables_get',
                                            'fields': {'VAR': 'L'}}}}}}}},
    ])
    assert ctx.variables['got'] == 3.0
    assert ctx.variables['L'] == [1.0, 2.0, 3.0]


def _set_index(mode: str, where: str, value, at=None) -> dict:
    inputs = {'LIST': {'block': {'type': 'variables_get',
                                 'fields': {'VAR': 'L'}}},
              'TO': {'block': _num(value)}}
    if at is not None:
        inputs['AT'] = {'block': _num(at)}
    return {'type': 'lists_setIndex',
            'fields': {'MODE': mode, 'WHERE': where}, 'inputs': inputs}


def test_insert_last_appends_like_blockly():
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': _list_of(1, 2, 3)}}},
        _set_index('INSERT', 'LAST', 99),
    ])
    assert ctx.variables['L'] == [1.0, 2.0, 3.0, 99.0], (
        'Blockly generates L.append(v), not an insert before the last item')


def test_insert_into_an_empty_list_appends():
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': {'type': 'lists_create_with'}}}},
        _set_index('INSERT', 'LAST', 99),
    ])
    assert ctx.variables['L'] == [99.0], (
        'building a list one item at a time must be possible')


def test_insert_one_past_the_end_appends():
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': _list_of(1, 2, 3)}}},
        _set_index('INSERT', 'FROM_START', 99, at=4),
    ])
    assert ctx.variables['L'] == [1.0, 2.0, 3.0, 99.0]


def test_set_still_requires_an_existing_element():
    """Blockly's `[][0] = v` raises; SET must not silently append."""
    with pytest.raises(InterpreterError):
        _run([
            {'type': 'variables_set', 'fields': {'VAR': 'L'},
             'inputs': {'VALUE': {'block': {'type': 'lists_create_with'}}}},
            _set_index('SET', 'FROM_START', 99, at=1),
        ])


def test_set_from_end_still_addresses_the_last_element():
    """Unchanged behaviour — Blockly generates L[-1] = v."""
    ctx = _run([
        {'type': 'variables_set', 'fields': {'VAR': 'L'},
         'inputs': {'VALUE': {'block': _list_of(1, 2, 3)}}},
        _set_index('SET', 'FROM_END', 99, at=1),
    ])
    assert ctx.variables['L'] == [1.0, 2.0, 99.0]


# ── RS-44 — the step-0 guard must be reachable ──────────────────────────────

def test_a_zero_step_for_loop_is_refused():
    """`float(x or 1)` made `0.0` become 1, so the guard could never fire and
    „in Schritten von 0" quietly ran 3 iterations and reported success."""
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'controls_for', 'fields': {'VAR': 'i'},
               'inputs': {'FROM': {'block': _num(1)},
                          'TO': {'block': _num(3)},
                          'BY': {'block': _num(0)},
                          'DO': {'block': _set_text('body', 'ran')}}}])
    assert 'Schrittweite 0' in str(exc.value)


def test_a_non_finite_step_is_refused_rather_than_running_zero_times():
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'controls_for', 'fields': {'VAR': 'i'},
               'inputs': {'FROM': {'block': _num(1)},
                          'TO': {'block': _num(3)},
                          'BY': {'block': {'type': 'text',
                                           'fields': {'TEXT': 'nan'}}},
                          'DO': {'block': _set_text('body', 'ran')}}}])
    assert 'Schrittweite' in str(exc.value)


def test_a_missing_step_still_defaults_to_one():
    ctx = _run([{'type': 'controls_for', 'fields': {'VAR': 'i'},
                 'inputs': {'FROM': {'block': _num(1)},
                            'TO': {'block': _num(3)},
                            'DO': {'block': {
                                'type': 'math_change', 'fields': {'VAR': 'n'},
                                'inputs': {'DELTA': {'block': _num(1)}}}}}}])
    assert ctx.variables['n'] == 3.0


# ── RS-53 — logic_compare must not swallow a type error into False ─────────

@pytest.mark.parametrize('op', ['LT', 'GT', 'LTE', 'GTE'])
@pytest.mark.parametrize('other', [
    {'type': 'lists_create_with', 'inputs': {'ADD0': {'block': _num(1)}}},
    {'type': 'text', 'fields': {'TEXT': 'a'}},
])
def test_comparing_incompatible_values_fails_loud(op, other):
    """It was False in BOTH directions at once, so flipping the operator never
    revealed the mistake and the sonst branch ran either way."""
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'variables_set', 'fields': {'VAR': 'r'},
               'inputs': {'VALUE': {'block': {
                   'type': 'logic_compare', 'fields': {'OP': op},
                   'inputs': {'A': {'block': other},
                              'B': {'block': _num(1)}}}}}}])
    assert 'vergleichen' in str(exc.value)


def test_an_empty_comparison_socket_names_the_real_problem():
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'variables_set', 'fields': {'VAR': 'r'},
               'inputs': {'VALUE': {'block': {
                   'type': 'logic_compare', 'fields': {'OP': 'LT'},
                   'inputs': {'B': {'block': _num(1)}}}}}}])
    assert 'fehlt ein Wert' in str(exc.value)


@pytest.mark.parametrize('op,a,b,expected', [
    ('LT', 1, 2, True), ('GT', 2, 1, True), ('LTE', 2, 2, True),
    ('GTE', 1, 2, False), ('EQ', 2, 2, True), ('NEQ', 1, 2, True),
])
def test_ordinary_numeric_comparisons_are_unchanged(op, a, b, expected):
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'r'},
                 'inputs': {'VALUE': {'block': {
                     'type': 'logic_compare', 'fields': {'OP': op},
                     'inputs': {'A': {'block': _num(a)},
                                'B': {'block': _num(b)}}}}}}])
    assert ctx.variables['r'] is expected


def test_equality_across_types_is_still_allowed():
    """`EQ`/`NEQ` never raise in Python and must keep answering."""
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'r'},
                 'inputs': {'VALUE': {'block': {
                     'type': 'logic_compare', 'fields': {'OP': 'NEQ'},
                     'inputs': {'A': {'block': {'type': 'text',
                                                'fields': {'TEXT': 'a'}}},
                                'B': {'block': _num(1)}}}}}}])
    assert ctx.variables['r'] is True


# ── RS-54 — unbounded list growth ──────────────────────────────────────────

def test_lists_repeat_is_capped():
    """Measured before the cap: 5 000 000 elements in 0.38 s from two blocks,
    inside the ROS node (container mem_limit 6 g)."""
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'variables_set', 'fields': {'VAR': 'L'},
               'inputs': {'VALUE': {'block': {
                   'type': 'lists_repeat',
                   'inputs': {'ITEM': {'block': _num(1)},
                              'NUM': {'block': _num(5_000_000)}}}}}}])
    assert 'begrenzt' in str(exc.value)


def test_a_reasonable_lists_repeat_still_works():
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'L'},
                 'inputs': {'VALUE': {'block': {
                     'type': 'lists_repeat',
                     'inputs': {'ITEM': {'block': _num(1)},
                                'NUM': {'block': _num(MAX_LIST_ITEMS)}}}}}}])
    assert len(ctx.variables['L']) == MAX_LIST_ITEMS


def test_the_variable_sentinel_does_not_serialize_a_huge_list():
    """The char cap was applied to the FINISHED string, so json.dumps ran over
    the whole object first (measured 0.74 s / ~268 MB for 10 M elements)."""
    ctx = _Ctx()
    Interpreter([])._set_variable(ctx, 'L', list(range(200_000)))
    assert len(ctx.logs) == 1
    assert len(ctx.logs[0]) < 3000
    assert 'Elemente' in ctx.logs[0], 'the truncation must be visible'


def test_a_small_value_is_still_serialized_whole():
    ctx = _Ctx()
    Interpreter([])._set_variable(ctx, 'L', [1, 2, 3])
    assert ctx.logs == ['[VAR:L=[1, 2, 3]]']


# ── RS-56A — a non-string EVENT_NAME must not reach the Protokoll ──────────

@pytest.mark.parametrize('raw', [3, None, [1], {'a': 1}, True])
def test_a_non_string_event_name_never_raises_a_python_error(raw):
    ctx = _run([{'type': 'edubotics_broadcast', 'fields': {'EVENT_NAME': raw}},
                _set_text('after', 'JA')])
    assert ctx.variables == {'after': 'JA'}


def test_a_numeric_event_name_still_pairs_up():
    from physical_ai_server.workflow.interpreter import event_name_of
    assert event_name_of({'fields': {'EVENT_NAME': 3}}) == '3'
    assert event_name_of({'fields': {'EVENT_NAME': ' go '}}) == 'go'
    assert event_name_of({'fields': {'EVENT_NAME': None}}) == ''
    assert event_name_of(None) == ''


# ── RS-35 — blocks below „wiederhole fortlaufend" ─────────────────────────

def test_blocks_under_a_forever_loop_are_reported_as_dead_code():
    """They are silent dead code (the loop's only exit is Stop). Removing the
    block's nextStatement is the real fix but is NOT backward-safe alone —
    Blockly's loader raises MissingConnection on any saved workspace that
    already has a block there — so the runtime says so instead."""
    ctx = _Ctx()
    stops = {'n': 0}

    def _stop():
        stops['n'] += 1
        return stops['n'] > 10
    ctx.should_stop = _stop
    with pytest.raises(WorkflowError):
        _run([{'type': 'edubotics_forever',
               'inputs': {'DO': {'block': _set_text('loop', 'ran')}},
               'next': {'block': _set_text('after', 'ran')}}], ctx=ctx)
    warnings = [m for m in ctx.logs if 'WARNUNG' in m]
    assert len(warnings) == 1, 'exactly once, not once per iteration'
    assert 'wiederhole fortlaufend' in warnings[0]
    assert 'after' not in ctx.variables


def test_a_forever_loop_with_nothing_after_it_says_nothing():
    ctx = _Ctx()
    stops = {'n': 0}

    def _stop():
        stops['n'] += 1
        return stops['n'] > 5
    ctx.should_stop = _stop
    with pytest.raises(WorkflowError):
        _run([{'type': 'edubotics_forever',
               'inputs': {'DO': {'block': _set_text('loop', 'ran')}}}], ctx=ctx)
    assert [m for m in ctx.logs if 'WARNUNG' in m] == []


# ── RS-24 — the destination pre-check must see real workspaces ─────────────

def _pin(name: str, x, y, z, nxt: dict | None = None) -> dict:
    block = {'type': 'edubotics_destination_pin', 'id': f'pin-{name}',
             'fields': {'NAME': name, 'X': x, 'Y': y, 'Z': z}}
    if nxt is not None:
        block['next'] = {'block': nxt}
    return block


def _move_to(name: str, block_id: str = 'm1') -> dict:
    return {'type': 'edubotics_move_to', 'id': block_id,
            'inputs': {'DESTINATION': {'block': {
                'type': 'edubotics_destination_ref',
                'fields': {'NAME': name}}}}}


def test_a_pinned_destination_referenced_by_name_is_pre_checked():
    """destination_pin is a STATEMENT with no output connection, so Blockly can
    NEVER place it inside a value input — which is the only shape the old
    pre-check matched. It therefore returned [] for every real workspace while
    React fully implemented the warning consumer."""
    interp = Interpreter.from_json(_ws([_pin('Weit', 5.0, 0.0, 0.0,
                                             _move_to('Weit'))]))
    found = interp.collect_concrete_destinations()
    assert [(f['block_id'], f['xyz']) for f in found] == [('m1', (5.0, 0.0, 0.0))]


def test_a_pin_in_a_later_top_level_stack_still_resolves():
    """Warnings must not appear or vanish with the creation order of two
    independent top-level stacks."""
    interp = Interpreter.from_json(_ws([
        _move_to('Weit'),
        _pin('Weit', 5.0, 0.0, 0.0),
    ]))
    assert len(interp.collect_concrete_destinations()) == 1


def test_an_unpinned_or_unknown_name_is_not_reported():
    interp = Interpreter.from_json(_ws([
        _pin('A', '—', '—', '—', _move_to('A', 'm-unpinned')),
        _move_to('Fehlt', 'm-missing'),
    ]))
    assert interp.collect_concrete_destinations() == []


def test_a_non_finite_pin_is_not_treated_as_concrete():
    """applyPinnedCoordinates writes Number(v).toFixed(3), and
    Number(NaN).toFixed(3) === "NaN", which float() happily parses."""
    for bad in ('NaN', 'Infinity', '1e400'):
        interp = Interpreter.from_json(_ws([_pin('A', bad, 0.0, 0.0,
                                                 _move_to('A'))]))
        assert interp.collect_concrete_destinations() == [], bad


def test_a_disabled_move_block_is_not_pre_checked():
    interp = Interpreter.from_json(_ws([
        _pin('Weit', 5.0, 0.0, 0.0,
             dict(_move_to('Weit'), disabledReasons=['manually_disabled'])),
    ]))
    assert interp.collect_concrete_destinations() == []


def test_pickup_and_drop_at_are_pre_checked_too():
    for btype, slot in (('edubotics_pickup', 'TARGET'),
                        ('edubotics_drop_at', 'DESTINATION')):
        consumer = {'type': btype, 'id': 'c1',
                    'inputs': {slot: {'block': {
                        'type': 'edubotics_destination_ref',
                        'fields': {'NAME': 'A'}}}}}
        interp = Interpreter.from_json(_ws([_pin('A', 5.0, 0.0, 0.0, consumer)]))
        assert len(interp.collect_concrete_destinations()) == 1, btype


def test_targets_only_inside_a_hat_are_still_not_pre_checked():
    """Unchanged: hat handlers fire too rarely to be worth flagging upfront."""
    interp = Interpreter.from_json(_ws([
        _pin('A', 5.0, 0.0, 0.0),
        {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'go'},
         'next': {'block': _move_to('A')}},
    ]))
    assert interp.collect_concrete_destinations() == []


# ── RS-60 — a PARKED value block is a no-op, exhaustively ───────────────────

# The 21 types spelled out. Deliberately NOT derived from _BUILTIN_VALUE_TYPES:
# a parametrisation read from the frozenset under test passes for whatever that
# frozenset happens to contain, including an empty one.
_PARKED_VALUE_TYPES = (
    'math_number', 'text', 'text_join', 'logic_boolean', 'logic_negate',
    'logic_compare', 'logic_operation', 'math_arithmetic', 'math_random_int',
    'math_constrain', 'math_modulo', 'math_round', 'variables_get',
    'lists_create_with', 'lists_repeat', 'lists_length', 'lists_isEmpty',
    'lists_indexOf', 'lists_getIndex', 'lists_getSublist',
    'procedures_callreturn',
)


@pytest.mark.parametrize('btype', _PARKED_VALUE_TYPES)
def test_every_builtin_value_type_is_a_no_op_when_parked(btype):
    """A value block has no previousStatement, so Blockly can only leave it
    lying at the TOP LEVEL, where the Code panel renders it harmlessly
    (pythonCodeGen's scrubNakedValue). The runtime must agree — for ALL of
    them, not just the eight the earlier test happened to list."""
    ctx = _run([{'type': btype}, _set_text('after', 'JA')])
    assert ctx.variables == {'after': 'JA'}, (
        f'a parked {btype} stopped the program that followed it')


@pytest.mark.parametrize('mode', ['GET', 'GET_REMOVE'])
def test_a_parked_lists_getindex_value_block_is_a_no_op(mode):
    """`lists_getIndex` is the ONE type that is both a value block and a
    statement, and the btype ladder used to route EVERY one of them into the
    statement executor. Measured: MODE=GET and MODE=GET_REMOVE both aborted the
    whole program with „Entferne-Element-Block hat keine Liste." — naming an
    operation the student never chose — for a block they had merely parked on
    the canvas."""
    ctx = _run([{'type': 'lists_getIndex',
                 'fields': {'MODE': mode, 'WHERE': 'FROM_START'}},
                _set_text('after', 'JA')])
    assert ctx.variables == {'after': 'JA'}


@pytest.mark.parametrize('block', [
    # Blockly's own shape: MODE=REMOVE drops the output connection and records
    # isStatement in extraState.
    {'type': 'lists_getIndex', 'extraState': {'isStatement': True},
     'fields': {'MODE': 'REMOVE', 'WHERE': 'FROM_START'}},
    # A hand-written payload may carry only one of the two signals, because the
    # mutator DERIVES isStatement_ from MODE. Both must be honoured.
    {'type': 'lists_getIndex', 'fields': {'MODE': 'REMOVE',
                                          'WHERE': 'FROM_START'}},
    {'type': 'lists_getIndex', 'extraState': {'isStatement': True},
     'fields': {'MODE': 'GET', 'WHERE': 'FROM_START'}},
])
def test_the_statement_form_of_lists_getindex_still_fails_loud(block):
    """„entferne Element" with no list plugged in is a REAL error and must
    stay one — the parked-value skip must not swallow it."""
    with pytest.raises(InterpreterError) as exc:
        _run([block])
    # The message now NAMES the block the student dragged, in the block's
    # own vocabulary — „Entferne-Element-Block" was a schema name.
    assert '„entferne Element" braucht eine Liste' in str(exc.value)


def test_the_parked_value_allowlist_matches_the_value_evaluator():
    """_BUILTIN_VALUE_TYPES must be EXACTLY the set `_eval_value_impl` handles.

    A missing entry re-aborts a parked block with the raw English type id; an
    extra one swallows a genuine typo as a silent no-op. AST-walk the evaluator
    rather than trusting a hand-kept list (house style: cf.
    test_edu6_geometry.py's AST comparison of the two link tables).
    """
    from physical_ai_server.workflow import interpreter as module

    tree = ast.parse(inspect.getsource(module))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_eval_value_impl')
    handled = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name) and node.left.id == 'btype'
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)):
            handled.add(node.comparators[0].value)

    assert handled == set(_BUILTIN_VALUE_TYPES)
    # and the literal parametrisation above covers the same ground
    assert set(_PARKED_VALUE_TYPES) == set(_BUILTIN_VALUE_TYPES)


# ── RS-61 — a HOLE in a procedure's argument sockets ────────────────────────

def _two_arg_program(call_extra: dict | None, inputs: dict) -> list[dict]:
    """A def `addiere(a, b)` whose body copies `b` into `r`, plus one call.

    Reading the SECOND parameter is the point: the truncating scan bound every
    parameter to None the moment the FIRST socket was left empty.
    """
    body = {'type': 'variables_set', 'fields': {'VAR': {'id': 'r-id'}},
            'inputs': {'VALUE': {'block': {'type': 'variables_get',
                                           'fields': {'VAR': {'id': 'b-id'}}}}}}
    call = {'type': 'procedures_callnoreturn', 'inputs': inputs}
    if call_extra is not None:
        call['extraState'] = call_extra
    return [
        {'type': 'procedures_defnoreturn', 'fields': {'NAME': 'addiere'},
         'extraState': {'params': [{'name': 'a', 'id': 'a-id'},
                                   {'name': 'b', 'id': 'b-id'}]},
         'inputs': {'STACK': {'block': body}}},
        call,
    ]


def test_an_empty_first_argument_socket_does_not_discard_the_later_ones():
    """Blockly omits an EMPTY input from `inputs` entirely, so the old
    `while True: … if arg_input is None: break` scan stopped at the first gap.
    Measured: ARG0 empty + ARG1 = 42 bound (None, None) — the 42 was discarded
    silently, while the Code panel showed `addiere(None, 42)`."""
    ctx = _run(_two_arg_program(
        {'name': 'addiere', 'params': ['a', 'b']},
        {'ARG1': {'block': _num(42)}},
    ), variables=[{'name': 'r', 'id': 'r-id'},
                  {'name': 'a', 'id': 'a-id'},
                  {'name': 'b', 'id': 'b-id'}])
    assert ctx.variables['r'] == 42.0


def test_both_arguments_still_bind_when_both_are_filled():
    ctx = _run(_two_arg_program(
        {'name': 'addiere', 'params': ['a', 'b']},
        {'ARG0': {'block': _num(7)}, 'ARG1': {'block': _num(42)}},
    ), variables=[{'name': 'r', 'id': 'r-id'},
                  {'name': 'a', 'id': 'a-id'},
                  {'name': 'b', 'id': 'b-id'}])
    assert ctx.variables['r'] == 42.0


def test_a_call_without_extrastate_params_falls_back_to_the_definitions_arity():
    """A hand-written or trimmed payload may carry only the call's NAME. The
    registered DEFINITION knows the arity, so the hole is still positional."""
    ctx = _run(_two_arg_program(
        {'name': 'addiere'},
        {'ARG1': {'block': _num(42)}},
    ), variables=[{'name': 'r', 'id': 'r-id'},
                  {'name': 'a', 'id': 'a-id'},
                  {'name': 'b', 'id': 'b-id'}])
    assert ctx.variables['r'] == 42.0


def test_the_returning_form_reads_the_name_before_the_arguments():
    """An unnamed call must fail BEFORE any argument SIDE EFFECT runs.

    The argument here is a call to a real function that writes a variable, so
    the assertion distinguishes the two orders: evaluate-then-name leaves the
    write behind, name-then-evaluate does not.
    """
    side_effect_def = {
        'type': 'procedures_defreturn', 'fields': {'NAME': 'nebenwirkung'},
        'inputs': {'STACK': {'block': _set_text('spur', 'JA')}},
    }
    unnamed_call = {'type': 'procedures_callreturn',
                    'inputs': {'ARG0': {'block': {
                        'type': 'procedures_callreturn',
                        'extraState': {'name': 'nebenwirkung'}}}}}
    ctx = _Ctx()
    with pytest.raises(InterpreterError) as exc:
        _run([side_effect_def,
              {'type': 'variables_set', 'fields': {'VAR': 'x'},
               'inputs': {'VALUE': {'block': unnamed_call}}}], ctx)
    assert 'Funktionsaufruf ohne Namen.' in str(exc.value)
    assert 'spur' not in ctx.variables, (
        'the arguments of a call that cannot be named must not run')


# ── RS-62 — a legacy XML extraState on a procedure definition ──────────────

def test_a_legacy_xml_extrastate_on_a_procedure_definition_does_not_crash():
    """`extraState` is a raw XML STRING for a block that defines only
    mutationToDom (an unparented procedures_ifreturn really saves
    "<mutation value=\\"1\\"></mutation>"). `(… or {}).get('params')` therefore
    raised AttributeError out of Interpreter.__init__ → from_json, and
    WorkflowManager.start guards only `except InterpreterError` — so it reached
    the service callback as a raw Python error. Unreachable through the editor,
    reachable through a hand-written /workflow/start payload, and rosbridge
    authenticates nobody."""
    blocks = [
        {'type': 'procedures_defnoreturn', 'fields': {'NAME': 'tuwas'},
         'extraState': '<mutation></mutation>',
         'inputs': {'STACK': {'block': _set_text('inner', 'JA')}}},
        _set_text('after', 'JA'),
    ]
    ctx = _run(blocks)
    assert ctx.variables == {'after': 'JA'}


@pytest.mark.parametrize('extra', ['<mutation/>', 42, [], True])
def test_a_non_dict_extrastate_never_reaches_the_service_as_a_python_error(extra):
    interp = Interpreter.from_json(_ws([
        {'type': 'procedures_defreturn', 'fields': {'NAME': 'f'},
         'extraState': extra},
    ]))
    assert interp._procedures['f']['params'] == []


# ── RS-63 — text_join is capped ────────────────────────────────────────────

def _join(*pieces: str) -> dict:
    return {'type': 'text_join',
            'inputs': {f'ADD{i}': {'block': {'type': 'text',
                                             'fields': {'TEXT': p}}}
                       for i, p in enumerate(pieces)}}


def test_text_join_refuses_an_absurd_result():
    """Measured before the cap: `setze x auf verbinde(x, x)` inside „wiederhole
    fortlaufend" reached 1 073 741 824 characters / 2.4 GB RSS in 5.07 s from
    four blocks; two more doublings exceed the container's mem_limit of 6 g and
    OOM-kill the node, which `restart: "no"` does not bring back.

    LITERAL sizes on purpose: written against MAX_TEXT_CHARS the pair would
    pass for any value of it, including 0."""
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
               'inputs': {'VALUE': {'block': _join('x' * 6000, 'y' * 6000)}}}])
    assert 'begrenzt' in str(exc.value)
    assert 'Zeichen' in str(exc.value)


def test_a_realistic_message_is_never_refused():
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
                 'inputs': {'VALUE': {'block': _join(
                     'Ich sehe ', '3', ' Würfel')}}}])
    assert ctx.variables['x'] == 'Ich sehe 3 Würfel'


def test_a_page_of_text_is_still_allowed():
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
                 'inputs': {'VALUE': {'block': _join('a' * 2000,
                                                     'b' * 2000)}}}])
    assert len(ctx.variables['x']) == 4000


def test_the_oversized_string_is_never_materialised():
    """The check runs on the running total BEFORE ''.join, so the refused value
    never exists — the same reason MAX_LIST_ITEMS raises instead of truncating.
    A silently shortened text would be a wrong answer, not a smaller one."""
    assert MAX_TEXT_CHARS == 10000
    with pytest.raises(InterpreterError):
        _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
               'inputs': {'VALUE': {'block': _join(*(['z' * 1000] * 20))}}}])


# ── RS-54b — the acceptance companion for MAX_LIST_ITEMS ───────────────────

def test_a_classroom_sized_list_is_never_refused():
    """LITERAL 500, not MAX_LIST_ITEMS: `test_a_reasonable_lists_repeat_still_
    works` builds a list of MAX_LIST_ITEMS elements, so with the constant
    mutated to 0 it builds an empty list and still passes."""
    ctx = _run([{'type': 'variables_set', 'fields': {'VAR': 'L'},
                 'inputs': {'VALUE': {'block': {
                     'type': 'lists_repeat',
                     'inputs': {'ITEM': {'block': _num(1)},
                                'NUM': {'block': _num(500)}}}}}}])
    assert len(ctx.variables['L']) == 500


# ══════════════════════════════════════════════════════════════════════════
# E-3 — „hole Element" out of range is RED, exactly like „setze Element"
# ══════════════════════════════════════════════════════════════════════════
#
# One function, one `statement` flag, three refusals gated on it: „setze
# Element 9" of a 2-element list raised in German while „hole Element 9"
# returned None — a blank Protokoll line, a green run, and the None then
# propagated (`setze x auf hole Element 9` leaves x = None) so the error
# surfaced later, somewhere unrelated. Blockly lists are 1-BASED, which makes
# „hole Element 4" of a three-element list the single most likely list mistake
# a twelve-year-old makes.

def _get_index(list_block, at, mode='GET'):
    return {'type': 'lists_getIndex',
            'fields': {'MODE': mode, 'WHERE': 'FROM_START'},
            'inputs': {'VALUE': {'block': list_block},
                       'AT': {'block': _num(at)}}}


def _list_of(*values):
    return {'type': 'lists_create_with',
            'extraState': {'itemCount': len(values)},
            'inputs': {f'ADD{i}': {'block': _num(v)}
                       for i, v in enumerate(values)}}


def test_the_list_create_limit_counts_filled_sockets():
    """The editor mirrors THIS number (blocks/savedValueWarnings.js).

    It is ours, not Blockly's: neither the gear dialog nor the +/- plugin caps
    a ``lists_create_with`` at all (measured 43 sockets), so the editor could
    always build a block this refuses. Counting FILLED sockets is the contract
    the editor's warning copies — a block with many empty sockets still runs.

    *Kills:* re-hardcoding 20, or switching either side to the item count.
    """
    ctx = _Ctx()
    at_limit = _list_of(*range(MAX_LIST_CREATE_ITEMS))
    _run([{'type': 'variables_set', 'fields': {'VAR': 'x'},
           'inputs': {'VALUE': {'block': at_limit}}}], ctx)
    assert ctx.variables['x'] == list(range(MAX_LIST_CREATE_ITEMS))

    over = _list_of(*range(MAX_LIST_CREATE_ITEMS + 1))
    with pytest.raises(InterpreterError) as exc:
        _run([{'type': 'variables_set', 'fields': {'VAR': 'y'},
               'inputs': {'VALUE': {'block': over}}}], ctx)
    assert str(MAX_LIST_CREATE_ITEMS) in str(exc.value), str(exc.value)

    # An EMPTY socket costs nothing: the same block with one value at a far
    # index is refused for its INDEX, while a short filled list is fine.
    sparse = {'type': 'lists_create_with',
              'inputs': {'ADD0': {'block': _num(1)},
                         f'ADD{MAX_LIST_CREATE_ITEMS}': {'block': _num(2)}}}
    with pytest.raises(InterpreterError):
        _run([{'type': 'variables_set', 'fields': {'VAR': 'z'},
               'inputs': {'VALUE': {'block': sparse}}}], ctx)


@pytest.mark.parametrize('mode', ['GET', 'GET_REMOVE'])
@pytest.mark.parametrize('at', [0, 3, 99])
def test_an_out_of_range_hole_element_is_refused_in_german(mode, at):
    """*Kills:* restoring `return None` for the value form."""
    ctx = _Ctx()
    ctx.variables['L'] = [10, 20]
    block = {'type': 'variables_set', 'fields': {'VAR': 'x'},
             'inputs': {'VALUE': {'block': _get_index(
                 {'type': 'variables_get', 'fields': {'VAR': 'L'}}, at, mode)}}}
    with pytest.raises(InterpreterError) as exc:
        _run([block], ctx)
    msg = str(exc.value)
    assert f'Element {at} gibt es nicht' in msg, msg
    assert 'die Liste hat nur 2 Elemente' in msg
    assert 'Das erste Element ist Nummer 1' in msg, (
        'the 1-based convention is the part that actually teaches')
    # No programmer German.
    assert 'Index' not in msg and 'Grenzen' not in msg
    # And the list is UNCHANGED — GET_REMOVE must not have popped anything.
    assert ctx.variables['L'] == [10, 20]


@pytest.mark.parametrize('mode', ['GET', 'GET_REMOVE'])
def test_hole_element_from_a_non_list_socket_is_refused(mode):
    ctx = _Ctx()
    block = {'type': 'variables_set', 'fields': {'VAR': 'x'},
             'inputs': {'VALUE': {'block': _get_index(_num(42), 1, mode)}}}
    with pytest.raises(InterpreterError) as exc:
        _run([block], ctx)
    assert '„hole Element" braucht eine Liste' in str(exc.value)


@pytest.mark.parametrize('mode', ['GET', 'GET_REMOVE'])
def test_hole_element_from_an_empty_list_is_refused(mode):
    ctx = _Ctx()
    ctx.variables['L'] = []
    block = {'type': 'variables_set', 'fields': {'VAR': 'x'},
             'inputs': {'VALUE': {'block': _get_index(
                 {'type': 'variables_get', 'fields': {'VAR': 'L'}}, 1, mode)}}}
    with pytest.raises(InterpreterError) as exc:
        _run([block], ctx)
    assert 'Die Liste ist leer' in str(exc.value)
    assert '„hole Element"' in str(exc.value)


def test_the_first_element_is_number_one():
    """*Kills:* an off-by-one "fix" of the bound in either direction."""
    ctx = _Ctx()
    ctx.variables['L'] = [10, 20]
    ok = {'type': 'variables_set', 'fields': {'VAR': 'x'},
          'inputs': {'VALUE': {'block': _get_index(
              {'type': 'variables_get', 'fields': {'VAR': 'L'}}, 1)}}}
    _run([ok], ctx)
    assert ctx.variables['x'] == 10, 'Element 1 is the FIRST element'
    bad = {'type': 'variables_set', 'fields': {'VAR': 'y'},
           'inputs': {'VALUE': {'block': _get_index(
               {'type': 'variables_get', 'fields': {'VAR': 'L'}}, 0)}}}
    with pytest.raises(InterpreterError):
        _run([bad], ctx)


def test_set_and_get_say_the_same_thing_about_the_bound():
    """THE finding: the two halves of one block family drifted apart. One
    sentence now, from one helper, so they cannot drift again."""
    ctx = _Ctx()
    ctx.variables['L'] = [10, 20]
    get_block = {'type': 'variables_set', 'fields': {'VAR': 'x'},
                 'inputs': {'VALUE': {'block': _get_index(
                     {'type': 'variables_get', 'fields': {'VAR': 'L'}}, 9)}}}
    set_block = {'type': 'lists_setIndex',
                 'fields': {'MODE': 'SET', 'WHERE': 'FROM_START'},
                 'inputs': {'LIST': {'block': {'type': 'variables_get',
                                               'fields': {'VAR': 'L'}}},
                            'AT': {'block': _num(9)},
                            'TO': {'block': _num(1)}}}
    with pytest.raises(InterpreterError) as get_exc:
        _run([get_block], ctx)
    with pytest.raises(InterpreterError) as set_exc:
        _run([set_block], ctx)
    assert str(get_exc.value) == str(set_exc.value), (
        f'„hole" says {str(get_exc.value)!r} and „setze" says '
        f'{str(set_exc.value)!r} — that drift IS the finding')
    assert 'Listen-Index außerhalb der Grenzen' not in str(set_exc.value), (
        'programmer German on a surface whose blocks say „Element" and „Liste"')
