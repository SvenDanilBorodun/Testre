"""Audit §1.2 — WorkflowManager fires the on_finished callback in the
_run finally block so the server can release on_workflow without a
polling timer. Without this, a workflow that completes naturally
locks the arm out of every other mode until the student manually
presses Stop on a workflow that's already done.
"""

from __future__ import annotations

import json
import time

import pytest


def _ws(blocks_list: list[dict]) -> str:
    return json.dumps({'blocks': {'languageVersion': 0, 'blocks': blocks_list}})


def test_on_finished_fires_with_finished_phase_on_natural_end():
    from physical_ai_server.workflow.workflow_manager import WorkflowManager

    received: list[str] = []
    mgr = WorkflowManager(
        publisher=lambda points: None,
        on_finished=lambda phase: received.append(phase),
    )
    # Trivial workflow: one log block.
    payload = _ws([{'type': 'edubotics_log', 'fields': {'MESSAGE': 'hi'}}])
    ok, _msg = mgr.start(payload, 'wf-natural')
    assert ok is True

    # Wait for the daemon thread to exit. The recovery path on
    # 'finished' is a no-op (only stopped / error trigger auto-home),
    # so this should resolve quickly.
    deadline = time.monotonic() + 5.0
    while mgr.is_running and time.monotonic() < deadline:
        time.sleep(0.05)

    assert mgr.is_running is False
    assert received == ['finished']


def test_on_finished_fires_with_stopped_phase_on_stop():
    from physical_ai_server.workflow.workflow_manager import WorkflowManager

    received: list[str] = []
    mgr = WorkflowManager(
        publisher=lambda points: None,
        on_finished=lambda phase: received.append(phase),
    )
    # Long loop body via wait_seconds so MAX_LOOP_ITERATIONS doesn't
    # fire before our test stop signal.
    payload = _ws([{
        'type': 'controls_whileUntil',
        'fields': {'MODE': 'WHILE'},
        'inputs': {
            'BOOL': {
                'block': {'type': 'logic_boolean', 'fields': {'BOOL': 'TRUE'}},
            },
            'DO': {
                'block': {
                    'type': 'edubotics_wait_seconds',
                    'fields': {'SECONDS': 0.1},
                },
            },
        },
    }])
    ok, _msg = mgr.start(payload, 'wf-loop')
    assert ok is True
    # Let the daemon enter the loop, then stop it. The wait_seconds
    # handler polls should_stop every 50 ms so the stop is observed
    # mid-tick.
    time.sleep(0.2)
    mgr.stop()

    deadline = time.monotonic() + 10.0  # auto-home recovery is ~4.5s
    while mgr.is_running and time.monotonic() < deadline:
        time.sleep(0.05)

    assert mgr.is_running is False
    assert received and received[-1] == 'stopped'


def test_size_limit_rejects_huge_payload():
    """Audit §2.5 — workflow_json over 256 KiB is rejected before
    Interpreter.from_json is called."""
    from physical_ai_server.workflow.workflow_manager import (
        MAX_WORKFLOW_JSON_BYTES,
        WorkflowManager,
    )
    mgr = WorkflowManager(publisher=lambda p: None)
    payload = '{"blocks":' + ('a' * (MAX_WORKFLOW_JSON_BYTES + 100)) + '}'
    ok, msg = mgr.start(payload, 'wf-big')
    assert ok is False
    assert 'zu groß' in msg.lower() or 'gross' in msg.lower()
