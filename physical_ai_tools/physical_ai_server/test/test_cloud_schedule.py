"""Cloud-Inferenz robot action schedule (spec §2.4, D7, D8, D43, M1, Q-D, F-6, R-11, R-22).

The schedule is pure (stdlib + numpy, no clock of its own), so every test drives
it with a simulated tick clock: tick w happens at ``now = w / FPS`` and an
observation issued at tick w is answered (or ended) in the step of tick w + L.
"""

from __future__ import annotations

import inspect
import math

import numpy as np
import pytest

from physical_ai_server.inference import cloud_schedule as cs

FPS = 30.0
DT = 1.0 / FPS
DIM = 6


def chunk_of(obs_e, n=50, dim=DIM):
    """Deterministic float32 chunk of an observation taken at executed count obs_e."""
    return np.random.default_rng(1000 + obs_e).standard_normal((n, dim)).astype(np.float32)


def make(limits=None, n=None, start=0.0):
    lim = cs.clamp_limits(limits or {})
    return cs.Schedule(lim, n if n is not None else lim.actions_per_chunk, DIM, start)


class Sim:
    """Drive a Schedule at FPS. ``latency(seq, w) -> ticks | None`` (w = the send tick; None = never answered);
    ``ends_without_chunk(seq) -> bool`` reports the request as (seq, obs_e, None) instead."""

    def __init__(self, sched, latency, ends_without_chunk=lambda seq: False, data_ready=lambda w: True):
        self.s = sched
        self.latency = latency
        self.ends_without = ends_without_chunk
        self.data_ready = data_ready
        self.w = 0
        self.pending = []          # (arrive_tick, seq, obs_e)
        self.actions = []          # per tick: action or None
        self.tickets = []          # (tick, Ticket)
        self.end = None
        self.end_tick = None

    def tick(self):
        w = self.w
        due = [p for p in self.pending if p[0] <= w]
        self.pending = [p for p in self.pending if p[0] > w]
        chunks = []
        for _, seq, obs_e in sorted(due):
            chunks.append((seq, obs_e, None if self.ends_without(seq) else chunk_of(obs_e, self.s.n_expected)))
        r = self.s.step(w * DT, chunks, self.data_ready(w))
        if r.end is not None:
            self.end, self.end_tick = r.end, w
            self.w += 1
            return r
        if r.ticket is not None:
            self.tickets.append((w, r.ticket))
            lat = self.latency(r.ticket.seq, w)
            if lat is not None:
                self.pending.append((w + lat, r.ticket.seq, r.ticket.obs_e))
        self.actions.append(r.action)
        self.w += 1
        return r

    def run(self, ticks=None, until_actions=None, max_ticks=200_000):
        while self.end is None and self.w < max_ticks:
            if ticks is not None and self.w >= ticks:
                break
            if until_actions is not None and self.s.e >= until_actions:
                break
            self.tick()
        return self


# ── basic semantics ─────────────────────────────────────────────────────────

def test_first_chunk_executes_index_0():
    s = make()
    r0 = s.step(0.0, [], True)
    assert r0.ticket is not None and r0.ticket.seq == 0 and r0.ticket.obs_e == 0
    assert r0.action is None
    c = chunk_of(0)
    r1 = s.step(DT * 3, [(0, 0, c)], True)
    assert np.array_equal(r1.action, c[0])
    assert s.e == 1
    r2 = s.step(DT * 4, [], True)
    assert np.array_equal(r2.action, c[1])


def test_rows_before_e_dropped():
    s = make()
    s.step(0.0, [], True)
    s.step(DT, [(0, 0, chunk_of(0))], True)          # e -> 1
    t = None
    w = 2
    while t is None:                                  # executes until the cadence sends seq 1
        r = s.step(w * DT, [], True)
        t = r.ticket
        w += 1
    assert t.seq == 1 and t.obs_e == 5
    for _ in range(3):                                # the answer lands 3 ticks later
        s.step(w * DT, [], True)
        w += 1
    e_before = s.e
    assert e_before > t.obs_e                         # rows obs_e .. e_before-1 are already in the past
    new = chunk_of(t.obs_e)
    r = s.step(w * DT, [(1, t.obs_e, new)], True)
    assert s.counters['adopted'] == 2
    # Only rows with ts >= e were taken: the queue ends at obs_e + 49 and starts at the new e.
    assert sorted(s.queue) == list(range(s.e, t.obs_e + 50))
    # the action popped in that tick is ts = e_before, blended from row e_before - obs_e
    old = chunk_of(0)
    i = e_before - t.obs_e
    assert np.array_equal(r.action, ((1.0 - 0.7) * old[e_before] + 0.7 * new[i]).astype(np.float32))


