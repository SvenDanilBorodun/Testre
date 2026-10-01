"""The capture timeline of a recording (Aufnahme 2.0 round 5, spec §2.3, §3, §6.5).

Deps-free: the module is stdlib only, so every rule is driven here with
synthetic, seeded streams. Covers

* ``ClockMap``: a constant clock-domain offset, arrival jitter, late callbacks,
  stamp 0, forward/backward clock steps, monotone output;
* ``SlotSampler``: aliasing at the worst phase (against a fixed-boundary
  reference that does alias), the second-order phase predictor for cameras
  1–1.5 % off the fps, honest decimation, two independent cameras, a 300 ms
  process stall, the completeness gate and its timeout, bounded catch-up, leader
  back-dating, the provisional lock and the one-repeat-per-drift-cycle hysteresis;
* the event-model regression of the final module (the architect's
  ``module_check`` scenarios indep/drift/stalls/cam25@30, 2 seeds × 20 s);
* ``TakeIntegrity``: excess repeats, lost slots, ages, the per-source gap rule
  (O2/C6, a leader gap is excused by a server stall) and the C5 sentences.
"""

from __future__ import annotations

import ast
import json
import math
import random
import statistics as st

import pytest

from physical_ai_server.communication import capture_timeline as ct
from physical_ai_server import signal_status

# ── stream helpers ───────────────────────────────────────────────────────────


def _cam_events(hz, dur, *, phase_frac=0.0, jit=0.004, lat=0.015, tail=0.001,
                offset=3.217, seed=1, start=0.0, fps=30.0):
    """(arrival, stamp, idx, true_t) of one camera: capture at start + phase +
    i/hz with clipped gaussian jitter, a constant stamp offset (its own clock
    domain), delivery = capture + latency + exponential tail, in order."""
    rnd = random.Random(seed)
    out = []
    prev = -1.0
    i = 0
    phase = phase_frac / fps
    while True:
        nominal = start + phase + i / hz
        if nominal > dur:
            break
        tc = nominal + max(-0.4 / hz, min(0.4 / hz, rnd.gauss(0.0, jit)))
        arr = tc + lat + (rnd.expovariate(1.0 / tail) if tail > 0 else 0.0)
        arr = max(arr, prev + 1e-4)
        prev = arr
        out.append((arr, tc + offset, i, tc))
        i += 1
    return out


def _joint_events(dur, *, hz=100.0, stamped=True, seed=2, lat=0.001):
    rnd = random.Random(seed)
    out = []
    ph = rnd.random() / hz
    i = 0
    while True:
        tc = ph + i / hz
        if tc > dur:
            break
        out.append((tc + lat + rnd.expovariate(1 / 0.0005), tc if stamped else 0.0, i, tc))
        i += 1
    return out


def _apply_stall(events, a, b):
    """A process stall [a, b): every callback inside runs at b, in order."""
    out = []
    n = 0
    for arr, stamp, idx, tc in events:
        if a <= arr < b:
            arr = b + 0.00002 * n
            n += 1
        out.append((arr, stamp, idx, tc))
    out.sort(key=lambda e: e[0])
    return out


def _run(fps, cams, follower, leader, dur, *, tick_jit=0.002, seed=3, stall=None,
         t_start=0.0):
    """Drive SlotSampler like the record tick: a tick every 1/fps (+ jitter), the
    histories holding every callback that ran by then. Returns the decisions and
    the sampler."""
    rnd = random.Random(seed)
    T = 1.0 / fps
    names = list(cams)
    hist = {n: ct.SourceHistory(ct.CAMERA_HISTORY_LEN) for n in names}
    hf = ct.SourceHistory(ct.JOINT_HISTORY_LEN)
    hl = ct.SourceHistory(ct.JOINT_HISTORY_LEN)
    ev = []
    for n in names:
        ev += [(a, n, s, i) for a, s, i, _tc in cams[n]]
    ev += [(a, 'f', s, i) for a, s, i, _tc in follower]
    if leader is not None:
        ev += [(a, 'l', 0.0, i) for a, _s, i, _tc in leader]
    ev.sort(key=lambda e: e[0])
    s = ct.SlotSampler(fps, names)
    decisions = []
    ei = 0
    k = 0
    stall_ticked = False
    last_now = -math.inf
    while True:
        k += 1
        now = t_start + k * T + abs(rnd.gauss(0.0, tick_jit))
        if stall is not None and stall[0] <= now < stall[1]:
            if stall_ticked:
                continue                    # the timer skips the periods it missed
            stall_ticked = True
            now = stall[1] + 0.002          # the burst is in the histories by then
        now = max(now, last_now)
        last_now = now
        if now >= dur:
            break
        while ei < len(ev) and ev[ei][0] <= now:
            arr, kind, stamp, idx = ev[ei]
            ei += 1
            if kind == 'f':
                hf.append(arr, stamp, idx)
            elif kind == 'l':
                hl.append(arr, 0.0, idx)
            else:
                hist[kind].append(arr, stamp, idx)
        snaps = {n: h.snapshot() for n, h in hist.items()}
        out = s.decide_due(now, snaps, hf.snapshot(), hl.snapshot() if leader is not None else None)
        assert len(out) <= ct.SlotSampler.MAX_SLOTS_PER_TICK
        decisions += out
    return decisions, s


