#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Levenberg–Marquardt with a numeric forward-difference Jacobian and
Gaussian priors (spec 40 S4).

``fun(p)`` answers the data residuals, already divided by their sigma. A
parameter ``i`` with a finite ``prior_sigma[i]`` adds the row
``(p[i] − prior_mean[i]) / prior_sigma[i]``; only the ``free`` parameters move.
The damping is Marquardt's (``λ · diag(JᵀJ)``).

Pure NumPy and the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass
class Result:
    """``p`` the solution (every parameter, fixed ones untouched), ``cost`` the
    sum of squares of the data and prior rows there, ``data_cost`` of the data
    rows alone, ``jacobian`` of all rows w.r.t. the free parameters at ``p``."""

    p: np.ndarray
    cost: float
    data_cost: float
    iterations: int
    converged: bool
    jacobian: np.ndarray
    free_index: np.ndarray


def _prior_rows(p, idx, mean, sigma):
    if mean is None or sigma is None:
        return np.zeros(0)
    s = sigma[idx]
    keep = np.isfinite(s)
    return (p[idx][keep] - mean[idx][keep]) / s[keep]


# LAPACK's Cholesky runs on one thread up to this size and wakes OpenBLAS's
# thread pool above it (numpy 1.26.4's OpenBLAS, measured: 63 × 63 one thread,
# 64 × 64 the pool). Up to it the step keeps LAPACK's factor, so the arm check
# (32 parameters) and the mat check (6) solve as before to the bit.
_LAPACK_CHOLESKY_MAX_N = 63


def _cholesky(A) -> np.ndarray:
    """The lower Cholesky factor of a symmetric positive definite ``A``, column
    by column from its lower triangle, in NumPy alone (no BLAS, no LAPACK).
    Raises ``LinAlgError`` when ``A`` is not positive definite (a pivot that is
    not positive, or not finite)."""
    A = np.asarray(A, dtype=np.float64)
    n = A.shape[0]
    L = np.zeros((n, n))
    for j in range(n):
        d = A[j, j] - float(np.sum(L[j, :j] * L[j, :j]))
        if not (math.isfinite(d) and d > 0.0):
            raise np.linalg.LinAlgError('Matrix is not positive definite')
        L[j, j] = ljj = math.sqrt(d)
        L[j + 1:, j] = (A[j + 1:, j] - np.sum(L[j + 1:, :j] * L[j, :j], axis=1)) / ljj
    return L


def _solve_spd(A, b) -> np.ndarray:
    """``A·x = b`` for a symmetric positive definite ``A``: Cholesky and two
    explicit substitutions. LAPACK's general solver (and BLAS's ``Jᵀ·r``) start
    OpenBLAS's thread pool even for a 32 × 32 system, and LAPACK's Cholesky
    does above ``_LAPACK_CHOLESKY_MAX_N`` (the handheld solve's system is
    114 × 114 at 18 views), so a larger system is factored in NumPy alone. The
    pool's threads spin on every core for a while after each call — measured,
    they cost several cores per process for a solve that needs a fraction of
    one, and under load they made the handheld solve about ten times slower.
    Raises ``LinAlgError`` when ``A`` is not positive definite."""
    L = np.linalg.cholesky(A) if b.size <= _LAPACK_CHOLESKY_MAX_N else _cholesky(A)
    n = b.size
    y = np.empty(n)
    for i in range(n):
        y[i] = (b[i] - float(np.sum(L[i, :i] * y[:i]))) / L[i, i]
    x = np.empty(n)
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - float(np.sum(L[i + 1:, i] * x[i + 1:]))) / L[i, i]
    return x


def jacobian(fun, p, idx, steps, r0=None) -> np.ndarray:
    """Forward differences of ``fun`` at ``p`` for the parameters ``idx``."""
    p = np.asarray(p, dtype=np.float64)
    r0 = fun(p) if r0 is None else r0
    J = np.empty((r0.size, len(idx)))
    for k, i in enumerate(idx):
        pp = p.copy()
        pp[i] += steps[i]
        J[:, k] = (fun(pp) - r0) / steps[i]
    return J


