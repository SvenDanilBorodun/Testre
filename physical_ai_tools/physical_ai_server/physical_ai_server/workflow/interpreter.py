#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Blockly workspace tree walker.

Two execution roles for blocks:

- **Statement** blocks DO things and chain via ``next.block``. Top-level
  workspace blocks are statements; ``DO0``/``DO1`` inputs of control
  blocks are statement chains.
- **Value** blocks RETURN things and live inside other blocks' input
  slots (e.g., ``DESTINATION``, ``IF0``, ``LIST``). A value block has an
  ``output`` connector instead of ``previousStatement``.

The interpreter dispatches each block to the statement handler table OR
the value evaluator table based on context. The allowlist
(``ALLOWED_BLOCK_TYPES``) is the security boundary: any unknown ``type``
field aborts the run with the German error before any handler runs.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.coco_classes import ALLOWED_CLASS_LABELS


ALLOWED_BLOCK_TYPES: frozenset[str] = frozenset({
    # Motion / output / destinations — statement handlers
    'edubotics_home',
    'edubotics_open_gripper',
    'edubotics_close_gripper',
    'edubotics_move_to',
    'edubotics_pickup',
    'edubotics_drop_at',
    'edubotics_wait_seconds',
    'edubotics_destination_pin',
    'edubotics_destination_current',
    'edubotics_log',
    'edubotics_play_sound',
    # Perception — value evaluators
    'edubotics_detect_color',
    'edubotics_wait_until_color',
    'edubotics_count_color',
    'edubotics_detect_marker',
    'edubotics_wait_until_marker',
    'edubotics_detect_object',
    'edubotics_wait_until_object',
    'edubotics_count_objects_class',
    # Logic + variables (Blockly built-ins)
    'controls_if',
    'controls_repeat_ext',
    'controls_whileUntil',
    'controls_for',
    'controls_forEach',
    'logic_compare',
    'logic_operation',
    'logic_negate',
    'logic_boolean',
    'math_number',
    'math_arithmetic',
    'variables_get',
    'variables_set',
    'text',
})


ALLOWED_COLORS: frozenset[str] = frozenset({'rot', 'gruen', 'blau', 'gelb'})

# Loop-iteration safety: prevent runaway controls_repeat_ext or while
# from hanging the daemon. 10000 ticks of motion-handler work would
# already be 5+ hours of wall time at the chunk pacing.
MAX_LOOP_ITERATIONS = 10_000


class InterpreterError(Exception):
    """Raised on workflow validation or runtime errors. ``args[0]`` is
    a German user-facing message."""


