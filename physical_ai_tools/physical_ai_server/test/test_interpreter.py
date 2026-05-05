"""Interpreter validation + execution tests.

The allowlist is the security boundary; this test fixes that contract
in place and will fail if a future change opens it inadvertently.
"""

from __future__ import annotations

import json

import pytest

from physical_ai_server.workflow.interpreter import (
    ALLOWED_BLOCK_TYPES,
    Interpreter,
    InterpreterError,
)


def _ws(blocks_list: list[dict]) -> str:
    return json.dumps({'blocks': {'languageVersion': 0, 'blocks': blocks_list}})


def test_unknown_block_type_rejected():
    payload = _ws([{'type': 'edubotics_evil_block'}])
    with pytest.raises(InterpreterError) as exc:
        Interpreter.from_json(payload)
    assert 'Unbekannter Block-Typ' in str(exc.value)


def test_unknown_color_rejected():
    payload = _ws([
        {
            'type': 'edubotics_detect_color',
            'fields': {'COLOR': 'lila'},
        }
    ])
    with pytest.raises(InterpreterError) as exc:
        Interpreter.from_json(payload)
    assert 'Unbekannte Farbe' in str(exc.value)


def test_unknown_object_class_rejected():
    payload = _ws([
        {
            'type': 'edubotics_detect_object',
            'fields': {'CLASS': 'Drache'},
        }
    ])
    with pytest.raises(InterpreterError) as exc:
        Interpreter.from_json(payload)
    assert 'Unbekannte Objektklasse' in str(exc.value)


def test_known_color_accepted():
    payload = _ws([
        {
            'type': 'edubotics_detect_color',
            'fields': {'COLOR': 'rot'},
        }
    ])
    Interpreter.from_json(payload)


def test_motion_blocks_in_allowlist():
    expected = {
        'edubotics_home',
        'edubotics_move_to',
        'edubotics_pickup',
        'edubotics_drop_at',
        'edubotics_open_gripper',
        'edubotics_close_gripper',
        'edubotics_wait_seconds',
    }
    assert expected.issubset(ALLOWED_BLOCK_TYPES)


def test_perception_object_blocks_in_allowlist():
    expected = {
        'edubotics_detect_object',
        'edubotics_wait_until_object',
        'edubotics_count_objects_class',
    }
    assert expected.issubset(ALLOWED_BLOCK_TYPES)


def test_legacy_workflow_array_format_accepted():
    """`blocks` may be a top-level list (older serialisations) or a
    `{languageVersion, blocks: [...]}` dict (current). Both must parse."""
    payload = json.dumps({'blocks': [{'type': 'edubotics_home'}]})
    Interpreter.from_json(payload)


def test_invalid_json_rejected():
    with pytest.raises(InterpreterError):
        Interpreter.from_json('{not valid json')


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


def test_repeat_loop_executes_n_times(monkeypatch):
    """Use a stubbed handler dict so the repeat block runs without
    actually publishing motion."""
    from physical_ai_server.workflow import interpreter as interp

    counter = {'n': 0}
    fake_log = {
        'edubotics_log': lambda ctx, args: counter.update({'n': counter['n'] + 1}),
    }
    monkeypatch.setattr(interp, 'STATEMENT_HANDLERS', fake_log)

    payload = _ws([
        {
            'type': 'controls_repeat_ext',
            'inputs': {
                'TIMES': {
                    'block': {'type': 'math_number', 'fields': {'NUM': 3}},
                },
                'DO': {
                    'block': {'type': 'edubotics_log', 'fields': {'MESSAGE': 'tick'}},
                },
            },
        }
    ])
    interpreter = Interpreter.from_json(payload)
    ctx = _StubCtx()
    interpreter.execute(ctx, lambda *a: None)
    assert counter['n'] == 3


def test_if_branch_chooses_then_when_truthy(monkeypatch):
    from physical_ai_server.workflow import interpreter as interp

    log = {'msg': None}
    fake_handlers = {
        'edubotics_log': lambda ctx, args: log.update({'msg': args.get('message')}),
    }
    monkeypatch.setattr(interp, 'STATEMENT_HANDLERS', fake_handlers)

    payload = _ws([
        {
            'type': 'controls_if',
            'inputs': {
                'IF0': {
                    'block': {'type': 'logic_boolean', 'fields': {'BOOL': 'TRUE'}},
                },
                'DO0': {
                    'block': {'type': 'edubotics_log', 'fields': {'MESSAGE': 'taken'}},
                },
            },
        }
    ])
    interpreter = Interpreter.from_json(payload)
    ctx = _StubCtx()
    interpreter.execute(ctx, lambda *a: None)
    assert log['msg'] == 'taken'


def test_for_each_iterates_list(monkeypatch):
    """Variable is exposed via ctx.variables so subsequent value blocks
    can read it. We don't have a direct way to inspect the loop body
    output without a real handler, so we check `ctx.variables`."""
    from physical_ai_server.workflow import interpreter as interp

    seen = []
    fake_handlers = {
        'edubotics_log': lambda ctx, args: seen.append(ctx.variables.get('item')),
    }
    monkeypatch.setattr(interp, 'STATEMENT_HANDLERS', fake_handlers)
    monkeypatch.setattr(
        interp,
        'VALUE_EVALUATORS',
        {
            'edubotics_count_color': lambda ctx, args: 0,
        },
        raising=False,
    )

    # Use variables_set to put a list into `item` first; the forEach
    # then iterates a hand-rolled detect-color result.
    # Since we can't easily fake a value block returning a list at
    # the JSON level, rely on the variable resolver for the iterable.
    ctx = _StubCtx()
    ctx.variables['mylist'] = [1, 2, 3]

    payload = _ws([
        {
            'type': 'controls_forEach',
            'fields': {'VAR': {'name': 'item'}},
            'inputs': {
                'LIST': {
                    'block': {
                        'type': 'variables_get',
                        'fields': {'VAR': {'name': 'mylist'}},
                    },
                },
                'DO': {
                    'block': {'type': 'edubotics_log', 'fields': {'MESSAGE': 'x'}},
                },
            },
        }
    ])
    interpreter = Interpreter.from_json(payload)
    interpreter.execute(ctx, lambda *a: None)
    assert seen == [1, 2, 3]


def test_arithmetic_evaluation():
    from physical_ai_server.workflow.interpreter import Interpreter

    payload = _ws([
        {
            'type': 'variables_set',
            'fields': {'VAR': {'name': 'sum'}},
            'inputs': {
                'VALUE': {
                    'block': {
                        'type': 'math_arithmetic',
                        'fields': {'OP': 'ADD'},
                        'inputs': {
                            'A': {'block': {'type': 'math_number', 'fields': {'NUM': 2}}},
                            'B': {'block': {'type': 'math_number', 'fields': {'NUM': 5}}},
                        },
                    },
                },
            },
        }
    ])
    interp = Interpreter.from_json(payload)
    ctx = _StubCtx()
    interp.execute(ctx, lambda *a: None)
    assert ctx.variables['sum'] == 7
