"""Interpreter validation + execution tests.

The 2026-05 stripdown removed the cloud-side/from_json block allowlist; an
unknown block type is now rejected at EXECUTION (dispatch-table miss). These
tests fix the current contract: the dispatch tables register the expected
blocks, an unknown block raises at execution, the WS3 destination_ref value
block resolves a pinned name, and the control-flow / arithmetic execution paths
work.
"""

from __future__ import annotations

import json

import pytest

from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import Interpreter, InterpreterError


def _ws(blocks_list: list[dict]) -> str:
    return json.dumps({'blocks': {'languageVersion': 0, 'blocks': blocks_list}})


class _StubCtx:
    """Minimal context for execution tests — captures handler calls
    without any ROS / hardware dependencies."""

    def __init__(self):
        self.calls: list[str] = []
        self.variables: dict = {}
        self.destinations: dict = {}
        self.should_stop = lambda: False
        self.log = lambda msg: None
        self.publisher = lambda points: None
        self.safety = None
        self.ik = None
        self.perception = None
        self.last_arm_joints = None
        self.last_full_joints = [0.0] * 6
        self.z_table = 0.0


# ── dispatch-table membership (the "allowlist" is now the tables) ────────────

def test_motion_blocks_registered():
    expected = {
        'edubotics_home', 'edubotics_move_to', 'edubotics_pickup',
        'edubotics_drop_at', 'edubotics_open_gripper', 'edubotics_close_gripper',
        'edubotics_wait_seconds',
    }
    assert expected.issubset(set(STATEMENT_HANDLERS))


def test_perception_object_blocks_registered():
    # Named-object (AprilTag) value blocks — the legacy colour/COCO/marker
    # detection blocks were removed (P4).
    expected = {
        'edubotics_see_object', 'edubotics_count_object',
        'edubotics_wait_until_object_seen',
    }
    assert expected.issubset(set(VALUE_EVALUATORS))


def test_destination_ref_is_a_value_block():
    # WS3: the new reference block must be a VALUE evaluator (so it can fill a
    # move_to/drop_at socket), NOT a statement.
    assert 'edubotics_destination_ref' in VALUE_EVALUATORS
    assert 'edubotics_destination_ref' not in STATEMENT_HANDLERS


# ── validation ───────────────────────────────────────────────────────────────

def test_unknown_block_type_rejected_at_execution():
    payload = _ws([{'type': 'edubotics_evil_block'}])
    interp = Interpreter.from_json(payload)
    ctx = _StubCtx()
    with pytest.raises(InterpreterError) as exc:
        interp.execute(ctx, lambda *a: None)
    assert 'Unbekannter Block-Typ' in str(exc.value)


def test_legacy_workflow_array_format_accepted():
    payload = json.dumps({'blocks': [{'type': 'edubotics_home'}]})
    Interpreter.from_json(payload)


def test_invalid_json_rejected():
    with pytest.raises(InterpreterError):
        Interpreter.from_json('{not valid json')


# ── WS3 destination_ref ──────────────────────────────────────────────────────

def test_destination_ref_evaluates_to_pinned_name():
    payload = _ws([{
        'type': 'variables_set',
        'fields': {'VAR': {'name': 'dest'}},
        'inputs': {'VALUE': {'block': {
            'type': 'edubotics_destination_ref', 'fields': {'NAME': 'A'},
        }}},
    }])
    interp = Interpreter.from_json(payload)
    ctx = _StubCtx()
    ctx.destinations = {'A': {'x': 0.2, 'y': 0.0, 'z': 0.0, 'label': 'A'}}
    interp.execute(ctx, lambda *a: None)
    assert ctx.variables['dest'] == 'A'


def test_destination_ref_unknown_name_raises():
    from physical_ai_server.workflow.handlers.destinations import destination_ref
    ctx = _StubCtx()
    ctx.destinations = {}
    with pytest.raises(WorkflowError):
        destination_ref(ctx, {'name': 'Z'})


# ── control flow + arithmetic execution ──────────────────────────────────────

