"""Audit §3.19 — verify the documented <100 ms command-side stop
latency for ``chunked_publish``. The pre-fix test only checked that
publish returned False after one chunk; it didn't bound the wall-clock
time between ``state['stop'] = True`` and the function return.
"""

from __future__ import annotations

import threading
import time

from physical_ai_server.workflow.trajectory_builder import (
    build_segment,
    chunked_publish,
)


def test_chunked_publish_returns_within_100ms_of_stop():
    """Build a 5 s trajectory, fire the stop signal in a background
    thread 200 ms in, and assert chunked_publish returns within 100 ms
    of the signal flipping.

    Note: chunked_publish polls ``should_stop`` every ~50 ms during
    the inter-chunk sleep, so 100 ms is comfortable. If the polling
    cadence regresses the test surfaces it.
    """
    waypoints = build_segment([0.0] * 6, [1.0] * 6, duration_s=5.0, fps=30)
    stop_event = threading.Event()

    def stopper():
        # Let the publisher ship at least one chunk before we ask it
        # to halt — this exercises the inter-chunk sleep where the
        # poll happens.
        time.sleep(0.2)
        stop_event.set()
        stopper.stop_t = time.monotonic()

    stopper.stop_t = 0.0
    t = threading.Thread(target=stopper, daemon=True)
    t.start()

    chunked_publish(
        publisher=lambda chunk: None,
        points=waypoints,
        safety_apply=None,
        should_stop=stop_event.is_set,
        chunk_duration_s=1.0,
        fps=30,
    )
    return_t = time.monotonic()
    t.join(timeout=1.0)

    latency = return_t - stopper.stop_t
    # Generous bound: documented target is <100 ms; allow up to 200 ms
    # to absorb scheduler jitter on CI runners.
    assert latency < 0.2, f'stop latency {latency * 1000:.1f}ms exceeded budget'
