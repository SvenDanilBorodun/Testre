"""The commission torque protocol (plan v6.13 §6), exhaustively, on the REAL class.

``station_model.Checker`` drives ``physical_ai_server/station/commission_worker.py``
through every reachable state of a fake world whose failed torque calls can still
be applied later, in order (the OMX driver), with a server respawn that leaves the
arm limp, concurrent hand-guide open/close actors (the reentrant
``_preempt_cb_group``; a Vormachen record start takes the same path: claim, wait
for ``_manual_lock``, D16 re-check, torque off), the idle watchdog, a workflow
start, explicit session end / takeover / re-enter, and every owner command.

Coverage is split over five configurations (a single all-actors run with four
failing torque switches is beyond a CI Python budget); together they hold the
plan's "up to 4 failing torque switches and up to 3 abort events" (CORE) and every
actor (SESS, HG, HG2, RESP). Each must-fail mutation of §12.1 is applied as ONE
source edit of the real module (or one fake-server switch for D15 / D16) and must
make the checker report a violation.
"""

from __future__ import annotations

import types

import pytest

import station_model as sm

CORE = dict(fail=4, abort=3, begin=2, tb=2, commit=1, rs=0, sid=2, solve=2)
SESS = dict(fail=3, abort=3, begin=2, tb=2, commit=1, rs=1, sid=3, solve=1)
HG = dict(fail=2, abort=1, begin=1, tb=1, commit=1, rs=1, sid=1, solve=1,
          ht=1, hf=1, wd=1)
HG2 = dict(fail=2, abort=1, begin=2, tb=0, commit=0, rs=1, sid=2, solve=0,
           ht=1, hf=1, wd=0)
RESP = dict(fail=1, abort=1, begin=1, tb=1, commit=1, rs=0, sid=1, solve=1,
            ht=1, hf=1, wd=1, wf=1, resp=1)
CONFIGS = dict(CORE=CORE, SESS=SESS, HG=HG, HG2=HG2, RESP=RESP)

# Labels each configuration must reach, so a silently pruned model cannot pass.
REACHED = {
    'CORE': {'timeoutW0', 'serveW0', 'step_RELOCK_rt_done', 'step_ABORT_rt_done',
             'step_COMMIT_written', 'cancel', 'idle_relock', 'retry_RELOCK',
             'retry_ABORT', 'step_TOUCH_SOLVE_check_gates_fail'},
    'SESS': {'sess_takeover', 'sess_end', 'step_RESOLVE_worked', 'step_RESTORE_worked',
             'enter'},
    'HG': {'ht_d16_refuse', 'hf_ml', 'wd_fire', 'step_CLAIM_claim'},
    'HG2': {'ht_d16_refuse', 'step_CLAIM_relocked'},
    'RESP': {'RESPAWN', 'wf_start', 'wd_fire'},
}


def _explore(budgets, worker_module=None, **cfg):
    checker = sm.Checker(worker_module, budgets=budgets, max_states=4_000_000, **cfg)
    n = checker.run()
    return checker, n


@pytest.mark.parametrize('name', sorted(CONFIGS))
def test_the_real_protocol_holds_every_invariant(name):
    checker, n = _explore(CONFIGS[name])
    assert checker.summary() == {}, (name, checker.summary(),
                                     [checker.trace(i) for i in checker.first.values()][:1])
    assert checker.stuck == [], (name, checker.trace(checker.stuck[0]))
    assert n > 100_000, (name, n)
    reached = set(checker.labels)
    missing = REACHED[name] - reached
    assert not missing, (name, missing)