def test_overlap_blend_0_7_new():
    s = make()
    s.step(0.0, [], True)
    old = chunk_of(0)
    s.step(DT, [(0, 0, old)], True)                   # e -> 1
    w, t = 2, None
    while t is None:
        r = s.step(w * DT, [], True)
        t = r.ticket
        w += 1
    new = chunk_of(t.obs_e)
    # The ticket's own tick popped ts = obs_e, so the answer (next tick) lands with e = obs_e + 1.
    r = s.step(w * DT, [(t.seq, t.obs_e, new)], True)
    ts = t.obs_e + 1
    expected = ((1.0 - 0.7) * old[ts] + 0.7 * new[ts - t.obs_e]).astype(np.float32)
    assert r.action.dtype == np.float32
    assert np.array_equal(r.action, expected)
    assert not np.array_equal(r.action, new[1]) and not np.array_equal(r.action, old[ts])
    # rows past the old chunk's end are adopted unblended
    assert np.array_equal(s.queue[t.obs_e + 49], new[49])


def test_blind_cap_50():
    sim = Sim(make(), latency=lambda seq, w: 2 if seq == 0 else None).run(ticks=400)
    executed = [a for a in sim.actions if a is not None]
    assert len(executed) == 50
    assert sim.s.holding


def test_one_obs_in_flight():
    sim = Sim(make(), latency=lambda seq, w: 2 if seq == 0 else None).run(ticks=300)
    assert len(sim.tickets) == 2                      # seq 0 answered, seq 1 never: nothing after it
    assert sim.s.inflight is not None and sim.s.inflight.seq == 1


def test_cadence_every_5_executed_steps():
    sim = Sim(make(), latency=lambda seq, w: 1).run(until_actions=200)
    obs_es = [t.obs_e for _, t in sim.tickets]
    assert obs_es[:6] == [0, 5, 10, 15, 20, 25]
    assert all(b - a == 5 for a, b in zip(obs_es, obs_es[1:]))


@pytest.mark.parametrize('lat,rate', [(6, 0.84), (8, 0.63), (12, 0.42)])
def test_cadence_self_slows_above_5_ticks(lat, rate):
    sim = Sim(make(), latency=lambda seq, w: lat).run(until_actions=3000)
    obs_es = [t.obs_e for _, t in sim.tickets]
    gaps = [b - a for a, b in zip(obs_es, obs_es[1:])]
    # the first gap is the cadence after the start (e stays 0 until the first chunk lands);
    # after it, one in flight: the cadence waits for the answer
    assert gaps[0] == 5 and set(gaps[1:]) == {lat}
    per5 = 5.0 * len(obs_es) / sim.s.e
    assert abs(per5 - rate) < 0.02


def test_max_obs_per_s():
    s = make({'cadence_steps': 1, 'max_obs_per_s': 2.0})
    sim = Sim(s, latency=lambda seq, w: 1).run(until_actions=300)
    times = [w * DT for w, _ in sim.tickets]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 0.5 - 1e-9
    assert s.lim.max_obs_per_s == 2.0


# ── hold, stale mark, M1 ────────────────────────────────────────────────────

def test_hold_publishes_nothing_then_fresh_obs():
    # seq 0 fast, seq 1 never answered → dry after 50 actions; once seq 1 ends without a
    # chunk (the client's timeout), a fresh observation at the current e is sent and its
    # chunk executes from its row 0.
    s = make()
    sim = Sim(s, latency=lambda seq, w: {0: 2, 1: 90}.get(seq, 2),
              ends_without_chunk=lambda seq: seq == 1)
    sim.run(ticks=200)
    assert s.e >= 50
    hold_ticks = [w for w, a in enumerate(sim.actions) if a is None and w > 10]
    assert hold_ticks, 'the arm never held'
    fresh = [t for _, t in sim.tickets if t.seq == 2][0]
    assert fresh.obs_e == 50                          # the picture is taken during the hold
    i = sim.tickets[[t.seq for _, t in sim.tickets].index(2)][0]
    resumed = next(a for a in sim.actions[i + 1:] if a is not None)
    assert np.array_equal(resumed, chunk_of(50)[0])
    assert s.counters['failed'] == 1