def full_residual(fun, p, idx, prior_mean=None, prior_sigma=None) -> np.ndarray:
    """The data rows and the prior rows of the free parameters ``idx``."""
    return np.concatenate([fun(p), _prior_rows(p, idx, prior_mean, prior_sigma)])


def full_jacobian(fun, p, idx, steps, prior_mean=None, prior_sigma=None, r0=None) -> np.ndarray:
    """The Jacobian of :func:`full_residual`: numeric data rows, exact prior rows."""
    Jd = jacobian(fun, p, idx, steps, r0)
    if prior_mean is None or prior_sigma is None:
        return Jd
    s = np.asarray(prior_sigma, dtype=np.float64)[idx]
    keep = np.flatnonzero(np.isfinite(s))
    Jp = np.zeros((keep.size, len(idx)))
    Jp[np.arange(keep.size), keep] = 1.0 / s[keep]
    return np.vstack([Jd, Jp])


def solve(fun, p0, steps, *, free=None, prior_mean=None, prior_sigma=None,
          max_iter: int = 60, rel_tol: float = 1e-9, lam0: float = 1e-3) -> Result:
    """Minimise ``|fun(p)|² + Σ ((p_i − m_i)/σ_i)²`` over the free parameters."""
    p = np.asarray(p0, dtype=np.float64).copy()
    steps = np.asarray(steps, dtype=np.float64)
    n = p.size
    free = np.ones(n, dtype=bool) if free is None else np.asarray(free, dtype=bool)
    idx = np.flatnonzero(free)
    if prior_mean is not None:
        prior_mean = np.asarray(prior_mean, dtype=np.float64)
    if prior_sigma is not None:
        prior_sigma = np.asarray(prior_sigma, dtype=np.float64)

    rd = fun(p)
    r = np.concatenate([rd, _prior_rows(p, idx, prior_mean, prior_sigma)])
    cost = float(r @ r)
    lam = lam0
    converged = False
    it = 0
    J = None
    for it in range(1, max_iter + 1):
        J = full_jacobian(fun, p, idx, steps, prior_mean, prior_sigma, r0=rd)
        g = np.einsum('ki,k->i', J, r)
        H = np.einsum('ki,kj->ij', J, J)
        D = np.diag(np.diag(H) + 1e-12)
        improved = False
        rel = 0.0
        for _ in range(10):
            try:
                dp = _solve_spd(H + lam * D, -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            pn = p.copy()
            pn[idx] += dp
            rdn = fun(pn)
            rn = np.concatenate([rdn, _prior_rows(pn, idx, prior_mean, prior_sigma)])
            cn = float(rn @ rn)
            if math.isfinite(cn) and cn < cost:
                rel = (cost - cn) / max(cost, 1e-300)
                p, rd, r, cost = pn, rdn, rn, cn
                lam = max(lam / 5.0, 1e-12)
                improved = True
                break
            lam *= 10.0
        if not improved or rel < rel_tol:
            converged = True
            break
    J = full_jacobian(fun, p, idx, steps, prior_mean, prior_sigma, r0=rd)
    return Result(p=p, cost=cost, data_cost=float(rd @ rd), iterations=it,
                  converged=converged, jacobian=J, free_index=idx)


def sigmas(J) -> np.ndarray:
    """1σ of each column's parameter: the root of the diagonal of ``(JᵀJ)⁻¹``
    (``J`` of σ-normalised rows, priors included). Taken from the SVD of ``J``
    itself, so a nearly unidentified direction keeps its large variance
    instead of being lost to the squared condition number; an exactly
    singular one answers ``inf``."""
    _, s, Vt = np.linalg.svd(np.asarray(J, dtype=np.float64), full_matrices=False)
    with np.errstate(divide='ignore'):
        inv = np.where(s > 0.0, 1.0 / np.square(s), np.inf)
    var = (np.square(Vt.T) * inv[None, :]).sum(axis=1)
    return np.sqrt(var)