def test_repeat_loop_executes_n_times(monkeypatch):
    from physical_ai_server.workflow import interpreter as interp

    counter = {'n': 0}
    fake_log = {
        'edubotics_log': lambda ctx, args: counter.update({'n': counter['n'] + 1}),
    }
    monkeypatch.setattr(interp, 'STATEMENT_HANDLERS', fake_log)

    payload = _ws([{
        'type': 'controls_repeat_ext',
        'inputs': {
            'TIMES': {'block': {'type': 'math_number', 'fields': {'NUM': 3}}},
            'DO': {'block': {'type': 'edubotics_log', 'fields': {'MESSAGE': 'tick'}}},
        },
    }])
    interpreter = Interpreter.from_json(payload)
    interpreter.execute(_StubCtx(), lambda *a: None)
    assert counter['n'] == 3


def test_if_branch_chooses_then_when_truthy(monkeypatch):
    from physical_ai_server.workflow import interpreter as interp

    log = {'msg': None}
    monkeypatch.setattr(interp, 'STATEMENT_HANDLERS', {
        'edubotics_log': lambda ctx, args: log.update({'msg': args.get('message')}),
    })
    payload = _ws([{
        'type': 'controls_if',
        'inputs': {
            'IF0': {'block': {'type': 'logic_boolean', 'fields': {'BOOL': 'TRUE'}}},
            'DO0': {'block': {'type': 'edubotics_log', 'fields': {'MESSAGE': 'taken'}}},
        },
    }])
    Interpreter.from_json(payload).execute(_StubCtx(), lambda *a: None)
    assert log['msg'] == 'taken'


def test_for_each_iterates_list(monkeypatch):
    from physical_ai_server.workflow import interpreter as interp

    seen = []
    monkeypatch.setattr(interp, 'STATEMENT_HANDLERS', {
        'edubotics_log': lambda ctx, args: seen.append(ctx.variables.get('item')),
    })
    ctx = _StubCtx()
    ctx.variables['mylist'] = [1, 2, 3]
    payload = _ws([{
        'type': 'controls_forEach',
        'fields': {'VAR': {'name': 'item'}},
        'inputs': {
            'LIST': {'block': {'type': 'variables_get', 'fields': {'VAR': {'name': 'mylist'}}}},
            'DO': {'block': {'type': 'edubotics_log', 'fields': {'MESSAGE': 'x'}}},
        },
    }])
    Interpreter.from_json(payload).execute(ctx, lambda *a: None)
    assert seen == [1, 2, 3]


def test_arithmetic_evaluation():
    payload = _ws([{
        'type': 'variables_set',
        'fields': {'VAR': {'name': 'sum'}},
        'inputs': {'VALUE': {'block': {
            'type': 'math_arithmetic',
            'fields': {'OP': 'ADD'},
            'inputs': {
                'A': {'block': {'type': 'math_number', 'fields': {'NUM': 2}}},
                'B': {'block': {'type': 'math_number', 'fields': {'NUM': 5}}},
            },
        }}},
    }])
    ctx = _StubCtx()
    Interpreter.from_json(payload).execute(ctx, lambda *a: None)
    assert ctx.variables['sum'] == 7


# ── #6: silent-wrong-value math now fails loud ───────────────────────────────

def _arith_ws(op: str, a, b) -> str:
    return _ws([{
        'type': 'variables_set',
        'fields': {'VAR': {'name': 'r'}},
        'inputs': {'VALUE': {'block': {
            'type': 'math_arithmetic',
            'fields': {'OP': op},
            'inputs': {
                'A': {'block': {'type': 'math_number', 'fields': {'NUM': a}}},
                'B': {'block': {'type': 'math_number', 'fields': {'NUM': b}}},
            },
        }}},
    }])


