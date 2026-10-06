"""Daten 2.0 hints (spec §F6, P12): each rule on recorded-shaped synthetic data,
each threshold just above and just below, ``still`` suppressing idle/no_grasp,
a 0.4 s glitch and a constant 16° offset NOT being lag, ``short``/``long`` only
with ≥ 4 episodes, and the columns following the joint NAMES (a 6-joint OMX and
a 7-joint arm with the lag on its sixth arm joint)."""

from __future__ import annotations

import numpy as np
import pytest

from physical_ai_server.data_processing import dataset_hints as H

FPS = 30
OMX = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
SEVEN = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6', 'gripper_joint_1']


def episode(seconds=20.0, names=OMX, phase=0.0):
    """A clean take: every arm joint keeps moving (≥ 40° range), the gripper
    opens and closes, the follower trails the leader by 2 frames."""
    n = int(seconds * FPS)
    t = np.arange(n) / FPS
    j = len(names)
    A = np.stack([0.4 * np.sin(0.8 * (k + 1) * t + phase + k) for k in range(j)], axis=1)
    A[:, -1] = 0.3 + 0.3 * np.sin(0.9 * t + phase)
    S = np.concatenate([np.repeat(A[:1], 2, 0), A[:-2]], axis=0)
    return S.copy(), A.copy()


def types(hints):
    return [h['type'] for h in hints]


def test_a_clean_episode_has_no_hint():
    S, A = episode()
    assert H.episode_hints(S, A, FPS, OMX) == []


def test_columns_come_from_the_names():
    assert H.split_columns(OMX) == ([0, 1, 2, 3, 4], 5)
    assert H.split_columns(SEVEN) == ([0, 1, 2, 3, 4, 5], 6)
    assert H.split_columns(['a', 'b', 'c']) == ([0, 1, 2], None)
    assert H.split_columns([]) == ([], None)


# ── still ────────────────────────────────────────────────────────────────────

def test_still_suppresses_idle_and_no_grasp():
    S, A = episode()
    A[:] = A[0] + np.random.default_rng(0).normal(0, 0.0015, A.shape)
    S = A.copy()
    hints = H.episode_hints(S, A, FPS, OMX)
    assert types(hints) == ['still']
    assert hints[0]['value_deg'] < H.STILL_DEG


@pytest.mark.parametrize('delta,flagged', [(-0.2, True), (+0.2, False)])
def test_still_threshold(delta, flagged):
    S, A = episode()
    A[:] = A[0]
    target = np.radians(H.STILL_DEG + delta)
    A[:, 2] = A[0, 2] + np.linspace(0, target, len(A))      # the largest arm range is STILL_DEG ± 0.2
    S = np.concatenate([np.repeat(A[:1], 2, 0), A[:-2]], axis=0)
    assert ('still' in types(H.episode_hints(S, A, FPS, OMX))) is flagged


# ── idle at the start / the end ──────────────────────────────────────────────

def _hold_start(S, A, frames):
    A[:frames] = A[frames]
    S[:frames] = S[frames]
    return S, A


def test_a_long_pause_at_the_start_and_its_threshold():
    S, A = _hold_start(*episode(), 4 * FPS)
    hints = H.episode_hints(S, A, FPS, OMX)
    assert types(hints) == ['idle_start']
    to_s = hints[0]['to_s']
    assert hints[0]['from_s'] == 0.0 and 3.0 <= to_s <= 4.0
    for delta, flagged in ((-0.01, True), (+0.01, False)):
        old = H.IDLE_START_S
        try:
            H.IDLE_START_S = to_s + delta
            assert ('idle_start' in types(H.episode_hints(S, A, FPS, OMX))) is flagged
        finally:
            H.IDLE_START_S = old


def test_a_long_pause_at_the_end_and_its_threshold():
    S, A = episode()
    n = len(A)
    A[n - 6 * FPS:] = A[n - 6 * FPS - 1]
    S[n - 6 * FPS:] = S[n - 6 * FPS - 1]
    hints = H.episode_hints(S, A, FPS, OMX)
    assert types(hints) == ['idle_end']
    span = hints[0]['to_s'] - hints[0]['from_s']
    assert hints[0]['to_s'] == round(n / FPS, 3) and 5.0 <= span <= 6.5
    for delta, flagged in ((-0.01, True), (+0.01, False)):
        old = H.IDLE_END_S
        try:
            H.IDLE_END_S = span + delta
            assert ('idle_end' in types(H.episode_hints(S, A, FPS, OMX))) is flagged
        finally:
            H.IDLE_END_S = old


def test_a_short_pause_is_not_idle():
    S, A = _hold_start(*episode(), int(2.0 * FPS))
    assert H.episode_hints(S, A, FPS, OMX) == []