def _steps(decisions, cam):
    idx = [d['cams'][cam]['item'][3] for d in decisions]
    return [b - a for a, b in zip(idx, idx[1:])]


def _rep_skip(steps, step=1):
    rep = sum(1 for x in steps if x <= 0)
    skip = sum(x - step for x in steps if x > step)
    return rep, skip


def _excess_pct(decisions, cam, hz, fps, skip_first=0):
    steps = _steps(decisions[skip_first:], cam)
    n = len(steps)
    rep, skip = _rep_skip(steps)
    honest_rep = n * max(0.0, 1.0 - hz / fps)
    honest_skip = n * max(0.0, hz / fps - 1.0)
    return 100.0 * (rep + skip - honest_rep - honest_skip) / n


def _simple_run(hz, fps=30.0, dur=60.0, *, phase_frac=0.0, jit=0.004, seed=1, **kw):
    cams = {'c': _cam_events(hz, dur + 1, phase_frac=phase_frac, jit=jit, seed=seed, fps=fps)}
    return _run(fps, cams, _joint_events(dur + 1, seed=seed + 10),
                _joint_events(dur + 1, stamped=False, seed=seed + 20), dur, seed=seed + 30, **kw)


# ── stdlib-only fence ────────────────────────────────────────────────────────

def test_module_is_stdlib_only():
    tree = ast.parse(open(ct.__file__, encoding='utf-8').read())
    top = set()
    lazy = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or '').split('.')[0])
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node not in tree.body:
            lazy.add(node.module)
    assert top <= {'__future__', 'collections', 'importlib', 'math', 'os', 'threading',
                   'typing'}, top
    # the one in-package import is the German sentence module, lazily
    assert lazy <= {'physical_ai_server.data_processing'}, lazy


def test_contract_constants():
    S = ct.SlotSampler
    assert (S.ALPHA, S.HYST, S.LOCK_TOL, S.RATE_FRAMES, S.MARGIN_S, S.MAX_SLOTS_PER_TICK,
            S.CATCHUP_MAX_S, S.LEADER_BURST_FRAC) == (0.05, 0.25, 0.06, 61, 0.10, 2, 0.25, 0.25)
    assert ct.CAMERA_HISTORY_LEN == 64 and ct.JOINT_HISTORY_LEN == 256
    assert ct.ClockMap.WINDOW_S == 2.0 and ct.ClockMap.MAX_LAG_S == 0.5
    assert ct.SOURCE_GAP_S == signal_status.SOURCE_GAP_S == 0.25


# ── ClockMap ─────────────────────────────────────────────────────────────────

def test_clockmap_absorbs_a_constant_offset_and_arrival_jitter():
    rnd = random.Random(5)
    cm = ct.ClockMap()
    errs = []
    for i in range(300):
        cap = 10.0 + i / 30.0
        arr = cap + 0.015 + rnd.uniform(0.0, 0.002)        # 2 ms jitter
        t = cm.map(arr, cap + 1234.5)                     # another clock domain
        if i >= 30:
            errs.append(t - cap)
    # t = capture + the minimum latency: constant, not jittering with the arrival
    assert max(errs) - min(errs) < 0.0005
    assert 0.0149 <= min(errs) <= 0.0156


def test_clockmap_a_200ms_late_callback_keeps_its_time():
    cm = ct.ClockMap()
    for i in range(60):
        cm.map(10.0 + i / 30 + 0.015, 10.0 + i / 30)
    cap = 10.0 + 60 / 30
    t_late = cm.map(cap + 0.015 + 0.200, cap)
    assert t_late == pytest.approx(cap + 0.015, abs=1e-9)


def test_clockmap_stamp_zero_or_none_is_arrival():
    cm = ct.ClockMap()
    assert cm.map(5.0, 0) == 5.0
    assert cm.map(5.1, None) == 5.1
    assert cm.map(5.2, float('nan')) == 5.2


def test_clockmap_forward_step_is_adopted_and_backward_step_is_bounded():
    cm = ct.ClockMap()
    out = []
    arrivals = []
    for i in range(240):
        cap = 10.0 + i / 30
        stamp = cap
        if 60 <= i < 120:
            stamp = cap + 7.0            # forward step of the source clock
        elif i >= 120:
            stamp = cap - 7.0            # backward step
        arr = cap + 0.015
        out.append(cm.map(arr, stamp))
        arrivals.append(arr)
    assert all(b >= a for a, b in zip(out, out[1:]))                    # monotone
    assert all(arr - ct.ClockMap.MAX_LAG_S - 1e-9 <= t <= arr + 1e-9
               for t, arr in zip(out, arrivals))                        # clamped
    # the forward step is adopted at once (still capture + latency)
    assert out[61] == pytest.approx(arrivals[61] - 7.0 + 7.0, abs=0.02)
    # the backward step is forgotten after the window: back on the arrival track
    assert out[239] == pytest.approx(arrivals[239], abs=0.002)