def test_hold_drops_the_pre_hold_chunk_without_cancelling():
    # Latency 30 ticks > 25: the queue runs dry while seq k is on the wire. Its chunk is
    # dropped on arrival (stale), and the next ticket is a fresh picture taken in the hold.
    s = make()
    sim = Sim(s, latency=lambda seq, w: 2 if seq == 0 else 30).run(ticks=200)
    assert s.counters['holds'] >= 1
    assert s.counters['stale'] >= 1
    assert not hasattr(s, 'abandon') and not hasattr(cs.StepResult(), 'abandoned')
    hold_entry = next(w for w, a in enumerate(sim.actions) if w >= 3 and a is None)   # first dry tick
    e_at_hold = sum(1 for a in sim.actions[:hold_entry] if a is not None)
    pre_tick, pre = [(w, t) for w, t in sim.tickets if w < hold_entry][-1]
    later = [(w, t) for w, t in sim.tickets if w > hold_entry]
    # nothing was sent while the pre-hold observation was on the wire: the next ticket
    # goes in the very tick its (dropped) answer landed, and carries a picture taken in the hold
    (next_tick, nxt) = later[0]
    assert next_tick == pre_tick + 30
    assert nxt.seq == pre.seq + 1
    assert nxt.obs_e == e_at_hold
    resumed = next(a for a in sim.actions[next_tick + 1:] if a is not None)
    assert np.array_equal(resumed, chunk_of(e_at_hold)[0])


def test_resend_marks_stale_without_cancelling():
    s = make()
    s.step(0.0, [], True)
    s.step(DT, [(0, 0, chunk_of(0))], True)
    w, t = 2, None
    while t is None:
        r = s.step(w * DT, [], True)
        t = r.ticket
        w += 1
    sent_at = t.sent_at
    # 2 s pass with nothing answered (the queue may run dry meanwhile)
    while w * DT < sent_at + 2.0:
        r = s.step(w * DT, [], True)
        assert r.ticket is None                       # nothing new while it is on the wire
        w += 1
    r = s.step(w * DT, [], True)
    assert r.ticket is None
    assert s.inflight is not None and s.inflight.seq == t.seq   # marked, still on the wire
    assert s.pre_hold_seq == t.seq
    stale_before = s.counters['stale']
    # its late chunk is dropped, and the slot is free for a fresh picture in the same step
    r = s.step((w + 1) * DT, [(t.seq, t.obs_e, chunk_of(t.obs_e))], True)
    assert s.counters['stale'] == stale_before + 1
    assert s.counters['adopted'] == 1
    assert r.ticket is not None and r.ticket.seq == t.seq + 1


def test_request_ended_without_chunk_frees_the_slot():
    s = make()
    r0 = s.step(0.0, [], True)
    r1 = s.step(0.2, [(r0.ticket.seq, r0.ticket.obs_e, None)], True)
    assert s.counters['failed'] == 1
    assert r1.ticket is not None and r1.ticket.seq == 1 and r1.ticket.obs_e == 0


def test_never_two_requests_on_the_wire():
    # Random latency (1–90 ticks), 10 % never answered (ended without a chunk by the
    # client's 8 s timeout = 240 ticks), 20 % error answers, 5 % data not ready.
    rng = np.random.default_rng(7)
    adopted_total = sent_total = 0
    for run in range(60):
        s = make()
        outstanding = set()
        pending = []                                  # (arrive_tick, seq, obs_e, gone)
        for w in range(3000):
            due = sorted(p for p in pending if p[0] <= w)
            pending = [p for p in pending if p[0] > w]
            chunks = []
            for _, seq, obs_e, gone in due:
                chunks.append((seq, obs_e, None if gone else chunk_of(obs_e)))
                outstanding.discard(seq)
            r = s.step(w * DT, chunks, rng.random() > 0.05)
            if r.end:
                break
            if r.ticket is not None:
                assert not outstanding, f'run {run}: a second request while {outstanding} is on the wire'
                outstanding.add(r.ticket.seq)
                if rng.random() < 0.1:
                    pending.append((w + 240, r.ticket.seq, r.ticket.obs_e, True))
                else:
                    pending.append((w + int(rng.integers(1, 90)), r.ticket.seq, r.ticket.obs_e,
                                    rng.random() < 0.2))
        adopted_total += s.counters['adopted']
        sent_total += s.counters['sent']
    assert sent_total > 1500 and adopted_total > 300


