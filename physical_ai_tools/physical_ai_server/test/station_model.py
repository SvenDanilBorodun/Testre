"""Breadth-first model checker of the station commission protocol (plan v6.13 §6, §12.1).

It drives the REAL ``physical_ai_server/station/commission_worker.py`` class. Only
the world around it is a model:

* a fake arm driver: a FIFO of torque requests ``(kind, hold, owner, live)``,
  served in order whenever; a call that timed out leaves its request in the FIFO
  (orphaned), so a "failed" torque call can still be applied later, exactly as the
  OMX driver's ``set_dxl_torque_srv_callback`` records a request and the next
  hardware ``write()`` applies it; in-flight requests survive a server respawn;
* the server's torque switch ``_set_follower_torque`` with D15 (an unconfirmed
  torque-off turns ``_follower_torque_on`` from True into None) and the
  measured-pose hold (published when the flag is not True at send time);
* the manual arbiter's hand-guide: ``hand_guide(true)`` / a Vormachen record start
  (claim under ``_mode_lock``, wait for ``_manual_lock``, D16 re-check, torque
  off) and ``hand_guide(false)`` (ungated, re-torque ≤ 3 attempts), as two actors
  that can run at the same time, as on the reentrant ``_preempt_cb_group``;
* the manual idle watchdog, a workflow start (torque assert), a server respawn
  (flag None, every in-memory claim gone, the arm and the FIFO kept);
* the station session (enter / end / takeover / re-enter) and the owner's commands;
* a station job that RAISES (budget ``exc``): before a step changes anything,
  before its effect sends anything, or when a torque call it made returns (the
  call confirmed, unavailable or timed out, its request possibly still applied
  later); the service then runs the worker's catch-all ``fail_running``.

Invariants (plan §6), checked in every reachable state:

* INV1 ``on_calibration`` is never released while the follower may be limp;
* INV2 the follower is never limp while the station holds ``on_calibration`` and no
  touch is active;
* INV3 liveness: from every state a clean state (no claim, no record) is reachable
  without a respawn;
* INV4 no new commission record while an old commission's job is pending;
* INV5 every torque-on over an arm that may be limp publishes the hold first.

The checker is parameterised so a test can run it against a mutated copy of the
worker module (source-level mutation of one clause) and against fake-server
variants without D15 or D16.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections import deque
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
WORKER_PATH = _PKG / 'station' / 'commission_worker.py'
TEXTS_PATH = _PKG / 'station' / 'texts_de.py'


def _load_texts():
    from physical_ai_server.station import texts_de
    return texts_de


def load_worker_module(source: str | None = None, tag: str = 'real'):
    """The worker module, from its file or from a (mutated) source text."""
    _load_texts()
    text = WORKER_PATH.read_text(encoding='utf-8') if source is None else source
    mod = types.ModuleType(f'station_commission_worker_{tag}')
    mod.__file__ = str(WORKER_PATH)
    exec(compile(text, str(WORKER_PATH), 'exec'), mod.__dict__)  # noqa: S102
    return mod


# ── world tuple ──────────────────────────────────────────────────────────────
(ARM, FIFO, FLAG, TLOCK, CAL, MAN, WF, SESS, WKC, HT, HF, PF, WD, BUD,
 MLOCK, WORKER) = range(16)
# budgets
(B_FAIL, B_ABORT, B_BEGIN, B_TB, B_HT, B_HF, B_WF, B_WD, B_RESP, B_COMMIT,
 B_RS, B_SID, B_CID, B_SOLVE, B_EXC) = range(15)
_BKEYS = ('fail', 'abort', 'begin', 'tb', 'ht', 'hf', 'wf', 'wd', 'resp',
          'commit', 'rs', 'sid', 'cid', 'solve', 'exc')
FLAG_FALSE, FLAG_TRUE, FLAG_NONE = 0, 1, 2

DEFAULT_CONFIG = dict(
    D15=True,          # the fake torque switch applies D15
    D16=True,          # hand_guide(true) re-checks its claim after _manual_lock
    ATT=3,             # attempts of _retorque_follower_or_keep_locked / a claim re-lock
    INIT_ARM=(1, 0),   # boot: arm locked or left limp by a previous server; flag None
    budgets=dict(fail=2, abort=2, begin=2, tb=2, ht=0, hf=0, wf=0, wd=0, resp=0,
                 commit=1, rs=0, sid=2, cid=9, solve=2, exc=0),
    stop_on_violation=False,
    max_states=3_000_000,
)


class Env:
    """The node's view as the worker sees it (under _mode_lock → _station_lock)."""

    __slots__ = ('L', 'chk')

    def __init__(self, L, chk):
        self.L = L
        self.chk = chk

    def now(self):
        return 0.0

    def modes_idle(self):
        L = self.L
        return L[CAL] == 0 and L[WF] == 0 and L[MAN] == 0

    def session_active(self, sid):
        return bool(sid) and self.L[SESS] == sid

    def torque_is_true(self):
        return self.L[FLAG] == FLAG_TRUE

    def claim_calibration(self):
        # _assert_no_other_active('calibration'): nothing else the model runs
        # (recording, inference, collision) can hold the arm here.
        self.L[CAL] = 1
        return True

    def release_calibration(self):
        L = self.L
        if L[ARM] == 0 or any(r[0] == 0 for r in L[FIFO]):
            self.chk.note('INV1_release_maybe_limp', tuple(L))
        L[CAL] = 0

    def new_commission_id(self):
        L = self.L
        b = list(L[BUD])
        b[B_CID] += 1
        L[BUD] = tuple(b)
        return b[B_CID]

    def finish_work(self, kind, ok, payload):
        return None

    def commit_swap(self, commission_id, payload):
        return None