def test_clockmap_is_monotone_under_random_input():
    rnd = random.Random(11)
    cm = ct.ClockMap()
    last = -math.inf
    arr = 0.0
    for _ in range(2000):
        arr += rnd.uniform(0.0, 0.05)
        t = cm.map(arr, rnd.choice([0.0, arr - rnd.uniform(-1, 1), arr + 100]))
        assert t >= last
        assert arr - 0.5 - 1e-9 <= t <= arr + 1e-9
        last = t


def test_source_history_seq_survives_clear():
    h = ct.SourceHistory(4)
    for i in range(6):
        h.append(float(i), 0.0, i)
    snap = h.snapshot()
    assert [it[0] for it in snap] == [3, 4, 5, 6]          # ring of 4
    assert [it[3] for it in snap] == [2, 3, 4, 5]
    h.clear()
    assert h.snapshot() == [] and len(h) == 0
    assert h.append(7.0, 0.0, 'x') == 7                     # seq keeps counting


# ── SlotSampler: aliasing and the phase predictor ────────────────────────────

def _delivery_jitter_events(hz, dur, *, phase, stamp_jit, arrival_jit, seed, lat=0.015):
    """Capture exactly periodic, the stamp read with ``stamp_jit`` (gauss), the
    delivery = capture + latency + uniform(0, ``arrival_jit``), in order."""
    rnd = random.Random(seed)
    out = []
    prev = -1.0
    i = 0
    while True:
        tc = phase + i / hz
        if tc > dur:
            break
        arr = max(tc + lat + rnd.uniform(0.0, arrival_jit), prev + 1e-4)
        prev = arr
        out.append((arr, tc + rnd.gauss(0.0, stamp_jit) + 3.217, i, tc))
        i += 1
    return out


def _fixed_boundary_reference(events, fps, dur):
    """HEAD-like: the newest frame that ARRIVED by G_k + T/2 (no clock map, no
    phase lock). Returns its repeat+skip %."""
    T = 1.0 / fps
    chosen = []
    j = 0
    best = None
    for k in range(1, int(dur * fps) - 1):
        g = k * T
        while j < len(events) and events[j][0] <= g + T / 2:
            best = events[j][2]
            j += 1
        chosen.append(best)
    steps = [b - a for a, b in zip(chosen, chosen[1:]) if a is not None and b is not None]
    rep, skip = _rep_skip(steps)
    return 100.0 * (rep + skip) / len(steps)


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_30hz_at_the_worst_phase_with_8ms_jitter_never_aliases(seed):
    # 8 ms of delivery jitter (network, executor) and the arrivals centred on a
    # fixed half-slot boundary: the reference aliases about half the slots, the
    # clock map + phase lock none (the anchor slots are the provisional-lock
    # test's business)
    fps = 30.0
    dur = 60.0
    jit = 0.008
    phase = (0.5 / fps - 0.015 - jit / 2) % (1.0 / fps)
    events = _delivery_jitter_events(30.0, dur + 1, phase=phase, stamp_jit=0.001,
                                     arrival_jit=jit, seed=seed)
    ref = _fixed_boundary_reference(events, fps, dur)
    assert ref > 5.0, ref                  # the reference DOES alias here
    decisions, _s = _run(fps, {'c': events}, _joint_events(dur + 1),
                         _joint_events(dur + 1, stamped=False, seed=9), dur)
    assert len(decisions) >= 1790
    rep, skip = _rep_skip(_steps(decisions, 'c')[3:])
    assert (rep, skip) == (0, 0)


@pytest.mark.parametrize('hz', [29.55, 29.7, 29.85, 30.15, 30.3, 30.45])
def test_cameras_1_5_percent_off_the_fps_add_no_excess(hz):
    # second-order phase tracking: without the predictor these produced up to
    # 11 % excess repeat+skip (spec T3); the budget is 0.2 % at every phase. The
    # first second (before the camera's rate is known, the session's warm-up)
    # is the provisional lock's business, not the predictor's.
    worst = []
    for p in range(10):
        decisions, _s = _simple_run(hz, phase_frac=p / 10.0, seed=100 + p)
        assert len(decisions) >= 1790
        worst.append(_excess_pct(decisions, 'c', hz, 30.0, skip_first=30))
    assert max(worst) <= 0.2, worst


def test_25hz_at_30fps_repeats_honestly():
    # an unlocked camera (25/30 is 6 % off any integer ratio): honest 16.67 %
    # repeats; the fixed half-slot boundary adds a few jitter pairs (spec T4:
    # 17.02 % / 0.35 % over 6 seeds x 60 s)
    reps, skips = [], []
    for seed in range(4):
        decisions, _s = _simple_run(25.0, jit=0.003, seed=21 + seed)
        steps = _steps(decisions, 'c')
        rep, skip = _rep_skip(steps)
        reps.append(100.0 * rep / len(steps))
        skips.append(100.0 * skip / len(steps))
    assert st.mean(reps) == pytest.approx(16.7, abs=0.5), reps
    assert st.mean(skips) <= 0.5, skips