def _modulo_ws(a, b) -> str:
    return _ws([{
        'type': 'variables_set',
        'fields': {'VAR': {'name': 'r'}},
        'inputs': {'VALUE': {'block': {
            'type': 'math_modulo',
            'inputs': {
                'DIVIDEND': {'block': {'type': 'math_number', 'fields': {'NUM': a}}},
                'DIVISOR': {'block': {'type': 'math_number', 'fields': {'NUM': b}}},
            },
        }}},
    }])


def test_divide_by_zero_raises_german_error():
    interp = Interpreter.from_json(_arith_ws('DIVIDE', 5, 0))
    with pytest.raises(InterpreterError) as exc:
        interp.execute(_StubCtx(), lambda *a: None)
    assert 'Division durch Null' in str(exc.value)


def test_modulo_by_zero_raises_german_error():
    interp = Interpreter.from_json(_modulo_ws(5, 0))
    with pytest.raises(InterpreterError) as exc:
        interp.execute(_StubCtx(), lambda *a: None)
    assert 'durch Null' in str(exc.value)


def test_power_negative_base_fractional_exponent_raises():
    interp = Interpreter.from_json(_arith_ws('POWER', -2, 0.5))
    with pytest.raises(InterpreterError) as exc:
        interp.execute(_StubCtx(), lambda *a: None)
    assert 'reelles Ergebnis' in str(exc.value)


def test_valid_divide_and_modulo_still_work():
    c1 = _StubCtx()
    Interpreter.from_json(_arith_ws('DIVIDE', 6, 2)).execute(c1, lambda *a: None)
    assert c1.variables['r'] == 3
    c2 = _StubCtx()
    Interpreter.from_json(_modulo_ws(7, 3)).execute(c2, lambda *a: None)
    assert c2.variables['r'] == 1


# ── semantics round 2026-10-18, area A (the preview is the meaning) ─────────
# The whole corpus runs in test_blockly_conformance.py; these pin the named
# rules one by one, so a failure says which rule broke.

def _num_block(v):
    return {'block': {'type': 'math_number', 'fields': {'NUM': v}}}


def _set_r(value_block: dict) -> str:
    return _ws([{
        'type': 'variables_set', 'fields': {'VAR': {'name': 'r'}},
        'inputs': {'VALUE': {'block': value_block}},
    }])


def _list_of(*values) -> dict:
    return {'type': 'lists_create_with', 'extraState': {'itemCount': len(values)},
            'inputs': {f'ADD{k}': _num_block(v) for k, v in enumerate(values)}}


def test_list_index_error_says_one_element():
    """N13: „nur 1 Element", not „nur 1 Elemente"."""
    interp = Interpreter.from_json(_set_r({
        'type': 'lists_getIndex', 'fields': {'MODE': 'GET', 'WHERE': 'FROM_START'},
        'inputs': {'VALUE': {'block': _list_of(7)}, 'AT': _num_block(3)},
    }))
    with pytest.raises(InterpreterError) as exc:
        interp.execute(_StubCtx(), lambda *a: None)
    assert 'die Liste hat nur 1 Element.' in str(exc.value), str(exc.value)


def _for_ws(start, end, by) -> str:
    return _ws([{
        'type': 'variables_set', 'fields': {'VAR': {'name': 'trace'}},
        'inputs': {'VALUE': {'block': {'type': 'lists_create_with', 'extraState': {'itemCount': 0}}}},
        'next': {'block': {
            'type': 'controls_for', 'fields': {'VAR': {'name': 'i'}},
            'inputs': {
                'FROM': _num_block(start), 'TO': _num_block(end), 'BY': _num_block(by),
                'DO': {'block': {
                    'type': 'lists_setIndex', 'fields': {'MODE': 'INSERT', 'WHERE': 'LAST'},
                    'inputs': {
                        'LIST': {'block': {'type': 'variables_get', 'fields': {'VAR': {'name': 'trace'}}}},
                        'TO': {'block': {'type': 'variables_get', 'fields': {'VAR': {'name': 'i'}}}},
                    },
                }},
            },
        }},
    }])


