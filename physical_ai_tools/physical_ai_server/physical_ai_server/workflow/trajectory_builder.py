#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Quintic-interpolated joint trajectories with chunked publishing.

The Roboter Studio workflow runtime publishes ``JointTrajectory``
messages onto the same ``/leader/joint_trajectory`` topic the inference
path uses (the topic is then remapped to the controller's
``/arm_controller/joint_trajectory`` per
``omx_f_follower_ai.launch.py:144``). This is intentional — we do not
introduce a parallel motion path.

To honour the <100 ms command-side stop guarantee, ``chunked_publish``
splits the full trajectory into <= 1 s pieces and calls
``should_stop()`` between pieces. The physical arm may overshoot by up
to one chunk while finishing the in-flight trajectory — that's the
documented v1 stop behaviour.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable

import numpy as np


DEFAULT_FPS = 30
DEFAULT_CHUNK_DURATION_S = 1.0

# OMX joint velocity limit (rad/s) — every revolute joint in omx_f.urdf
# carries velocity="4.8". We size segment durations so no joint's PEAK
# quintic velocity exceeds this fraction of the limit.
JOINT_VELOCITY_LIMIT_RAD_S = 4.8
_VELOCITY_SAFETY_FRACTION = 0.6
# A quintic blend (10s³−15s⁴+6s⁵) has a peak velocity factor of 15/8 = 1.875
# at s=0.5, so peak joint velocity = |delta| · 1.875 / duration.
_QUINTIC_PEAK_VELOCITY_FACTOR = 15.0 / 8.0


def quintic_blend(s: float) -> float:
    """Smooth scalar blend ``s in [0, 1]`` -> blended position with zero
    velocity and zero acceleration at the endpoints."""
    s = max(0.0, min(1.0, s))
    return 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5


def _velocity_safe_duration(
    delta: "np.ndarray",
    duration_s: float,
    velocity_limit: float = JOINT_VELOCITY_LIMIT_RAD_S,
) -> float:
    """Return a duration at least large enough that no joint's peak quintic
    velocity exceeds ``_VELOCITY_SAFETY_FRACTION`` of ``velocity_limit``.

    A large single-joint swing — most dangerously a joint1 base-yaw that
    sweeps the long way (~2π) when consecutive targets straddle ±π, e.g. a
    front-of-arm point followed by a manually-pinned behind-the-base
    destination — would otherwise hit the requested 2.5 s and graze the
    4.8 rad/s limit (peak ≈ |delta|·1.875/duration → 6.28·1.875/2.5 ≈
    4.71 rad/s). Scaling the duration with the largest joint delta keeps the
    motion smooth and within limits for ANY swing; small moves keep their
    requested (faster) duration unchanged. ``velocity_limit`` defaults to the
    OMX URDF limit (4.8 rad/s); an ArmProfile with a different limit passes its
    own through ``build_segment``."""
    if delta.size == 0:
        return duration_s
    max_delta = float(np.max(np.abs(delta)))
    if max_delta <= 0.0:
        return duration_s
    v_safe = _VELOCITY_SAFETY_FRACTION * velocity_limit
    min_duration = max_delta * _QUINTIC_PEAK_VELOCITY_FACTOR / v_safe
    return max(duration_s, min_duration)


def build_segment(
    q_start: list[float],
    q_end: list[float],
    duration_s: float,
    fps: int = DEFAULT_FPS,
    velocity_limit: float = JOINT_VELOCITY_LIMIT_RAD_S,
) -> list[tuple[list[float], float]]:
    """Return a list of (q, t_from_start_s) waypoints sampled at ``fps``
    Hz over a quintic-blended path from q_start to q_end. The first
    sample is at ``t = dt`` (not 0) so chained segments don't emit a
    zero-time duplicate.

    ``duration_s`` is the REQUESTED duration; it is automatically extended
    (never shortened) when a large joint delta would otherwise exceed the
    safe per-joint velocity fraction — see ``_velocity_safe_duration``."""
    if len(q_start) != len(q_end):
        raise ValueError('q_start and q_end must have the same length')
    # A non-positive requested duration must NOT collapse into a single
    # instantaneous waypoint — a large joint delta would then be an unsafe instant
    # jump that bypasses the per-joint velocity floor. Floor the duration to one
    # frame and fall through to _velocity_safe_duration below, which stretches it
    # back up for a big delta (a zero/tiny delta keeps the one-frame move, so the
    # degenerate result is behaviourally identical to the old single-point return).
    if duration_s <= 0:
        duration_s = 1.0 / fps

    q_start_arr = np.asarray(q_start, dtype=np.float64)
    q_end_arr = np.asarray(q_end, dtype=np.float64)
    delta = q_end_arr - q_start_arr

    duration_s = _velocity_safe_duration(delta, duration_s, velocity_limit)

    num_samples = max(1, int(round(duration_s * fps)))
    dt = duration_s / num_samples
    samples: list[tuple[list[float], float]] = []
    for i in range(1, num_samples + 1):
        s = i / num_samples
        q = q_start_arr + delta * quintic_blend(s)
        samples.append((q.tolist(), i * dt))
    return samples