def test_26hz_at_25fps_locks():
    decisions, s = _simple_run(26.0, fps=25.0, seed=22)
    assert s.locked['c'] is True
    assert _excess_pct(decisions, 'c', 26.0, 25.0) <= 0.5


def test_30hz_at_15fps_takes_every_second_frame():
    decisions, s = _simple_run(30.0, fps=15.0, seed=23)
    steps = _steps(decisions, 'c')
    assert len(steps) >= 880
    assert set(steps[10:]) == {2}
    assert decisions[-1]['cams']['c']['step'] == 2


def test_two_independent_cameras_spread_is_their_phase_difference():
    fps = 30.0
    dur = 40.0
    jit = 0.003
    cams = {'gripper': _cam_events(30.0, dur + 1, phase_frac=0.1, jit=jit, seed=31),
            'scene': _cam_events(29.97, dur + 1, phase_frac=0.6, jit=jit, seed=32)}
    decisions, _s = _run(fps, cams, _joint_events(dur + 1), _joint_events(dur + 1, stamped=False),
                         dur)
    spreads = sorted(abs(d['cams']['gripper']['item'][2] - d['cams']['scene']['item'][2])
                     for d in decisions)
    T = 1.0 / fps
    # the spread is the cameras' phase difference, never bought down with a
    # stale frame (F1/C2-A): within one period + 3 sigma for 95 % of the frames;
    # while a drifting camera sits in its hysteresis band the pair can be up to
    # (1 + 2 HYST) periods apart, never more
    assert spreads[int(0.95 * len(spreads))] <= T + 3 * jit
    assert spreads[-1] <= (1 + 2 * ct.SlotSampler.HYST) * T + 6 * jit
    for cam, hz in (('gripper', 30.0), ('scene', 29.97)):
        assert _excess_pct(decisions, cam, hz, fps) <= 0.3


def test_a_300ms_stall_bunching_every_arrival_changes_no_decision():
    fps = 30.0
    dur = 12.0
    cam = _cam_events(30.0, dur + 1, phase_frac=0.3, jit=0.003, seed=41)
    fol = _joint_events(dur + 1, seed=42)
    lea = _joint_events(dur + 1, stamped=False, seed=43)
    clean, _ = _run(fps, {'c': cam}, fol, lea, dur, tick_jit=0.0)
    a, b = 6.0, 6.3
    stalled, s2 = _run(fps, {'c': _apply_stall(cam, a, b)}, _apply_stall(fol, a, b),
                       _apply_stall(lea, a, b), dur, tick_jit=0.0, stall=(a, b))
    assert s2.lost == _.lost
    assert [d['k'] for d in stalled] == [d['k'] for d in clean]
    assert ([d['cams']['c']['item'][3] for d in stalled]
            == [d['cams']['c']['item'][3] for d in clean])
    assert [d['fol'][3] for d in stalled] == [d['fol'][3] for d in clean]
    # the unstamped leader is back-dated inside the burst: at most one sample off
    assert max(abs(x['lea'][3] - y['lea'][3]) for x, y in zip(stalled, clean)) <= 1


def _hist(items):
    """A history snapshot from (arrival, t, idx) triples (t used verbatim)."""
    return [(i + 1, arr, t, idx) for i, (arr, t, idx) in enumerate(items)]


def _steady(n, period, offset=0.0, lat=0.0, start=0.0):
    return [(start + offset + i * period + lat, start + offset + i * period, i) for i in range(n)]


def _warm_sampler(fps=30.0):
    s = ct.SlotSampler(fps, ['c'])
    assert s.decide_due(0.0, {'c': []}, [], []) == []        # anchors t0 = 0
    return s


def test_the_completeness_gate_waits_for_an_in_flight_frame():
    s = _warm_sampler()
    T = 1 / 30
    fol = _hist(_steady(100, 0.01))
    lea = _hist(_steady(100, 0.01))
    # frames at t = 0.01 + i*T, but frame 2 (t=0.0767) arrives 60 ms late, and
    # frame 3 after it (one stream is delivered in order)
    cam = [(0.01 + i * T + (0.06 if i >= 2 else 0.0), 0.01 + i * T, i) for i in range(4)]
    now = 2 * T + s.early + 0.01                               # slot 2 due, frame 2 in flight
    avail = [c for c in cam if c[0] <= now]
    out = []
    for _ in range(3):
        out += s.decide_due(now, {'c': _hist(avail)}, fol, lea)
    # slot 1 cannot be proven complete either: only frame 2 would prove that
    # nothing older than slot 1's boundary is still on the way
    assert [d['k'] for d in out] == [0]
    now2 = cam[2][0] + 0.001                     # frame 2 arrived: proves slot 1
    out2 = s.decide_due(now2, {'c': _hist([c for c in cam if c[0] <= now2])}, fol, lea)
    assert [d['k'] for d in out2] == [1]
    now3 = cam[3][0] + 0.001                     # frame 3 arrived: proves slot 2
    out3 = s.decide_due(now3, {'c': _hist([c for c in cam if c[0] <= now3])}, fol, lea)
    assert [d['k'] for d in out3] == [2]
    assert out3[0]['cams']['c']['item'][3] == 2  # the late frame, not a repeat
    assert not any(d['timed_out'] for d in out2 + out3)


