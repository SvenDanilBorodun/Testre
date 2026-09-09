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
the value evaluator table based on context. Unknown block types raise a
KeyError out of the handler tables — the upstream behavior after the
2026-05 stripdown removed the cloud-side and runtime allowlists.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import random
import time
from typing import Any, Callable, Iterable

from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import (
    WorkflowError,
    _reacquire_after_release,
)


# Hat block types — collected by Interpreter.split_roots() and run as
# separate handler stacks by WorkflowManager.
HAT_BLOCK_TYPES: frozenset[str] = frozenset({
    'edubotics_when_broadcast',
    'edubotics_when_object_seen',
    'edubotics_when_counter_gt',
})

# Hard cap on iterations for any single loop construct
# (repeat / while / until / for / for-each). Documented in CLAUDE.md §6.7.
# Reaching this raises InterpreterError with a German message rather than
# silently truncating — a student who actually needed 11k iterations is
# almost certainly looking at an infinite-loop bug.
MAX_LOOP_ITERATIONS = 10000

# Cap on the [VAR:..] inspector-sentinel payload. The [VAR:] path bypasses
# output.log's MAX_LOG_CHARS, so without this a forever-loop self-concatenating a
# text variable would re-emit an unbounded, growing string into the realtime
# status channel every iteration.
_MAX_VAR_PAYLOAD_CHARS = 2000

# How many container elements the [VAR:] sentinel serializes before it truncates.
# The CHAR cap above is applied to the FINISHED string, so a 10-million-element
# list was fully json.dumps()'d (measured 0.74 s, ~268 MB RSS) and then thrown
# away down to 2000 chars. Serializing a bounded prefix instead makes the cost
# proportional to what is actually shown.
#
# 200 items is the BINDING cap for ordinary list contents — measured with the
# real _jsonable on 1000-element lists: 200 ints = 916 chars, 200 floats = 1316,
# 200 short strings = 1226, 200 booleans = 1326, all well under the 2000-char
# cap. Deliberately so: THIS cap bounds the COST, and the char cap is only the
# backstop for one pathological VALUE. (An earlier revision claimed „200 items
# comfortably overflows the 2000-char cap … so nothing visible is lost" — the
# opposite of what it measures.) The student is told what was dropped
# (`… (N Elemente)`), so a shorter prefix is a smaller view, not a wrong one;
# raising it to 400 would double what a twelve-year-old reads in a debug panel
# with no evidence that more is better.
_MAX_VAR_PAYLOAD_ITEMS = 200

# Hard cap on the length of a list a single block may materialize.
# ``lists_create_with`` has always been capped at 20 (its mutator's own limit),
# but ``lists_repeat`` had NO cap: measured 5 000 000 elements in 0.38 s and
# 100 000 000 in 0.04 s from a two-block program, inside the ROS node whose
# container mem_limit is 6g. This is the classroom-generous ceiling for the
# blocks that BUILD a list; it is deliberately far above any teaching use and
# far below "kill the node".
MAX_LIST_ITEMS = 1000

# Hard cap on the length of a string a single ``text_join`` may produce.
# ``verbinde`` had NO cap: measured, ``setze x auf verbinde(x, x)`` inside
# „wiederhole fortlaufend" reached 1 073 741 824 characters / 2.4 GB RSS in
# 5.07 s from four blocks, and two more doublings exceed the container's
# mem_limit of 6g and OOM-kill the ROS node — which `restart: "no"` does not
# bring back. 10 000 characters is five times output.log's MAX_LOG_CHARS, so
# every message a student can actually READ still fits with room to spare.
# Raise rather than truncate, exactly like MAX_LIST_ITEMS: a silently
# shortened text is a wrong answer.
MAX_TEXT_CHARS = 10000


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# Empty-window for the named-object „Solange <Typ> sichtbar" loop: terminate only
# after the type has been CONTINUOUSLY empty (0 unclaimed visible) for ≥
# WHILE_EMPTY_SECONDS AND across ≥ WHILE_EMPTY_FRAMES consecutive detections, so a
# briefly occluded/dropped frame — or a slow hand placing a recycled object back —
# doesn't end the loop early. A reclaim that returns an object (count>0) resets the
# empty state.
#
# 3, not 2, since 2026-09-08 — and the reason is the STUDENT, not the camera.
# With the reclaim rebuilt around position (perception_blocks._RECLAIM_MOVE_M),
# the put-back demo works whenever the student gets the object back onto the
# table before the empty window closes. Measured over 40 realistic one-cube
# timings: 21/40 at two looks, 38/40 at three; the remaining failures are simply
# a student slower than the loop. The cost is honest and paid by EVERY „Solange
# sichtbar" loop on every arm, including ones nobody intends to put anything back
# into: +7.7 s before the „nichts mehr sichtbar — fertig" line.
WHILE_EMPTY_FRAMES = _env_int('EDUBOTICS_WHILE_EMPTY_FRAMES', 3)
WHILE_EMPTY_SECONDS = _env_float('EDUBOTICS_WHILE_EMPTY_SECONDS', 5.0)
# Flicker-spin guard (#3): break the loop after this many CONSECUTIVE passes where
# the count gate said >0 but the body made no progress (a GraspSkip that neither
# grasped nor skipped a tag), instead of spinning to MAX_LOOP_ITERATIONS. A pass
# that claims OR skips a tag resets the counter.
WHILE_STALL_PASSES = _env_int('EDUBOTICS_WHILE_STALL_PASSES', 3)
# Wall-clock cap (#3): break the loop with a German notice once it has run this
# long, regardless of progress (monotonic).
WHILE_MAX_SECONDS = _env_float('EDUBOTICS_WHILE_MAX_SECONDS', 120.0)
# Settle delay (#3) AFTER the retreat-to-observation-pose and BEFORE re-detecting,
# so the first detect frame isn't a still-settling/blurred arm.
WHILE_SETTLE_S = _env_float('EDUBOTICS_WHILE_SETTLE_S', 0.2)

# Forever-loop rate floor (#H1): a „wiederhole fortlaufend" whose body is only
# fast value/log blocks would otherwise loop thousands of times/sec — flooding
# the WorkflowStatus realtime channel (3 publishes per body block, no server-side
# throttle) and spinning the executor. Enforce a minimum cycle time so the
# publish rate is bounded (~20 Hz); a motion-bearing body naturally exceeds this
# and pays nothing. Plain constant (NOT EDUBOTICS_* — a new env knob would need a
# docker-compose forward per the env-forwarding-guard); monkeypatchable in tests.
FOREVER_MIN_CYCLE_S = 0.05

# Wall-clock safety cap for the generic „warte bis <Bedingung>"
# (edubotics_wait_until). Scratch's wait-until is uncapped, but in a classroom a
# student can author a condition that never becomes true (typo'd threshold, an
# object that is never placed) and wedge the whole session indefinitely — the
# same failure mode WHILE_MAX_SECONDS guards in the while-visible loop. So the
# wait breaks with a German [WARNUNG] (the workflow then CONTINUES, exactly like
# the while-visible wall-clock break — not a hard raise) once it has polled this
# long. Deliberately a plain module constant, NOT an env-var override: a new env
# knob would have to be forwarded through docker-compose (ci.yml's
# env-forwarding-guard scans this package — and would flag the very token name if
# it appeared here), which is out of this change's scope. Promote it to an
# `_env_float(...)` read + a compose forward later if operators need it tunable.
# Tests monkeypatch the constant.
WAIT_UNTIL_MAX_SECONDS = 300.0


# Every block type ``_eval_value_impl`` knows how to evaluate. Used ONLY to tell
# a PARKED value block (one a student dragged onto the canvas but never plugged
# in) from a genuinely unknown type: a value block has no ``previousStatement``,
# so Blockly can only ever place it at the top level, where it is a no-op.
# ``pythonCodeGen.js`` already renders it harmlessly via ``scrubNakedValue``;
# the runtime used to abort the WHOLE program with „Unbekannter Block-Typ:
# math_arithmetic" — an English type id on a German surface (Rule §1), and 11
# of 11 value types tested behaved that way regardless of workspace order.
_BUILTIN_VALUE_TYPES: frozenset[str] = frozenset({
    'math_number', 'text', 'text_join', 'logic_boolean', 'logic_negate',
    'logic_compare', 'logic_operation', 'math_arithmetic', 'math_random_int',
    'math_constrain', 'math_modulo', 'math_round', 'variables_get',
    'lists_create_with', 'lists_repeat', 'lists_length', 'lists_isEmpty',
    'lists_indexOf', 'lists_getIndex', 'lists_getSublist',
    'procedures_callreturn',
})


def _is_disabled(block: Any) -> bool:
    """True when Blockly has marked ``block`` disabled.

    TWO serialization shapes, both real and both must be honoured:
    ``disabledReasons: ["manually_disabled"]`` (Blockly ≥ 11, what this app
    saves — verified against the shipped Blockly 12.5.1) and the legacy
    ``enabled: false`` that older saved workflows still carry.

    The interpreter read NEITHER, so a block the student greyed out kept
    running: measured, a disabled ``variables_set`` still set its variable and
    a disabled ``controls_if`` still ran its enabled body. The Code panel
    (``pythonCodeGen.js``) and ``collectReplayNames`` DO skip them, so a
    disabled „spiele Bewegung ab" was not fetched but WAS executed, failing
    with „Unbekannte Aufnahme".
    """
    if not isinstance(block, dict):
        return False
    reasons = block.get('disabledReasons')
    if isinstance(reasons, (list, tuple, set)) and len(reasons) > 0:
        return True
    return block.get('enabled') is False


def _is_list_get_statement(block: Any) -> bool:
    """True when a ``lists_getIndex`` arrived in its STATEMENT form.

    Blockly's MODE=REMOVE drops the block's output connection and gives it
    previous/next connectors, recording ``extraState {"isStatement": true}``
    (verified against the shipped Blockly 12.5.1). Every OTHER mode leaves it a
    VALUE block, and Blockly can only leave a value block lying at the TOP
    LEVEL, where it is a no-op. Routing those into the statement executor
    aborted the whole program with „Entferne-Element-Block hat keine Liste." —
    naming an operation the student never chose — for a block they had merely
    parked on the canvas. Both signals are read because the mutator derives
    ``isStatement_`` from MODE, so a hand-written payload may carry only one.
    """
    if not isinstance(block, dict):
        return False
    extra = block.get('extraState')
    if isinstance(extra, dict) and extra.get('isStatement') is True:
        return True
    return (block.get('fields') or {}).get('MODE') == 'REMOVE'


