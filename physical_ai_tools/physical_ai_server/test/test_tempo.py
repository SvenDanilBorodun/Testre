"""Phase-2 Tempo tests for the Roboter Studio workflow runtime.

Tempo is a *teaching* speed multiplier on every motion DURATION (Rule §2:
NOT an inference safety envelope — it runs only on the workflow runtime and
never reshapes a recorded/replayed action). These tests pin:

* the global multiplier reaches the SINGLE choke point ``_publish_motion``
  (slow lengthens, fast shortens),
* a fast tempo can never undercut build_segment's velocity floor (so the arm
  is never driven dangerously quickly),
* a slow tempo is capped strictly below the ros2_control goal_time tolerance
  (5.0 s) so a stretched move never out-runs the controller's timing slack,
* ``wait_seconds`` (and any non-motion sleep) is NOT scaled,
* the per-move „mit Tempo" override flows to ``safe_move`` / ``_execute_pickup``,
* ``_resolve_tempo`` / ``_move_tempo`` / ``WorkflowManager._parse_tempo``
  clamp + default defensively.

``chunked_publish``'s 1 s inter-chunk pacing is replaced with a fake clock so
the suite stays fast (the published WAYPOINTS are byte-identical to production).
"""

from __future__ import annotations

import threading
import types

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers.motion import (
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    move_to,
    pickup,
    wait_seconds,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.workflow_manager import WorkflowManager


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    """Fake monotonic clock so chunked_publish's 1 s inter-chunk sleep doesn't
    slow the suite (same pattern as test_motion_handlers)."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    yield


class _PubCtx:
    """Minimal ctx carrying exactly what ``_publish_motion`` reads, plus an
    optional ``tempo`` (omitted entirely when None so ``getattr(ctx, 'tempo',
    1.0)`` exercises the default path)."""

    def __init__(self, tempo=None):
        self.published: list[list[tuple[list[float], float]]] = []
        self.motion_lock = threading.RLock()
        self.should_stop = lambda: False
        if tempo is not None:
            self.tempo = tempo

    def publisher(self, chunk):
        self.published.append(list(chunk))

    @property
    def end_time(self) -> float:
        """Total trajectory duration of the published motion.

        ``chunked_publish`` re-bases each chunk's waypoint times to that chunk's
        start, so a chunk's LAST relative time is its own span; the chunks
        partition the trajectory contiguously, so the sum of those spans is the
        full duration build_segment produced (== the tempo-scaled, floor-extended
        requested duration)."""
        assert self.published, 'no motion was published'
        return sum(chunk[-1][1] for chunk in self.published if chunk)


def _ik_ctx():
    ctx = _PubCtx()
    ctx.ik = IKSolver()
    ctx.z_table = 0.0
    ctx.table_plane = None
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
    ctx.last_arm_joints = None
    ctx.destinations = {}
    ctx.log = lambda _m: None
    return ctx


# Tiny single-joint delta — below the velocity floor, so the published duration
# equals the (tempo-scaled) requested duration exactly.
_SMALL_START = [0.0] * 6
_SMALL_END = [0.01, 0.0, 0.0, 0.0, 0.0, 0.0]
# Huge joint1 swing — the velocity floor dominates any reasonable requested time.
_BIG_END = [6.0, 0.0, 0.0, 0.0, 0.0, 0.0]


# ── global tempo scales _publish_motion ──────────────────────────────────────

def test_global_tempo_normal_is_unchanged():
    ctx = _PubCtx()  # no .tempo attribute → default 1.0
    motion._publish_motion(ctx, _SMALL_START, _SMALL_END, 2.0)
    assert ctx.end_time == pytest.approx(2.0, abs=1e-6)


def test_global_tempo_slow_lengthens():
    ctx = _PubCtx(tempo=0.5)
    motion._publish_motion(ctx, _SMALL_START, _SMALL_END, 2.0)
    # 2.0 / 0.5 = 4.0 (under the 4.5 stretch cap), floor negligible.
    assert ctx.end_time == pytest.approx(4.0, abs=1e-6)


def test_global_tempo_fast_shortens():
    ctx = _PubCtx(tempo=2.0)
    motion._publish_motion(ctx, _SMALL_START, _SMALL_END, 2.0)
    # 2.0 / 2.0 = 1.0; tiny delta → floor doesn't kick in.
    assert ctx.end_time == pytest.approx(1.0, abs=1e-6)


def test_fast_tempo_cannot_undercut_velocity_floor():
    # A huge swing at fast tempo: the requested 2.5/2.0 = 1.25 s is well below
    # the per-joint velocity-safe floor, so build_segment extends it BACK UP —
    # the fast tempo can never drive the arm dangerously quickly.
    delta = np.asarray(_BIG_END, dtype=np.float64) - np.asarray(_SMALL_START, dtype=np.float64)
    floor = trajectory_builder._velocity_safe_duration(delta, 2.5 / 2.0)
    ctx = _PubCtx(tempo=2.0)
    motion._publish_motion(ctx, _SMALL_START, _BIG_END, 2.5)
    assert ctx.end_time == pytest.approx(floor, abs=1e-3)
    assert ctx.end_time > 1.25  # the fast tempo did NOT make it that quick


def test_slow_tempo_capped_below_goal_time():
    # HOME (3.0 s) at the slow floor would be 6.0 s — capped strictly below the
    # ros2_control goal_time tolerance (5.0 s).
    ctx = _PubCtx(tempo=motion._TEMPO_MIN)
    motion._publish_motion(ctx, _SMALL_START, _SMALL_END, motion.DEFAULT_HOME_DURATION_S)
    assert ctx.end_time == pytest.approx(motion._MAX_STRETCHED_DURATION_S, abs=1e-6)
    assert ctx.end_time < 5.0


def test_per_move_override_beats_global_in_publish_motion():
    # ctx.tempo is slow, but the explicit per-move tempo wins.
    ctx = _PubCtx(tempo=0.5)
    motion._publish_motion(ctx, _SMALL_START, _SMALL_END, 2.0, motion._TEMPO_MAX)
    assert ctx.end_time == pytest.approx(1.0, abs=1e-6)  # 2.0 / 2.0


# ── wait_seconds / non-motion sleeps are NOT tempo-scaled ─────────────────────

def test_wait_seconds_does_not_route_through_tempo_scaler(monkeypatch):
    called = {'n': 0}
    orig = motion._tempo_scaled_duration

    def _spy(ctx, dur, tempo):
        called['n'] += 1
        return orig(ctx, dur, tempo)

    monkeypatch.setattr(motion, '_tempo_scaled_duration', _spy)
    # Fake the wait loop's clock so it returns immediately.
    state = {'t': 0.0}

    def _mono():
        state['t'] += 100.0
        return state['t']

    monkeypatch.setattr(motion, 'time',
                        types.SimpleNamespace(monotonic=_mono, sleep=lambda _s: None))
    ctx = _PubCtx(tempo=0.5)
    ctx.log = lambda _m: None
    wait_seconds(ctx, {'seconds': 1.0})
    # wait_seconds never publishes a motion, so the duration scaler must not run.
    assert called['n'] == 0


# ── _resolve_tempo / _move_tempo ─────────────────────────────────────────────

def test_resolve_tempo_clamps_and_defaults():
    assert motion._resolve_tempo(_PubCtx(), None) == 1.0          # no attr
    assert motion._resolve_tempo(_PubCtx(tempo=0.1), None) == motion._TEMPO_MIN
    assert motion._resolve_tempo(_PubCtx(tempo=9.0), None) == motion._TEMPO_MAX
    assert motion._resolve_tempo(_PubCtx(tempo=float('nan')), None) == 1.0
    assert motion._resolve_tempo(_PubCtx(tempo=float('inf')), None) == 1.0
    assert motion._resolve_tempo(_PubCtx(tempo=0.0), None) == 1.0
    assert motion._resolve_tempo(_PubCtx(tempo=-1.0), None) == 1.0
    assert motion._resolve_tempo(_PubCtx(tempo='x'), None) == 1.0
    # per-move override wins over ctx.tempo.
    assert motion._resolve_tempo(_PubCtx(tempo=1.0), motion._TEMPO_MIN) == motion._TEMPO_MIN


def test_move_tempo_mapping():
    assert motion._move_tempo({}) is None
    assert motion._move_tempo({'geschwindigkeit': 'global'}) is None
    assert motion._move_tempo({'geschwindigkeit': 'unbekannt'}) is None
    assert motion._move_tempo({'geschwindigkeit': 'langsam'}) == motion._TEMPO_MIN
    assert motion._move_tempo({'geschwindigkeit': 'normal'}) == 1.0
    assert motion._move_tempo({'geschwindigkeit': 'schnell'}) == motion._TEMPO_MAX
    assert motion._move_tempo({'geschwindigkeit': 'SCHNELL'}) == motion._TEMPO_MAX


# ── per-move field flows into the transit handlers ───────────────────────────

def test_move_to_passes_per_move_tempo_to_safe_move(monkeypatch):
    captured = {}

    def _fake_safe_move(ctx, qs, qe, dur, roll=None, tempo=None):
        captured['tempo'] = tempo

    monkeypatch.setattr(motion, 'safe_move', _fake_safe_move)
    ctx = _ik_ctx()
    move_to(ctx, {'destination': {'x': 0.20, 'y': 0.0, 'z': 0.0},
                  'geschwindigkeit': 'langsam'})
    assert captured['tempo'] == motion._TEMPO_MIN
    # No field → None (use global).
    captured.clear()
    move_to(ctx, {'destination': {'x': 0.20, 'y': 0.0, 'z': 0.0}})
    assert captured['tempo'] is None


def test_pickup_passes_per_move_tempo_to_execute_pickup(monkeypatch):
    captured = {}
    monkeypatch.setattr(motion, '_execute_pickup',
                        lambda *a, **k: captured.update(k))
    ctx = _ik_ctx()
    pickup(ctx, {'target': (0.20, 0.0, 0.0), 'geschwindigkeit': 'schnell'})
    assert captured.get('tempo') == motion._TEMPO_MAX


# ── WorkflowManager._parse_tempo ─────────────────────────────────────────────

def test_parse_tempo_defaults_and_clamps():
    assert WorkflowManager._parse_tempo('{"blocks": {}}') == 1.0      # absent
    assert WorkflowManager._parse_tempo('{"tempo": 0.5}') == 0.5
    assert WorkflowManager._parse_tempo('{"tempo": 2.0}') == 2.0
    assert WorkflowManager._parse_tempo('{"tempo": 9}') == motion._TEMPO_MAX
    assert WorkflowManager._parse_tempo('{"tempo": 0.01}') == motion._TEMPO_MIN
    assert WorkflowManager._parse_tempo('{"tempo": true}') == 1.0     # bool rejected
    assert WorkflowManager._parse_tempo('{"tempo": "x"}') == 1.0
    assert WorkflowManager._parse_tempo('{"tempo": NaN}') == 1.0      # json allows NaN
    assert WorkflowManager._parse_tempo('{"tempo": 0}') == 1.0        # non-positive
    assert WorkflowManager._parse_tempo('not json') == 1.0
    assert WorkflowManager._parse_tempo('[1, 2]') == 1.0             # non-dict