def test_a_silent_camera_times_out_after_margin_and_repeats():
    s = _warm_sampler()
    T = 1 / 30
    fol = _hist(_steady(100, 0.01))
    lea = _hist(_steady(100, 0.01))
    cam = _hist([(0.01 + i * T, 0.01 + i * T, i) for i in range(3)])   # then silence
    got = []
    t = 0.0
    while t < 0.6:
        t += T
        got += s.decide_due(t, {'c': cam}, fol, lea)
    by_k = {d['k']: d for d in got}
    assert by_k[3]['cams']['c']['repeat'] is True
    assert by_k[3]['timed_out'] is True
    # decided no earlier than G + early + MARGIN_S
    assert by_k[3]['decided_at'] >= 3 * T + s.early + s.MARGIN_S - 1e-9


def test_catch_up_is_bounded_and_given_up_slots_are_counted():
    s = _warm_sampler()
    T = 1 / 30
    cam = _hist([(0.01 + i * T, 0.01 + i * T, i) for i in range(60)])
    fol = _hist(_steady(200, 0.01))
    lea = _hist(_steady(200, 0.01))
    out = s.decide_due(1.5, {'c': cam}, fol, lea)          # a 1.5 s pause of the tick
    assert len(out) == 2
    deadline = s.delay + s.CATCHUP_MAX_S
    expected_lost = sum(1 for k in range(100) if 1.5 - (k * T + s.delay) > s.CATCHUP_MAX_S)
    assert s.lost == expected_lost
    assert len(out[0]['lost_before']) == expected_lost
    assert out[1]['lost_before'] == []
    assert all(1.5 - (d['g'] + s.delay) <= s.CATCHUP_MAX_S for d in out)
    assert out[0]['late'] is True and deadline > 0


def test_leader_burst_is_back_dated():
    period = 0.01
    leader = _hist([(0.01 * i, 0.01 * i, i) for i in range(50)]
                   + [(0.80 + 0.0002 * j, 0.80 + 0.0002 * j, 50 + j) for j in range(10)])
    # the burst (10 samples within 2 ms) is back-dated one period per sample
    got = ct._newest_le_backdated(leader, 0.745, 1.0, ct.SlotSampler.LEADER_BURST_FRAC)
    assert got is not None
    # newest 59 keeps t = 0.8018; idx 59-j is back-dated to 0.8018 - j periods,
    # so the newest at or before 0.745 is idx 53 (0.7418)
    assert got[3] == 53
    assert got[2] == pytest.approx(0.8018 - 6 * period, abs=1e-6)
    # without back-dating every burst sample has t ~ 0.80: the newest <= 0.745 is idx 49
    assert ct._newest_le(leader, 0.745, 1.0)[3] == 49


def test_provisional_lock_at_a_bad_phase():
    # the first ~0.7 s, before the camera's rate is known, at the worst phase
    fps = 30.0
    worst = 0.5 - 0.015 * fps
    events = _cam_events(30.0, 3.0, phase_frac=worst, jit=0.006, tail=0.0, seed=51)
    decisions, _s = _run(fps, {'c': events}, _joint_events(3.0), _joint_events(3.0, stamped=False),
                         2.0)
    early = [d for d in decisions if d['g'] < 0.8]
    assert len(early) >= 20
    rep, skip = _rep_skip(_steps(early, 'c'))
    assert rep + skip <= 1


def test_one_repeat_per_drift_cycle():
    # 29.97 Hz at 30 fps drifts one frame per 33.3 s: in 70 s exactly two honest
    # repeats, never a repeat/skip flip-flop while the phase sits on the boundary
    decisions, _s = _simple_run(29.97, dur=70.0, jit=0.004, seed=61)
    rep, skip = _rep_skip(_steps(decisions, 'c'))
    assert skip == 0
    assert rep == 2


def test_a_wrapping_boundary_never_steps_back_in_time():
    for seed in range(5):
        decisions, _s = _simple_run(29.55, dur=30.0, jit=0.006, seed=70 + seed)
        assert all(x >= 0 for x in _steps(decisions, 'c'))


def test_no_leader_session_and_empty_histories():
    s = ct.SlotSampler(30.0, ['c'])
    s.decide_due(0.0, {}, [], None)
    out = s.decide_due(0.5, {'c': []}, [], None)
    assert out == []
    T = 1 / 30
    cam = _hist([(0.01 + i * T, 0.01 + i * T, i) for i in range(40)])
    fol = _hist(_steady(200, 0.01))
    got = []
    t = 0.5
    while t < 1.2:
        t += T
        got += s.decide_due(t, {'c': cam}, fol, None)
    assert got and all(d['lea'] is None for d in got)