def chunked_publish(
    publisher: Callable[[list[tuple[list[float], float]]], None],
    points: Iterable[tuple[list[float], float]],
    should_stop: Callable[[], bool],
    chunk_duration_s: float = DEFAULT_CHUNK_DURATION_S,
    fps: int = DEFAULT_FPS,
) -> bool:
    """Publish ``points`` to ``publisher`` in chunks of ``chunk_duration_s``.

    Between chunks ``should_stop()`` is polled — a True return cancels
    the rest of the publish and returns ``False`` from this function.

    Returns ``True`` if the entire trajectory was published, ``False`` if
    cancellation aborted it.
    """
    chunk_size = max(1, int(round(chunk_duration_s * fps)))
    chunk: list[tuple[list[float], float]] = []
    chunk_start_t = 0.0

    def _pace(seconds: float) -> bool:
        """Block ``seconds`` of real wall-clock time (polling should_stop)
        so the controller has time to consume the just-published chunk
        before anything else is published onto the same topic. Returns
        ``False`` if a stop was requested mid-pace."""
        sleep_target = time.monotonic() + seconds
        while time.monotonic() < sleep_target:
            if should_stop():
                return False
            time.sleep(min(0.05, sleep_target - time.monotonic()))
        return True

    for pt in points:
        if should_stop():
            return False
        q = pt[0]
        t = pt[1]
        # Carry an optional per-point velocity (3-tuple, replay only) through the
        # time re-basing; workflow moves / jog pass 2-tuples (position-only).
        chunk.append((q, t - chunk_start_t, pt[2]) if len(pt) >= 3
                     else (q, t - chunk_start_t))
        if len(chunk) >= chunk_size:
            publisher(chunk)
            # Pace by the chunk's ACTUAL playback span (its last point's re-based
            # time), NOT the fixed chunk_duration_s. A fixed 1.0 s under-waits a
            # 25 Hz replay chunk (~1.2 s span), so the next JointTrajectory
            # preempts the still-running one early → a forward jump every chunk.
            # This mirrors the remainder branch below. A 30 Hz workflow/jog chunk
            # already spans ~1.0 s, so this is a no-op there (zero regression).
            span = chunk[-1][1]
            chunk = []
            chunk_start_t = t
            if not _pace(span):
                return False

    if chunk:
        if should_stop():
            return False
        # The remainder (a sub-chunk_size tail) is the LAST piece of this
        # segment. Pace it by its own remaining duration before returning so
        # the NEXT _publish_motion call can't pre-empt it the instant it's
        # sent. Without this, a short segment whose whole trajectory fits in
        # one remainder (e.g. a 0.5 s / 15-point gripper close, below the
        # 1.0 s / 30-point chunk_size) returns in ~0 ms and the following
        # segment (the lift) is published immediately — the arm starts
        # lifting before the gripper has physically closed, slipping the
        # object. ``chunk[-1][1]`` is the remainder's end-time measured from
        # ``chunk_start_t`` (build_segment emits monotonically increasing
        # per-chunk times), i.e. exactly how long the controller needs to
        # play it out.
        remainder_duration_s = chunk[-1][1] if chunk else 0.0
        publisher(chunk)
        if remainder_duration_s > 0.0:
            if not _pace(remainder_duration_s):
                return False
    return True