def test_step_result_has_no_abandoned():
    fields = set(cs.StepResult.__dataclass_fields__)
    assert fields == {'action', 'ticket', 'end'}
    assert not hasattr(cs.Schedule, 'abandon')
    src = inspect.getsource(cs)
    assert 'cancel(' not in src


def test_step_takes_no_epoch():
    params = list(inspect.signature(cs.Schedule.step).parameters)
    assert params == ['self', 'now', 'chunks', 'data_ready']


def _two_s_plus_one_tick(seq, w0):
    """The reply lands one tick after the tick in which the 2 s resend mark fires (J-6/J-7:
    „a reply of ≥ 2 s + one tick"). Computed per send tick, because 60 ticks of 1/30 s
    sometimes sum to a hair under 2.0 in floating point."""
    k = 1
    while (w0 + k) * DT - w0 * DT < 2.0:
        k += 1
    return k + 1


def test_rtt_above_resend_never_adopts():
    # Q-D (J-6/J-7): a reply of ≥ 2 s + one tick is never adopted.
    sim = Sim(make(), latency=_two_s_plus_one_tick).run()
    assert sim.s.counters['adopted'] == 0
    assert sim.end == 'loading_timeout'
    assert sim.end_tick * DT == pytest.approx(cs.LOADING_BUDGET_S, abs=DT)
    # after a good start, a line that slows to ≥ 2 s + one tick ends with the stall
    sim = Sim(make(), latency=lambda seq, w: 2 if seq < 3 else _two_s_plus_one_tick(seq, w)).run()
    assert sim.end == 'stall'
    assert sim.s.counters['adopted'] == 3
    assert sim.end_tick * DT - sim.s.last_adopt == pytest.approx(15.0, abs=DT)
    # one tick earlier (the reply lands in the mark's own tick, before the mark) is adopted
    sim = Sim(make(), latency=lambda seq, w: _two_s_plus_one_tick(seq, w) - 1).run(until_actions=500)
    assert sim.end is None and sim.s.counters['adopted'] > 5


def test_stale_seq_dropped():
    s = make()
    r0 = s.step(0.0, [], True)
    s.step(DT, [(r0.ticket.seq + 7, r0.ticket.obs_e, chunk_of(0))], True)
    assert s.counters['stale'] == 1
    assert s.inflight is not None and s.inflight.seq == r0.ticket.seq
    s.step(2 * DT, [(r0.ticket.seq, r0.ticket.obs_e + 1, chunk_of(0))], True)
    assert s.counters['stale'] == 2 and s.counters['adopted'] == 0


@pytest.mark.parametrize('bad', [
    np.zeros((49, DIM), np.float32), np.zeros((50, DIM + 1), np.float32),
    np.zeros((50, DIM), np.float64), np.zeros((50,), np.float32)])
def test_malformed_ends(bad):
    s = make()
    r0 = s.step(0.0, [], True)
    r = s.step(DT, [(r0.ticket.seq, r0.ticket.obs_e, bad)], True)
    assert r.end == 'malformed' and r.action is None and r.ticket is None


def test_nonfinite_ends_never_clamped():
    for bad in (np.nan, np.inf, -np.inf):
        s = make()
        r0 = s.step(0.0, [], True)
        c = chunk_of(0)
        c[17, 3] = bad
        r = s.step(DT, [(r0.ticket.seq, r0.ticket.obs_e, c)], True)
        assert r.end == 'nonfinite' and r.action is None
        assert s.queue == {}                          # nothing of it was adopted


def test_stall_end_15_s():
    sim = Sim(make(), latency=lambda seq, w: 2 if seq == 0 else 30,
              ends_without_chunk=lambda seq: seq > 0).run()
    assert sim.end == 'stall'
    last_adopt = sim.s.last_adopt
    assert sim.end_tick * DT - last_adopt == pytest.approx(15.0, abs=DT)
    # a lower served value is honoured, a higher one is clamped (D7)
    sim = Sim(make({'stall_end_s': 4.0}), latency=lambda seq, w: 2 if seq == 0 else None).run()
    assert sim.end == 'stall' and sim.end_tick * DT - sim.s.last_adopt == pytest.approx(4.0, abs=DT)


