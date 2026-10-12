#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Cloud-Inferenz action schedule (robot side). Pure: stdlib + numpy, no ROS, no asyncio, no clock of its own.

Semantics: ``e`` = actions published this run = the timestep of the next action. An observation sent
when ``e`` actions were published carries ``obs_e = e``; chunk row i is the action for timestep
``obs_e + i``. Each tick, in this order (load-bearing):

1. adopt — every received ``(seq, obs_e, actions | None)``: stale when no observation is in flight or
   seq/obs_e differ from the in-flight ticket (dropped, counted); failed when ``actions`` is None (the
   request ended without a chunk: the slot is free at once, M1); pre-hold when seq is the observation
   marked stale at hold entry or as overdue (dropped, counted stale, the slot is free — D8, D43);
   malformed (dtype not float32 or shape not (n_expected, action_dim)) → END ``'malformed'``;
   non-finite → END ``'nonfinite'`` (never clamped); else row i is queued at ``ts = obs_e + i`` for
   every ``ts >= e``, blended ``(1 - w)·old + w·row`` with an already queued action (w =
   ``aggregate_new_weight``, float32).
2. drop — queue entries with ``ts < e``.
3. hold — no action for ``e``: entering the hold marks the in-flight observation pre-hold. It is NOT
   cancelled (a cancel closes the kept-alive HTTPS connection, D43); its chunk is dropped when it lands
   (D8: only a picture taken during the hold may end it).
4. overdue — an in-flight observation ``resend_after_s`` old is marked stale (M1: never cancelled;
   nothing new is sent while it is on the wire; it ends with its late chunk, dropped, or as
   ``(seq, obs_e, None)``).
5. verdict — before the first adoption: ``LOADING_BUDGET_S`` since the start → END
   ``'loading_timeout'``; after it: ``stall_end_s`` since the last adoption → END ``'stall'``.
6. obs — the robot's data is ready, nothing is in flight, an observation is due (the first one, a
   hold, or ``cadence_steps`` executed since the last one) and ``1 / max_obs_per_s`` has passed: a
   Ticket — the caller builds and sends that observation.
7. pop — the action for ``e`` is published and ``e`` advances; otherwise nothing is published (the arm
   holds its last commanded pose; torque stays on).

Consequences: at most one observation in flight; the cadence self-slows when the round trip exceeds
``cadence_steps`` ticks; the first chunk is executed from its index 0 (``e`` is still 0 when it lands);
the queue never holds more than ``actions_per_chunk`` actions past the newest adopted observation (the
blind cap, ≈ 1.7 s at 30 fps). One observation in flight with a 50-row blind cap sustains only while the
round trip is at most ``actions_per_chunk / (2 · fps)`` (0.83 s at 30 fps); above that the queue runs dry
once per round trip, and a round trip of ``resend_after_s`` (2 s) or more never adopts a chunk (Q-D).
Robot ceilings (D7): ``actions_per_chunk`` ≤ 50 and ``stall_end_s`` ≤ 15 s; a served ``ready.limits``
value can only tighten them (``clamp_limits``).
"""
from dataclasses import dataclass
import math

import numpy as np

ACTIONS_PER_CHUNK_MAX = 50        # O9': blind cap; a server value can only lower it
STALL_END_S_MAX = 15.0            # O4': end after 15 s without an adopted plan; can only be lowered
LOADING_BUDGET_S = 120.0          # O3': budget from the start until the first adopted chunk
DEFAULTS = {'cadence_steps': 5, 'actions_per_chunk': 50, 'aggregate_new_weight': 0.7,
            'max_obs_per_s': 8.0, 'stall_end_s': 15.0, 'resend_after_s': 2.0}
BOUNDS = {'cadence_steps': (1, 30), 'actions_per_chunk': (1, ACTIONS_PER_CHUNK_MAX),
          'aggregate_new_weight': (0.0, 1.0), 'max_obs_per_s': (0.5, 8.0),
          'stall_end_s': (1.0, STALL_END_S_MAX), 'resend_after_s': (0.5, 10.0)}


@dataclass(frozen=True)
class Limits:
    cadence_steps: int
    actions_per_chunk: int
    aggregate_new_weight: float
    max_obs_per_s: float
    stall_end_s: float
    resend_after_s: float


def clamp_limits(raw):
    """``ready.limits`` → Limits. Missing / non-numeric / bool / non-finite / not convertible to float
    (a 400-digit JSON int) → the default; out of range → clamped into BOUNDS (D7, R-11)."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            v = default
        try:
            f = float(v)                       # a 400-digit JSON int raises OverflowError here (R-11)
        except (OverflowError, ValueError):
            f = float(default)
        if not math.isfinite(f):
            f = float(default)
        lo, hi = BOUNDS[key]
        f = min(max(f, lo), hi)
        out[key] = int(f) if isinstance(default, int) else f
    return Limits(**out)