class Checker:
    def __init__(self, worker_module=None, **config):
        cfg = dict(DEFAULT_CONFIG)
        budgets = dict(DEFAULT_CONFIG['budgets'])
        budgets.update(config.pop('budgets', {}))
        cfg.update(config)
        cfg['budgets'] = budgets
        self.c = cfg
        self.bmax = tuple(budgets[k] for k in _BKEYS)
        self.wm = worker_module or load_worker_module()
        self.w = self.wm.CommissionWorker(job_ids=lambda: 0)
        self.fresh = self.w.snapshot()
        self.viol = {}
        self.first = {}
        self.cur = None

    # ── bookkeeping ───────────────────────────────────────────────────────────
    def note(self, name, state):
        cnt, ex = self.viol.get(name, (0, None))
        self.viol[name] = (cnt + 1, ex if ex is not None else state)
        if name not in self.first and self.cur is not None:
            self.first[name] = self.cur

    def ok(self, L, i):
        return L[BUD][i] < self.bmax[i]

    @staticmethod
    def inc(L, i):
        b = list(L[BUD])
        b[i] += 1
        L[BUD] = tuple(b)

    def worker(self, s):
        self.w.restore(s[WORKER])
        return self.w

    def initial(self):
        bud = (0,) * len(_BKEYS)
        return [(arm, (), FLAG_NONE, None, 0, 0, 0, 0, None, None, None, None,
                 None, bud, None, self.fresh) for arm in self.c['INIT_ARM']]

    def check_state(self, s):
        if s[CAL]:
            rec = s[WORKER][0]
            if not (rec is not None and rec.touch_active) and s[ARM] == 0:
                self.note('INV2_limp_without_touch', s)

    # ── torque calls ──────────────────────────────────────────────────────────
    OWN = {WKC: 'W', HT: 'T', HF: 'F', PF: 'P', WD: 'D'}
    SLOT = {'W': WKC, 'T': HT, 'F': HF, 'P': PF, 'D': WD}

    def apply_unconfirmed(self, L, kind):
        if kind == 0 and self.c['D15'] and L[FLAG] == FLAG_TRUE:
            L[FLAG] = FLAG_NONE

    def call_events(self, s, slot, out):
        a = s[slot]
        if a is None or a[-1] is None:
            return
        call = a[-1]
        owner = self.OWN[slot]
        if call[0] == 'A':
            if s[TLOCK] is not None:
                return
            kind = call[1]
            L = list(s)
            hold = 1 if (kind == 1 and s[FLAG] != FLAG_TRUE) else 0
            L[FIFO] = s[FIFO] + ((kind, hold, owner, 1),)
            L[TLOCK] = owner
            L[slot] = a[:-1] + (('W', kind, 0),)
            out.append((f'send{owner}{kind}', tuple(L)))
            if self.ok(s, B_FAIL):
                L = list(s)
                self.inc(L, B_FAIL)
                self.apply_unconfirmed(L, kind)
                L[slot] = a[:-1] + (None,)
                self.cont(L, slot, False)
                out.append((f'unavail{owner}{kind}', tuple(L)))
                if slot == WKC and self.ok(s, B_EXC):
                    L = list(s)
                    self.inc(L, B_FAIL)
                    self.apply_unconfirmed(L, kind)
                    L[slot] = None
                    self.job_raised(L, out)
        else:
            kind, served = call[1], call[2]
            if served:
                L = list(s)
                L[FLAG] = FLAG_TRUE if kind == 1 else FLAG_FALSE
                L[TLOCK] = None
                L[slot] = a[:-1] + (None,)
                self.cont(L, slot, True)
                out.append((f'confirm{owner}{kind}', tuple(L)))
                if slot == WKC and self.ok(s, B_EXC):
                    L = list(s)
                    L[FLAG] = FLAG_TRUE if kind == 1 else FLAG_FALSE
                    L[TLOCK] = None
                    L[slot] = None
                    self.job_raised(L, out)
            if self.ok(s, B_FAIL):
                L = list(s)
                self.inc(L, B_FAIL)
                L[FIFO] = tuple((r[0], r[1], r[2], 0) if (r[2] == owner and r[3]) else r
                                for r in s[FIFO])
                L[TLOCK] = None
                self.apply_unconfirmed(L, kind)
                L[slot] = a[:-1] + (None,)
                self.cont(L, slot, False)
                out.append((f'timeout{owner}{kind}', tuple(L)))
                if slot == WKC and self.ok(s, B_EXC):
                    L = list(s)
                    self.inc(L, B_FAIL)
                    L[FIFO] = tuple((r[0], r[1], r[2], 0) if (r[2] == owner and r[3]) else r
                                    for r in s[FIFO])
                    L[TLOCK] = None
                    self.apply_unconfirmed(L, kind)
                    L[slot] = None
                    self.job_raised(L, out)

    def driver_events(self, s, out):
        if not s[FIFO]:
            return
        kind, hold, owner, live = s[FIFO][0]
        L = list(s)
        L[FIFO] = s[FIFO][1:]
        if kind == 1:
            if s[ARM] == 0 and not hold:
                self.note('INV5_snap', s)
            L[ARM] = 1
        else:
            L[ARM] = 0
        if live:
            slot = self.SLOT[owner]
            a = L[slot]
            L[slot] = a[:-1] + (('W', a[-1][1], 1),)
        out.append((f'serve{owner}{kind}', tuple(L)))

    def cont(self, L, slot, ok):
        if slot == WKC:
            self.wk_result(L, ok)
        elif slot == HT:
            pc, was, _ = L[HT]
            if not ok:
                L[MAN] = was
            L[MLOCK] = None
            L[HT] = None
        elif slot == HF:
            self.hf_result(L, ok)
        elif slot == PF:
            if ok:
                L[PF] = ('run', None)
            else:
                L[WF] = 0
                L[PF] = None
        elif slot == WD:
            pc, fresh, _ = L[WD]
            if ok and not fresh:
                L[MAN] = 0
            L[MLOCK] = None
            L[WD] = None

    # ── the worker thread around the real class ───────────────────────────────
    def _deliver(self, L, ok):
        w = self.worker(L)
        w.deliver(ok, '' if ok else 'driver')
        L[WORKER] = w.snapshot()

    def wk_result(self, L, ok):
        kind, n, _ = L[WKC]
        if kind == 'rt':
            if ok:
                L[WKC] = None
                self._deliver(L, True)
            elif n < self.c['ATT']:
                L[WKC] = ('rt', n + 1, ('A', 1))
            else:
                L[WKC] = None
                self._deliver(L, False)
        else:
            L[WKC] = None
            self._deliver(L, ok)

    def job_raised(self, L, out):
        """The running job raised: the service's catch-all
        (``StationService._fail_running_job``) ends it through the worker's
        ``fail_running`` under the locks."""
        self.inc(L, B_EXC)
        w = self.worker(L)
        run = w.running
        label = 'exc_' + run.job.kind + '_' + run.pc
        w.fail_running(Env(L, self), 'internal')
        L[WORKER] = w.snapshot()
        out.append((label, tuple(L)))

    def worker_events(self, s, out):
        if s[WKC] is not None:
            return
        snap = s[WORKER]
        running = snap[3]
        if running is None:
            if snap[1]:
                w = self.worker(s)
                w.take_next()
                L = list(s)
                L[WORKER] = w.snapshot()
                out.append(('take_' + running_kind(snap[1][0]), tuple(L)))
            return
        if running.pc in self.wm._WAIT_NEXT:
            return  # an effect without a call is delivered in the same transition
        if self.ok(s, B_EXC):
            # The step raises before it changes anything.
            self.job_raised(list(s), out)
        w = self.worker(s)
        L = list(s)
        env = Env(L, self)
        rec_before = snap[0]
        eff = w.step(env)
        snap_after = w.snapshot()
        rec_after = snap_after[0]
        if rec_after is not None and (rec_before is None
                                      or rec_after.commission_id != rec_before.commission_id):
            pend = [j for j in snap_after[1]] + [r.job for r in snap_after[2]]
            if any(j.kind not in self.wm.CLAIM_KINDS for j in pend):
                self.note('INV4_old_job_pending', s)
        L[WORKER] = snap_after
        label = 'step_' + running.job.kind + '_' + running.pc
        if eff is None or eff.kind == self.wm.E_CLEANUP:
            out.append((label, tuple(L)))
            return
        if self.ok(s, B_EXC):
            # The effect raises before it sends anything.
            self.job_raised(list(L), out)
            w.restore(snap_after)   # job_raised rebinds the shared worker
        k = eff.kind
        if k in (self.wm.E_TORQUE_ON, self.wm.E_RETORQUE):
            if eff.args and eff.args[0] and L[FLAG] == FLAG_TRUE:
                L[FLAG] = FLAG_NONE   # mark the torque state unknown (True → None)
            L[WKC] = ('on' if k == self.wm.E_TORQUE_ON else 'rt', 1, ('A', 1))
            out.append((label, tuple(L)))
        elif k == self.wm.E_TORQUE_OFF:
            L[WKC] = ('off', 1, ('A', 0))
            out.append((label, tuple(L)))
        elif k == self.wm.E_SOLVE:
            for gates in (True, False):
                L2 = list(L)
                w.restore(L2[WORKER])
                w.deliver(True, '', (gates, 'gate' if not gates else 'ok', 'sol'))
                L2[WORKER] = w.snapshot()
                out.append((label + ('_gates_ok' if gates else '_gates_fail'), tuple(L2)))
        elif k in (self.wm.E_WRITE, self.wm.E_WORK):
            w.deliver(True, '', 'payload')
            L[WORKER] = w.snapshot()
            out.append((label, tuple(L)))
        else:
            raise AssertionError(f'unexpected effect {k}')

    # ── hand-guide actors (concurrent open and close), watchdog, workflow ──────
    def hg_events(self, s, out):
        if s[HT] is None and self.ok(s, B_HT) and s[CAL] == 0 and s[WF] == 0:
            L = list(s)
            self.inc(L, B_HT)
            L[HT] = ('ml', s[MAN], None)
            L[MAN] = 1
            self.wd_stamp(L)
            out.append(('ht_claim', tuple(L)))
        a = s[HT]
        if a is not None and a[-1] is None and a[0] == 'ml':
            L = list(s)
            if s[MLOCK] is None:
                L[MLOCK] = 'T'
                if self.c['D16'] and L[MAN] != 1:
                    L[MLOCK] = None   # D16: claim ended meanwhile → refuse, restore nothing
                    L[HT] = None
                    out.append(('ht_d16_refuse', tuple(L)))
                else:
                    L[HT] = ('off', a[1], ('A', 0))
                    out.append(('ht_ml', tuple(L)))
            else:
                L[MAN] = a[1]
                L[HT] = None
                out.append(('ht_ml_timeout', tuple(L)))
        if s[HF] is None and self.ok(s, B_HF):
            L = list(s)
            self.inc(L, B_HF)
            L[HF] = ('fml', 0, None)
            self.wd_stamp(L)
            out.append(('hf_start', tuple(L)))
        a = s[HF]
        if a is not None and a[-1] is None and a[0] == 'fml':
            L = list(s)
            if s[MLOCK] is None:
                L[MLOCK] = 'F'
                L[HF] = ('frt', 1, ('A', 1))
                out.append(('hf_ml', tuple(L)))
            else:
                L[HF] = None
                out.append(('hf_ml_timeout', tuple(L)))

    def hf_result(self, L, ok):
        _, n, _ = L[HF]
        if ok:
            L[MAN] = 0
            L[MLOCK] = None
            L[HF] = None
        elif n < self.c['ATT']:
            L[HF] = ('frt', n + 1, ('A', 1))
        else:
            L[MLOCK] = None
            L[HF] = None

    def wd_stamp(self, L):
        if L[WD] is not None:
            L[WD] = (L[WD][0], 1, L[WD][2])

    def wd_events(self, s, out):
        if (s[WD] is None and s[MAN] == 1 and s[MLOCK] is None and self.ok(s, B_WD)
                and s[HT] is None and s[HF] is None):
            L = list(s)
            self.inc(L, B_WD)
            L[MLOCK] = 'D'
            if s[FLAG] != FLAG_TRUE:
                L[WD] = ('rt', 0, ('A', 1))
            else:
                L[MAN] = 0
                L[MLOCK] = None
            out.append(('wd_fire', tuple(L)))

    def pf_events(self, s, out):
        a = s[PF]
        if a is None and self.ok(s, B_WF) and s[CAL] == 0 and s[MAN] == 0:
            L = list(s)
            self.inc(L, B_WF)
            L[WF] = 1
            L[PF] = ('start', ('A', 1))
            out.append(('wf_start', tuple(L)))
        if a is not None and a[0] == 'run':
            L = list(s)
            L[WF] = 0
            L[PF] = None
            out.append(('wf_finish', tuple(L)))

    # ── session, owner commands, timer, respawn ───────────────────────────────
    def _apply(self, s, fn, label, out, budget=None):
        """Run a worker service on a copy; keep the successor only if it changed."""
        L = list(s)
        w = self.worker(s)
        before = L[WORKER]
        res = fn(w, L)
        after = w.snapshot()
        if after == before and res is not True:
            return
        if budget is not None:
            self.inc(L, budget)
        L[WORKER] = after
        out.append((label, tuple(L)))

    def session_events(self, s, out):
        rec = s[WORKER][0]
        if s[SESS] == 0 and self.ok(s, B_SID):
            refuse = (s[CAL] != 0 or s[WF] != 0 or s[MAN] != 0
                      or (rec is not None and rec.abort_marked))
            if not refuse:
                L = list(s)
                self.inc(L, B_SID)
                L[SESS] = L[BUD][B_SID]
                out.append(('enter', tuple(L)))
        if s[SESS] != 0:
            for kind in ('end', 'takeover'):
                if kind == 'takeover' and not self.ok(s, B_SID):
                    continue
                if not self.ok(s, B_ABORT) and rec is not None and not rec.abort_marked:
                    if kind == 'takeover':
                        continue
                L = list(s)
                w = self.worker(s)
                marked = w.mark_abort() is not None
                if marked:
                    self.inc(L, B_ABORT)
                if kind == 'end':
                    L[SESS] = 0
                else:
                    self.inc(L, B_SID)
                    L[SESS] = L[BUD][B_SID]
                L[WORKER] = w.snapshot()
                out.append(('sess_' + kind, tuple(L)))

    def owner_events(self, s, out):
        sid = s[SESS]
        if sid == 0:
            return
        wm = self.wm
        idle = s[CAL] == 0 and s[WF] == 0 and s[MAN] == 0
        refusal = '' if idle else 'mode'
        if self.ok(s, B_BEGIN):
            self._apply(s, lambda w, L: w.request_claim(wm.CLAIM, sid, refusal).result == 'QUEUED',
                        'begin', out, B_BEGIN)
        if self.ok(s, B_RS):
            for kind, label in ((wm.RESOLVE, 'resolve'), (wm.RESTORE, 'restore')):
                self._apply(s, lambda w, L, k=kind: w.request_claim(k, sid, refusal).result == 'QUEUED',
                            label, out, B_RS)
        rec = s[WORKER][0]
        if rec is None:
            return
        cid = rec.commission_id
        if self.ok(s, B_TB):
            self._apply(s, lambda w, L: w.touch_begin(cid).result == 'QUEUED',
                        'touch_begin', out, B_TB)
        if not rec.taps:
            self._apply(s, lambda w, L: w.touch_capture(cid, 'tap', 0).result == 'OK',
                        'touch_capture', out)
        if self.ok(s, B_SOLVE):
            self._apply(s, lambda w, L: w.touch_solve(cid).result == 'QUEUED',
                        'touch_solve', out, B_SOLVE)
        if self.ok(s, B_COMMIT):
            # The lens, board and world gates are outside the torque protocol:
            # the model passes them together with the commit request.
            self._apply(s, lambda w, L: (w.set_ready(cid), w.commit(cid))[1].result == 'QUEUED',
                        'commit', out, B_COMMIT)
        if not rec.abort_marked and self.ok(s, B_ABORT):
            self._apply(s, lambda w, L: w.cancel(cid).result == 'QUEUED',
                        'cancel', out, B_ABORT)

    def timer_events(self, s, out):
        self._apply(s, lambda w, L: w.idle_tick(1e9) is not None, 'idle_relock', out)
        for entry in s[WORKER][2]:
            self._apply(s, lambda w, L, e=entry: w.promote_retry(e),
                        'retry_' + entry.job.kind, out)

    def respawn_events(self, s, out):
        if self.ok(s, B_RESP):
            L = list(s)
            self.inc(L, B_RESP)
            L[FLAG] = FLAG_NONE
            L[TLOCK] = None
            L[CAL] = L[MAN] = L[WF] = L[SESS] = 0
            L[WKC] = L[HT] = L[HF] = L[PF] = L[WD] = L[MLOCK] = None
            L[WORKER] = self.fresh
            L[FIFO] = tuple((r[0], r[1], r[2], 0) for r in s[FIFO])
            out.append(('RESPAWN', tuple(L)))

    def succ(self, s):
        out = []
        self.driver_events(s, out)
        for slot in (WKC, HT, HF, PF, WD):
            self.call_events(s, slot, out)
        self.worker_events(s, out)
        self.hg_events(s, out)
        self.wd_events(s, out)
        self.pf_events(s, out)
        self.session_events(s, out)
        self.owner_events(s, out)
        self.timer_events(s, out)
        self.respawn_events(s, out)
        return out

    @staticmethod
    def clean(s):
        return s[CAL] == 0 and s[WORKER][0] is None

    # ── exploration ───────────────────────────────────────────────────────────
    def run(self):
        import array
        seen = {}
        parent = array.array('i')
        label_of = []
        states = []
        frontier = deque()
        for st in self.initial():
            seen[st] = len(states)
            states.append(st)
            parent.append(-1)
            label_of.append('init')
            frontier.append(len(states) - 1)
        esrc = array.array('i')
        edst = array.array('i')
        stop = self.c['stop_on_violation']
        while frontier:
            si = frontier.popleft()
            s = states[si]
            self.cur = si
            self.check_state(s)
            for lab, t in self.succ(s):
                ti = seen.get(t)
                if ti is None:
                    ti = len(states)
                    seen[t] = ti
                    states.append(t)
                    parent.append(si)
                    label_of.append(lab)
                    frontier.append(ti)
                    if ti > self.c['max_states']:
                        raise RuntimeError('state limit reached')
                if lab != 'RESPAWN':
                    esrc.append(si)
                    edst.append(ti)
            if stop and self.viol:
                break
        self.states, self.parent, self.labels = states, parent, label_of
        self.stuck = [] if (stop and self.viol) else self._stuck(states, esrc, edst)
        return len(states)

    def _stuck(self, states, esrc, edst):
        n = len(states)
        rev = [[] for _ in range(n)]
        for a, b in zip(esrc, edst):
            rev[b].append(a)
        good = bytearray(n)
        stack = [i for i, st in enumerate(states) if self.clean(st)]
        for i in stack:
            good[i] = 1
        while stack:
            v = stack.pop()
            for u in rev[v]:
                if not good[u]:
                    good[u] = 1
                    stack.append(u)
        return [i for i in range(n) if not good[i]]

    def trace(self, i):
        out = []
        while i >= 0:
            out.append(self.labels[i])
            i = self.parent[i]
        return list(reversed(out))

    def summary(self):
        return {k: v[0] for k, v in self.viol.items()}


def running_kind(job):
    return job.kind
