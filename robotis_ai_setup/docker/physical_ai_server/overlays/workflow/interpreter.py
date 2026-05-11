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

Hat blocks (``edubotics_when_*``) are top-only: they have no
``previousStatement`` and start their own statement chain. The
WorkflowManager pulls them out of the root list and runs each as a
separate handler — they fire when the named broadcast or sensor
condition is observed. A single ``motion_lock`` in WorkflowContext
keeps motion serialized between event handlers and the main stack.

The interpreter dispatches each block to the statement handler table OR
the value evaluator table based on context. The allowlist
(``ALLOWED_BLOCK_TYPES``) is the security boundary: any unknown ``type``
field aborts the run with the German error before any handler runs.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Callable, Iterable

from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.coco_classes import ALLOWED_CLASS_LABELS


# Hat block types — collected by Interpreter.split_roots() and run as
# separate handler stacks by WorkflowManager.
HAT_BLOCK_TYPES: frozenset[str] = frozenset({
    'edubotics_when_broadcast',
    'edubotics_when_marker_seen',
    'edubotics_when_color_seen',
})


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
    'edubotics_speak_de',
    'edubotics_play_tone',
    # Events — statement (broadcast) + hat (when_*)
    'edubotics_broadcast',
    'edubotics_when_broadcast',
    'edubotics_when_marker_seen',
    'edubotics_when_color_seen',
    # Perception — value evaluators
    'edubotics_detect_color',
    'edubotics_wait_until_color',
    'edubotics_count_color',
    'edubotics_detect_marker',
    'edubotics_wait_until_marker',
    'edubotics_detect_object',
    'edubotics_wait_until_object',
    'edubotics_count_objects_class',
    'edubotics_detect_open_vocab',
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
    'math_random_int',
    'math_constrain',
    'math_modulo',
    'math_round',
    'variables_get',
    'variables_set',
    'text',
    # Lists — Blockly built-ins, value + statement
    'lists_create_with',
    'lists_repeat',
    'lists_length',
    'lists_isEmpty',
    'lists_indexOf',
    'lists_getIndex',
    'lists_setIndex',
    'lists_getSublist',
    # Procedures — Blockly built-ins
    'procedures_defnoreturn',
    'procedures_defreturn',
    'procedures_callnoreturn',
    'procedures_callreturn',
    'procedures_ifreturn',
})


ALLOWED_COLORS: frozenset[str] = frozenset({'rot', 'gruen', 'blau', 'gelb'})

# Loop-iteration safety: prevent runaway controls_repeat_ext or while
# from hanging the daemon. 10000 ticks of motion-handler work would
# already be 5+ hours of wall time at the chunk pacing.
MAX_LOOP_ITERATIONS = 10_000


class _ProcedureReturn(Exception):
    """Internal control-flow exception for procedures_ifreturn."""

    def __init__(self, value: Any) -> None:
        self.value = value


class InterpreterError(Exception):
    """Raised on workflow validation or runtime errors. ``args[0]`` is
    a German user-facing message."""