@dataclass(frozen=True)
class Ticket:
    seq: int
    obs_e: int
    sent_at: float


@dataclass
class StepResult:
    action: object = None      # np.float32[action_dim] to publish now, or None (hold)
    ticket: object = None      # Ticket: build + send an observation now
    end: object = None         # None | 'loading_timeout' | 'stall' | 'malformed' | 'nonfinite'


class Schedule:
    def __init__(self, limits, n_expected, action_dim, start):
        self.lim = limits
        self.n_expected = int(n_expected)
        self.dim = int(action_dim)
        self.start = float(start)
        self.e = 0
        self.queue = {}
        self.next_seq = 0
        self.inflight = None
        self.last_obs_e = None
        self.last_obs_at = -math.inf
        self.last_adopt = None
        self.holding = True
        self.hold_since = float(start)
        self.counters = {'sent': 0, 'adopted': 0, 'stale': 0, 'failed': 0, 'overdue': 0, 'holds': 0}
        self.pre_hold_seq = None   # D43: the obs marked stale (hold entry or overdue) stays on the wire; its chunk is
                                   # dropped on arrival. The schedule NEVER cancels a request (M1).

    def step(self, now, chunks, data_ready):      # chunks: (seq, obs_e, actions | None)
        r = StepResult()
        for seq, obs_e, actions in chunks:
            verdict = self._adopt(seq, obs_e, actions, now)
            if verdict in ('malformed', 'nonfinite'):
                r.end = verdict
                return r
        for ts in [ts for ts in self.queue if ts < self.e]:
            del self.queue[ts]
        have = self.e in self.queue
        if not have and not self.holding:
            self.holding, self.hold_since = True, now
            self.counters['holds'] += 1
            if self.inflight is not None:
                self.pre_hold_seq = self.inflight.seq      # D43: NOT cancelled (it would close the connection)
        elif have and self.holding:
            self.holding = False
        if (self.inflight is not None and now - self.inflight.sent_at >= self.lim.resend_after_s
                and self.pre_hold_seq != self.inflight.seq):
            self.pre_hold_seq = self.inflight.seq      # M1: marked stale, never cancelled
            self.counters['overdue'] += 1
        if self.last_adopt is None:
            if now - self.start >= LOADING_BUDGET_S:
                r.end = 'loading_timeout'
                return r
        elif now - self.last_adopt >= self.lim.stall_end_s:
            r.end = 'stall'
            return r
        due = (self.last_obs_e is None or self.holding
               or self.e - self.last_obs_e >= self.lim.cadence_steps)
        if (data_ready and self.inflight is None and due
                and now - self.last_obs_at >= 1.0 / self.lim.max_obs_per_s):
            t = Ticket(self.next_seq, self.e, now)
            self.next_seq += 1
            self.inflight = t
            self.last_obs_e, self.last_obs_at = self.e, now
            self.counters['sent'] += 1
            r.ticket = t
        if have:
            r.action = self.queue.pop(self.e)
            self.e += 1
        return r

    def _adopt(self, seq, obs_e, actions, now):
        t = self.inflight
        if t is None or seq != t.seq or obs_e != t.obs_e:
            self.counters['stale'] += 1
            return 'stale'
        if actions is None:                              # M1: the request ended without a chunk
            self.inflight = None
            self.pre_hold_seq = None
            self.counters['failed'] += 1
            return 'failed'
        if seq == self.pre_hold_seq:                     # D8: a picture taken before the hold never ends it
            self.inflight = None
            self.pre_hold_seq = None
            self.counters['stale'] += 1
            return 'stale'
        a = np.asarray(actions)
        if a.dtype != np.float32 or a.shape != (self.n_expected, self.dim):
            return 'malformed'
        if not np.all(np.isfinite(a)):
            return 'nonfinite'
        w = self.lim.aggregate_new_weight
        for i in range(a.shape[0]):
            ts = obs_e + i
            if ts < self.e:
                continue
            old = self.queue.get(ts)
            self.queue[ts] = a[i].copy() if old is None else ((1.0 - w) * old + w * a[i]).astype(np.float32)
        self.inflight = None
        self.last_adopt = now
        self.counters['adopted'] += 1
        return 'adopted'

    def facts(self, now):
        return {'executed': self.e, 'queued': len(self.queue), 'holding': self.holding,
                'hold_s': round(now - self.hold_since, 2) if self.holding and self.last_adopt is not None else None,
                'plan_age_ms': None if self.last_adopt is None else round((now - self.last_adopt) * 1000.0, 1),
                **self.counters}
