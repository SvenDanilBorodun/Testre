#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Counter block handlers.

A workflow-scoped named integer-counter store (``ctx.counters``: name → int):
``reset`` zeros a counter, ``add`` bumps it by one, ``get`` reads it (VALUE), and
the ``edubotics_when_counter_gt`` hat fires when a counter exceeds a threshold.

Counters are fresh per run (seeded in ``WorkflowManager.start()``) and shared
between the main interpreter stack and the hat-handler threads, so every
read/write is serialized under ``ctx.var_lock`` — the same re-entrant lock that
guards ``ctx.variables`` (counters are the same kind of shared mutable state,
and reusing the lock keeps the WorkflowContext surface minimal).

Every counter WRITE also emits a ``[CNT:name=int]`` sentinel on ``ctx.log`` so
the React Debug-Panel can mirror it — see ``_emit`` below for the token, and
``get``/``get_count`` for why the READ paths stay silent.
"""

from __future__ import annotations

from typing import Any

from physical_ai_server.workflow.handlers.motion import WorkflowError

# Magnitude cap so a runaway „erhöhe Zähler" inside a forever loop can't grow an
# unbounded integer.
_COUNTER_MAX = 1_000_000_000


def _name(args: dict[str, Any]) -> str:
    return str(args.get('name') or '').strip()


def _store(ctx) -> dict:
    """The per-run counter dict on ctx, lazily created so a unit-test ctx without
    the field still works (mirrors the getattr-tolerant handler style)."""
    store = getattr(ctx, 'counters', None)
    if store is None:
        store = {}
        try:
            ctx.counters = store
        except Exception:
            pass
    return store


def _emit(ctx, name: str, value: int) -> None:
    """Push the ``[CNT:name=int]`` inspector sentinel for ONE counter write.

    A SEPARATE token from the interpreter's ``[VAR:name=json]``, and a separate
    store on the React side, because „Punkte" is the pre-filled name in all four
    Zähler blocks AND a name a student can just as easily give a variable — one
    shared map would have each silently overwrite the other. The Debug-Panel
    keeps them in two labelled sections for the same reason the toolbox keeps
    „Zähler" as its own category.

    The value is emitted as BARE DIGITS, not JSON: a counter is an integer by
    construction (``reset`` writes 0, ``add`` writes ``int + 1``, both clamped to
    ``_COUNTER_MAX``), so the consumer's digits-only capture IS the type check
    and there is nothing for JSON to quote. The name is emitted VERBATIM — the
    student reads back the name they typed.

    CALLED OUTSIDE ``ctx.var_lock``, deliberately, for the same reason
    ``Interpreter._set_variable`` emits outside it: ``ctx.log`` is
    ``WorkflowManager``'s status publisher, so holding the counter lock across it
    would (a) park every ``when_counter_gt`` hat thread's ``get_count`` poll
    behind a ROS publish and (b) create a ``var_lock`` → publisher lock ordering
    edge that no other path has. The caller CAPTURES the new value under the lock
    and passes it in, so the number shown is the one this write produced rather
    than whatever a racing thread left behind by the time we get here.

    Best-effort: observability must never abort a student's program.
    """
    try:
        ctx.log(f'[CNT:{name}={int(value)}]')
    except Exception:
        pass


def get_count(ctx, name) -> int:
    """Current value of the named counter (0 if never set). Read under
    ``ctx.var_lock`` so the ``when_counter_gt`` hat thread polling the counter
    can't tear the dict against a main-stack write. Used by the value block AND
    by ``WorkflowManager._wait_for_hat_trigger``.

    NEVER emits. This is the hat thread's poll (5 Hz per armed
    ``edubotics_when_counter_gt``, on its own daemon thread): a sentinel here
    would flood the status channel from a second thread with a value that a
    preceding ``add`` has already reported."""
    key = str(name or '').strip()
    if not key:
        return 0
    store = _store(ctx)
    lock = getattr(ctx, 'var_lock', None)
    if lock is not None:
        with lock:
            return int(store.get(key, 0))
    return int(store.get(key, 0))


def reset(ctx, args: dict[str, Any]) -> None:
    """„setze Zähler <Name> auf 0" — zero the named counter.

    A WRITE, so it emits: pressing „Start" on the points lesson has to move the
    „Punkte" row to 0 in the panel, otherwise the student reads the PREVIOUS
    run's number (React's own retirement clears the map on Start, but only the
    server can say the counter is back at 0 mid-run)."""
    key = _name(args)
    if not key:
        raise WorkflowError('Zähler ohne Namen — bitte einen Namen eingeben.')
    store = _store(ctx)
    lock = getattr(ctx, 'var_lock', None)
    if lock is not None:
        with lock:
            store[key] = 0
            new_value = store[key]
    else:
        store[key] = 0
        new_value = store[key]
    _emit(ctx, key, new_value)


def add(ctx, args: dict[str, Any]) -> None:
    """„erhöhe Zähler <Name> um 1" — increment the named counter by one (capped
    at ``_COUNTER_MAX``).

    A WRITE, so it emits — this is the one that makes the counting VISIBLE, and
    the reason the panel was empty on a program that was plainly counting."""
    key = _name(args)
    if not key:
        raise WorkflowError('Zähler ohne Namen — bitte einen Namen eingeben.')
    store = _store(ctx)
    lock = getattr(ctx, 'var_lock', None)
    if lock is not None:
        with lock:
            store[key] = min(_COUNTER_MAX, int(store.get(key, 0)) + 1)
            new_value = store[key]
    else:
        store[key] = min(_COUNTER_MAX, int(store.get(key, 0)) + 1)
        new_value = store[key]
    _emit(ctx, key, new_value)


def get(ctx, args: dict[str, Any]) -> int:
    """„Zähler <Name>" — VALUE: the current value of the named counter (0 if
    never set).

    A READ, so it emits NOTHING. It is a VALUE block: „wenn Zähler Punkte
    größer als 3" re-evaluates it on every pass of the surrounding loop, and a
    ``solange``/``wiederhole bis`` condition re-evaluates it many times a
    second. An emit here would repeat a number that has not changed, at the loop
    rate, and would peg the panel's „Aktualisiert" column at „jetzt" for a
    counter that is standing still — worse than silence, because it reports
    motion that is not happening. The value it would report was already emitted
    by the ``add``/``reset`` that produced it."""
    return get_count(ctx, _name(args))
