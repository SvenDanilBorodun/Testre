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


def test_build_segment_nonpositive_duration_floors_large_delta():
    # A non-positive duration must NOT collapse a large delta into one instant
    # jump — the per-joint velocity floor stretches it into a multi-sample quintic.
    wps = build_segment([0.0] * 6, [3.0] + [0.0] * 5, duration_s=0.0, fps=30)
    assert len(wps) > 1                             # stretched, not a single jump
    assert abs(wps[-1][0][0] - 3.0) < 1e-3          # still ends at the target
    # A negative duration is treated identically.
    wps_neg = build_segment([0.0] * 6, [3.0] + [0.0] * 5, duration_s=-1.0, fps=30)
    assert len(wps_neg) > 1


def test_build_segment_nonpositive_duration_zero_delta_is_single_frame():
    # A zero delta keeps the one-frame degenerate behaviour (no needless samples).
    wps = build_segment([0.0] * 6, [0.0] * 6, duration_s=0.0, fps=30)
    assert len(wps) == 1


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


def _fake_clock(monkeypatch):
    """Patch trajectory_builder's time so _pace advances a virtual clock instead
    of sleeping. Returns the clock dict (clock['t'] is the current fake time)."""
    import physical_ai_server.workflow.trajectory_builder as tb
    clock = {'t': 0.0}
    monkeypatch.setattr(tb.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(tb.time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s))
    return clock


def test_chunked_publish_paces_full_chunk_by_actual_span(monkeypatch):
    """The main-loop pace must match each chunk's REAL playback span, NOT a fixed
    chunk_duration_s. A 25 Hz replay chunk (30 points × 0.04 s) spans ~1.2 s; if
    it were paced only 1.0 s the next JointTrajectory would preempt the running
    one early and the arm would jump. Regression guard for the replay-jump fix."""
    clock = _fake_clock(monkeypatch)
    # 60 waypoints spaced 0.04 s (25 Hz) → two 30-point chunks, each spans ~1.2 s.
    points = [([float(i)] * 6, (i + 1) * 0.04) for i in range(60)]
    pub_at: list[float] = []
    ok = chunked_publish(
        publisher=lambda chunk: pub_at.append(clock['t']),
        points=points,
        should_stop=lambda: False,
        chunk_duration_s=1.0,
        fps=30,
    )
    assert ok is True
    assert len(pub_at) == 2
    # Gap between the two publishes == the pace of chunk 1 == its real span (~1.2 s),
    # NOT the fixed 1.0 s of the old (buggy) behaviour.
    assert 1.1 < (pub_at[1] - pub_at[0]) < 1.3


def test_chunked_publish_30hz_still_paces_one_second(monkeypatch):
    """A 30 Hz workflow-move chunk already spans ~1.0 s, so span-pacing is a no-op
    there — zero regression vs the old fixed chunk_duration_s."""
    clock = _fake_clock(monkeypatch)
    waypoints = build_segment([0.0] * 6, [1.0] * 6, duration_s=2.0, fps=30)  # 60 pts
    pub_at: list[float] = []
    chunked_publish(
        publisher=lambda chunk: pub_at.append(clock['t']),
        points=waypoints,
        should_stop=lambda: False,
        chunk_duration_s=1.0,
        fps=30,
    )
    assert len(pub_at) == 2
    assert 0.9 < (pub_at[1] - pub_at[0]) < 1.1  # ~1.0 s, unchanged


def test_chunked_publish_carries_velocity_tuples(monkeypatch):
    """A 3-tuple (q, t, v) stream is re-based and forwarded WITH the velocity
    intact (the publisher receives (q, t_rebased, v))."""
    _fake_clock(monkeypatch)
    points = [([float(i)] * 6, (i + 1) * 0.04, [0.5] * 6) for i in range(3)]
    got: list = []
    chunked_publish(
        publisher=lambda chunk: got.extend(chunk),
        points=points,
        should_stop=lambda: False,
        chunk_duration_s=1.0,
        fps=30,
    )
    assert got and all(len(item) == 3 for item in got)
    assert got[0][2] == [0.5] * 6