# ── the gripper ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('deg,flagged', [(H.GRIP_DEG - 0.2, True), (H.GRIP_DEG + 0.2, False)])
def test_no_grasp_threshold(deg, flagged):
    S, A = episode()
    A[:, -1] = np.radians(10) + np.linspace(0, np.radians(deg), len(A))
    S[:, -1] = A[:, -1]
    assert ('no_grasp' in types(H.episode_hints(S, A, FPS, OMX))) is flagged


def test_no_gripper_named_no_no_grasp():
    S, A = episode(names=['a', 'b', 'c'])
    A[:, -1] = A[0, -1]           # constant last column — but it is an ARM joint here
    S[:, -1] = A[:, -1]
    assert 'no_grasp' not in types(H.episode_hints(S, A, FPS, ['a', 'b', 'c']))


# ── lag ──────────────────────────────────────────────────────────────────────

def test_a_stuck_follower_is_lag_on_that_joint_at_that_time():
    S, A = episode()
    S[200:260, 1] += np.radians(28)                     # 2 s stuck 28° away
    hints = H.episode_hints(S, A, FPS, OMX)
    assert types(hints) == ['lag']
    lag = hints[0]
    assert lag['joint'] == 1
    assert 6.5 <= lag['at_s'] <= 8.7
    assert 20 <= lag['value_deg'] <= 30


@pytest.mark.parametrize('deg,flagged', [(H.LAG_DEG - 1.0, False), (H.LAG_DEG + 1.0, True)])
def test_lag_threshold(deg, flagged):
    S, A = episode()
    S[200:260, 1] += np.radians(deg)
    assert ('lag' in types(H.episode_hints(S, A, FPS, OMX))) is flagged


def test_a_short_glitch_is_not_lag():
    S, A = episode()
    S[200:212, 1] += np.radians(28)                     # 0.4 s
    assert 'lag' not in types(H.episode_hints(S, A, FPS, OMX))


def test_a_constant_offset_is_not_lag():
    S, A = episode()
    S[:, 2] += np.radians(16)                          # a leader/follower calibration offset
    assert 'lag' not in types(H.episode_hints(S, A, FPS, OMX))


def test_a_pure_delay_is_not_lag():
    S, A = episode()
    S = np.concatenate([np.repeat(A[:1], 12, 0), A[:-12]], axis=0)   # 0.4 s behind, well inside 0.6 s
    assert 'lag' not in types(H.episode_hints(S, A, FPS, OMX))


# ── short / long ─────────────────────────────────────────────────────────────

def _dataset(lengths, names=OMX):
    return [episode(seconds=n / FPS, names=names, phase=i) for i, n in enumerate(lengths)]


def test_short_and_long_need_four_episodes():
    three = H.dataset_hints(_dataset([600, 600, 300]), FPS, OMX)
    assert all('short' not in types(h) for h in three)
    four = H.dataset_hints(_dataset([600, 600, 600, 300, 1000]), FPS, OMX)
    assert types(four[3]) == ['short'] and four[3][0]['ratio'] == 0.5
    assert four[3][0]['median_s'] == 20.0
    assert types(four[4]) == ['long'] and four[4][0]['ratio'] == round(1000 / 600, 2)
    assert all(h == [] for h in four[:3])


@pytest.mark.parametrize('ratio,kind', [(H.SHORT_RATIO - 0.02, 'short'), (H.SHORT_RATIO + 0.02, None),
                                        (H.LONG_RATIO + 0.02, 'long'), (H.LONG_RATIO - 0.02, None)])
def test_short_long_thresholds(ratio, kind):
    S, A = episode(seconds=round(600 * ratio) / FPS)
    hints = H.episode_hints(S, A, FPS, OMX, median_len=600, n_episodes=4)
    got = [t for t in types(hints) if t in ('short', 'long')]
    assert got == ([kind] if kind else [])


# ── the columns follow the names ─────────────────────────────────────────────

def test_a_seven_joint_arm_lag_on_its_sixth_arm_joint_and_its_own_gripper():
    S, A = episode(names=SEVEN)
    S[200:260, 5] += np.radians(28)                     # outside an OMX A[:, :5] slice
    hints = H.episode_hints(S, A, FPS, SEVEN)
    assert types(hints) == ['lag'] and hints[0]['joint'] == 5
    S, A = episode(names=SEVEN)
    A[:, 6] = A[0, 6]                                   # the gripper is column 6, not 5
    S[:, 6] = A[:, 6]
    assert types(H.episode_hints(S, A, FPS, SEVEN)) == ['no_grasp']


def test_degenerate_inputs_never_raise():
    assert H.episode_hints([], [], FPS, OMX) == []
    assert H.episode_hints([[0] * 6], [[0] * 6], FPS, OMX) == []
    S, A = episode(seconds=0.2)
    H.episode_hints(S, A, FPS, OMX)
    assert H.dataset_hints([], FPS, OMX) == []