def test_fps_must_be_positive():
    with pytest.raises(ValueError):
        ct.SlotSampler(0, ['c'])


# ── event-model regression of the final module (architect's module_check) ────

_SCEN = {
    'indep': dict(fps=30, cams=[dict(f=30.0, jit=0.003), dict(f=29.97, jit=0.003)]),
    'drift': dict(fps=30, cams=[dict(f=30.02, jit=0.006), dict(f=29.98, jit=0.006)]),
    'cam25@30': dict(fps=30, cams=[dict(f=25.0, jit=0.003), dict(f=25.0, jit=0.003)]),
    'stalls': dict(fps=30, cams=[dict(f=30.0, jit=0.003), dict(f=29.97, jit=0.003)],
                   stall_every=5.0, stall_len=(0.06, 0.2)),
}


class _Src:
    def __init__(self, t, stamp, dl):
        self.t, self.stamp, self.dl, self.cb = t, stamp, dl, list(dl)
        self.drop = [False] * len(t)


def _model_cam(rnd, f, phase, jit, lat, tail, dur, offset):
    t, s, d = [], [], []
    prev = -1.0
    i = 0
    while True:
        tc = phase + i / f + max(-0.4 / f, min(0.4 / f, rnd.gauss(0, jit)))
        if tc > dur:
            break
        dl = max(tc + lat + rnd.expovariate(1.0 / tail), prev + 1e-4)
        prev = dl
        t.append(tc)
        s.append(tc + offset)
        d.append(dl)
        i += 1
    return _Src(t, s, d)


def _model_joint(rnd, dur, stamped):
    t, s, d = [], [], []
    ph = rnd.random() / 100.0
    i = 0
    while True:
        tc = ph + i / 100.0 + rnd.gauss(0, 0.0003)
        if tc > dur:
            break
        t.append(tc)
        s.append(tc if stamped else 0.0)
        d.append(tc + 0.001 + rnd.expovariate(1 / 0.0005))
        i += 1
    return _Src(t, s, d)


def _model_stalls(src, stalls, depth):
    cb = []
    burst = {}
    for dl in src.dl:
        c = dl
        for a, b in stalls:
            if a <= dl < b:
                n = burst.get(b, 0)
                burst[b] = n + 1
                c = b + 0.0002 * n
                break
        if cb and c < cb[-1]:
            c = cb[-1] + 1e-5
        cb.append(c)
    for a, b in stalls:
        idx = [i for i, dl in enumerate(src.dl) if a <= dl < b]
        for i in idx[:-depth] if len(idx) > depth else ():
            src.drop[i] = True
    src.cb = cb


def _stalled_until(t, stalls):
    for a, b in stalls:
        if a <= t < b:
            return b
    return t


def _model_run(scn, seed, dur=20.0):
    sc = _SCEN[scn]
    rnd = random.Random(seed)
    fps = sc['fps']
    T = 1.0 / fps
    cams = [_model_cam(rnd, c['f'], rnd.random() / c['f'], c['jit'], 0.015, 0.002, dur, 3.217)
            for c in sc['cams']]
    fol = _model_joint(rnd, dur, True)
    lea = _model_joint(rnd, dur, False)
    stalls = []
    if sc.get('stall_every'):
        t = rnd.expovariate(1 / sc['stall_every'])
        while t < dur:
            d = rnd.uniform(*sc['stall_len'])
            stalls.append((t, t + d))
            t += d + rnd.expovariate(1 / sc['stall_every'])
    for src in cams + [fol, lea]:
        _model_stalls(src, stalls, 32)
    ev = []
    for ci, c in enumerate(cams):
        ev += [(c.cb[i], ci, i) for i in range(len(c.t)) if not c.drop[i]]
    for kind, src in (('f', fol), ('l', lea)):
        ev += [(src.cb[i], kind, i) for i in range(len(src.t)) if not src.drop[i]]
    ev.sort(key=lambda e: e[0])
    names = [f'c{i}' for i in range(len(cams))]
    hist = {n: ct.SourceHistory(ct.CAMERA_HISTORY_LEN) for n in names}
    hf, hl = ct.SourceHistory(ct.JOINT_HISTORY_LEN), ct.SourceHistory(ct.JOINT_HISTORY_LEN)
    samp = ct.SlotSampler(fps, names)
    trnd = random.Random(7)
    frames = []
    ei = 0
    t = 0.5
    busy = 0.0
    k = 0
    while t < dur - 0.5:
        E = _stalled_until(max(t, busy), stalls)
        while ei < len(ev) and ev[ei][0] <= E:
            cb, kind, i = ev[ei]
            ei += 1
            if kind == 'f':
                hf.append(cb, fol.stamp[i], i)
            elif kind == 'l':
                hl.append(cb, 0.0, i)
            else:
                hist[names[kind]].append(cb, cams[kind].stamp[i], i)
        dl = samp.decide_due(E, {n: h.snapshot() for n, h in hist.items()},
                             hf.snapshot(), hl.snapshot())
        for d in dl:
            frames.append({'R': d['r'], 'cams': [d['cams'][n]['item'][3] for n in names],
                           'fol': d['fol'][3], 'lea': d['lea'][3]})
        w = max(0.001, trnd.gauss(0.006, 0.002))
        busy = _stalled_until(E + w * max(1, len(dl)), stalls)
        k = max(k + 1, int(math.ceil((E - 0.5) / T)))
        t = 0.5 + k * T
    return cams, fol, lea, fps, dur, frames, samp.lost