@pytest.mark.parametrize('lat,holds', [(24, False), (25, False), (26, True), (28, True), (30, True)])
def test_hold_threshold_0_83_s(lat, holds):
    sim = Sim(make(), latency=lambda seq, w: lat).run(until_actions=2000)
    assert sim.end is None
    assert (sim.s.counters['holds'] > 0) is holds
    if holds:
        assert sim.s.counters['holds'] >= 20


def test_loading_budget_120_s():
    sim = Sim(make(), latency=lambda seq, w: None).run()
    assert sim.end == 'loading_timeout'
    assert sim.end_tick * DT == pytest.approx(120.0, abs=DT)
    assert cs.LOADING_BUDGET_S == 120.0


# ── clamp_limits (D7, R-11) ─────────────────────────────────────────────────

def test_clamp_cannot_widen_ceilings():
    lim = cs.clamp_limits({'cadence_steps': 500, 'actions_per_chunk': 100, 'aggregate_new_weight': 3,
                           'max_obs_per_s': 100, 'stall_end_s': 60, 'resend_after_s': 99})
    assert lim == cs.Limits(30, 50, 1.0, 8.0, 15.0, 10.0)
    lim = cs.clamp_limits({'cadence_steps': 0, 'actions_per_chunk': 0, 'aggregate_new_weight': -1,
                           'max_obs_per_s': 0.01, 'stall_end_s': 0, 'resend_after_s': 0})
    assert lim == cs.Limits(1, 1, 0.0, 0.5, 1.0, 0.5)
    lower = cs.clamp_limits({'actions_per_chunk': 20, 'stall_end_s': 5.5})
    assert (lower.actions_per_chunk, lower.stall_end_s) == (20, 5.5)
    assert cs.ACTIONS_PER_CHUNK_MAX == 50 and cs.STALL_END_S_MAX == 15.0
    assert isinstance(lower.cadence_steps, int) and isinstance(lower.actions_per_chunk, int)


def test_clamp_survives_huge_ints_and_garbage():
    default = cs.clamp_limits({})
    assert default == cs.Limits(5, 50, 0.7, 8.0, 15.0, 2.0)
    for v in (10 ** 400, -10 ** 400, float('nan'), float('inf'), float('-inf'), 'x', '5',
              True, False, None, [], {}, (1,)):
        lim = cs.clamp_limits({k: v for k in cs.DEFAULTS})
        assert lim == default, (v, lim)
    for raw in (None, [], 'limits', 5, ['cadence_steps']):
        assert cs.clamp_limits(raw) == default


# ── Appendix E (R-22) ───────────────────────────────────────────────────────

def sim_fixed_executed_count(chunk_of, T, L, period=5, agg=0.7):
    """Appendix E verbatim (spec): the spike's sim_fixed with the executed count as the timestep."""
    import numpy as np
    queue, pending, ex, e, w, sent_e = {}, [], [], 0, 0, None
    while len(ex) < T:
        if sent_e is None or e - sent_e >= period:
            pending.append((e, w + L))
            sent_e = e
        for (of, ar) in [p for p in pending if p[1] <= w]:
            pending.remove((of, ar))
            for j, a in enumerate(chunk_of(of)):
                ts = of + j
                if ts < e:
                    continue
                queue[ts] = ((1 - agg) * queue[ts] + agg * a).astype(np.float32) if ts in queue else a.copy()
        for k in [k for k in queue if k < e]:
            del queue[k]
        if e in queue:
            ex.append(queue.pop(e))
            e += 1
        w += 1
    return np.stack(ex)


@pytest.mark.parametrize('L', [1, 2, 3, 4, 5])
def test_identical_to_appendix_e_for_latency_1_to_5(L):
    ref = sim_fixed_executed_count(chunk_of, 3000, L)
    sim = Sim(make(), latency=lambda seq, w: L).run(until_actions=3000)
    got = np.stack([a for a in sim.actions if a is not None][:3000])
    assert got.shape == ref.shape == (3000, DIM)
    assert np.array_equal(got, ref)
    assert np.array_equal(got[0], chunk_of(0)[0])


def test_facts_shape():
    s = make()
    f = s.facts(0.0)
    assert f['executed'] == 0 and f['holding'] is True and f['hold_s'] is None and f['plan_age_ms'] is None
    assert set(f) >= {'sent', 'adopted', 'stale', 'failed', 'overdue', 'holds', 'queued'}
