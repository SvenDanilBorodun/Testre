"""trajectory_builder + chunked_publish behaviour.

The 100 ms command-side stop guarantee is checked by injecting a
``should_stop`` that flips True after one chunk and asserting the
publisher is not called for the next chunk."""

from __future__ import annotations

from physical_ai_server.workflow.trajectory_builder import (
    build_segment,
    chunked_publish,
    quintic_blend,
)


def test_quintic_blend_endpoints():
    assert quintic_blend(0.0) == 0.0
    assert abs(quintic_blend(1.0) - 1.0) < 1e-6
    # Symmetric around 0.5.
    assert abs(quintic_blend(0.5) - 0.5) < 1e-6


def test_quintic_blend_clamps_outside_range():
    assert quintic_blend(-0.5) == 0.0
    assert abs(quintic_blend(1.5) - 1.0) < 1e-6


def test_build_segment_starts_at_dt_not_zero():
    waypoints = build_segment([0.0] * 6, [1.0] * 6, duration_s=1.0, fps=30)
    assert waypoints[0][1] > 0
    # First sample isn't the start state — it's already nudged forward.
    assert waypoints[0][0][0] > 0


def test_build_segment_ends_at_target():
    waypoints = build_segment([0.0] * 6, [1.0] * 6, duration_s=1.0, fps=30)
    last_q = waypoints[-1][0]
    for v in last_q:
        assert abs(v - 1.0) < 1e-3


def test_chunked_publish_calls_publisher_per_chunk():
    """30 fps × 2 s of motion at 1 s chunks = 2 publishes."""
    waypoints = build_segment([0.0] * 6, [1.0] * 6, duration_s=2.0, fps=30)
    calls: list[int] = []

    def publisher(chunk):
        calls.append(len(chunk))

    ok = chunked_publish(
        publisher=publisher,
        points=waypoints,
        should_stop=lambda: False,
        chunk_duration_s=1.0,
        fps=30,
    )
    assert ok is True
    assert len(calls) == 2
    # 30 fps × 1 s = 30 points per chunk.
    assert calls[0] == 30


def test_chunked_publish_halts_on_stop():
    waypoints = build_segment([0.0] * 6, [1.0] * 6, duration_s=3.0, fps=30)
    state = {'i': 0, 'stop': False}
    calls: list[int] = []

    def publisher(chunk):
        calls.append(len(chunk))
        state['i'] += 1
        if state['i'] == 1:
            state['stop'] = True

    ok = chunked_publish(
        publisher=publisher,
        points=waypoints,
        should_stop=lambda: state['stop'],
        chunk_duration_s=1.0,
        fps=30,
    )
    assert ok is False
    assert len(calls) == 1