def event_name_of(block: Any) -> str:
    """The trimmed EVENT_NAME of a broadcast / when_broadcast block, ''-safe.

    Shared by the interpreter's „sende Ereignis" and the manager's hat trigger +
    start-time event diagnostics, so the two can never disagree about which
    names pair up. Tolerant of a non-string field (an imported or hand-written
    payload): a number is read as its text, anything else as no name at all.
    """
    if not isinstance(block, dict):
        return ''
    raw = (block.get('fields') or {}).get('EVENT_NAME')
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw).strip()
    return ''


def _claim_progress_count(ctx) -> int:
    """Total claimed+skipped tag count — the progress signal for the while-visible
    flicker-spin guard (#3). Read under claim_lock so a concurrent grasp in a hat
    thread can't tear the sets."""
    lock = getattr(ctx, 'claim_lock', None)
    claimed = getattr(ctx, 'claimed_tags', None) or set()
    skipped = getattr(ctx, 'skipped_tags', None) or set()
    if lock is not None:
        with lock:
            return len(claimed) + len(skipped)
    return len(claimed) + len(skipped)


class _ProcedureReturn(Exception):
    """Internal control-flow exception for procedures_ifreturn."""

    def __init__(self, value: Any) -> None:
        self.value = value


@contextlib.contextmanager
def _procedure_return_barrier():
    """Stop the INTERNAL ``_ProcedureReturn`` control-flow exception escaping a
    top-level stack or a hat body.

    ``procedures_ifreturn`` („gib zurück, falls …") is only meaningful inside a
    procedure; Blockly auto-disables one dropped at the top level
    (UNPARENTED_IFRETURN). But that auto-disable happens in the RENDERED editor,
    the interpreter never read the disabled flag at all until this round, and a
    hand-written or imported payload carries no flag either — so the exception
    escaped ``_run``'s catch-all and was written into the student's Protokoll as
    ``traceback.format_exc()``. Convert it into the German message it should
    always have been.
    """
    try:
        yield
    except _ProcedureReturn:
        raise InterpreterError(
            '„gib zurück" steht außerhalb einer Funktion — bitte den Block in '
            'einen Funktions-Block ziehen.'
        )


class InterpreterError(Exception):
    """Raised on workflow validation or runtime errors. ``args[0]`` is
    a German user-facing message."""