def _model_metrics(cams, fol, lea, fps, dur, frames):
    T = 1.0 / fps
    n = len(frames)
    rep = []
    skip = []
    for c in range(len(cams)):
        exp_step = max(1, round((len(cams[c].t) / dur) / fps))
        r = s = 0
        for a, b in zip(frames, frames[1:]):
            d = b['cams'][c] - a['cams'][c]
            if d <= 0:
                r += 1
            elif d > exp_step:
                s += d - exp_step
        rep.append(100.0 * r / (n - 1))
        skip.append(100.0 * s / (n - 1))
    aa = sorted(f['R'] - lea.t[f['lea']] for f in frames)
    t0 = fol.t[frames[0]['fol']]
    err = [abs((fol.t[f['fol']] - t0) - i * T) for i, f in enumerate(frames)]
    span = fol.t[frames[-1]['fol']] - t0
    return {'n': n, 'rep%': max(rep), 'skip%': max(skip),
            'act_age_p99_ms': 1000 * aa[int(0.99 * n)],
            'time_err_max_ms': 1000 * max(err),
            'compress%': 100.0 * (1 - (n - 1) * T / span)}


# Thresholds: spec T4 (6 seeds x 60 s) plus margin for 20 s runs (1 event = 0.17 %).
_MODEL_LIMITS = {
    'indep': {'rep%': (0.0, 0.5), 'skip%': (0.0, 0.5)},
    'drift': {'rep%': (0.0, 1.2), 'skip%': (0.0, 1.2)},
    'stalls': {'rep%': (0.0, 0.5), 'skip%': (0.0, 0.5), 'act_age_p99_ms': (0.0, 30.0)},
    'cam25@30': {'rep%': (16.0, 18.0), 'skip%': (0.0, 1.0)},
}


@pytest.mark.parametrize('scn', sorted(_MODEL_LIMITS))
@pytest.mark.parametrize('seed', [0, 1])
def test_event_model_regression(scn, seed):
    cams, fol, lea, fps, dur, frames, lost = _model_run(scn, seed)
    m = _model_metrics(cams, fol, lea, fps, dur, frames)
    assert lost == 0
    assert m['n'] >= int((dur - 1.2) * fps)
    assert m['time_err_max_ms'] <= 70.0, m
    assert abs(m['compress%']) <= 0.2, m
    for key, (lo, hi) in _MODEL_LIMITS[scn].items():
        assert lo <= m[key] <= hi, (key, m)


# ── TakeIntegrity ─────────────────────────────────────────────────────────────

def _decisions_for_take(hz=30.0, fps=30.0, dur=10.0, seed=81, cam_gap=None, leader_gap=None,
                        stall=None):
    cam = _cam_events(hz, dur + 1, phase_frac=0.3, jit=0.003, seed=seed, fps=fps)
    if cam_gap is not None:
        cam = [e for e in cam if not (cam_gap[0] <= e[3] < cam_gap[1])]
    fol = _joint_events(dur + 1, seed=seed + 1)
    lea = _joint_events(dur + 1, stamped=False, seed=seed + 2)
    if leader_gap is not None:
        lea = [e for e in lea if not (leader_gap[0] <= e[3] < leader_gap[1])]
    if stall is not None:
        cam = _apply_stall(cam, *stall)
        fol = _apply_stall(fol, *stall)
        lea = _apply_stall(lea, *stall)
    return _run(fps, {'scene': cam}, fol, lea, dur, tick_jit=0.001, stall=stall)


def _integrity(decisions, fps=30.0, start=1.0, n=None):
    ti = ct.TakeIntegrity(['scene'], fps)
    ti.reset(start_mono=start)
    take = [d for d in decisions if d['g'] >= start]
    if n is not None:
        take = take[:n]
    for d in take:
        ti.add(d)
    return ti


def test_a_clean_take_has_no_warning_and_no_gap():
    decisions, _s = _decisions_for_take()
    ti = _integrity(decisions, n=240)
    assert ti.frames == 240
    assert ti.lost == 0
    assert ti.repeats['scene'] == 0 and ti.skips['scene'] == 0
    assert ti.rate_hz('scene') == pytest.approx(30.0, abs=0.1)
    assert ti.gap_source() is None
    assert max(ti.max_gap_s.values()) < 0.06
    assert ti.warning_de(3) == ''
    log = ti.as_log_dict()
    json.dumps(log)
    assert log['frames'] == 240 and log['cameras']['scene']['rep'] == 0
    assert set(log['max_gap_s']) == {'camera:scene', 'follower', 'leader'}