# ── must-fail mutations (§12.1), each one clause ─────────────────────────────
SRC_MUTATIONS = {
    'failed TOUCH_OFF clears the flag': (
        [("            return self._effect('rt_wait', E_RETORQUE, True, aux=message)\n",
          "            self._set_record(touch_active=False)\n"
          "            return self._end(FAILED, message)\n")],
        'CORE', {'INV2_limp_without_touch'}),
    'ABORT without its re-torque': (
        [("            if rec.touch_active:\n"
          "                return self._effect('rt_wait', E_RETORQUE, False)\n"
          "            return self._abort_final(env, job)",
          "            if False:\n"
          "                return self._effect('rt_wait', E_RETORQUE, False)\n"
          "            return self._abort_final(env, job)")],
        'CORE', {'INV1_release_maybe_limp'}),
    'begin without its job-pending refusal': (
        [("        if self.station_job_pending():\n"
          "            return Answer('REFUSED', 0, T.JOB_PENDING_DE)",
          "        if False:\n"
          "            return Answer('REFUSED', 0, T.JOB_PENDING_DE)")],
        'CORE', {'INV4_old_job_pending'}),
    'a claim without its session check': (
        [("        return env.session_active(job.session_id)", "        return True")],
        'CORE', 'stuck'),
    'the claim hold without its is-True check': (
        [("                    and env.torque_is_true()):", "                    ):")],
        'HG', {'INV1_release_maybe_limp', 'INV2_limp_without_touch'}),
    'the COMMIT touch check together with solved := false': (
        [("                    and not rec.touch_active and not rec.abort_marked",
          "                    and not rec.abort_marked"),
         ("            solved=False, relock_owed=False)", "            relock_owed=False)")],
        'CORE', {'INV1_release_maybe_limp'}),
}


def _mutated(edits):
    src = sm.WORKER_PATH.read_text(encoding='utf-8')
    for old, new in edits:
        assert src.count(old) == 1, f'mutation anchor not unique: {old!r}'
        src = src.replace(old, new)
    return src


@pytest.mark.parametrize('label', sorted(SRC_MUTATIONS))
def test_each_source_mutation_breaks_the_protocol(label):
    edits, config, expected = SRC_MUTATIONS[label]
    module = sm.load_worker_module(_mutated(edits), tag=label.replace(' ', '_'))
    liveness = expected == 'stuck'
    checker, _ = _explore(CONFIGS[config], module, stop_on_violation=not liveness)
    if liveness:
        assert checker.stuck, label
    else:
        assert set(checker.summary()) & expected, (label, checker.summary())


@pytest.mark.parametrize('label,switch,config,expected', [
    ('an unconfirmed torque-off without D15', 'D15', 'HG',
     {'INV1_release_maybe_limp', 'INV2_limp_without_touch', 'INV5_snap'}),
    ('an unconfirmed torque-off without D15', 'D15', 'RESP',
     {'INV1_release_maybe_limp', 'INV2_limp_without_touch', 'INV5_snap'}),
    ('D16 removed, open and close concurrent', 'D16', 'HG',
     {'INV1_release_maybe_limp', 'INV2_limp_without_touch'}),
    ('D16 removed, open and close concurrent', 'D16', 'RESP',
     {'INV1_release_maybe_limp', 'INV2_limp_without_touch'}),
])
def test_each_fake_server_mutation_breaks_the_protocol(label, switch, config, expected):
    checker, _ = _explore(CONFIGS[config], stop_on_violation=True, **{switch: False})
    assert set(checker.summary()) & expected, (label, config, checker.summary())


def test_the_mutation_anchors_exist_exactly_once():
    src = sm.WORKER_PATH.read_text(encoding='utf-8')
    for edits, _, _ in SRC_MUTATIONS.values():
        for old, _ in edits:
            assert src.count(old) == 1, old


# ── named cases (§12.1), deterministic, on the real class ────────────────────
W = sm.load_worker_module()


class ScriptEnv:
    """A scripted node view: mode flags, session, torque flag, claim."""

    def __init__(self):
        self.t = 0.0
        self.session = 's1'
        self.cal = False
        self.manual = False
        self.workflow = False
        self.flag = None
        self.arm_locked = True
        self.cid = 0
        self.released = []
        self.swapped = []

    def now(self):
        return self.t

    def modes_idle(self):
        return not (self.cal or self.manual or self.workflow)

    def session_active(self, sid):
        return bool(sid) and sid == self.session

    def torque_is_true(self):
        return self.flag is True

    def claim_calibration(self):
        self.cal = True
        return True

    def release_calibration(self):
        assert self.arm_locked, 'on_calibration released over a limp arm'
        self.cal = False
        self.released.append(self.t)

    def new_commission_id(self):
        self.cid += 1
        return f'c{self.cid}'

    def finish_work(self, kind, ok, payload):
        self.swapped.append((kind, ok))

    def commit_swap(self, cid, payload):
        self.swapped.append(('commit', cid))


