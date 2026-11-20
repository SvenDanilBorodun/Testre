#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``autocal.chain`` (spec 40 S4, WP-10): the two serial chains are the
solvers' own at zero tilts, and a tilt has teeth.

The truth is the solvers themselves (``IKSolver`` / ``V1IKSolver``): their
``fk``, ``link_points`` (n = 1 and 5) and the V1's ``link_frames``, for 200
random poses per arm inside the joint limits.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from physical_ai_server.workflow.autocal import chain
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver

_SOLVERS = {'omx': IKSolver(), 'v1': V1IKSolver()}


def _poses(kind, n=200, seed=0):
    rng = np.random.default_rng([seed, 0 if kind == 'omx' else 1])
    lims = _SOLVERS[kind].joint_limits
    return [np.array([rng.uniform(lo, hi) for lo, hi in lims]) for _ in range(n)]


@pytest.mark.parametrize('kind', chain.KINDS)
def test_ee_is_the_solvers_fk_at_zero_tilts(kind):
    solver = _SOLVERS[kind]
    worst = 0.0
    for q in _poses(kind):
        R, t = solver.fk(q)
        E = chain.ee(kind, q)
        worst = max(worst, float(np.abs(E[:3, :3] - R).max()), float(np.abs(E[:3, 3] - t).max()))
        # all-zero tilts are the untilted chain too
        E0 = chain.ee(kind, q, tilts=np.zeros(6))
        worst = max(worst, float(np.abs(E0 - E).max()))
    assert worst < 1e-9, worst


@pytest.mark.parametrize('kind', chain.KINDS)
@pytest.mark.parametrize('n', (1, 5))
def test_link_points_are_the_solvers_at_zero_tilts(kind, n):
    solver = _SOLVERS[kind]
    worst = 0.0
    for q in _poses(kind):
        ref = np.array(solver.link_points(q, samples_per_link=n))
        got = np.array(chain.link_points(kind, q, n=n))
        assert got.shape == ref.shape, (got.shape, ref.shape)
        worst = max(worst, float(np.abs(got - ref).max()))
    assert worst < 1e-9, worst


def test_link_frames_are_the_v1_solvers_at_zero_tilts():
    solver = _SOLVERS['v1']
    worst = 0.0
    for q in _poses('v1'):
        ref = solver.link_frames(q)
        got = chain.link_frames(q)
        assert len(got) == len(ref) == 6
        worst = max(worst, max(float(np.abs(a - b).max()) for a, b in zip(got, ref)))
    assert worst < 1e-9, worst


@pytest.mark.parametrize('kind', chain.KINDS)
def test_the_batched_chain_equals_the_single_one(kind):
    Q = np.array(_poses(kind, n=20, seed=3))
    tilts = np.radians([0.3, 0.0, -0.4, 0.2, 0.5, -0.3])
    E = chain.ee_batch(kind, Q, tilts)
    for i, q in enumerate(Q):
        assert np.abs(E[i] - chain.ee(kind, q, tilts)).max() < 1e-12


def test_the_omx_tool_points_follow_the_tip_argument():
    q = _poses('omx', n=1)[0]
    pts = chain.link_points('omx', q, n=5, tip=0.0026)
    E = chain.ee('omx', q)
    axis = E[:3, 0] / np.linalg.norm(E[:3, 0])
    assert np.allclose(pts[-2], E[:3, 3] + axis * 0.0013, atol=1e-12)
    assert np.allclose(pts[-1], E[:3, 3] + axis * 0.0026, atol=1e-12)


@pytest.mark.parametrize('kind', chain.KINDS)
def test_a_j3a_tilt_of_0_4_degrees_moves_the_tool_point_by_millimetres(kind):
    """The tilt has teeth (spec: 1.5 … 3.5 mm over 200 random poses; probe 2.3 / 2.8 mm)."""
    tilts = np.zeros(6)
    tilts[2] = math.radians(0.4)
    Q = np.array(_poses(kind))
    moved = np.linalg.norm(chain.ee_batch(kind, Q, tilts)[:, :3, 3] - chain.ee_batch(kind, Q)[:, :3, 3], axis=1)
    largest = float(moved.max()) * 1000.0
    assert 1.5 <= largest <= 3.5, largest


@pytest.mark.parametrize('kind', chain.KINDS)
def test_a_tilt_turns_about_the_two_axes_perpendicular_to_its_joint(kind):
    """Slot ``a`` turns about x, slot ``b`` about z (OMX, joint axis y) or y
    (V1, joint axis z), in the joint's frame, before its own rotation."""
    perp = {'omx': ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)), 'v1': ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))}[kind]
    q = np.array([0.3, 0.4, -0.2, 0.5, 0.1])
    for joint in (1, 2, 3):                        # joints 2, 3, 4
        q0 = q.copy()
        q0[joint] = 0.0
        F_n = chain.frames(kind, q0)[joint]
        for slot, axis in enumerate(perp):
            tilts = np.zeros(6)
            tilts[2 * (joint - 1) + slot] = math.radians(0.7)
            F_t = chain.frames(kind, q0, tilts)[joint]
            rel = F_n[:3, :3].T @ F_t[:3, :3]
            assert np.allclose(chain.rvec_of(rel), np.radians(0.7) * np.asarray(axis), atol=1e-12)
            assert np.allclose(F_t[:3, 3], F_n[:3, 3], atol=1e-15)


def test_j2b_is_a_joint_1_zero_offset_on_the_omx():
    """A10: the tilt of joint 2 about the vertical IS a turn of joint 1."""
    b = math.radians(0.7)
    tilts = np.zeros(6)
    tilts[1] = b
    for q in _poses('omx', n=50):
        q1 = q.copy()
        q1[0] += b
        assert np.abs(chain.ee('omx', q, tilts) - chain.ee('omx', q1)).max() < 1e-12


def test_rodrigues_and_rvec_of_are_inverse():
    rng = np.random.default_rng(7)
    for _ in range(200):
        r = rng.normal(0, 1, 3)
        r *= rng.uniform(0, 3.1) / np.linalg.norm(r)
        assert np.abs(chain.rvec_of(chain.rodrigues(r)) - r).max() < 1e-9
