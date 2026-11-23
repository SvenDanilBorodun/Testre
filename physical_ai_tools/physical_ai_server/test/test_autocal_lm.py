#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``autocal.lm`` (spec 40 S4): Levenberg–Marquardt, forward-difference
Jacobian, Gaussian priors, the 1σ of a solution."""

from __future__ import annotations

import numpy as np

from physical_ai_server.workflow.autocal import lm


def _problem(seed=0, n=60, noise=0.0):
    rng = np.random.default_rng(seed)
    x = np.linspace(-1.0, 1.0, n)
    truth = np.array([1.3, -0.7, 0.25, 2.0])

    def model(p):
        return p[0] * np.exp(p[1] * x) + p[2] * np.sin(p[3] * x)

    y = model(truth) + rng.normal(0, noise, n) if noise else model(truth)
    return (lambda p: (model(p) - y) / 0.01), truth


def test_solve_recovers_a_nonlinear_truth():
    fun, truth = _problem()
    res = lm.solve(fun, np.array([1.0, -0.3, 0.1, 1.7]), np.full(4, 1e-7))
    assert res.converged
    assert np.allclose(res.p, truth, atol=1e-6)
    assert res.data_cost < 1e-8


def test_a_fixed_parameter_stays_where_it_was():
    fun, truth = _problem()
    p0 = np.array([1.0, 0.0, 0.1, 2.0])
    free = np.array([True, True, True, False])
    res = lm.solve(fun, p0, np.full(4, 1e-7), free=free)
    assert res.p[3] == 2.0
    assert res.jacobian.shape[1] == 3 and list(res.free_index) == [0, 1, 2]


def test_a_prior_pulls_an_unobserved_parameter_to_its_mean():
    def fun(p):              # only p[0] is observed
        return np.array([(p[0] - 3.0) / 0.1])

    mean = np.array([0.0, 5.0])
    sigma = np.array([np.inf, 0.5])
    res = lm.solve(fun, np.array([0.0, 0.0]), np.full(2, 1e-6), prior_mean=mean, prior_sigma=sigma)
    assert abs(res.p[0] - 3.0) < 1e-6 and abs(res.p[1] - 5.0) < 1e-6


def test_jacobian_is_the_forward_difference():
    def fun(p):
        return np.array([p[0] ** 2, p[0] * p[1]])

    J = lm.jacobian(fun, np.array([2.0, 3.0]), [0, 1], np.array([1e-6, 1e-6]))
    assert np.allclose(J, [[4.0, 0.0], [3.0, 2.0]], atol=1e-5)


def test_full_jacobian_adds_exact_prior_rows():
    def fun(p):
        return np.array([p[0] + p[1]])

    J = lm.full_jacobian(fun, np.zeros(3), np.array([0, 1, 2]), np.full(3, 1e-6),
                         np.zeros(3), np.array([np.inf, 2.0, 4.0]))
    assert J.shape == (3, 3)
    assert np.allclose(J[1:], [[0.0, 0.5, 0.0], [0.0, 0.0, 0.25]])


def test_sigmas_are_the_root_of_the_inverse_normal_matrix_and_keep_a_near_null_direction():
    rng = np.random.default_rng(3)
    J = rng.normal(size=(40, 4))
    expected = np.sqrt(np.diag(np.linalg.inv(J.T @ J)))
    assert np.allclose(lm.sigmas(J), expected, rtol=1e-9)
    # two columns nearly equal: a nearly unidentified direction keeps its large 1σ
    J2 = J.copy()
    J2[:, 3] = J2[:, 2] * (1.0 + 1e-9)
    s = lm.sigmas(J2)
    assert s[3] > 1e6 and s[2] > 1e6


def _spd(n, seed):
    rng = np.random.default_rng(seed)
    J = rng.normal(size=(n + 20, n))
    return J.T @ J + np.diag(rng.uniform(1e-6, 1.0, n)), rng.normal(size=n)


def test_the_step_solve_is_the_cholesky_solve_of_lapack_at_every_size():
    """FIX-K-r1 (K-3): above 63 parameters the step's Cholesky is NumPy alone, so it wakes no BLAS thread pool
    (LAPACK's does from 64 × 64 on); it is LAPACK's factor and solve to rounding, at the handheld solve's 114
    parameters and beyond. Up to 63 the step keeps LAPACK's factor (the arm check's 32: test_autocal_arm_check's
    digest). The thread pool itself is test_autocal_handheld's."""
    assert lm._LAPACK_CHOLESKY_MAX_N == 63
    for n in (1, 2, 6, 32, 63, 64, 114, 150):
        A, b = _spd(n, n)
        L = lm._cholesky(A)
        assert np.allclose(L, np.linalg.cholesky(A), rtol=1e-10, atol=1e-12), n
        assert np.array_equal(L, np.tril(L)), n
        x = lm._solve_spd(A, b)
        assert np.allclose(x, np.linalg.solve(A, b), rtol=1e-8, atol=1e-10), n


def test_a_matrix_that_is_not_positive_definite_raises():
    """Like LAPACK for an indefinite and a singular matrix; a NaN pivot raises too (OpenBLAS's Cholesky returns a
    NaN factor there), which lm.solve treats exactly like the NaN step it would have tried: the step is refused and
    the damping grows."""
    A, _b = _spd(6, 1)
    lapack_refuses = (A - np.eye(6) * (np.linalg.eigvalsh(A)[0] + 1.0),   # indefinite
                      np.zeros((6, 6)))                                     # singular
    for bad in lapack_refuses + (np.where(np.eye(6, k=-1) > 0, np.nan, A),):   # NaN below the diagonal
        try:
            lm._cholesky(bad)
        except np.linalg.LinAlgError:
            pass
        else:
            raise AssertionError(bad)
    for bad in lapack_refuses:
        try:
            np.linalg.cholesky(bad)
        except np.linalg.LinAlgError:
            pass
        else:
            raise AssertionError(('LAPACK accepts', bad))