def test_a_camera_gap_of_0_3s_makes_a_redo_and_0_2s_does_not():
    decisions, _s = _decisions_for_take(cam_gap=(4.0, 4.3))
    ti = _integrity(decisions, n=240)
    assert ti.gap_source() == ('camera', 'scene')
    assert ti.max_gap_s['camera:scene'] >= 0.3
    decisions, _s = _decisions_for_take(cam_gap=(4.0, 4.2))
    ti = _integrity(decisions, n=240)
    assert ti.max_gap_s['camera:scene'] == pytest.approx(0.2 + 1 / 30, abs=0.01)
    assert ti.gap_source() is None


def test_a_source_that_dies_before_the_end_shows_an_open_gap():
    decisions, _s = _decisions_for_take(cam_gap=(8.6, 99.0))
    ti = _integrity(decisions, n=240)              # the take ends ~9.0 s
    assert ti.gap_source() == ('camera', 'scene')


def test_a_real_leader_dropout_counts_and_a_server_stall_does_not():
    decisions, _s = _decisions_for_take(leader_gap=(5.0, 5.35))
    ti = _integrity(decisions, n=240)
    assert ti.gap_source() == ('leader', None)
    # a 0.35 s stall of the whole process: every arrival bunches afterwards
    decisions, _s = _decisions_for_take(stall=(5.0, 5.35))
    ti = _integrity(decisions, n=240)
    assert ti.gap_source() is None, ti.as_log_dict()


def test_gaps_are_clipped_to_the_take_start():
    decisions, _s = _decisions_for_take(cam_gap=(0.5, 1.2))
    ti = _integrity(decisions, start=1.15, n=200)
    assert ti.max_gap_s['camera:scene'] < 0.12
    assert ti.gap_source() is None


def test_lost_slots_warn_in_german():
    decisions, s = _decisions_for_take(dur=12.0, stall=(5.0, 5.9))
    assert s.lost > 0
    ti = _integrity(decisions, n=300)
    assert ti.lost == s.lost
    text = ti.warning_de(4)
    assert text.startswith('Episode 4: Der Rechner kam nicht hinterher, ')
    assert f'{ti.lost} von {ti.frames + ti.lost} Bildern fehlen' in text


def test_excess_repeats_warn_only_beyond_the_cameras_honest_rate():
    # 25 Hz at 30 fps: 16.7 % repeats are honest, no warning
    decisions, _s = _decisions_for_take(hz=25.0)
    ti = _integrity(decisions, n=240)
    assert ti.repeats['scene'] >= 35
    assert abs(ti.excess_repeats('scene')) < 3
    assert ti.warning_de(1) == ''
    # a camera that repeats beyond its measured rate (frames vanish while the
    # rate is unchanged) warns with the camera's German name
    ti = ct.TakeIntegrity(['scene'], 30.0)
    ti.reset(start_mono=0.0)
    seq = 0
    for k in range(100):
        if k % 10 != 0:
            seq += 1
        it = (seq, k / 30, k / 30, None)
        ti.add({'k': k, 'g': k / 30, 'r': k / 30,
                'cams': {'scene': {'item': it, 'rate': 30.0, 'step': 1}},
                'fol': (k, k / 30, k / 30, None), 'lea': (k, k / 30, k / 30, None),
                'gaps': {}, 'lost_before': []})
    assert ti.warning_de(2) == ('Episode 2: Die Szenen-Kamera hat zu wenige Bilder geliefert, '
                                '9 % der Bilder sind Wiederholungen. Mehr Licht hilft oft.')


def test_late_arm_data_warns():
    ti = ct.TakeIntegrity(['gripper'], 30.0)
    ti.reset(start_mono=0.0)
    for k in range(30):
        it = (k + 1, k / 30, k / 30, None)
        lea_t = k / 30 - (0.15 if k == 12 else 0.01)
        ti.add({'k': k, 'g': k / 30, 'r': k / 30,
                'cams': {'gripper': {'item': it, 'rate': 30.0, 'step': 1}},
                'fol': (k, k / 30, k / 30 - 0.005, None), 'lea': (k, k / 30, lea_t, None),
                'gaps': {}, 'lost_before': []})
    assert ti.warning_de(5) == ('Episode 5: Die Armdaten kamen zeitweise verspätet an '
                                '(bis 150 ms). Die Episode wurde gespeichert; nimm sie neu '
                                'auf, wenn sie wichtig ist.')


def test_integrity_without_a_start_ignores_what_came_before_the_first_frame():
    decisions, _s = _decisions_for_take(cam_gap=(0.5, 1.2))
    ti = ct.TakeIntegrity(['scene'], 30.0)
    first = next(i for i, d in enumerate(decisions) if d['g'] >= 1.3)
    for d in decisions[first:first + 100]:
        ti.add(d)
    assert ti.gap_source() is None
    assert ti.lost == 0