class Interpreter:
    """Stateful walker over a parsed Blockly workspace tree."""

    def __init__(self, root_blocks: list[dict[str, Any]]) -> None:
        self._roots = root_blocks
        # Procedure registry: name → {block, params, return}. Populated
        # during execute() so callers from any handler stack can invoke
        # them. The registry is shared across hat-block stacks (a "when"
        # handler can call a procedure defined in the main stack).
        self._procedures: dict[str, dict[str, Any]] = {}

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
    # Public introspection used by WorkflowManager
    # ------------------------------------------------------------------
    @property
    def roots(self) -> list[dict[str, Any]]:
        return self._roots

    def split_roots(self) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Return (main_stacks, hat_stacks). Hat blocks have a top-only
        shape and are run as separate handler stacks by the manager.
        Procedure definitions live with the main stacks (they're
        executed once on encounter to register, then never as runtime).
        """
        main: list[dict[str, Any]] = []
        hats: list[dict[str, Any]] = []
        for block in self._roots:
            btype = block.get('type')
            if btype in HAT_BLOCK_TYPES:
                hats.append(block)
            else:
                main.append(block)
        return main, hats

    def collect_concrete_destinations(self) -> list[dict[str, Any]]:
        """Walk the tree and collect every move_to / pickup / drop_at
        block whose target is an immediately-resolvable XYZ. Used by
        WorkflowManager.start() for the IK pre-check.

        A target is "concrete" if it's a destination_pin block whose
        X/Y/Z labels are real numbers (not the '—' sentinel) AND the
        block is reachable from a non-hat root (we don't pre-check
        targets only inside hat handlers — those run on demand).
        """
        out: list[dict[str, Any]] = []

        def walk(block: dict[str, Any] | None) -> None:
            if not isinstance(block, dict):
                return
            btype = block.get('type')
            if btype in {'edubotics_move_to', 'edubotics_pickup', 'edubotics_drop_at'}:
                target = self._get_input_block(block, 'DESTINATION') or self._get_input_block(block, 'TARGET')
                xyz = self._extract_concrete_xyz(target)
                if xyz is not None:
                    out.append({
                        'block_id': block.get('id', ''),
                        'block_type': btype,
                        'xyz': xyz,
                    })
            inputs = block.get('inputs') or {}
            if isinstance(inputs, dict):
                for slot in inputs.values():
                    if isinstance(slot, dict):
                        walk(slot.get('block'))
                        walk(slot.get('shadow'))
            nxt = block.get('next')
            if isinstance(nxt, dict):
                walk(nxt.get('block'))

        # Pre-check the main stacks only; hat handlers fire too rarely
        # to be worth flagging unreachable upfront, and the runtime
        # safety envelope catches anything we miss.
        main, _ = self.split_roots()
        for root in main:
            walk(root)
        return out

    @staticmethod
    def _extract_concrete_xyz(block: dict[str, Any] | None) -> tuple[float, float, float] | None:
        if not isinstance(block, dict):
            return None
        if block.get('type') != 'edubotics_destination_pin':
            return None
        fields = block.get('fields') or {}
        try:
            x = float(fields.get('X', '—'))
            y = float(fields.get('Y', '—'))
            z = float(fields.get('Z', '—'))
        except (TypeError, ValueError):
            return None
        return (x, y, z)

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
        # Register procedures from the entire tree (main + hat stacks)
        # before running anything, so a "when" handler can call a
        # procedure defined in main.
        self._procedures = self._build_procedure_registry()
        # Expose to ctx so handlers can check / call.
        ctx.procedures = self._procedures
        ctx.call_procedure = lambda name, args: self._call_procedure(name, args, ctx, on_block_change)

        main_roots, _ = self.split_roots()
        total = max(1, len(main_roots))
        for idx, root in enumerate(main_roots):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            on_block_change(root.get('id', ''), 'running', idx / total)
            self._exec_chain(root, ctx, on_block_change)
            on_block_change(root.get('id', ''), 'done', (idx + 1) / total)

    def execute_chain(
        self,
        root: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        """Public wrapper used by WorkflowManager hat-block scheduler
        to run a single root chain (the body of a hat handler) under
        the same execution semantics as the main loop. Called inside
        ctx.motion_lock so two handlers don't race motion blocks."""
        if not hasattr(ctx, 'variables') or ctx.variables is None:
            ctx.variables = {}
        if not hasattr(ctx, 'procedures'):
            ctx.procedures = self._procedures
        if not hasattr(ctx, 'call_procedure'):
            ctx.call_procedure = lambda name, args: self._call_procedure(
                name, args, ctx, on_block_change,
            )
        # Skip the hat block itself (it has no behavior beyond the
        # trigger) and run the chained statement body.
        first = self._next_block(root)
        if first is None:
            return
        self._exec_chain(first, ctx, on_block_change)

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
            current = self._next_block(current)

    @staticmethod
    def _next_block(block: dict[str, Any]) -> dict[str, Any] | None:
        nxt = block.get('next')
        if isinstance(nxt, dict) and isinstance(nxt.get('block'), dict):
            return nxt['block']
        return None

    def _exec_block(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        btype = block.get('type')
        block_id = block.get('id', '')

        # Phase-2 debugger: respect breakpoints + pause flag *before*
        # the block runs. Breakpoints are simple — if the block id is
        # in ctx.breakpoints, set the pause event and emit a 'paused'
        # phase. The manager waits for ctx.resume_event to be set
        # before this method returns control.
        if getattr(ctx, 'breakpoints', None) and block_id in ctx.breakpoints:
            self._pause_for_breakpoint(ctx, block_id, on_block_change)
        elif callable(getattr(ctx, 'wait_if_paused', None)):
            ctx.wait_if_paused()

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
        if btype == 'lists_setIndex':
            self._exec_lists_set_index(block, ctx)
            return

        # Procedure definitions are registered up-front but contribute
        # nothing as runtime statements; skip silently.
        if btype in {'procedures_defnoreturn', 'procedures_defreturn'}:
            return
        if btype == 'procedures_callnoreturn':
            self._exec_procedure_call(block, ctx, on_block_change, expect_return=False)
            return
        if btype == 'procedures_ifreturn':
            self._exec_procedure_if_return(block, ctx, on_block_change)
            return

        # Broadcasts: fire the named event so any matching when_broadcast
        # hat handler in another thread wakes up. The manager owns the
        # event registry on ctx.broadcast_events.
        if btype == 'edubotics_broadcast':
            self._exec_broadcast(block, ctx)
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

    def _pause_for_breakpoint(
        self,
        ctx,
        block_id: str,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        """Block until the manager clears the pause event (or stop is
        requested). Emits a 'paused' phase so the React debugger can
        toggle the run-control buttons.
        """
        on_block_change(block_id, 'paused', 0.0)
        ctx.log(f'⏸ Haltepunkt erreicht: {block_id}')
        # Set pause flag if the manager hasn't already.
        if hasattr(ctx, 'set_paused') and callable(ctx.set_paused):
            ctx.set_paused(True)
        # Wait for resume; the manager exposes wait_for_resume() that
        # returns when either resume or stop is signaled.
        wait = getattr(ctx, 'wait_for_resume', None)
        if callable(wait):
            wait()
        if hasattr(ctx, 'set_paused') and callable(ctx.set_paused):
            ctx.set_paused(False)
        # Re-check stop after the wait — a stop fired while paused
        # would otherwise allow this breakpointed block to execute
        # before the chain's next should_stop check (audit §3 minor
        # finding: one extra block runs after stop-during-pause).
        if ctx.should_stop():
            from physical_ai_server.workflow.handlers.motion import WorkflowError
            raise WorkflowError('Workflow wurde gestoppt.')

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
        # Audit round-3 §18: raise BEFORE the loop so a student requesting
        # repeat=1_000_000 doesn't get 10000 motions executed before the
        # cap error fires. Mirrors _exec_while_until's pre-loop check.
        if n > MAX_LOOP_ITERATIONS:
            raise InterpreterError(
                f'Wiederhole {n} mal: Limit von {MAX_LOOP_ITERATIONS} überschritten.'
            )
        do_block = self._get_input_block(block, 'DO')
        for i in range(n):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            self._exec_chain(do_block, ctx, on_block_change)

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
            self._set_variable(ctx, var_name, i)
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
            self._set_variable(ctx, var_name, item)
            self._exec_chain(do_block, ctx, on_block_change)

    def _exec_variables_set(self, block: dict[str, Any], ctx) -> None:
        var_name = self._read_variable_name(block, 'VAR')
        if var_name is None:
            raise InterpreterError('Variable hat keinen Namen.')
        value_block = self._get_input_block(block, 'VALUE')
        value = self._eval_value(value_block, ctx) if value_block else None
        self._set_variable(ctx, var_name, value)

    def _set_variable(self, ctx, name: str, value: Any) -> None:
        # Audit §A1: serialize all variable writes via ctx.var_lock so
        # hat handlers and the main stack don't tear the dict.
        lock = getattr(ctx, 'var_lock', None)
        if lock is not None:
            with lock:
                ctx.variables[name] = value
        else:
            ctx.variables[name] = value
        # Emit a [VAR:name=json] sentinel so the React variable
        # inspector (debugger panel) can mirror the change. Failures
        # here must not raise — variables are best-effort observability.
        try:
            ctx.log(f'[VAR:{name}={json.dumps(_jsonable(value))}]')
        except Exception:
            pass

    @staticmethod
    def _read_variable(ctx, name: str) -> Any:
        lock = getattr(ctx, 'var_lock', None)
        if lock is not None:
            with lock:
                return ctx.variables.get(name)
        return ctx.variables.get(name)

    # ------------------------------------------------------------------
    # Lists statement
    # ------------------------------------------------------------------
    def _exec_lists_set_index(self, block: dict[str, Any], ctx) -> None:
        list_block = self._get_input_block(block, 'LIST')
        target = self._eval_value(list_block, ctx) if list_block else None
        if not isinstance(target, list):
            raise InterpreterError('Setze-Element-Block hat keine Liste.')
        mode = block.get('fields', {}).get('MODE', 'SET')  # SET or INSERT
        where = block.get('fields', {}).get('WHERE', 'FROM_START')
        at_block = self._get_input_block(block, 'AT')
        at = int(self._eval_value(at_block, ctx) or 0) if at_block else 0
        value_block = self._get_input_block(block, 'TO')
        value = self._eval_value(value_block, ctx) if value_block else None
        if not target:
            raise InterpreterError('Liste ist leer.')
        idx = self._resolve_index(target, where, at)
        if idx < 0 or idx >= len(target):
            raise InterpreterError(
                f'Listen-Index außerhalb der Grenzen (Länge {len(target)}).'
            )
        if mode == 'INSERT':
            target.insert(idx, value)
        else:
            target[idx] = value

    @staticmethod
    def _resolve_index(items: list, where: str, at: int) -> int:
        if where == 'FROM_END':
            return len(items) - at
        if where == 'FIRST':
            return 0
        if where == 'LAST':
            return len(items) - 1
        if where == 'RANDOM':
            return random.randrange(0, len(items)) if items else 0
        return at - 1  # FROM_START is 1-indexed in Blockly

    # ------------------------------------------------------------------
    # Procedures
    # ------------------------------------------------------------------
    def _build_procedure_registry(self) -> dict[str, dict[str, Any]]:
        """Walk all roots and find procedures_def* blocks. Each entry:
        {block, params, return_input}.
        """
        registry: dict[str, dict[str, Any]] = {}
        for root in self._roots:
            self._scan_for_procedures(root, registry)
        return registry

    def _scan_for_procedures(
        self,
        block: Any,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        if not isinstance(block, dict):
            return
        btype = block.get('type')
        if btype in {'procedures_defnoreturn', 'procedures_defreturn'}:
            name = (block.get('fields') or {}).get('NAME', '').strip()
            if name:
                params = (block.get('extraState') or {}).get('params') or []
                # Each param entry: {name, id} — Blockly's saveExtraState shape.
                param_names = [p.get('name', '') for p in params if isinstance(p, dict)]
                registry[name] = {
                    'block': block,
                    'params': param_names,
                    'has_return': btype == 'procedures_defreturn',
                }
        # Recurse via inputs and next.
        inputs = block.get('inputs') or {}
        if isinstance(inputs, dict):
            for slot in inputs.values():
                if isinstance(slot, dict):
                    self._scan_for_procedures(slot.get('block'), registry)
                    self._scan_for_procedures(slot.get('shadow'), registry)
        nxt = block.get('next')
        if isinstance(nxt, dict):
            self._scan_for_procedures(nxt.get('block'), registry)

    def _call_procedure(
        self,
        name: str,
        args: list[Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> Any:
        spec = self._procedures.get(name)
        if spec is None:
            raise InterpreterError(f'Unbekannte Funktion: {name}')
        # Save+restore caller's variable scope so procedure params don't
        # leak out. (Block-style procedures share globals for non-param
        # writes — that's documented Blockly behaviour.)
        saved: dict[str, Any] = {}
        param_names = spec.get('params') or []
        # Move the param-shadow setup *inside* the try/finally so a
        # partial failure (e.g., args[i] eval throws) doesn't leave the
        # caller's scope half-overwritten. Audit §B4.
        proc_block = spec['block']
        body = self._get_statement_block(proc_block, 'STACK')
        return_value: Any = None
        try:
            lock = getattr(ctx, 'var_lock', None)
            for i, pname in enumerate(param_names):
                if not pname:
                    continue
                if lock is not None:
                    with lock:
                        if pname in ctx.variables:
                            saved[pname] = ctx.variables[pname]
                        ctx.variables[pname] = args[i] if i < len(args) else None
                else:
                    if pname in ctx.variables:
                        saved[pname] = ctx.variables[pname]
                    ctx.variables[pname] = args[i] if i < len(args) else None
            if body is not None:
                self._exec_chain(body, ctx, on_block_change)
            if spec.get('has_return'):
                ret_block = self._get_input_block(proc_block, 'RETURN')
                return_value = self._eval_value(ret_block, ctx) if ret_block else None
        except _ProcedureReturn as early:
            return_value = early.value
        finally:
            # Restore param-shadowed variables. Use the same lock for
            # consistency with the setup path above.
            lock = getattr(ctx, 'var_lock', None)
            for pname in param_names:
                if not pname:
                    continue
                if lock is not None:
                    with lock:
                        if pname in saved:
                            ctx.variables[pname] = saved[pname]
                        else:
                            ctx.variables.pop(pname, None)
                else:
                    if pname in saved:
                        ctx.variables[pname] = saved[pname]
                    else:
                        ctx.variables.pop(pname, None)
        return return_value

    def _exec_procedure_call(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
        expect_return: bool,
    ) -> Any:
        name = (block.get('fields') or {}).get('NAME', '').strip()
        if not name:
            raise InterpreterError('Funktionsaufruf ohne Namen.')
        # Args are ARG0, ARG1, ... value inputs.
        args: list[Any] = []
        idx = 0
        while True:
            arg_input = self._get_input_block(block, f'ARG{idx}')
            if arg_input is None:
                break
            args.append(self._eval_value(arg_input, ctx))
            idx += 1
        return self._call_procedure(name, args, ctx, on_block_change)

    def _exec_procedure_if_return(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        cond = self._eval_value(self._get_input_block(block, 'CONDITION'), ctx)
        if self._truthy(cond):
            value_block = self._get_input_block(block, 'VALUE')
            value = self._eval_value(value_block, ctx) if value_block else None
            raise _ProcedureReturn(value)

    # ------------------------------------------------------------------
    # Broadcasts
    # ------------------------------------------------------------------
    def _exec_broadcast(self, block: dict[str, Any], ctx) -> None:
        name = (block.get('fields') or {}).get('EVENT_NAME', '').strip()
        if not name:
            return
        if hasattr(ctx, 'fire_broadcast') and callable(ctx.fire_broadcast):
            ctx.fire_broadcast(name)

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

        if btype == 'math_random_int':
            lo = int(self._eval_value(self._get_input_block(block, 'FROM'), ctx) or 0)
            hi = int(self._eval_value(self._get_input_block(block, 'TO'), ctx) or 0)
            if lo > hi:
                lo, hi = hi, lo
            return random.randint(lo, hi)
        if btype == 'math_constrain':
            v = float(self._eval_value(self._get_input_block(block, 'VALUE'), ctx) or 0)
            lo = float(self._eval_value(self._get_input_block(block, 'LOW'), ctx) or 0)
            hi = float(self._eval_value(self._get_input_block(block, 'HIGH'), ctx) or 0)
            if lo > hi:
                lo, hi = hi, lo
            return min(max(v, lo), hi)
        if btype == 'math_modulo':
            a = float(self._eval_value(self._get_input_block(block, 'DIVIDEND'), ctx) or 0)
            b = float(self._eval_value(self._get_input_block(block, 'DIVISOR'), ctx) or 1)
            return a % b if b != 0 else 0.0
        if btype == 'math_round':
            op = (block.get('fields') or {}).get('OP', 'ROUND')
            n = float(self._eval_value(self._get_input_block(block, 'NUM'), ctx) or 0)
            if op == 'ROUNDUP':
                import math
                return float(math.ceil(n))
            if op == 'ROUNDDOWN':
                import math
                return float(math.floor(n))
            return float(round(n))

        if btype == 'variables_get':
            var_name = self._read_variable_name(block, 'VAR')
            if var_name is None:
                return None
            return self._read_variable(ctx, var_name)

        # Lists value evaluators.
        if btype == 'lists_create_with':
            items: list[Any] = []
            idx = 0
            while True:
                item_block = self._get_input_block(block, f'ADD{idx}')
                if item_block is None:
                    if idx == 0 and not block.get('inputs'):
                        break
                    if not (block.get('inputs') or {}).get(f'ADD{idx}'):
                        break
                    items.append(None)
                else:
                    items.append(self._eval_value(item_block, ctx))
                idx += 1
                if idx > 20:
                    break
            return items
        if btype == 'lists_repeat':
            v = self._eval_value(self._get_input_block(block, 'ITEM'), ctx)
            n = int(self._eval_value(self._get_input_block(block, 'NUM'), ctx) or 0)
            n = max(0, min(n, MAX_LOOP_ITERATIONS))
            return [v] * n
        if btype == 'lists_length':
            target = self._eval_value(self._get_input_block(block, 'VALUE'), ctx)
            if isinstance(target, (list, str, tuple)):
                return len(target)
            return 0
        if btype == 'lists_isEmpty':
            target = self._eval_value(self._get_input_block(block, 'VALUE'), ctx)
            if isinstance(target, (list, str, tuple)):
                return len(target) == 0
            return target is None
        if btype == 'lists_indexOf':
            target = self._eval_value(self._get_input_block(block, 'VALUE'), ctx)
            find = self._eval_value(self._get_input_block(block, 'FIND'), ctx)
            end = (block.get('fields') or {}).get('END', 'FIRST')
            if not isinstance(target, list):
                return 0
            try:
                if end == 'LAST':
                    for i in range(len(target) - 1, -1, -1):
                        if target[i] == find:
                            return i + 1
                    return 0
                return target.index(find) + 1
            except ValueError:
                return 0
        if btype == 'lists_getIndex':
            target = self._eval_value(self._get_input_block(block, 'VALUE'), ctx)
            if not isinstance(target, list):
                return None
            where = (block.get('fields') or {}).get('WHERE', 'FROM_START')
            at_block = self._get_input_block(block, 'AT')
            at = int(self._eval_value(at_block, ctx) or 0) if at_block else 0
            idx = self._resolve_index(target, where, at)
            if idx < 0 or idx >= len(target):
                return None
            return target[idx]
        if btype == 'lists_getSublist':
            target = self._eval_value(self._get_input_block(block, 'LIST'), ctx)
            if not isinstance(target, list):
                return []
            where1 = (block.get('fields') or {}).get('WHERE1', 'FROM_START')
            where2 = (block.get('fields') or {}).get('WHERE2', 'FROM_END')
            at1_block = self._get_input_block(block, 'AT1')
            at2_block = self._get_input_block(block, 'AT2')
            at1 = int(self._eval_value(at1_block, ctx) or 0) if at1_block else 1
            at2 = int(self._eval_value(at2_block, ctx) or 0) if at2_block else 1
            i1 = max(0, self._resolve_index(target, where1, at1))
            i2 = self._resolve_index(target, where2, at2) + 1
            i2 = max(i1, min(len(target), i2))
            return list(target[i1:i2])

        # Procedure call (returning).
        if btype == 'procedures_callreturn':
            args: list[Any] = []
            idx = 0
            while True:
                arg_input = self._get_input_block(block, f'ARG{idx}')
                if arg_input is None:
                    break
                args.append(self._eval_value(arg_input, ctx))
                idx += 1
            name = (block.get('fields') or {}).get('NAME', '').strip()
            if not name:
                return None
            # Reuse the manager-level caller hook on ctx so procedures
            # are visible across hat handlers.
            return ctx.call_procedure(name, args)

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
    def _get_statement_block(block: dict[str, Any], name: str) -> dict[str, Any] | None:
        # Statement inputs use the same `inputs[name].block` shape.
        return Interpreter._get_input_block(block, name)

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


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of an arbitrary block-runtime value to a
    JSON-serializable shape for the [VAR:..] sentinel.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)
