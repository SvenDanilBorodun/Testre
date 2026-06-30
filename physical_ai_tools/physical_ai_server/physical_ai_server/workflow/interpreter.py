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

import json
import os
import random
import time
from typing import Any, Callable, Iterable

from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.handlers.motion import WorkflowError


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
WHILE_EMPTY_FRAMES = _env_int('EDUBOTICS_WHILE_EMPTY_FRAMES', 2)
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

        return cls(blocks)

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
                    ctx.log(f'„{label}": noch {n_visible} sichtbar — greife eines.')
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
                        ctx.log(
                            '[WARNUNG] „Solange sichtbar": kein Fortschritt — '
                            'Schleife beendet.'
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
                try:
                    ctx.log(
                        f'„{_pb.label_for(ctx, type_name)}": nichts mehr sichtbar '
                        '— fertig.'
                    )
                except Exception:
                    pass
                break
            self._interruptible_wait(ctx, WHILE_EMPTY_SECONDS)

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
                # Bounded reacquire so the hat's `with motion_lock` __exit__ has
                # something to release; a stuck lock raises a clear German error
                # rather than hanging (matches _poll_until's audit fix #9).
                if not motion_lock.acquire(timeout=10.0):
                    raise WorkflowError(
                        'Bewegung-Sperre konnte nicht zurückgewonnen werden.'
                    )

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
        # Cap the payload (the [VAR:] path bypasses output.log's MAX_LOG_CHARS):
        # `forever { setze x = verbinde(x, …) }` would otherwise re-emit an
        # ever-growing string ~20×/s and flood the realtime channel.
        try:
            payload = json.dumps(_jsonable(value))
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
            parts: list[str] = []
            for i in add_indices:
                inner = self._get_input_block(block, f'ADD{i}')
                if inner is None:
                    parts.append('')
                else:
                    parts.append(self._to_text(self._eval_value(inner, ctx)))
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
            n = int(self._eval_value(self._get_input_block(block, 'NUM'), ctx) or 0)
            n = max(0, n)
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