@pytest.mark.parametrize('start,end,by,want', [
    (3, 1, 1, [3, 2, 1]),
    (1, 3, -1, [1, 2, 3]),
    (3, 1, -1, [3, 2, 1]),
    (1, 3, 1, [1, 2, 3]),
    (10, 1, 3, [10, 7, 4, 1]),
])
def test_for_counts_in_the_direction_from_to_with_the_size_of_by(start, end, by, want):
    """F1: direction from FROM vs TO, step |BY| — what the preview runs."""
    ctx = _StubCtx()
    Interpreter.from_json(_for_ws(start, end, by)).execute(ctx, lambda *a: None)
    assert ctx.variables['trace'] == want


def test_and_or_never_evaluate_b_when_a_decides():
    """F6: „falsch und (1/0 = 1)" is falsch, „wahr oder (1/0 = 1)" is wahr."""
    div0 = {'type': 'logic_compare', 'fields': {'OP': 'EQ'}, 'inputs': {
        'A': {'block': {'type': 'math_arithmetic', 'fields': {'OP': 'DIVIDE'},
                        'inputs': {'A': _num_block(1), 'B': _num_block(0)}}},
        'B': _num_block(1)}}
    for op, a, want in (('AND', 'FALSE', False), ('OR', 'TRUE', True)):
        ctx = _StubCtx()
        Interpreter.from_json(_set_r({
            'type': 'logic_operation', 'fields': {'OP': op},
            'inputs': {'A': {'block': {'type': 'logic_boolean', 'fields': {'BOOL': a}}},
                       'B': {'block': div0}},
        })).execute(ctx, lambda *a: None)
        assert ctx.variables['r'] is want


def test_a_text_in_a_math_socket_is_refused_in_german():
    """F7: „2" + 1 is refused (the preview raises), never coerced to 3."""
    interp = Interpreter.from_json(_set_r({
        'type': 'math_arithmetic', 'fields': {'OP': 'ADD'},
        'inputs': {'A': {'block': {'type': 'text', 'fields': {'TEXT': '2'}}},
                   'B': _num_block(1)},
    }))
    with pytest.raises(InterpreterError) as exc:
        interp.execute(_StubCtx(), lambda *a: None)
    assert str(exc.value) == 'Hier wird eine Zahl gebraucht — „2" ist keine Zahl.'


def test_length_of_a_number_is_refused_and_an_empty_socket_is_zero():
    """N13: „Länge von 5" refuses (the preview's len() raises); an empty
    socket is the generator's len([]) = 0."""
    with pytest.raises(InterpreterError) as exc:
        Interpreter.from_json(_set_r({
            'type': 'lists_length', 'inputs': {'VALUE': _num_block(5)},
        })).execute(_StubCtx(), lambda *a: None)
    assert '„Länge von" braucht eine Liste oder einen Text.' == str(exc.value)
    ctx = _StubCtx()
    Interpreter.from_json(_set_r({'type': 'lists_length'})).execute(ctx, lambda *a: None)
    assert ctx.variables['r'] == 0


def test_index_of_works_on_text_and_an_empty_at_is_element_one():
    """N13: the position of „b" in „abcb" (first 2, last 4); „hole Element"
    with no number takes element 1, as the generator does."""
    for end, want in (('FIRST', 2), ('LAST', 4)):
        ctx = _StubCtx()
        Interpreter.from_json(_set_r({
            'type': 'lists_indexOf', 'fields': {'END': end},
            'inputs': {'VALUE': {'block': {'type': 'text', 'fields': {'TEXT': 'abcb'}}},
                       'FIND': {'block': {'type': 'text', 'fields': {'TEXT': 'b'}}}},
        })).execute(ctx, lambda *a: None)
        assert ctx.variables['r'] == want
    ctx = _StubCtx()
    Interpreter.from_json(_set_r({
        'type': 'lists_getIndex', 'fields': {'MODE': 'GET', 'WHERE': 'FROM_START'},
        'inputs': {'VALUE': {'block': _list_of(10, 20, 30)}},
    })).execute(ctx, lambda *a: None)
    assert ctx.variables['r'] == 10