class Driver:
    """Runs the worker: torque effects answered from a script (True / False),
    applied to the fake arm only when confirmed (or when told 'late')."""

    def __init__(self, env, worker, script=()):
        self.env = env
        self.w = worker
        self.script = list(script)
        self.effects = []

    def _torque(self, enabled):
        answer = self.script.pop(0) if self.script else True
        if answer == 'late':          # fails now, applied by the driver later
            if enabled:
                self.env.arm_locked = True
            else:
                self.env.arm_locked = False
            if not enabled and self.env.flag is True:
                self.env.flag = None  # D15
            return False
        if answer:
            self.env.flag = bool(enabled)
            self.env.arm_locked = bool(enabled)
            return True
        if not enabled and self.env.flag is True:
            self.env.flag = None      # D15
        return False

    def run(self, solve=(True, 'ok', 'sol'), work=True, write=True):
        w = self.w
        while w.running is not None or w.queue:
            if w.running is None:
                w.take_next()
            eff = w.step(self.env)
            if eff is None:
                continue
            self.effects.append(eff.kind)
            if eff.kind == W.E_TORQUE_ON:
                if eff.args[0] and self.env.flag is True:
                    self.env.flag = None
                w.deliver(self._torque(True), 'drv')
            elif eff.kind == W.E_TORQUE_OFF:
                w.deliver(self._torque(False), 'drv')
            elif eff.kind == W.E_RETORQUE:
                if eff.args[0] and self.env.flag is True:
                    self.env.flag = None
                ok = False
                for _ in range(3):
                    if self._torque(True):
                        ok = True
                        break
                w.deliver(ok, '' if ok else 'drv')
            elif eff.kind == W.E_SOLVE:
                w.deliver(True, '', solve)
            elif eff.kind == W.E_WRITE:
                w.deliver(write, '', 'yaml')
            elif eff.kind == W.E_WORK:
                w.deliver(work, '', 'payload')


def _commission(script=()):
    env = ScriptEnv()
    worker = W.CommissionWorker()
    drv = Driver(env, worker, script)
    assert worker.request_claim(W.CLAIM, 's1').result == 'QUEUED'
    drv.run()
    assert worker.record is not None and env.cal
    return env, worker, drv


def test_fresh_boot_with_flag_none_still_commissions():
    env, worker, drv = _commission()
    assert env.flag is True              # the claim's own re-lock made it True
    assert drv.effects[0] == W.E_TORQUE_ON


def test_touch_begin_solve_touch_begin_commit_is_stale_and_releases_nothing():
    env, w, drv = _commission()
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    w.touch_capture(cid, 'tap', 0)
    w.touch_solve(cid)
    drv.run()
    assert w.record.solved
    w.touch_begin(cid)
    w.set_ready(cid)
    answer = w.commit(cid)
    drv.run()
    assert w.job_state(answer.job_id)[0] == 'stale'
    assert env.cal and w.record is not None and not env.arm_locked
    assert not w.record.solved


def test_a_failed_touch_off_applied_later_keeps_the_flag_then_relocks():
    env, w, drv = _commission(script=[True, 'late', True])
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    assert not w.record.touch_active and env.arm_locked
    assert w.job_state(w.owner_last_job('') or 0)[0] in ('none', 'failed')