class Interpreter:
    """Stateful walker over a parsed Blockly workspace tree."""

    def __init__(
        self,
        root_blocks: list[dict[str, Any]],
        variable_names: dict[str, str] | None = None,
    ) -> None:
        self._roots = root_blocks
        # Blockly variable id → human name, from the workspace's top-level
        # ``variables: [{name, id}]`` array. LOAD-BEARING: a saved workspace
        # stores a variable REFERENCE as ``fields: {"VAR": {"id": "…"}}`` with
        # NO name (verified against Blockly 12.5.1 at BOTH the default save and
        # `doFullSerialization:false`, which is what this app uses), so without
        # this map every student variable was keyed by its 20-character random
        # id. That is why procedure parameters could never bind — the caller
        # wrote ctx.variables['n'] and the body read ctx.variables['x)D5DHV%…'] —
        # and why the React variable inspector, which requires an
        # identifier-shaped name, dropped 300 of 300 generated ids.
        self._variable_names: dict[str, str] = {}
        if isinstance(variable_names, dict):
            for vid, vname in variable_names.items():
                if isinstance(vid, str) and isinstance(vname, str) and vname:
                    self._variable_names[vid] = vname
        # Procedure registry: name → {block, params, has_return}. Built
        # HERE rather than in execute() so a hat-block stack — whose thread can
        # reach execute_chain() before the main thread finishes execute() —
        # never sees an empty registry, and so the parameter ids it discovers
        # are available to _read_variable_name from the first block onwards.
        self._procedures: dict[str, dict[str, Any]] = self._build_procedure_registry()

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

        # The workspace's variable table rides a top-level ``variables`` sibling
        # of ``blocks`` (same shape at every serialization setting):
        # ``[{"name": "zaehler", "id": "…"}, …]``. It is the ONLY place the
        # human name of a variable exists in the payload — the blocks reference
        # it by id alone.
        var_names: dict[str, str] = {}
        raw_vars = data.get('variables')
        if isinstance(raw_vars, list):
            for entry in raw_vars:
                if not isinstance(entry, dict):
                    continue
                vid, vname = entry.get('id'), entry.get('name')
                if isinstance(vid, str) and isinstance(vname, str) and vname:
                    var_names[vid] = vname

        return cls(blocks, variable_names=var_names)

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
                # A DISABLED hat spawns no handler thread at all. The asymmetry
                # with main stacks below is deliberate: a disabled main root
                # still has to be walked, because _exec_chain skips the disabled
                # block itself but keeps following its `next` (a disabled block
                # can carry enabled ones). A hat has no such chain semantics —
                # its body only ever runs on a trigger, and execute_chain()
                # starts AT the body, so keeping it would run the body of a
                # block the student explicitly switched off.
                if not _is_disabled(block):
                    hats.append(block)
            else:
                main.append(block)
        return main, hats

    def collect_concrete_destinations(self) -> list[dict[str, Any]]:
        """Walk the tree and collect every move_to / pickup / drop_at
        block whose target is an immediately-resolvable XYZ. Used by
        WorkflowManager.start() for the IK / Sperrzone pre-check.

        RESOLVE THE WAY THE RUNTIME DOES. This used to match only a
        ``destination_pin`` sitting INSIDE a value input — and that block is a
        STATEMENT with no output connection, so Blockly can never place it
        there. The pre-check was therefore dead code on every real workspace:
        measured, a 5 m unreachable pin and a pin inside a Sperrzone both
        returned ``[]``, while React fully implements the consumer
        (``RunControls.jsx`` → setDebuggerWarnings + a German plural toast).

        At runtime a ``destination_pin`` STATEMENT populates ``ctx.destinations``
        by NAME and a ``destination_ref`` VALUE block references it, so that is
        what we resolve here: collect every pin's name → xyz first, then match
        the refs against it.

        Pins are collected across ALL main roots BEFORE any ref is resolved, on
        purpose. Execution order between top-level stacks is creation order and
        a student can reorder them freely; a *warning* that appears or vanishes
        depending on which stack happens to run first would be worse than no
        warning. The runtime remains the authoritative gate either way.
        """
        pins: dict[str, tuple[float, float, float]] = {}
        consumers: list[dict[str, Any]] = []

        def walk(block: dict[str, Any] | None) -> None:
            if not isinstance(block, dict):
                return
            # A disabled block never runs, so it must never raise a warning.
            if _is_disabled(block):
                nxt = block.get('next')
                if isinstance(nxt, dict):
                    walk(nxt.get('block'))
                return
            btype = block.get('type')
            if btype == 'edubotics_destination_pin':
                name = self._pin_name(block)
                xyz = self._extract_concrete_xyz(block)
                if name and xyz is not None:
                    pins[name] = xyz
            elif btype in {'edubotics_move_to', 'edubotics_pickup',
                           'edubotics_drop_at'}:
                target = (self._get_input_block(block, 'DESTINATION')
                          or self._get_input_block(block, 'TARGET'))
                consumers.append({
                    'block_id': block.get('id', ''),
                    'block_type': btype,
                    'target': target,
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

        out: list[dict[str, Any]] = []
        for consumer in consumers:
            xyz = self._resolve_concrete_target(consumer['target'], pins)
            if xyz is not None:
                out.append({
                    'block_id': consumer['block_id'],
                    'block_type': consumer['block_type'],
                    'xyz': xyz,
                })
        return out

    @classmethod
    def _resolve_concrete_target(
        cls,
        target: dict[str, Any] | None,
        pins: dict[str, tuple[float, float, float]],
    ) -> tuple[float, float, float] | None:
        """XYZ for a move_to/pickup/drop_at target, or None when it can only be
        known at run time (a „Position von" lookup, a variable, an unpinned
        name). Only ``destination_ref`` → a pinned name is resolvable statically;
        ``destination_current`` deliberately is not (it captures wherever the
        arm happens to be)."""
        if not isinstance(target, dict):
            return None
        btype = target.get('type')
        if btype == 'edubotics_destination_ref':
            name = cls._pin_name(target)
            return pins.get(name) if name else None
        # Kept for hand-written / imported JSON: nothing the editor can build.
        if btype == 'edubotics_destination_pin':
            return cls._extract_concrete_xyz(target)
        return None

    @staticmethod
    def _pin_name(block: dict[str, Any]) -> str:
        fields = block.get('fields') or {}
        name = fields.get('NAME')
        return name.strip() if isinstance(name, str) else ''

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
        # NaN / Infinity are not "concrete": a degenerate projection can write
        # the literal string "NaN" into the label (applyPinnedCoordinates uses
        # Number(v).toFixed(3), and Number(NaN).toFixed(3) === "NaN"), and
        # float('NaN') parses. Feeding one to ik.solve would report an
        # unreachable warning for a reason the student cannot act on.
        if not all(math.isfinite(v) for v in (x, y, z)):
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
        # The registry is built in __init__ (so a hat thread can never observe
        # it empty); expose it to ctx so handlers can check / call.
        ctx.procedures = self._procedures
        ctx.call_procedure = lambda name, args: self._call_procedure(name, args, ctx, on_block_change)

        main_roots, _ = self.split_roots()
        total = max(1, len(main_roots))
        for idx, root in enumerate(main_roots):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            on_block_change(root.get('id', ''), 'running', idx / total)
            with _procedure_return_barrier():
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
        # Assigned UNCONDITIONALLY. The `hasattr(ctx, 'procedures')` /
        # `hasattr(ctx, 'call_procedure')` guards that used to sit here were
        # DEAD: WorkflowContext declares both as dataclass FIELDS with defaults,
        # so hasattr is always True. The default `call_procedure` is
        # ``lambda _name, _args: None`` — a silent no-op — so a hat thread
        # reaching execute_chain() before the main thread's execute() has bound
        # the real hook would have kept it, and every „Funktionsaufruf" inside
        # that hat body would have returned None without running the function.
        ctx.procedures = self._procedures
        ctx.call_procedure = lambda name, args: self._call_procedure(
            name, args, ctx, on_block_change,
        )
        # Skip the hat block itself (it has no behavior beyond the
        # trigger) and run the chained statement body.
        first = self._next_block(root)
        if first is None:
            return
        with _procedure_return_barrier():
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

        # A block the student greyed out must not run — and neither must its
        # inner content. The `next` chain is deliberately NOT skipped: it is
        # followed by _exec_chain, and a disabled block can carry enabled ones
        # below it. Returning here (rather than at the chain level) is what
        # gives that exact shape. Checked BEFORE the breakpoint/pause plumbing
        # so a breakpoint on a disabled block cannot wedge the run.
        if _is_disabled(block):
            return

        # Phase-2 debugger: respect breakpoints + pause flag *before*
        # the block runs. Breakpoints are simple — if the block id is
        # in ctx.breakpoints, set the pause event and emit a 'paused'
        # phase. The manager waits for ctx.resume_event to be set
        # before this method returns control.
        # Audit fix #4: prefer ctx.get_breakpoints() (returns the
        # manager's freshest frozenset) over the captured-at-start
        # snapshot so set_breakpoints() updates are honored mid-run.
        bp_getter = getattr(ctx, 'get_breakpoints', None)
        if callable(bp_getter):
            try:
                bp_snapshot = bp_getter() or frozenset()
            except Exception:
                bp_snapshot = getattr(ctx, 'breakpoints', None) or frozenset()
        else:
            bp_snapshot = getattr(ctx, 'breakpoints', None) or frozenset()
        if bp_snapshot and block_id in bp_snapshot:
            self._pause_for_breakpoint(ctx, block_id, on_block_change)
        elif callable(getattr(ctx, 'wait_if_paused', None)):
            ctx.wait_if_paused()

        on_block_change(block_id, 'running', 0.0)

        # Audit fix #20: emit a 'done' phase after the block body completes
        # (control-flow OR statement handler) so the React debugger panel's
        # block-state machine can transition off 'running' even when nothing
        # follows in the chain. Try/finally so 'done' fires on exception
        # paths too — the surrounding _exec_chain still re-raises so the
        # workflow status remains correct overall.
        try:
            # Control-flow first — they manage their own input/statement eval.
            #
            # Every branch below runs inside the SAME error classification the
            # handler-table call at the bottom has always had. It used not to:
            # the control-flow ladder sat under a bare try/finally, so an
            # unexpected Python exception out of one of these (measured: a
            # non-string EVENT_NAME → AttributeError in _exec_broadcast)
            # travelled all the way to WorkflowManager._run's catch-all and was
            # written into the student-facing Protokoll as
            # traceback.format_exc() — a raw English traceback on a German
            # surface (Rule §1).
            try:
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
                if btype == 'edubotics_while_visible':
                    self._exec_while_visible(block, ctx, on_block_change)
                    return
                if btype == 'edubotics_forever':
                    self._exec_forever(block, ctx, on_block_change)
                    return
                if btype == 'edubotics_wait_until':
                    self._exec_wait_until(block, ctx, on_block_change)
                    return
                if btype == 'variables_set':
                    self._exec_variables_set(block, ctx)
                    return
                if btype == 'math_change':
                    self._exec_math_change(block, ctx)
                    return
                if btype == 'lists_setIndex':
                    self._exec_lists_set_index(block, ctx)
                    return
                if btype == 'lists_getIndex' and _is_list_get_statement(block):
                    # MODE=REMOVE mutates the block into a STATEMENT: Blockly
                    # drops its output connection and adds a previous/next pair
                    # (verified — extraState {"isStatement": true}). Reaching it
                    # only through the VALUE evaluator therefore aborted the
                    # program with „Unbekannter Block-Typ: lists_getIndex".
                    #
                    # Every OTHER mode is a VALUE block, and falling through is
                    # intentional: STATEMENT_HANDLERS has no 'lists_getIndex'
                    # entry and the type IS in _BUILTIN_VALUE_TYPES, so the
                    # parked-value skip below handles it — including its 'done'
                    # status emission.
                    self._exec_lists_get_index(block, ctx)
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
            except (WorkflowError, InterpreterError, _ProcedureReturn):
                raise
            except Exception as e:
                raise InterpreterError(
                    f'Fehler beim Ausführen von "{btype}": {e}'
                )

            handler = STATEMENT_HANDLERS.get(btype)
            if handler is None:
                # A PARKED value block. It has no previousStatement, so Blockly
                # can only leave it lying at the top level, where it does
                # nothing — which is exactly what the Code panel renders
                # (pythonCodeGen's scrubNakedValue). Skipping it silently is the
                # Blockly semantic; aborting the whole program with the raw
                # English type id was not.
                if btype in _BUILTIN_VALUE_TYPES or btype in VALUE_EVALUATORS:
                    return
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
        finally:
            try:
                on_block_change(block_id, 'done', 0.0)
            except Exception:
                # Status callback failures must not mask the original
                # block-execution exception (or success).
                pass

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
        #
        # DO NOT clear the pause flag afterwards. ``wait_for_resume`` returns on
        # TWO different events and they need opposite treatment: „Fortsetzen"
        # has already cleared the pause itself (WorkflowManager.resume), while
        # „Schritt" deliberately LEAVES it set so the very next block blocks
        # again. The unconditional ``set_paused(False)`` that used to sit here
        # un-armed exactly that re-armed pause, turning „Schritt" into a full
        # Resume: measured on a 10-block chain with a breakpoint on b0, five
        # presses executed [10, 0, 0, 0, 0] blocks (the last four answering
        # „Workflow ist nicht pausiert.") where the contract is [1, 1, 1, 1, 1].
        # Pause-button stepping was correct throughout, which is why this only
        # ever showed up after a breakpoint.
        wait = getattr(ctx, 'wait_for_resume', None)
        if callable(wait):
            wait()
        elif hasattr(ctx, 'set_paused') and callable(ctx.set_paused):
            # No resume plumbing at all (a degenerate/stub ctx): clear the flag
            # we just set rather than leaving the run wedged.
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
        #
        # An EMPTY condition socket is FALSE and the scan CONTINUES — it does
        # not end the clause list. The old `if condition_block is None: break`
        # truncated at the first hole, and a student leaves holes constantly
        # (drop a „sonst wenn" row, fill the second one first). Measured, all
        # three wrong: IF0 empty + IF1 true + ELSE ran ELSE; IF0 empty + IF1
        # true with no else ran NOTHING; IF0 false + IF1 empty + IF2 true ran
        # ELSE. Blockly's own generator treats an empty condition as false and
        # keeps going.
        #
        # Scan the IFk sockets the payload actually CONTAINS. `extraState.
        # elseIfCount` is deliberately NOT read: an index in range(declared+1)
        # that is absent from `inputs` always evaluates to False, so it can
        # never change an outcome — verified over 1296 configurations
        # (declared 0..3 x every subset of present IF0..IF3 x every truth
        # assignment x DO presence x ELSE presence), 0 differences. Its only
        # effect was iteration COST, which is why it needed a clamp at all:
        # elseIfCount rides the untrusted /workflow/start payload and bounded a
        # range(). Not reading it removes the DoS surface instead of capping it,
        # and the scan is bounded by the JSON's own size.
        inputs = block.get('inputs')
        indices: set[int] = set()
        if isinstance(inputs, dict):
            for key in inputs:
                if (isinstance(key, str) and key.startswith('IF')
                        and key[2:].isdigit()):
                    indices.add(int(key[2:]))
        for idx in sorted(indices):
            condition_block = self._get_input_block(block, f'IF{idx}')
            cond = (self._eval_value(condition_block, ctx)
                    if condition_block is not None else False)
            if self._truthy(cond):
                do_block = self._get_input_block(block, f'DO{idx}')
                if do_block is not None:
                    self._exec_chain(do_block, ctx, on_block_change)
                return
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
        # Negative count → silent empty loop (matches Python's range());
        # warn so the student notices the wrong sign instead of a silently
        # skipped block.
        if n < 0:
            try:
                ctx.log('[WARNUNG] Negative Wiederholung wird ignoriert.')
            except Exception:
                pass
            n = 0
        # MAX_LOOP_ITERATIONS cap: raise rather than silently truncate so
        # the student sees an actionable German error.
        if n > MAX_LOOP_ITERATIONS:
            raise InterpreterError(
                'Schleife abgebrochen — Maximum von 10000 Wiederholungen erreicht.'
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
        iter_count = 0
        while True:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            cond = self._eval_value(bool_block, ctx) if bool_block else False
            stay = self._truthy(cond) if mode == 'WHILE' else not self._truthy(cond)
            if not stay:
                break
            iter_count += 1
            if iter_count > MAX_LOOP_ITERATIONS:
                raise InterpreterError(
                    'Schleife abgebrochen — Maximum von 10000 Wiederholungen erreicht.'
                )
            self._exec_chain(do_block, ctx, on_block_change)

    def _exec_while_visible(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        """„Solange <Typ> sichtbar" — the reliable multi-object loop.

        Each pass: retreat to the observation pose (arm out of the scene-cam
        view so it doesn't occlude the remaining objects), re-detect UNCLAIMED
        instances of the type on a FRESH frame, and run the body if any remain.
        Terminate only after WHILE_EMPTY_FRAMES consecutive empty detections
        (empty-debounce — one occluded/dropped frame won't end the loop early).
        The body typically holds „greife <Typ>" (which CLAIMS the grabbed tag, so
        the loop makes progress and terminates) + „lege ab". ``should_stop`` +
        ``MAX_LOOP_ITERATIONS`` bound it. This is a CONTROL block: the field
        OBJECT_TYPE + the DO statement body are read directly (not via the
        handler dispatch tables), so the type must be added to ci.yml's
        tutorials-validate built-in allowlist."""
        fields = block.get('fields') or {}
        type_name = fields.get('OBJECT_TYPE')
        if not type_name:
            raise InterpreterError('„Solange sichtbar": kein Objekt ausgewählt.')
        # Student-facing repetition cap (#6): the optional MAX_REPS field on the
        # block bounds how many passes (≈ objects) the loop runs. 0 / absent /
        # malformed = unbegrenzt (the hidden wall-clock + no-progress env guards
        # still bound it). A negative value is treated as 0. This is the visible
        # counterpart to those env caps.
        max_reps = 0
        raw_max = fields.get('MAX_REPS')
        if raw_max is not None:
            try:
                max_reps = max(0, int(float(raw_max)))
            except (TypeError, ValueError):
                max_reps = 0
        do_block = self._get_statement_block(block, 'DO')
        # Lazy import (handlers/__init__ already loads these; avoids any cycle).
        from physical_ai_server.workflow.handlers import motion as _mo
        from physical_ai_server.workflow.handlers import perception_blocks as _pb
        from physical_ai_server.workflow.handlers.motion import GraspSkip
        iter_count = 0
        empty_streak = 0
        empty_since: float | None = None
        stall_passes = 0
        loop_start = time.monotonic()
        while True:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            # Wall-clock cap (#3): never run forever even if the body keeps making
            # (slow) progress or a flaky camera keeps the gate flickering.
            if time.monotonic() - loop_start > WHILE_MAX_SECONDS:
                ctx.log(
                    '[WARNUNG] „Solange sichtbar": Zeitlimit erreicht — Schleife '
                    'beendet.'
                )
                break
            # Retreat so the arm doesn't occlude remaining objects during detect.
            # LIMITATION (rig-validate): if a grasping `edubotics_when_object_seen`
            # hat runs CONCURRENTLY with this loop, the hat can hold motion_lock
            # for a full pickup (~6.5 s); this retreat's _publish_motion waits up
            # to 10 s for the lock and then raises "Bewegung blockiert", ending the
            # loop. Two things driving the arm at once is an inherently conflicting
            # program — don't pair a grasping when-seen hat with this loop. The
            # failure is a clean German error, not a hang (motion_lock is an RLock
            # with a bounded acquire). Revisit (graceful retreat-skip) if a real
            # use case needs them together.
            _mo.go_to_observation_pose(ctx)
            # Settle after the retreat so the first detect frame isn't a still-
            # settling/blurred arm (#3).
            self._interruptible_wait(ctx, WHILE_SETTLE_S)
            n_visible = _pb.count_unclaimed_visible(ctx, type_name)
            if n_visible > 0:
                empty_streak = 0
                empty_since = None
                iter_count += 1
                if iter_count > MAX_LOOP_ITERATIONS:
                    raise InterpreterError(
                        'Schleife abgebrochen — Maximum von 10000 '
                        'Wiederholungen erreicht.'
                    )
                # Per-pass positive feedback (#7): tell the student how many of
                # this type are still to do, so the loop isn't a silent black box.
                try:
                    label = _pb.label_for(ctx, type_name)
                    # „greife EINES" was the neuter accusative pronoun, wrong
                    # for *der Würfel* (which needs „einen") — and this line
                    # prints once per pass, so it is the most-read German string
                    # in the whole „Solange sichtbar" lesson. The passive has no
                    # pronoun and no gender at all, and it stops reading as an
                    # imperative aimed at the student. Pre-existing on `main`,
                    # swept with its three neighbours so the file does not ship
                    # two conventions.
                    ctx.log(f'„{label}": noch {n_visible} sichtbar — '
                            'das nächste wird gegriffen.')
                except Exception:
                    pass
                # Flicker-spin guard (#3): a pass makes progress iff the body
                # CLAIMS or SKIPS a tag (the claimed+skipped count grows).
                # count_unclaimed_visible already ran the reclaim, so this snapshot
                # is post-reclaim; the body (grasp) is the only thing that mutates
                # the sets between here and the re-read.
                before = _claim_progress_count(ctx)
                if do_block is not None:
                    try:
                        self._exec_chain(do_block, ctx, on_block_change)
                    except GraspSkip as e:
                        # One instance couldn't be grasped (out of reach /
                        # orientation unreadable / vanished / not held after
                        # retries) and was marked skipped by grasp_object — keep
                        # going on the rest instead of aborting the whole loop. A
                        # HARD error (calibration, stop) is NOT a GraspSkip and
                        # still propagates out and ends the loop.
                        ctx.log(f'[WARNUNG] {e} Wird übersprungen.')
                if _claim_progress_count(ctx) > before:
                    stall_passes = 0
                else:
                    stall_passes += 1
                    if stall_passes >= WHILE_STALL_PASSES:
                        # „kein Fortschritt" names the SYMPTOM. When the body
                        # provably contains no claiming block, we also know the
                        # CAUSE and the remedy, so say them: the split grasp
                        # path („finde" → „fahre über" → „senke auf" → „schließe
                        # um" → „hebe an") does not claim, so „finde …" hands
                        # back the same nearest instance every pass and the arm
                        # re-grasps ONE object while the others are never
                        # touched. Measured 2026-09-07, 3 cubes: 3 gripper
                        # closes, ALL on tag 22, claimed {}, ending here.
                        ctx.log(
                            '[WARNUNG] „Solange sichtbar": kein Fortschritt — '
                            'Schleife beendet.'
                        )
                        # An EMPTY body is excluded: „der Block fehlt" would be
                        # the wrong half of the truth when the whole body is
                        # missing, and nothing was grasped to mark done.
                        if do_block is not None and not self._body_can_claim(do_block):
                            ctx.log(
                                '[WARNUNG] Im Schleifenkörper fehlt „merke … '
                                'als erledigt": ohne diesen Block bleibt jedes '
                                'Objekt offen, „finde …" wählt immer wieder '
                                'dasselbe und der Arm greift nur dieses eine. '
                                'Bitte „merke <Ziel> als erledigt" nach dem '
                                'Greifen einsetzen — oder den Block „Greife …" '
                                'benutzen, der das selbst erledigt.'
                            )
                        break
                # Student repetition cap (#6): stop after the requested number of
                # passes even if more objects remain visible.
                if max_reps > 0 and iter_count >= max_reps:
                    ctx.log(
                        f'„Solange sichtbar": Höchstzahl von {max_reps} erreicht '
                        '— Schleife beendet.'
                    )
                    break
                continue
            # No unclaimed instances visible — start/continue the empty window. A
            # reclaim that returns an object (count>0 above) resets empty_since/
            # empty_streak, so this measures CONTINUOUS emptiness.
            now = time.monotonic()
            if empty_since is None:
                empty_since = now
            empty_streak += 1
            # Terminate only after BOTH the consecutive-frame count AND the
            # continuous-empty duration thresholds are met.
            if (empty_streak >= WHILE_EMPTY_FRAMES
                    and (now - empty_since) >= WHILE_EMPTY_SECONDS):
                # Clean completion (#7): a friendly "done" line so the student
                # knows the loop finished because nothing is left (not an error).
                #
                # „nichts mehr sichtbar" is a claim about the WORLD, and it is
                # FALSE whenever an instance ended the loop SKIPPED: the gate
                # counts UNCLAIMED-visible, and a skipped tag is excluded from
                # that view (claims.excluded_ids), so a cube the arm could not
                # reach lies in plain sight while the loop reports it gone. The
                # reason for each skip was already logged as a [WARNUNG]; what
                # was missing is that the ENDING acknowledged it, which is what
                # made the loop look better than the identical block-by-block
                # program while moving the same cubes.
                try:
                    label = _pb.label_for(ctx, type_name)
                    n_left = self._skipped_count_for_type(ctx, type_name)
                    # The type is already CITED by the „{label}:" prefix, so
                    # the second mention was pure repetition — and it was
                    # mis-inflected: „ein „{label}"" is masculine-only, and
                    # „{n} „{label}"" is an invariant plural (*Kugel* → *Kugeln*).
                    # Counting OBJEKTE removes the gender surface entirely.
                    # („Die Gründe stehen" in the plural branch: n skips produce
                    # n separate [WARNUNG]s above.)
                    if n_left == 1:
                        ctx.log(
                            f'„{label}": fertig — aber ein Objekt konnte nicht '
                            'gegriffen werden und liegt noch da. Der Grund '
                            'steht oben im Protokoll.'
                        )
                    elif n_left > 1:
                        ctx.log(
                            f'„{label}": fertig — aber {n_left} Objekte konnten '
                            'nicht gegriffen werden und liegen noch da. Die '
                            'Gründe stehen oben im Protokoll.'
                        )
                    else:
                        ctx.log(f'„{label}": nichts mehr sichtbar — fertig.')
                except Exception:
                    pass
                break
            self._interruptible_wait(ctx, WHILE_EMPTY_SECONDS)

    @staticmethod
    def _skipped_count_for_type(ctx, type_name) -> int:
        """How many tags of ``type_name`` ended the loop SKIPPED — i.e. objects
        still lying on the table that the loop's gate no longer sees.

        Read under ``claim_lock`` (a ``when_object_seen`` hat thread mutates the
        same set). Best-effort: any failure — no catalog, an unknown type, no
        claim bookkeeping on a stub ctx — answers 0, which yields the unchanged
        „nichts mehr sichtbar" line. A diagnostic may never break a loop."""
        from physical_ai_server.workflow.handlers import perception_blocks as _pb
        try:
            recipe = _pb._recipe_for(_pb._require_catalog(ctx), type_name)
            type_ids = {int(i) for i in recipe.tag_ids}
        except Exception:  # noqa: BLE001 — a diagnostic never fails a run
            return 0
        lock = getattr(ctx, 'claim_lock', None)
        try:
            if lock is not None:
                with lock:
                    skipped = {int(i) for i in (ctx.skipped_tags or set())}
            else:
                skipped = {int(i) for i in (getattr(ctx, 'skipped_tags', None) or set())}
        except Exception:  # noqa: BLE001
            return 0
        return len(type_ids & skipped)

    # Statement blocks that CLAIM a tag, and therefore the only two ways a
    # „Solange <Typ> sichtbar" loop body can make the progress that terminates
    # it: the composite „Greife …" (claims after check_grasp_held — never a
    # missed grab) and the split path's explicit „merke … als erledigt". Keep in
    # lockstep with handlers/__init__.py::STATEMENT_HANDLERS.
    _CLAIMING_BLOCK_TYPES = frozenset({
        'edubotics_grasp_object',
        'edubotics_mark_done',
    })
    # Depth cap for the body scan below: statement nesting a student can build
    # is far shallower, and the payload is untrusted (/workflow/start).
    _CLAIM_SCAN_MAX_DEPTH = 64

    def _body_can_claim(self, block: Any, _depth: int = 0, _seen=None) -> bool:
        """Best-effort: can the statement chain rooted at ``block`` reach a block
        that CLAIMS a tag?

        Used ONLY to choose between two wordings of the stall warning, after the
        loop has already MEASURED three passes with zero progress — so the scan
        never invents an alarm, it only decides whether we may also name the
        cause. Accordingly it FAILS OPEN: anything it cannot decide (an
        unresolvable procedure call, a recursion/depth cap, a malformed payload)
        answers True and the student gets the unchanged generic line. A false
        "cannot claim" would blame a missing block that is right there; a false
        "can claim" merely withholds a hint."""
        if _depth > self._CLAIM_SCAN_MAX_DEPTH:
            return True                     # undecided ⇒ fail open
        if not isinstance(block, dict):
            return False                    # end of chain: nothing found here
        btype = block.get('type')
        if btype in self._CLAIMING_BLOCK_TYPES:
            return True
        if btype in {'procedures_callnoreturn', 'procedures_callreturn'}:
            # A procedure body may well claim. Resolve it; an unresolvable call
            # is undecided, not "no".
            seen = set(_seen) if _seen else set()
            name = self._read_call_name(block)
            spec = self._procedures.get(name) if name else None
            if spec is None or name in seen:
                return True                 # undecided ⇒ fail open
            seen.add(name)
            body = self._get_statement_block(spec.get('block') or {}, 'STACK')
            if self._body_can_claim(body, _depth + 1, seen):
                return True
            _seen = seen
        # Recurse into every nested statement/value slot (if / repeat / while /
        # forever bodies all hang off `inputs`), then along the chain.
        inputs = block.get('inputs')
        if isinstance(inputs, dict):
            for slot in inputs.values():
                if not isinstance(slot, dict):
                    continue
                for key in ('block', 'shadow'):
                    if self._body_can_claim(slot.get(key), _depth + 1, _seen):
                        return True
        nxt = block.get('next')
        if isinstance(nxt, dict):
            return self._body_can_claim(nxt.get('block'), _depth + 1, _seen)
        return False

    @staticmethod
    def _interruptible_wait(ctx, seconds: float) -> None:
        """Sleep up to ``seconds`` (monotonic), bailing immediately on stop with
        the standard German WorkflowError. Used by the while-visible loop's settle
        + empty-window waits."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _exec_forever(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        """„wiederhole fortlaufend" — an intentionally infinite C-block.

        The Stop button (``ctx.should_stop``) is the ONLY exit; this loop
        deliberately does NOT apply ``MAX_LOOP_ITERATIONS`` — a finite cap
        would silently end a loop the student meant to run forever. The body
        (the ``DO`` statement chain) already re-checks ``should_stop`` at the
        top of every block via ``_exec_chain``, and the outer check below
        bounds the gap between body runs.

        Empty-body decision (DO is None): we do NOT raise a „leere Schleife"
        error. A forever loop with no body is a legitimate "keep the workflow
        alive while hat handlers (when_broadcast / when_object_seen) do the
        work" idiom — exactly Scratch's bottom-of-script ``forever``. Raising
        would also punish a student who dropped the block and hit Run before
        filling it. Instead the empty body idles in a small ``should_stop``-
        honoring sleep so it can never spin a tight CPU loop and always stops
        promptly.

        Rate floor (#H1): a NON-empty body of only fast value/log blocks would
        otherwise loop thousands of times/sec and flood the WorkflowStatus
        realtime channel (unlike ``controls_whileUntil``, which self-terminates
        at ``MAX_LOOP_ITERATIONS`` — ``forever`` is deliberately uncapped, so it
        needs its own rate limit). We enforce ``FOREVER_MIN_CYCLE_S`` per
        iteration: a body faster than the floor sleeps the remainder (bounding
        the publish rate to ~20 Hz); a motion-bearing body naturally exceeds the
        floor and pays nothing. NOTE: a ``forever`` nested INSIDE a ``when_*`` hat
        handler holds ``motion_lock`` for its whole (infinite) run — that is the
        same "two writers driving the arm" conflict the while-visible loop
        documents as unsupported; don't pair them.
        """
        do_block = self._get_statement_block(block, 'DO')
        # „wiederhole fortlaufend" carries a nextStatement connector (Scratch's
        # forever deliberately does not), so a student can and does snap blocks
        # underneath it — where they are silent dead code, because the only exit
        # from this loop is Stop. Measured BEFORE=1 LOOP=19 AFTER=0. Say so
        # rather than swallowing it. Removing the connector is the real fix but
        # it is NOT backward-safe on its own: Blockly's loader raises
        # MissingConnection on any saved workspace that already has a block
        # there, i.e. the student loses the program. The editor-side half is
        # `blocks/control.js::attachControlWorkspaceValidators`, which flags the
        # same blocks with a KEYED setWarningText while the program is being
        # written — it exists now; this used to claim a warning that did not.
        if self._next_block(block) is not None:
            try:
                ctx.log(
                    # IMPERSONAL register, like the ~26 other strings this
                    # surface emits: „bis auf „Stopp" gedrückt wird", not „bis
                    # du drückst". Both are fine German; the impersonal one is
                    # the majority and is what a teacher reading over a
                    # shoulder expects.
                    '[WARNUNG] Blöcke unter „wiederhole fortlaufend" werden nie '
                    'ausgeführt — die Schleife läuft, bis auf „Stopp" gedrückt '
                    'wird. Bitte die Blöcke nach OBEN oder IN die Schleife '
                    'ziehen.'
                )
            except Exception:
                pass
        while True:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            # Honor Pause between iterations / for an empty body — the body's own
            # blocks check pause per-block, but the empty-body idle and the
            # rate-floor sleep would otherwise ignore it (#E3).
            if callable(getattr(ctx, 'wait_if_paused', None)):
                ctx.wait_if_paused()
            if do_block is None:
                # Empty body: never busy-wait. Idle one tick (still bailing on
                # stop via _interruptible_wait's German WorkflowError) and loop.
                self._interruptible_wait(ctx, 0.1)
                continue
            cycle_start = time.monotonic()
            self._exec_chain(do_block, ctx, on_block_change)
            elapsed = time.monotonic() - cycle_start
            if elapsed < FOREVER_MIN_CYCLE_S:
                self._interruptible_wait(ctx, FOREVER_MIN_CYCLE_S - elapsed)

    def _exec_wait_until(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        """„warte bis <Bedingung>" — block until the Boolean input is truthy.

        Polls the ``BOOL`` value input every ~0.1 s, RE-EVALUATING it each
        tick (so a perception / variable / count condition is checked live,
        not snapshotted once). ``ctx.should_stop`` raises the standard German
        stopped error so the Stop button always works.

        Safety-cap decision: unlike Scratch's uncapped wait-until, we apply a
        generous wall-clock cap (``WAIT_UNTIL_MAX_SECONDS`` = 300 s). A
        classroom student can author a condition that never becomes true
        (typo'd threshold, an object that is never placed) and wedge the whole
        session — the same risk WHILE_MAX_SECONDS guards in the while-visible
        loop. On hitting the cap we MIRROR that loop: log a German [WARNUNG]
        and break (the workflow CONTINUES to the next block), rather than
        hanging forever or hard-raising. 300 s is long enough that a normal
        „warte bis Banane gesehen" never trips it.

        Motion-lock (#H2): we RELEASE ``motion_lock`` around the poll (mirroring
        ``perception_blocks._poll_until``, audit S1) and bounded-reacquire on
        exit. A ``warte bis`` dropped inside a ``when_*`` hat handler — whose
        body runs under ``with ctx.motion_lock`` — would otherwise hold the lock
        for up to the 300 s cap, starving the main stack + every other hat's
        motion (the 10 s ``acquire`` would raise „Bewegung blockiert") and
        re-opening the collision-recovery race ``_poll_until`` was written to
        close. The poll itself is pure value evaluation (no motion), so dropping
        the lock during it is safe; reacquire keeps the hat's locking invariant.
        """
        bool_block = self._get_input_block(block, 'BOOL')
        motion_lock = getattr(ctx, 'motion_lock', None)
        released = False
        if motion_lock is not None:
            try:
                motion_lock.release()
                released = True
            except RuntimeError:
                # Not held by this thread (the normal main-stack path) — fine;
                # don't reacquire in finally.
                released = False
        try:
            start = time.monotonic()
            while True:
                if ctx.should_stop():
                    raise WorkflowError('Workflow wurde gestoppt.')
                # Honor Pause inside the wait (#E3) — the poll never runs a block,
                # so without this Pause would do nothing until the condition/cap.
                if callable(getattr(ctx, 'wait_if_paused', None)):
                    ctx.wait_if_paused()
                cond = self._eval_value(bool_block, ctx) if bool_block is not None else False
                if self._truthy(cond):
                    return
                if time.monotonic() - start > WAIT_UNTIL_MAX_SECONDS:
                    try:
                        ctx.log(
                            '[WARNUNG] „Warte bis": Zeitlimit erreicht — die '
                            'Bedingung wurde nicht erfüllt. Es geht weiter.'
                        )
                    except Exception:
                        pass
                    return
                # ~0.2 s between polls — matches perception_blocks._poll_until so a
                # perception condition isn't detected at 2× the usual cadence; bails
                # immediately on stop.
                self._interruptible_wait(ctx, 0.2)
        finally:
            if released and motion_lock is not None:
                # Restore the caller's invariant unconditionally. This was a
                # bounded reacquire that RAISED „Bewegung-Sperre konnte nicht
                # zurückgewonnen werden." past the bound, claiming to give the
                # hat's outer release "something to release" — it gives it
                # nothing, and the resulting `RuntimeError: cannot release
                # un-acquired lock` escapes into `_run_hat_handler`'s OUTER
                # bare `except Exception: return`: hat dead, no message, run
                # green. It also masked an in-flight Stop. See
                # motion._reacquire_after_release.
                _reacquire_after_release(ctx)

    def _exec_for(
        self,
        block: dict[str, Any],
        ctx,
        on_block_change: Callable[[str, str, float], None],
    ) -> None:
        var_name = self._read_variable_name(block, 'VAR') or 'i'
        # Read WITHOUT the `or <default>` coercion — the same trap math_modulo
        # already carries an explicit comment about. `0.0 or 1` evaluates to 1,
        # so the step-0 guard below could NEVER fire: „zähle i von 1 bis 3 in
        # Schritten von 0" ran 3 iterations and reported success, silently
        # substituting a step the student did not ask for. Only a genuinely
        # missing (None) input falls back to the default.
        start = self._number_or(self._eval_value(self._get_input_block(block, 'FROM'), ctx), 0.0)
        end = self._number_or(self._eval_value(self._get_input_block(block, 'TO'), ctx), 0.0)
        step = self._number_or(self._eval_value(self._get_input_block(block, 'BY'), ctx), 1.0)
        if step == 0:
            # Name the FIX, not only the fault. „ungleich 0", not „größer als
            # 0": a negative step is legal and counts down.
            raise InterpreterError(
                'Schrittweite 0 ist ungültig — bitte eine Schrittweite '
                'ungleich 0 wählen.')
        # A non-finite step makes both loop conditions false, so the loop would
        # run ZERO times and report success — the same silent-wrong-answer class
        # the `or` trap above produced. Fail loud instead.
        if not math.isfinite(step):
            raise InterpreterError('Schrittweite ist keine gültige Zahl.')
        do_block = self._get_input_block(block, 'DO')
        i = start
        iter_count = 0
        while (step > 0 and i <= end) or (step < 0 and i >= end):
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            iter_count += 1
            if iter_count > MAX_LOOP_ITERATIONS:
                raise InterpreterError(
                    'Schleife abgebrochen — Maximum von 10000 Wiederholungen erreicht.'
                )
            self._set_variable(ctx, var_name, i)
            self._exec_chain(do_block, ctx, on_block_change)
            i += step

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
        iter_count = 0
        for item in items:
            if ctx.should_stop():
                raise WorkflowError('Workflow wurde gestoppt.')
            iter_count += 1
            if iter_count > MAX_LOOP_ITERATIONS:
                raise InterpreterError(
                    'Schleife abgebrochen — Maximum von 10000 Wiederholungen erreicht.'
                )
            self._set_variable(ctx, var_name, item)
            self._exec_chain(do_block, ctx, on_block_change)

    def _exec_variables_set(self, block: dict[str, Any], ctx) -> None:
        var_name = self._read_variable_name(block, 'VAR')
        # TRUTHINESS, not `is None`. `_read_variable_name` ends
        # `if isinstance(value, str): return value`, so a bare-string field
        # `{"VAR": ""}` yields '' and framed as `[VAR:=5]` — which matches
        # NEITHER React regex (`[^=]+` needs ≥1 char), falls through
        # `interceptToken` and is rendered VERBATIM in the student's Protokoll.
        # The two neighbouring call sites (`controls_for`, `controls_forEach`)
        # were already truthiness-guarded (`… or 'i'`); these two were the odd
        # ones out. `.strip()` makes the server agree with React's
        # `isDisplayableVariableName`, which already refuses whitespace-only —
        # that half is consistency, not a bug fix (a blank-name frame is
        # consumed silently there).
        if not var_name or not var_name.strip():
            raise InterpreterError('Variable hat keinen Namen.')
        value_block = self._get_input_block(block, 'VALUE')
        value = self._eval_value(value_block, ctx) if value_block else None
        self._set_variable(ctx, var_name, value)

    # The characters that cannot survive the `[VAR:name=json]` frame, and the
    # one place that still holds the WHOLE name.
    #
    # React captures the name with `^\[VAR:([^=]+)=(.*)\]$`, so a name
    # containing `=` RE-SPLITS the frame: measured end to end through the real
    # hook and the real reducer, a variable „mein Wert=2" set to 5 emits
    # `[VAR:mein Wert=2=5.0]`, which parses as name „mein Wert", value „2=5.0" —
    # the panel grows a row the student never created and, if a real „mein Wert"
    # exists, its value is SILENTLY CLOBBERED, with nothing in the Protokoll to
    # say so. `variableName.js`'s `=` refusal cannot catch that: by the time it
    # runs, `[^=]+` has already cut the name.
    #
    # SPLITTING ON THE LAST `=` LIKE `[CNT:]` IS REFUSED, and this must stay
    # written down: the `[VAR:]` value is JSON and can legitimately contain `=`
    # inside a string (`setze x auf "a=b"` → `[VAR:x="a=b"]`). `[CNT:]`'s value
    # is DIGITS-ONLY, which is the entire reason last-`=` is unambiguous there.
    # The two frames may never be harmonised.
    #
    # `[` and `]` are the frame's own delimiters and go with it. Blockly 12.5.1
    # permits all three in a variable name (`Variables.promptName`'s whole
    # normalization is `replace(/[\s\xa0]+/g,' ').trim()`), so „x=5" is
    # editor-reachable and a plausible beginner name.
    _UNSHOWABLE_NAME_CHARS = '=[]'

    def _warn_unshowable_variable_once(self, ctx, name: str) -> None:
        """One German [WARNUNG] per NAME per run — a loop would otherwise flood.

        A wrong row becomes an EXPLAINED ABSENCE. The program is unaffected: the
        variable itself is stored and readable, only the panel cannot show it.
        """
        try:
            seen = getattr(ctx, '_unshowable_var_warned', None)
            if seen is None:
                seen = set()
                ctx._unshowable_var_warned = seen
            if name in seen:
                return
            seen.add(name)
            ctx.log(
                f'[WARNUNG] Die Variable „{name}" kann in der Variablen-Tafel '
                'nicht angezeigt werden — die Zeichen = [ ] sind im Namen nicht '
                'erlaubt. Das Programm läuft normal weiter.'
            )
        except Exception:  # noqa: BLE001 — observability never breaks a run
            pass

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
        # Cap the payload (the [VAR:] path bypasses output.log's MAX_LOG_CHARS):
        # `forever { setze x = verbinde(x, …) }` would otherwise re-emit an
        # ever-growing string ~20×/s and flood the realtime channel.
        try:
            # Serialize a BOUNDED prefix, not the whole value. json.dumps() ran
            # over the entire object before the char cap was applied, so a
            # 10-million-element list cost 0.74 s and ~268 MB RSS to produce
            # 2000 characters — on the ROS node's own thread.
            if any(c in name for c in self._UNSHOWABLE_NAME_CHARS):
                # See _UNSHOWABLE_NAME_CHARS. This is the only layer that still
                # holds the whole name, so the refusal belongs here; the React
                # gate stays exactly as it is, as honest defence-in-depth
                # against an untrusted wire rather than the primary defence.
                self._warn_unshowable_variable_once(ctx, name)
                return
            payload = json.dumps(_jsonable(value, _MAX_VAR_PAYLOAD_ITEMS))
            if len(payload) > _MAX_VAR_PAYLOAD_CHARS:
                payload = payload[:_MAX_VAR_PAYLOAD_CHARS] + ' …'
            ctx.log(f'[VAR:{name}={payload}]')
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
        """„setze/füge ein Element" — matched to Blockly's own generated Python.

        Three divergences fixed, each verified against what
        ``pythonGenerator`` emits for the same block:

        * INSERT + LAST → ``L.append(v)`` (index ``len``). It used to resolve to
          ``len - 1`` and insert BEFORE the last element: [1,2,3] + 99 gave
          [1,2,99,3] where Blockly gives [1,2,3,99].
        * INSERT on an EMPTY list → ``[].append(v)`` works; the blanket
          „Liste ist leer." refusal made the only way to build a list one item
          at a time impossible.
        * INSERT at ``len`` (one past the end) → an append; only SET needs the
          index to address an EXISTING element.
        """
        list_block = self._get_input_block(block, 'LIST')
        target = self._eval_value(list_block, ctx) if list_block else None
        if not isinstance(target, list):
            raise InterpreterError('Setze-Element-Block hat keine Liste.')
        fields = block.get('fields') or {}
        mode = fields.get('MODE', 'SET')  # SET or INSERT
        where = fields.get('WHERE', 'FROM_START')
        at_block = self._get_input_block(block, 'AT')
        at = int(self._eval_value(at_block, ctx) or 0) if at_block else 0
        value_block = self._get_input_block(block, 'TO')
        value = self._eval_value(value_block, ctx) if value_block else None
        inserting = (mode == 'INSERT')
        if not target and not inserting:
            raise InterpreterError('Liste ist leer.')
        if len(target) >= MAX_LIST_ITEMS and inserting:
            raise InterpreterError(
                f'Liste ist auf {MAX_LIST_ITEMS} Elemente begrenzt.'
            )
        idx = self._resolve_index(target, where, at, for_insert=inserting)
        # An insert may address one past the end (that is an append); a SET must
        # land on an element that exists.
        upper = len(target) if inserting else len(target) - 1
        if idx < 0 or idx > upper:
            raise InterpreterError(_list_index_error_de(at, len(target)))
        if inserting:
            target.insert(idx, value)
        else:
            target[idx] = value

    def _exec_lists_get_index(self, block: dict[str, Any], ctx) -> None:
        """``lists_getIndex`` in its STATEMENT form (MODE=REMOVE).

        Blockly's REMOVE mode drops the block's output connection and gives it
        previous/next connectors, so it arrives here rather than at the value
        evaluator. Generated Python is ``L.pop(i)`` (``L.pop()`` for LAST) with
        the result discarded."""
        self._lists_get_index(block, ctx, statement=True)

    def _lists_get_index(
        self,
        block: dict[str, Any],
        ctx,
        statement: bool = False,
    ) -> Any:
        # ALL THREE REFUSALS FIRE IN BOTH FORMS. They used to be gated on
        # ``statement``, so „setze Element 9" of a 2-element list was RED and
        # „hole Element 9" was GREEN with a blank Protokoll line — same block
        # family, same lesson, opposite answers — and the resulting ``None``
        # then propagated (`setze x auf hole Element 9` leaves x = None) so the
        # error surfaced later, somewhere unrelated. Blockly lists are 1-BASED,
        # which makes „hole Element 4" of a three-element list the single most
        # likely list mistake a twelve-year-old makes.
        #
        # THIS DOES NOT TOUCH THE PARKED-BLOCK RULE. „EXACTLY TWO things are
        # skipped silently … a DISABLED block and a PARKED value block" still
        # holds: a parked block never reaches this function at all —
        # ``_exec_block``'s ladder gates the statement route on
        # ``_is_list_get_statement`` and every other mode falls through to the
        # ``_BUILTIN_VALUE_TYPES`` skip, which contains this type. An empty
        # SOCKET inside a LIVE chain is a different thing from a parked BLOCK,
        # and the round already refused one („Beim Vergleich fehlt ein Wert").
        #
        # Blockly's own generated Python is ``L[i]``, which raises IndexError,
        # so raising is PARITY with the code the student sees in the Code panel;
        # returning None was the divergence.
        # The block NAMES itself: in its statement form the student dragged
        # „entferne Element", in its value form „hole Element".
        block_de = 'entferne Element' if statement else 'hole Element'
        target = self._eval_value(self._get_input_block(block, 'VALUE'), ctx)
        if not isinstance(target, list):
            raise InterpreterError(
                f'„{block_de}" braucht eine Liste — bitte eine Liste in den '
                'Sockel ziehen.')
        fields = block.get('fields') or {}
        mode = fields.get('MODE', 'GET')
        where = fields.get('WHERE', 'FROM_START')
        at_block = self._get_input_block(block, 'AT')
        at = int(self._eval_value(at_block, ctx) or 0) if at_block else 0
        if not target:
            raise InterpreterError(
                f'Die Liste ist leer — „{block_de}" findet nichts.')
        idx = self._resolve_index(target, where, at)
        if idx < 0 or idx >= len(target):
            raise InterpreterError(_list_index_error_de(at, len(target)))
        # GET_REMOVE and REMOVE both MUTATE — Blockly generates `L.pop(i)` for
        # each. GET_REMOVE used to return the item and leave the list untouched,
        # so „entferne und hole" silently behaved as a plain „hole" and a loop
        # draining a list never terminated.
        if mode in ('GET_REMOVE', 'REMOVE'):
            return target.pop(idx)
        return target[idx]

    def _exec_math_change(self, block: dict[str, Any], ctx) -> None:
        """„ändere <x> um <n>" — the ONLY counting idiom Blockly's Variablen
        flyout offers, and it had no handler at all.

        It is palette-reachable with no crafted payload: the flyout emits
        exactly ``[variables_set, math_change, variables_get]`` (verified
        against the shipped Blockly), so it sits between the two blocks a
        student uses constantly, and the Code panel renders working Python for
        it while the run died with „Unbekannter Block-Typ: math_change".
        @blockly/suggested-blocks then stores the type in the SAVED workflow, so
        it reappears after deletion, next session, and for anyone the workflow is
        shared with.

        Semantics are Blockly's own generated Python,
        ``x = (x if isinstance(x, Number) else 0) + delta``: a variable holding
        text (or nothing yet) counts as 0 rather than raising.
        """
        var_name = self._read_variable_name(block, 'VAR')
        # See _exec_variables_set: truthiness, not `is None`.
        if not var_name or not var_name.strip():
            raise InterpreterError('Variable hat keinen Namen.')
        delta = self._number_or(
            self._eval_value(self._get_input_block(block, 'DELTA'), ctx), 0.0)
        current = self._read_variable(ctx, var_name)
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            base = 0.0
        else:
            base = float(current)
        self._set_variable(ctx, var_name, base + delta)

    @staticmethod
    def _number_or(value: Any, default: float) -> float:
        """float(value), with ``default`` ONLY for a genuinely missing input.

        Deliberately not ``float(value or default)``: that turns an explicit
        zero into the default (see _exec_for's step-0 guard, which the `or`
        trap made unreachable)."""
        if value is None:
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _resolve_index(items: list, where: str, at: int,
                       for_insert: bool = False) -> int:
        if where == 'FROM_END':
            return len(items) - at
        if where == 'FIRST':
            return 0
        if where == 'LAST':
            # For an INSERT, "last" means AFTER the last element — Blockly
            # generates `L.append(v)`, i.e. index len. For every read/remove it
            # means the last element itself.
            return len(items) if for_insert else len(items) - 1
        if where == 'RANDOM':
            if for_insert:
                return random.randrange(0, len(items) + 1)
            return random.randrange(0, len(items)) if items else 0
        return at - 1  # FROM_START is 1-indexed in Blockly

    # ------------------------------------------------------------------
    # Procedures
    # ------------------------------------------------------------------
    def _build_procedure_registry(self) -> dict[str, dict[str, Any]]:
        """Walk all roots and find procedures_def* blocks. Each entry:
        ``{block, params, has_return}``.
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
            # The DEFINITION's name IS a serializable FieldTextInput, so
            # fields.NAME is correct here — unlike the CALL block below, whose
            # NAME is a FieldLabel with isSerializable:false.
            raw_name = (block.get('fields') or {}).get('NAME')
            name = raw_name.strip() if isinstance(raw_name, str) else ''
            if name:
                # ``extraState`` is a raw XML STRING — not a dict — for a block
                # that defines only mutationToDom (verified: an unparented
                # procedures_ifreturn saves "<mutation value=\"1\"></mutation>").
                # ``(… or {}).get('params')`` therefore raised AttributeError,
                # which escapes Interpreter.__init__ → from_json, and
                # WorkflowManager.start guards only `except InterpreterError`,
                # so it reached the service callback as a raw Python error. Not
                # reachable through the editor; reachable through a hand-written
                # /workflow/start payload — and rosbridge authenticates nobody.
                extra = block.get('extraState')
                raw_params = (extra.get('params')
                              if isinstance(extra, dict) else None)
                params = raw_params if isinstance(raw_params, list) else []
                # Each param entry: {name, id} — Blockly's saveExtraState shape.
                param_names = [p.get('name', '') for p in params if isinstance(p, dict)]
                registry[name] = {
                    'block': block,
                    'params': param_names,
                    'has_return': btype == 'procedures_defreturn',
                }
                # A procedure parameter IS a workspace variable, so it normally
                # arrives in the top-level `variables` table too. Registering
                # the def's own {name, id} pairs as well makes the id → name
                # resolution work even for a hand-written or trimmed payload
                # that carries no variables table — without it the body's
                # variables_get would read the raw parameter id and the binding
                # would be None again.
                for p in params:
                    if not isinstance(p, dict):
                        continue
                    pid, pname = p.get('id'), p.get('name')
                    if (isinstance(pid, str) and isinstance(pname, str)
                            and pname and pid not in self._variable_names):
                        self._variable_names[pid] = pname
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
        name = self._read_call_name(block)
        if not name:
            raise InterpreterError('Funktionsaufruf ohne Namen.')
        # Args are ARG0, ARG1, ... value inputs — read POSITIONALLY, because an
        # empty socket is omitted from `inputs` entirely (see _read_call_args).
        args = self._read_call_args(block, ctx, name)
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
        # .strip() straight off the raw field raised AttributeError for a
        # non-string EVENT_NAME (measured for int / None / list), and the
        # control-flow ladder used to have no error classification, so the raw
        # Python traceback landed in the student's Protokoll.
        name = event_name_of(block)
        if not name:
            return
        if hasattr(ctx, 'fire_broadcast') and callable(ctx.fire_broadcast):
            ctx.fire_broadcast(name)

    # ------------------------------------------------------------------
    # Value evaluation
    # ------------------------------------------------------------------
    def _eval_value(self, block: dict[str, Any] | None, ctx) -> Any:
        """Public wrapper that mirrors _exec_block's error-classification
        contract. WorkflowError / InterpreterError / _ProcedureReturn pass
        through unchanged; any other exception coming out of a value
        evaluator (a perception handler called inside `controls_if`,
        Blockly-arithmetic on bad input, etc.) is re-raised as a German
        InterpreterError so the student sees an actionable message
        instead of a raw Python traceback in the WorkflowStatus log strip.
        """
        if block is None:
            return None
        # A disabled VALUE block contributes nothing, exactly as the Code panel
        # renders it — an empty socket, i.e. None. Same rule as the statement
        # side in _exec_block.
        if _is_disabled(block):
            return None
        try:
            return self._eval_value_impl(block, ctx)
        except WorkflowError:
            raise
        except InterpreterError:
            raise
        except _ProcedureReturn:
            raise
        except Exception as e:
            btype = block.get('type', '?')
            raise InterpreterError(
                f'Fehler beim Auswerten von "{btype}": {e}'
            )

    def _eval_value_impl(self, block: dict[str, Any], ctx) -> Any:
        btype = block.get('type')

        if btype == 'math_number':
            value = block.get('fields', {}).get('NUM', 0)
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
        if btype == 'text':
            return block.get('fields', {}).get('TEXT', '')
        if btype == 'text_join':
            # Blockly built-in string composition (mutator → ADD0..ADDn value
            # inputs). Lets a student build „Ich sehe 3 Bananen" for melde/sage
            # instead of only constant literals. Scan every ADDk input present
            # (mirrors lists_create_with's sort-by-integer-suffix scan), convert
            # each value to text, and concatenate. A present-but-empty slot OR a
            # gap in the index sequence contributes '' (an absent middle item).
            # Iterating only the present indices (not range(max+1)) keeps a
            # crafted huge ADDk index from blowing up the loop, and gives the
            # same result as gap→'' since '' adds nothing.
            inputs = block.get('inputs') or {}
            add_indices = sorted(
                int(k[3:]) for k in inputs.keys()
                if isinstance(k, str) and k.startswith('ADD') and k[3:].isdigit()
            )
            # Accumulate and check BEFORE joining, so the oversized string is
            # never materialized at all (see MAX_TEXT_CHARS).
            parts: list[str] = []
            total = 0
            for i in add_indices:
                inner = self._get_input_block(block, f'ADD{i}')
                piece = ('' if inner is None
                         else self._to_text(self._eval_value(inner, ctx)))
                total += len(piece)
                if total > MAX_TEXT_CHARS:
                    raise InterpreterError(
                        f'Text ist auf {MAX_TEXT_CHARS} Zeichen begrenzt.'
                    )
                parts.append(piece)
            return ''.join(parts)
        if btype == 'logic_boolean':
            return block.get('fields', {}).get('BOOL', 'FALSE') == 'TRUE'

        if btype == 'logic_negate':
            inner = self._get_input_block(block, 'BOOL')
            return not self._truthy(self._eval_value(inner, ctx))

        if btype == 'logic_compare':
            op = block.get('fields', {}).get('OP', 'EQ')
            a = self._eval_value(self._get_input_block(block, 'A'), ctx)
            b = self._eval_value(self._get_input_block(block, 'B'), ctx)
            # Both operands carry check:null, so Blockly lets ANY pairing
            # connect — a student can compare „finde Würfel" to 3, or a text to
            # a number, in two drags. Swallowing the TypeError into False was
            # the worst possible answer: it is wrong in BOTH directions at once
            # ([1] < 1 → False AND [1] > 1 → False; "a" < 1 → False AND
            # "a" > 1 → False), so flipping the operator never revealed the
            # problem and the sonst branch ran either way with no message.
            # Blockly's own generated Python raises here; so do we, in German.
            if op in ('LT', 'LTE', 'GT', 'GTE'):
                if a is None or b is None:
                    raise InterpreterError(
                        'Beim Vergleich fehlt ein Wert — bitte beide Felder '
                        'des Vergleichs-Blocks füllen.'
                    )
            try:
                return self._apply_compare(op, a, b)
            except (TypeError, ValueError):
                raise InterpreterError(
                    'Diese beiden Werte lassen sich nicht der Größe nach '
                    'vergleichen — bitte Zahlen mit Zahlen und Texte mit '
                    'Texten vergleichen.'
                )

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
            # Read WITHOUT the old `or 1` coercion: `0 or 1 == 1` silently turned
            # an explicit zero divisor into 1 (a different silent-wrong-value
            # bug). Only a genuinely missing (None) input falls back to 1.
            raw_a = self._eval_value(self._get_input_block(block, 'DIVIDEND'), ctx)
            raw_b = self._eval_value(self._get_input_block(block, 'DIVISOR'), ctx)
            a = float(raw_a if raw_a is not None else 0)
            b = float(raw_b if raw_b is not None else 1)
            # Fail loud instead of the old silent `0.0`: a modulo-by-zero is a
            # student mistake, and returning 0 would feed a wrong value silently
            # into the rest of the program (violates "no silent fallbacks").
            if b == 0:
                raise InterpreterError(
                    'Rest-Division durch Null ist nicht möglich — bitte den '
                    'Divisor prüfen.'
                )
            return a % b
        if btype == 'math_round':
            op = (block.get('fields') or {}).get('OP', 'ROUND')
            n = float(self._eval_value(self._get_input_block(block, 'NUM'), ctx) or 0)
            # NOTE: `math` is imported at module scope. A function-local
            # `import math` here would rebind `math` as a LOCAL for the WHOLE of
            # _eval_value_impl, so every earlier `math.` use in this function
            # would raise UnboundLocalError.
            if op == 'ROUNDUP':
                return float(math.ceil(n))
            if op == 'ROUNDDOWN':
                return float(math.floor(n))
            return float(round(n))

        if btype == 'variables_get':
            var_name = self._read_variable_name(block, 'VAR')
            if var_name is None:
                return None
            return self._read_variable(ctx, var_name)

        # Lists value evaluators.
        if btype == 'lists_create_with':
            # Audit fix: previously broke on the first missing ADDk and also
            # silently truncated past index 20. New behaviour: scan every
            # ADDk key present, sort by integer suffix, fill gaps with
            # None, and RAISE on overflow rather than silently dropping
            # items the student authored.
            inputs = block.get('inputs') or {}
            add_indices: list[int] = []
            for k in inputs.keys():
                if isinstance(k, str) and k.startswith('ADD'):
                    suffix = k[3:]
                    if suffix.isdigit():
                        add_indices.append(int(suffix))
            if add_indices:
                if len(add_indices) > 20 or max(add_indices) >= 20:
                    raise InterpreterError(
                        'Listen-Erstellen-Block ist auf 20 Elemente begrenzt.'
                    )
                add_indices.sort()
                items: list[Any] = []
                # Fill from 0 to max(add_indices) so gaps become None.
                for i in range(max(add_indices) + 1):
                    inner = self._get_input_block(block, f'ADD{i}')
                    if inner is None:
                        items.append(None)
                    else:
                        items.append(self._eval_value(inner, ctx))
                return items
            return []
        if btype == 'lists_repeat':
            v = self._eval_value(self._get_input_block(block, 'ITEM'), ctx)
            raw_n = self._number_or(
                self._eval_value(self._get_input_block(block, 'NUM'), ctx), 0.0)
            if not math.isfinite(raw_n):
                raise InterpreterError('Anzahl ist keine gültige Zahl.')
            n = max(0, int(raw_n))
            # lists_create_with has always been capped (at its mutator's 20);
            # this one had NO cap at all, so a two-block program materialized
            # 5 000 000 elements in 0.38 s inside the ROS node. Raise rather
            # than silently truncate — a student who asked for 5 000 000 has a
            # bug, and a quietly shortened list is a wrong answer.
            if n > MAX_LIST_ITEMS:
                raise InterpreterError(
                    f'Liste ist auf {MAX_LIST_ITEMS} Elemente begrenzt.'
                )
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
            return self._lists_get_index(block, ctx, statement=False)
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
            # Name FIRST: an unnamed call must not run its arguments' side
            # effects (a nested call, a list mutation) before it fails. This
            # used to `return None`, so a „Funktionsaufruf" whose name could not
            # be read fed a silent None into whatever socket it filled.
            name = self._read_call_name(block)
            if not name:
                raise InterpreterError('Funktionsaufruf ohne Namen.')
            # Reuse the manager-level caller hook on ctx so procedures
            # are visible across hat handlers.
            return ctx.call_procedure(name, self._read_call_args(block, ctx, name))

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
            # Fail loud instead of the old silent `0.0`. Returning 0 on a
            # division-by-zero silently propagates a wrong value through the
            # rest of the student's program ("no silent fallbacks").
            if b == 0:
                raise InterpreterError(
                    'Division durch Null ist nicht möglich — bitte den Teiler '
                    'prüfen.'
                )
            return a / b
        if op == 'POWER':
            result = a ** b
            # A negative base with a fractional exponent yields a Python complex
            # (e.g. (-2)**0.5) that would silently corrupt every downstream
            # numeric comparison. Reject it with a clear German message rather
            # than letting the complex leak into the program.
            if isinstance(result, complex):
                raise InterpreterError(
                    'Diese Potenz hat kein reelles Ergebnis (negative Basis mit '
                    'gebrochenem Exponenten).'
                )
            return float(result)
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
    def _read_call_name(block: dict[str, Any]) -> str:
        """Name of the procedure a ``procedures_call*`` block invokes.

        It lives in ``extraState.name``, NOT in ``fields`` — a call block has no
        ``fields`` key at all (verified against Blockly 12.5.1: the call's NAME
        is a FieldLabel with ``isSerializable: false``, so the serializer omits
        it and the mutator round-trips the name through extraState instead).
        Reading ``fields.NAME`` meant ``procedures_callnoreturn`` aborted every
        run with „Funktionsaufruf ohne Namen." and ``procedures_callreturn``
        silently evaluated to None — the whole Funktionen category was dead.

        ``fields.NAME`` is still honoured as a fallback for hand-written or
        imported JSON.
        """
        extra = block.get('extraState')
        if isinstance(extra, dict):
            name = extra.get('name')
            if isinstance(name, str) and name.strip():
                return name.strip()
        name = (block.get('fields') or {}).get('NAME')
        return name.strip() if isinstance(name, str) else ''

    def _read_call_args(self, block: dict[str, Any], ctx, name: str) -> list[Any]:
        """Evaluate ARG0..ARG(n-1) POSITIONALLY. An EMPTY socket is a HOLE.

        Blockly omits an empty input from ``inputs`` entirely, so the old
        ``while True: … if arg_input is None: break`` scan stopped at the first
        gap and bound EVERY parameter to None as soon as the student left the
        FIRST socket empty (measured: ARG0 empty, ARG1 = 42 → [None, None]; the
        42 was discarded silently, while the Code panel showed ``f(None, 42)``).
        Same bug shape ``_exec_if`` fixed via the clause scan.

        Arity comes from the CALL's own ``extraState.params``, else from the
        registered DEFINITION's params. Both are LIST LENGTHS out of the
        payload, so they are bounded by MAX_WORKFLOW_JSON_BYTES and never by an
        attacker-chosen integer. The legacy contiguous scan remains only for a
        hand-written payload that carries neither.
        """
        n: int | None = None
        extra = block.get('extraState')
        if isinstance(extra, dict):
            params = extra.get('params')
            if isinstance(params, (list, tuple)):
                n = len(params)
        if n is None and name:
            spec = self._procedures.get(name)
            if spec is not None:
                n = len(spec.get('params') or [])
        if n is None:
            n = 0
            inputs = block.get('inputs') or {}
            while f'ARG{n}' in inputs:
                n += 1
        return [self._eval_value(self._get_input_block(block, f'ARG{i}'), ctx)
                for i in range(n)]

    def _read_variable_name(self, block: dict[str, Any], field_name: str) -> str | None:
        """Resolve a variable-field reference to the student's own name.

        A saved workspace stores the reference as ``{"id": "…"}`` with no
        ``name`` (both at the default save and at ``doFullSerialization:false``,
        which is what this app uses), so the id must be looked up in the
        workspace's top-level ``variables`` table — see ``_variable_names``.
        Falling back to the raw id, as this used to do unconditionally, is what
        made procedure parameters unbindable (write by name / read by id) and
        left the React variable inspector with 20-character random keys it
        rejects.

        The id fallback REMAINS for a payload that carries no table at all: a
        consistent key is still better than dropping the write.
        """
        fields = block.get('fields') or {}
        value = fields.get(field_name)
        if isinstance(value, dict):
            name = value.get('name')
            if isinstance(name, str) and name:
                return name
            vid = value.get('id')
            if isinstance(vid, str) and vid:
                return self._variable_names.get(vid, vid)
            return None
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

    @staticmethod
    def _to_text(value: Any) -> str:
        """Stringify a block-runtime value for text_join concatenation.

        None → '' (a missing item adds nothing). bool → German 'wahr'/'falsch'
        (a student-facing surface — Rule §1). An integer-valued float drops the
        trailing '.0' so „Anzahl Banane" (which evaluates to e.g. 3.0) reads as
        „3", not „3.0". Everything else falls back to str(). bool is checked
        before the float branch because ``bool`` is an ``int`` subclass but is
        NOT a ``float`` — order only matters to keep True from ever reaching
        the numeric formatter.
        """
        if value is None:
            return ''
        if isinstance(value, bool):
            return 'wahr' if value else 'falsch'
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return str(value)
        return str(value)


def _list_index_error_de(at: Any, length: int) -> str:
    """ONE out-of-range sentence, for „hole Element" AND „setze Element".

    It replaces „Listen-Index außerhalb der Grenzen (Länge 2)." — „Index",
    „Grenzen" and a bare „(Länge 2)" are programmer German on a surface whose
    blocks say „Element" and „Liste", and a student reads it and learns nothing
    about what to change. The last sentence is the part that actually teaches:
    Blockly lists are 1-based, and the off-by-one is the mistake that brought
    them here. Shared so the two blocks cannot drift apart again — the drift IS
    the finding."""
    try:
        which = int(at)
    except (TypeError, ValueError):
        which = at
    return (f'Element {which} gibt es nicht — die Liste hat nur {length} '
            'Elemente. Das erste Element ist Nummer 1.')


def _jsonable(value: Any, budget: int = _MAX_VAR_PAYLOAD_ITEMS) -> Any:
    """Best-effort conversion of an arbitrary block-runtime value to a
    JSON-serializable shape for the [VAR:..] sentinel.

    ``budget`` caps how many container elements are walked. The caller truncates
    the finished STRING anyway, so anything past the budget could never have
    been displayed — walking it only cost time and memory proportional to the
    student's list, not to the 2000-character sentinel.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        out = [_jsonable(v, budget) for v in value[:budget]]
        if len(value) > budget:
            out.append(f'… ({len(value)} Elemente)')
        return out
    if isinstance(value, dict):
        items = list(value.items())[:budget]
        return {str(k): _jsonable(v, budget) for k, v in items}
    # An unknown object reaching a STUDENT surface must be rendered the way the
    # student surface renders it. `output._student_text` already knows what a
    # Greifziel is — „Greifziel (Marker 22) bei x=0,158 m, y=-0,050 m,
    # z=0,015 m" — while `repr()` printed the raw dataclass:
    # `Detection(centroid_px=(267, 305), bbox_px=…, corners_px=array([[…]]),
    # extras={…})`, capped only by _MAX_VAR_PAYLOAD_CHARS. That is the FIRST
    # content the Variablen-Tafel shows for the split-grasp idiom the block
    # tooltip prescribes, every loop pass. Two stringifiers for student-visible
    # values, and only one of them knew what a Greifziel was.
    #
    # LAZILY imported: output.py imports Interpreter for its own fallback, so a
    # module-level import here closes the cycle (the house style already uses a
    # lazy import for exactly this shape — see trajectory.py → path_guard).
    # `_student_text`'s own fallback is `Interpreter._to_text`, so every
    # NON-Greifziel object renders exactly as it did.
    try:
        from physical_ai_server.workflow.handlers.output import _student_text
        return _student_text(value)
    except Exception:  # noqa: BLE001 — observability never breaks a run
        return repr(value)
