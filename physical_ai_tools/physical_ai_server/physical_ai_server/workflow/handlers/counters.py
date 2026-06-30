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


def get_count(ctx, name) -> int:
    """Current value of the named counter (0 if never set). Read under
    ``ctx.var_lock`` so the ``when_counter_gt`` hat thread polling the counter
    can't tear the dict against a main-stack write. Used by the value block AND
    by ``WorkflowManager._wait_for_hat_trigger``."""
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
    """„setze Zähler <Name> auf 0" — zero the named counter."""
    key = _name(args)
    if not key:
        raise WorkflowError('Zähler ohne Namen — bitte einen Namen eingeben.')
    store = _store(ctx)
    lock = getattr(ctx, 'var_lock', None)
    if lock is not None:
        with lock:
            store[key] = 0
    else:
        store[key] = 0


def add(ctx, args: dict[str, Any]) -> None:
    """„erhöhe Zähler <Name> um 1" — increment the named counter by one (capped
    at ``_COUNTER_MAX``)."""
    key = _name(args)
    if not key:
        raise WorkflowError('Zähler ohne Namen — bitte einen Namen eingeben.')
    store = _store(ctx)
    lock = getattr(ctx, 'var_lock', None)
    if lock is not None:
        with lock:
            store[key] = min(_COUNTER_MAX, int(store.get(key, 0)) + 1)
    else:
        store[key] = min(_COUNTER_MAX, int(store.get(key, 0)) + 1)


def get(ctx, args: dict[str, Any]) -> int:
    """„Zähler <Name>" — VALUE: the current value of the named counter (0 if
    never set)."""
    return get_count(ctx, _name(args))