def test_a_failed_touch_solve_retorque_schedules_a_relock_two_seconds_later():
    env, w, drv = _commission()
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    w.touch_capture(cid, 'tap', 0)
    drv.script = [False, False, False]
    w.touch_solve(cid)
    drv.run()
    assert w.record.touch_active and w.record.relock_owed
    assert len(w.retries) == 1 and w.retries[0].job.kind == W.RELOCK
    assert w.retries[0].due == pytest.approx(env.t + W.RETRY_DELAY_S)
    assert w.touch_capture(cid, 'tap', 1).message.startswith('Bitte den Arm loslassen')
    assert w.touch_solve(cid).message.startswith('Bitte den Arm loslassen')
    w.promote_due(env.t + 2.0)
    drv.run()
    assert not w.record.touch_active and not w.record.relock_owed and w.record.taps == ()


def test_the_idle_relock_succeeds_or_retries_and_the_owner_poll_resets_it():
    env, w, drv = _commission()
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    w.note_owner_activity(100.0)
    assert w.idle_tick(129.0) is None          # the owner's poll keeps it alive
    drv.script = [False, False, False]
    assert w.idle_tick(130.0).kind == W.RELOCK
    assert w.idle_tick(130.0) is None          # never a second RELOCK of this touch
    drv.run()
    assert w.record.touch_active and len(w.retries) == 1
    assert w.idle_tick(200.0) is None          # its retry is pending
    w.promote_due(1e9)
    drv.run()
    assert not w.record.touch_active


def test_a_stale_relock_after_touch_solve_and_a_new_touch_begin():
    """A RELOCK scoped to an older touch never acts on a newer one (rule 1's
    counter scoping). Under rule 2 the newer touch cannot start while the job is
    pending, so the stale job is built by rewinding the snapshot."""
    env, w, drv = _commission()
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    old = w.idle_tick(1e9)
    assert old is not None and w.touch_solve(cid).result == 'REFUSED'
    record, queue, retry, running, act = w.snapshot()
    # the touch was solved and a new one began meanwhile (touch_counter + 1)
    w.restore((record._replace(touch_counter=record.touch_counter + 1),
               queue, retry, running, act))
    drv.run()
    assert w.job_state(old.job_id)[0] == 'stale'
    assert w.record.touch_active and not env.arm_locked


def test_cancel_marks_the_abort_answers_the_same_job_and_is_refused_during_commit():
    env, w, drv = _commission()
    cid = w.record.commission_id
    first = w.cancel(cid)
    again = w.cancel(cid)
    assert first.result == again.result == 'QUEUED' and first.job_id == again.job_id
    drv.run()
    assert w.record is None and not env.cal
    assert w.job_state(first.job_id) == ('done', w.job_state(first.job_id)[1])
    env2, w2, drv2 = _commission()
    cid2 = w2.record.commission_id
    w2.touch_begin(cid2)
    drv2.run()
    w2.touch_capture(cid2, 'tap', 0)
    w2.touch_solve(cid2)
    drv2.run()
    w2.set_ready(cid2)
    commit = w2.commit(cid2)
    refused = w2.cancel(cid2)
    assert refused.result == 'REFUSED' and 'gespeichert' in refused.message
    drv2.run()
    assert w2.job_state(commit.job_id)[0] == 'done'   # readable after the record cleared
    assert w2.record is None and not env2.cal


def test_an_abort_during_a_queued_relock_or_commit():
    env, w, drv = _commission()
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    relock = w.idle_tick(1e9)
    abort = w.mark_abort()
    drv.run()
    assert w.job_state(relock.job_id)[0] == 'stale'
    assert w.job_state(abort.job_id)[0] == 'done' and not env.cal and env.arm_locked


def test_begin_then_session_end_before_the_claim_runs_is_stale():
    env = ScriptEnv()
    w = W.CommissionWorker()
    drv = Driver(env, w)
    job = w.request_claim(W.CLAIM, 's1')
    env.session = ''
    drv.run()
    assert w.job_state(job.job_id)[0] == 'stale'
    assert w.record is None and not env.cal and drv.effects == []


def test_a_hand_guide_opened_while_claim_is_queued_makes_it_stale_before_its_relock():
    env = ScriptEnv()
    w = W.CommissionWorker()
    drv = Driver(env, w)
    job = w.request_claim(W.CLAIM, 's1')
    env.manual = True
    drv.run()
    assert w.job_state(job.job_id)[0] == 'stale' and drv.effects == []


