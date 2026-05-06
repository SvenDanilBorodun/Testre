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
splits the full trajectory into <= 1 s pieces, calls
``should_stop()`` between pieces, and runs each commanded point through
the caller-supplied ``SafetyEnvelope.apply``. The physical arm may
overshoot by up to one chunk while finishing the in-flight trajectory
— that's the documented v1 stop behaviour.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable

import numpy as np


class TrajectoryRejectedError(RuntimeError):
    """Raised by ``chunked_publish`` when the safety envelope rejects a
    waypoint. Caught by the workflow handler and surfaced as a German
    ``WorkflowError`` so the operator sees an actionable message rather
    than a silently truncated motion."""

DEFAULT_FPS = 30
DEFAULT_CHUNK_DURATION_S = 1.0


def quintic_blend(s: float) -> float:
    """Smooth scalar blend ``s in [0, 1]`` -> blended position with zero
    velocity and zero acceleration at the endpoints."""
    s = max(0.0, min(1.0, s))
    return 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5


def build_segment(
    q_start: list[float],
    q_end: list[float],
    duration_s: float,
    fps: int = DEFAULT_FPS,
) -> list[tuple[list[float], float]]:
    """Return a list of (q, t_from_start_s) waypoints sampled at ``fps``
    Hz over a quintic-blended path from q_start to q_end. The first
    sample is at ``t = dt`` (not 0) so chained segments don't emit a
    zero-time duplicate."""
    if len(q_start) != len(q_end):
        raise ValueError('q_start and q_end must have the same length')
    if duration_s <= 0:
        return [(list(q_end), 1.0 / fps)]

    q_start_arr = np.asarray(q_start, dtype=np.float64)
    q_end_arr = np.asarray(q_end, dtype=np.float64)
    delta = q_end_arr - q_start_arr

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
    safety_apply: Callable[[np.ndarray], np.ndarray | None] | None,
    should_stop: Callable[[], bool],
    chunk_duration_s: float = DEFAULT_CHUNK_DURATION_S,
    fps: int = DEFAULT_FPS,
) -> bool:
    """Publish ``points`` to ``publisher`` in chunks of ``chunk_duration_s``.

    Each point's joint vector is passed through ``safety_apply`` (when
    provided); a ``None`` return drops the point. Between chunks
    ``should_stop()`` is polled — a True return cancels the rest of the
    publish and returns ``False`` from this function.

    Returns ``True`` if the entire trajectory was published, ``False`` if
    cancellation aborted it.
    """
    chunk_size = max(1, int(round(chunk_duration_s * fps)))
    chunk: list[tuple[list[float], float]] = []
    chunk_start_t = 0.0

    for q, t in points:
        if should_stop():
            return False
        if safety_apply is not None:
            clamped = safety_apply(np.asarray(q, dtype=np.float32))
            if clamped is None:
                # Previously this `continue`d, silently dropping the point
                # AND leaving chunk_start_t pinned to the last accepted
                # waypoint — so subsequent points received wrong
                # time_from_start values and the controller logged
                # tolerance violations. The trajectory is generated
                # deterministically (quintic blend), so a None here means
                # NaN in the input or a shape mismatch — both are
                # programmer/configuration bugs that the operator must see.
                raise TrajectoryRejectedError(
                    'Safety envelope hat einen Trajektorien-Punkt verworfen '
                    '(NaN/Inf oder unerwartete Aktionsgroesse). Bewegung '
                    'abgebrochen.'
                )
            q = clamped.tolist()
        chunk.append((q, t - chunk_start_t))
        if len(chunk) >= chunk_size:
            publisher(chunk)
            chunk = []
            chunk_start_t = t
            # Sleep for the chunk duration so the controller has time to
            # consume the trajectory before the next chunk is published.
            # Real-time clock — ros2 controllers want their inputs paced.
            sleep_target = time.monotonic() + chunk_duration_s
            while time.monotonic() < sleep_target:
                if should_stop():
                    return False
                time.sleep(min(0.05, sleep_target - time.monotonic()))

    if chunk:
        if should_stop():
            return False
        publisher(chunk)
    return True