class Interpreter:
    """Stateful walker over a parsed Blockly workspace tree."""

    def __init__(self, root_blocks: list[dict[str, Any]]) -> None:
        self._roots = root_blocks

    # ------------------------------------------------------------------
    # Construction + validation
    # ------------------------------------------------------------------
    @classmethod
    def from_json(cls, raw: str) -> 'Interpreter':
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise InterpreterError(f'Workflow-JSON konnte nicht gelesen werden: {e}')

        top = data.get('blocks')
        if isinstance(top, dict):
            blocks = top.get('blocks', [])
        elif isinstance(top, list):
            blocks = top
        else:
            blocks = []

        if not isinstance(blocks, list):
            raise InterpreterError('Workflow-JSON hat kein gültiges "blocks"-Array.')

        for block in blocks:
            cls._validate_block(block)

        return cls(blocks)

    @classmethod
    def _validate_block(cls, block: Any) -> None:
        if not isinstance(block, dict):
            raise InterpreterError('Ungültiger Block-Eintrag im Workflow.')
        btype = block.get('type')
        if btype not in ALLOWED_BLOCK_TYPES:
            raise InterpreterError(f'Unbekannter Block-Typ: {btype}')

        # Validate fields against allowlists where applicable.
        fields = block.get('fields') or {}
        if isinstance(fields, dict):
            for name, value in fields.items():
                lname = name.lower()
                if lname == 'color' and value not in ALLOWED_COLORS:
                    raise InterpreterError(f'Unbekannte Farbe: {value}')
                if lname == 'class' and value not in ALLOWED_CLASS_LABELS:
                    raise InterpreterError(f'Unbekannte Objektklasse: {value}')

        # Recurse through inputs and next.
        inputs = block.get('inputs') or {}
        if isinstance(inputs, dict):
            for slot in inputs.values():
                if not isinstance(slot, dict):
                    continue
                child = slot.get('block')
                if isinstance(child, dict):
                    cls._validate_block(child)
                shadow = slot.get('shadow')
                if isinstance(shadow, dict):
                    cls._validate_block(shadow)

        nxt = block.get('next')
        if isinstance(nxt, dict):
            child = nxt.get('block')
            if isinstance(child, dict):
                cls._validate_block(child)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(
        self,
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        if not hasattr(ctx, 'variables') or ctx.variables is None:
            ctx.variables = {}
        total = max(1, len(self._roots))
        for idx, root in enumerate(self._roots):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            on_block_change(root.get('id', ''), 'running', idx / total)
            self._exec_chain(root, ctx, on_block_change)
            on_block_change(root.get('id', ''), 'done', (idx + 1) / total)

    def _exec_chain(
        self,
        block: dict[str, Any] | None,
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        """Execute ``block`` and follow its ``next`` chain."""
        current = block
        while current is not None:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            self._exec_block(current, ctx, on_block_change)
            nxt = current.get('next')
            if isinstance(nxt, dict) and isinstance(nxt.get('block'), dict):
                current = nxt['block']
            else:
                current = None

    def _exec_block(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        btype = block.get('type')
        block_id = block.get('id', '')
        on_block_change(block_id, 'running', 0.0)

        # Control-flow first — they manage their own input/statement eval.
        if btype == 'controls_if':
            self._exec_if(block, ctx, on_block_change)
            return
        if btype == 'controls_repeat_ext':
            self._exec_repeat(block, ctx, on_block_change)
            return
        if btype == 'controls_whileUntil':
            self._exec_while_until(block, ctx, on_block_change)
            return
        if btype == 'controls_for':
            self._exec_for(block, ctx, on_block_change)
            return
        if btype == 'controls_forEach':
            self._exec_for_each(block, ctx, on_block_change)
            return
        if btype == 'variables_set':
            self._exec_variables_set(block, ctx)
            return

        handler = STATEMENT_HANDLERS.get(btype)
        if handler is None:
            raise InterpreterError(f'Unbekannter Block-Typ: {btype}')

        args = self._build_args(block, ctx)
        try:
            handler(ctx, args)
        except WorkflowError:
            raise
        except InterpreterError:
            raise
        except Exception as e:
            raise InterpreterError(f'Fehler beim Ausführen von "{btype}": {e}')

    # ------------------------------------------------------------------
    # Statement helpers — control flow
    # ------------------------------------------------------------------
    def _exec_if(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        # controls_if can have IF0, IF1, ... + matching DO0, DO1, ... + ELSE.
        idx = 0
        while True:
            if_key = f'IF{idx}'
            do_key = f'DO{idx}'
            condition_block = self._get_input_block(block, if_key)
            if condition_block is None:
                break
            cond = self._eval_value(condition_block, ctx)
            if self._truthy(cond):
                do_block = self._get_input_block(block, do_key)
                if do_block is not None:
                    self._exec_chain(do_block, ctx, on_block_change)
                return
            idx += 1
        else_block = self._get_input_block(block, 'ELSE')
        if else_block is not None:
            self._exec_chain(else_block, ctx, on_block_change)

    def _exec_repeat(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        times_block = self._get_input_block(block, 'TIMES')
        times_val = self._eval_value(times_block, ctx) if times_block else block.get('fields', {}).get('TIMES')
        try:
            n = int(times_val) if times_val is not None else 0
        except (TypeError, ValueError):
            raise InterpreterError('Wiederhole-Block hat keine gültige Zahl.')
        do_block = self._get_input_block(block, 'DO')
        for i in range(min(n, MAX_LOOP_ITERATIONS)):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            self._exec_chain(do_block, ctx, on_block_change)
        if n > MAX_LOOP_ITERATIONS:
            raise InterpreterError(
                f'Wiederhole {n} mal: Limit von {MAX_LOOP_ITERATIONS} überschritten.'
            )

    def _exec_while_until(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        mode = block.get('fields', {}).get('MODE', 'WHILE')
        bool_block = self._get_input_block(block, 'BOOL')
        do_block = self._get_input_block(block, 'DO')
        iterations = 0
        while True:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            if iterations > MAX_LOOP_ITERATIONS:
                raise InterpreterError(
                    f'Schleifen-Limit von {MAX_LOOP_ITERATIONS} überschritten.'
                )
            cond = self._eval_value(bool_block, ctx) if bool_block else False
            stay = self._truthy(cond) if mode == 'WHILE' else not self._truthy(cond)
            if not stay:
                break
            self._exec_chain(do_block, ctx, on_block_change)
            iterations += 1

    def _exec_for(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        var_name = self._read_variable_name(block, 'VAR') or 'i'
        start = float(self._eval_value(self._get_input_block(block, 'FROM'), ctx) or 0)
        end = float(self._eval_value(self._get_input_block(block, 'TO'), ctx) or 0)
        step = float(self._eval_value(self._get_input_block(block, 'BY'), ctx) or 1)
        if step == 0:
            raise InterpreterError('Schrittweite 0 ist ungültig.')
        do_block = self._get_input_block(block, 'DO')
        iterations = 0
        i = start
        while (step > 0 and i <= end) or (step < 0 and i >= end):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            if iterations > MAX_LOOP_ITERATIONS:
                raise InterpreterError(
                    f'Zähl-Schleifen-Limit von {MAX_LOOP_ITERATIONS} überschritten.'
                )
            ctx.variables[var_name] = i
            self._exec_chain(do_block, ctx, on_block_change)
            i += step
            iterations += 1

    def _exec_for_each(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        var_name = self._read_variable_name(block, 'VAR') or 'item'
        list_block = self._get_input_block(block, 'LIST')
        items = self._eval_value(list_block, ctx) if list_block else []
        if items is None:
            items = []
        if not hasattr(items, '__iter__'):
            raise InterpreterError('Für-jedes-Block hat keinen iterierbaren Wert.')
        do_block = self._get_input_block(block, 'DO')
        iterations = 0
        for item in items:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            iterations += 1
            if iterations > MAX_LOOP_ITERATIONS:
                raise InterpreterError(
                    f'Für-jedes-Limit von {MAX_LOOP_ITERATIONS} überschritten.'
                )
            ctx.variables[var_name] = item
            self._exec_chain(do_block, ctx, on_block_change)

    def _exec_variables_set(self, block: dict[str, Any], ctx) -> None:
        var_name = self._read_variable_name(block, 'VAR')
        if var_name is None:
            raise InterpreterError('Variable hat keinen Namen.')
        value_block = self._get_input_block(block, 'VALUE')
        value = self._eval_value(value_block, ctx) if value_block else None
        ctx.variables[var_name] = value

    # ------------------------------------------------------------------
    # Value evaluation
    # ------------------------------------------------------------------
    def _eval_value(self, block: dict[str, Any] | None, ctx) -> Any:
        if block is None:
            return None
        btype = block.get('type')

        if btype == 'math_number':
            value = block.get('fields', {}).get('NUM', 0)
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
        if btype == 'text':
            return block.get('fields', {}).get('TEXT', '')
        if btype == 'logic_boolean':
            return block.get('fields', {}).get('BOOL', 'FALSE') == 'TRUE'

        if btype == 'logic_negate':
            inner = self._get_input_block(block, 'BOOL')
            return not self._truthy(self._eval_value(inner, ctx))

        if btype == 'logic_compare':
            op = block.get('fields', {}).get('OP', 'EQ')
            a = self._eval_value(self._get_input_block(block, 'A'), ctx)
            b = self._eval_value(self._get_input_block(block, 'B'), ctx)
            try:
                return self._apply_compare(op, a, b)
            except (TypeError, ValueError):
                return False

        if btype == 'logic_operation':
            op = block.get('fields', {}).get('OP', 'AND')
            a = self._truthy(self._eval_value(self._get_input_block(block, 'A'), ctx))
            b = self._truthy(self._eval_value(self._get_input_block(block, 'B'), ctx))
            return (a and b) if op == 'AND' else (a or b)

        if btype == 'math_arithmetic':
            op = block.get('fields', {}).get('OP', 'ADD')
            a = float(self._eval_value(self._get_input_block(block, 'A'), ctx) or 0)
            b = float(self._eval_value(self._get_input_block(block, 'B'), ctx) or 0)
            return self._apply_arithmetic(op, a, b)

        if btype == 'variables_get':
            var_name = self._read_variable_name(block, 'VAR')
            if var_name is None:
                return None
            return ctx.variables.get(var_name)

        # Perception value blocks are evaluated through the dispatch table.
        evaluator = VALUE_EVALUATORS.get(btype)
        if evaluator is not None:
            args = self._build_args(block, ctx)
            return evaluator(ctx, args)

        # Statement blocks shouldn't be eval'd as values; signal clearly.
        raise InterpreterError(f'Block "{btype}" kann nicht als Wert ausgewertet werden.')

    @staticmethod
    def _apply_compare(op: str, a: Any, b: Any) -> bool:
        if op == 'EQ':
            return a == b
        if op == 'NEQ':
            return a != b
        if op == 'LT':
            return a < b
        if op == 'LTE':
            return a <= b
        if op == 'GT':
            return a > b
        if op == 'GTE':
            return a >= b
        return False

    @staticmethod
    def _apply_arithmetic(op: str, a: float, b: float) -> float:
        if op == 'ADD':
            return a + b
        if op == 'MINUS':
            return a - b
        if op == 'MULTIPLY':
            return a * b
        if op == 'DIVIDE':
            return a / b if b != 0 else 0.0
        if op == 'POWER':
            return a ** b
        return 0.0

    # ------------------------------------------------------------------
    # Args + helpers
    # ------------------------------------------------------------------
    def _build_args(self, block: dict[str, Any], ctx) -> dict[str, Any]:
        """Build the arg dict the handlers consume.

        Fields → flat key/value pairs (lowercased). Inputs → evaluated
        value (lowercased input name as key).
        """
        args: dict[str, Any] = {}
        fields = block.get('fields') or {}
        if isinstance(fields, dict):
            for name, value in fields.items():
                args[name.lower()] = value

        inputs = block.get('inputs') or {}
        if isinstance(inputs, dict):
            for input_name, slot in inputs.items():
                if not isinstance(slot, dict):
                    continue
                inner = slot.get('block')
                if not isinstance(inner, dict):
                    inner = slot.get('shadow')
                if isinstance(inner, dict):
                    # Skip statement-only inputs (DO0, DO1, etc.); they're
                    # handled by the control-flow executors directly.
                    if input_name.startswith('DO') or input_name == 'ELSE':
                        continue
                    args[input_name.lower()] = self._eval_value(inner, ctx)
        return args

    @staticmethod
    def _get_input_block(block: dict[str, Any], name: str) -> dict[str, Any] | None:
        inputs = block.get('inputs') or {}
        slot = inputs.get(name)
        if not isinstance(slot, dict):
            return None
        inner = slot.get('block')
        if isinstance(inner, dict):
            return inner
        shadow = slot.get('shadow')
        if isinstance(shadow, dict):
            return shadow
        return None

    @staticmethod
    def _read_variable_name(block: dict[str, Any], field_name: str) -> str | None:
        fields = block.get('fields') or {}
        value = fields.get(field_name)
        if isinstance(value, dict):
            # Blockly stores variable references as `{id, name}` after a save.
            return value.get('name') or value.get('id')
        if isinstance(value, str):
            return value
        return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, (list, tuple, dict, str)):
            return len(value) > 0
        return bool(value)