def test_claims_are_refused_at_the_service_while_the_arm_is_not_idle():
    w = W.CommissionWorker()
    a = w.request_claim(W.CLAIM, 's1', 'Handbetrieb ist aktiv — bitte zuerst den Handbetrieb beenden.')
    assert a.result == 'REFUSED' and a.message.startswith('Handbetrieb')
    w.request_claim(W.RESOLVE, 's1')
    b = w.request_claim(W.RESTORE, 's1')
    assert b.result == 'REFUSED' and b.message == 'Eine Einrichtung läuft noch.'


def test_a_hand_guide_off_timing_out_between_relock_and_claim_hold_is_stale():
    env = ScriptEnv()
    w = W.CommissionWorker()
    w.request_claim(W.CLAIM, 's1')
    w.take_next()
    assert w.step(env) is None                  # check
    eff = w.step(env)                           # relock attempt 1
    assert eff.kind == W.E_TORQUE_ON
    env.flag = True
    w.deliver(True)
    assert w.step(env) is None                  # relocked → claim next
    env.flag = None                             # D15: a hand-guide off timed out meanwhile
    assert w.step(env) is None
    assert w.record is None and not env.cal


def test_a_failed_relock_claims_nothing():
    env = ScriptEnv()
    w = W.CommissionWorker()
    drv = Driver(env, w, script=[False, False, False])
    job = w.request_claim(W.CLAIM, 's1')
    drv.run()
    assert w.job_state(job.job_id)[0] == 'failed'
    assert w.record is None and not env.cal


def test_resolve_and_restore_relock_then_claim_then_release():
    for kind in (W.RESOLVE, W.RESTORE):
        env = ScriptEnv()
        w = W.CommissionWorker()
        drv = Driver(env, w)
        job = w.request_claim(kind, 's1')
        drv.run()
        assert w.job_state(job.job_id)[0] == 'done'
        assert drv.effects[:2] == [W.E_TORQUE_ON, W.E_WORK]
        assert not env.cal and env.swapped == [(kind, True)]


def test_an_abort_retorque_failing_keeps_the_claim_and_retries():
    env, w, drv = _commission()
    cid = w.record.commission_id
    w.touch_begin(cid)
    drv.run()
    drv.script = [False, False, False]
    abort = w.mark_abort()
    drv.run()
    assert env.cal and w.record is not None and w.record.abort_marked
    assert w.job_state(abort.job_id)[0] == 'retry_wait'
    assert w.retorque_pending_message().startswith('drv')
    w.promote_due(1e9)
    drv.run()
    assert not env.cal and w.record is None


def test_retorque_pending_sentence_while_marked_or_running():
    env, w, drv = _commission()
    w.mark_abort()
    assert w.retorque_pending_message() == 'Die Einrichtung wird gerade beendet – bitte kurz warten.'


def test_touch_capture_order_and_refusals():
    env, w, drv = _commission()
    cid = w.record.commission_id
    assert w.touch_capture(cid, 'x', 0).message == 'Bitte zuerst „Arm freigeben“ drücken.'
    w.touch_begin(cid)
    assert w.touch_begin(cid).message == 'Der Arm ist bereits frei.'
    assert w.touch_capture(cid, 'x', 0).message == 'Bitte warten, der Roboter ist noch beschäftigt.'
    drv.run()
    assert w.touch_capture(cid, 'x', 1).result == 'INVALID'
    assert w.touch_capture(cid, 'x', 0).result == 'OK'
    assert w.touch_capture('nope', 'x', 1).result == 'INVALID'
    w.mark_abort()
    assert w.touch_begin(cid).message in ('Der Arm ist bereits frei.',
                                          'Die Einrichtung wird gerade beendet.')


def test_status_reports_the_owner_last_job_per_session():
    w = W.CommissionWorker()
    job = w.request_claim(W.CLAIM, 's1')
    assert w.owner_last_job('s1') == job.job_id
    assert w.owner_last_job('s2') == 0
    assert w.job_state(job.job_id)[0] == 'queued'
    assert isinstance(types.SimpleNamespace(), object)
