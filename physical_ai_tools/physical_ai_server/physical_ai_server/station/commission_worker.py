#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The commission torque protocol of plan v6.13 §6 (rules 1–9), as one pure class.

rclpy-free and I/O-free ("sans-IO"). The class holds the commission record,
the job queue, the retry list and the running job, and decides every step;
it never calls a torque service, never touches a file and never takes a lock.

* **Services** (``request_claim``, ``touch_begin``, ``touch_capture``,
  ``touch_solve``, ``commit``, ``cancel``) are called by the node with
  ``_mode_lock`` → ``_station_lock`` held, O(1).
* **The 5 Hz timer** calls ``mark_abort``, ``idle_tick`` and ``promote_due``
  under the same locks; it never runs a job.
* **The one station worker thread** calls ``take_next``, then ``step(env)``
  under the locks; ``step`` either ends the job or returns an :class:`Effect`
  (a torque call, a solve, a file write) that the worker executes OUTSIDE the
  locks and feeds back with ``deliver``, again under the locks.

``env`` is the node's view, read only under those locks: the session, the arm's
mode flags, ``_follower_torque_on is True``, the ``on_calibration`` claim and its
release, the in-memory calibration swap, the clock and the id factories.

Every attribute that makes up the protocol state is an immutable value that is
rebound, never mutated, so :meth:`snapshot` / :meth:`restore` are O(1): the CI
model checker (``test/test_station_commission_worker.py``) explores this very
class through every interleaving with a fake arm whose failed torque calls may
still be applied later, in order, as the OMX driver does.

What a torque result means (plan §6): a failed torque-off is *maybe limp* and
never clears a touch; a failed torque-on is *maybe still limp* and never
releases anything; only a confirmed torque-on is locked.
"""

from collections import namedtuple

from physical_ai_server.station import texts_de as T

# ── Job kinds ────────────────────────────────────────────────────────────────
CLAIM = 'CLAIM'
RESOLVE = 'RESOLVE'
RESTORE = 'RESTORE'
TOUCH_OFF = 'TOUCH_OFF'
TOUCH_SOLVE = 'TOUCH_SOLVE'
RELOCK = 'RELOCK'
COMMIT = 'COMMIT'
ABORT = 'ABORT'
CLAIM_KINDS = (CLAIM, RESOLVE, RESTORE)
TOUCH_KINDS = (TOUCH_OFF, TOUCH_SOLVE, RELOCK)

# ── Timing (plan §6 rules 3, 5, 6, 7, 9) ─────────────────────────────────────
RETRY_DELAY_S = 2.0          # a failed re-torque is retried 2 s later
IDLE_RELOCK_S = 30.0         # no owner commission command for 30 s → RELOCK
RELOCK_ATTEMPTS = 3          # a claim's re-lock, like _retorque_follower_or_keep_locked

# ── Job outcome states (plan §8.2 / §8.3) ────────────────────────────────────
DONE = 'done'
FAILED = 'failed'
STALE = 'stale'

Job = namedtuple('Job', 'job_id kind commission_id touch_counter session_id')
Record = namedtuple(
    'Record',
    'commission_id touch_counter touch_active taps off_ok solved relock_owed '
    'abort_marked abort_message solution ready')
Running = namedtuple('Running', 'job pc attempt result aux')
Retry = namedtuple('Retry', 'job due')
Effect = namedtuple('Effect', 'kind args needs_result')
Answer = namedtuple('Answer', 'result job_id message')

# Effect kinds the worker thread executes outside the locks.
E_TORQUE_ON = 'torque_on'      # args: (mark_unknown,) — one attempt of a claim's re-lock
E_TORQUE_OFF = 'torque_off'    # args: ()
E_RETORQUE = 'retorque'        # args: (mark_unknown,) — _retorque_follower_or_keep_locked
E_SOLVE = 'solve'              # args: (commission_id, taps) — the §6.3 registration
E_WRITE = 'write'              # args: (commission_id, solution) — the station YAML
E_WORK = 'work'                # args: (kind, session_id) — a re-solve or a restore
E_CLEANUP = 'cleanup'          # args: (commission_id,) — delete the staging dir

_WAIT_NEXT = {
    'relock_wait': 'relocked',
    'off_wait': 'off_done',
    'rt_wait': 'rt_done',
    'solve_wait': 'solved',
    'write_wait': 'written',
    'work_wait': 'worked',
}


class CommissionWorker:
    """The commission record, the job queue and the step logic of plan §6."""

    def __init__(self, job_ids=None, on_job_end=None):
        # Job ids for status reporting. The CI model passes ``lambda: 0`` so a
        # job's identity never enters the explored state.
        if job_ids is None:
            counter = [0]

            def job_ids():
                counter[0] += 1
                return counter[0]
        self._job_ids = job_ids
        self._on_job_end = on_job_end or (lambda job, state, message: None)
        # Protocol state: immutable values, rebound only (snapshot / restore).
        self._record = None
        self._queue = ()
        self._retry = ()
        self._running = None
        self._last_owner_activity = 0.0
        # Bookkeeping that is NOT protocol state (outside snapshot()).
        self._outcomes = {}
        self._outcome_order = ()
        self._owner_last = None   # (session_id, job_id) of the owner's newest job

    # ── snapshot / restore (O(1); the model checker's state) ───────────────────
    def snapshot(self):
        return (self._record, self._queue, self._retry, self._running,
                self._last_owner_activity)

    def restore(self, snap):
        (self._record, self._queue, self._retry, self._running,
         self._last_owner_activity) = snap

    # ── read-only views ───────────────────────────────────────────────────────
    @property
    def record(self):
        return self._record

    @property
    def running(self):
        return self._running

    @property
    def queue(self):
        return self._queue

    @property
    def retries(self):
        return self._retry

    def touch_active(self):
        rec = self._record
        return rec is not None and rec.touch_active

    def station_job_pending(self):
        """A job of any commission is queued, running or waiting to retry."""
        return bool(self._queue) or bool(self._retry) or self._running is not None

    def awaiting_result(self):
        run = self._running
        return run is not None and run.pc in _WAIT_NEXT

    def _pending_jobs(self):
        jobs = [j for j in self._queue]
        jobs.extend(r.job for r in self._retry)
        if self._running is not None:
            jobs.append(self._running.job)
        return jobs

    def _jobs_of(self, commission_id):
        return [j for j in self._pending_jobs()
                if j.kind not in CLAIM_KINDS and j.commission_id == commission_id]

    def _touch_jobs(self, commission_id, touch_counter):
        return [j for j in self._jobs_of(commission_id)
                if j.kind in TOUCH_KINDS and j.touch_counter == touch_counter]

    def retorque_pending_message(self):
        """§5.3 RETORQUE_PENDING: the sentence while an abort is marked or its
        re-torque runs; after a failed round, the driver's sentence wins."""
        rec = self._record
        if rec is None or not rec.abort_marked:
            return ''
        return rec.abort_message or T.RETORQUE_IN_PROGRESS_DE

    def job_state(self, job_id):
        """(state, message) of a job: queued / running / retry_wait or its outcome."""
        if not job_id:
            return 'none', ''
        run = self._running
        if run is not None and run.job.job_id == job_id:
            return 'running', ''
        for j in self._queue:
            if j.job_id == job_id:
                return 'queued', ''
        for r in self._retry:
            if r.job.job_id == job_id:
                return 'retry_wait', (self._record.abort_message
                                      if (self._record is not None and r.job.kind == ABORT)
                                      else '')
        return self._outcomes.get(job_id, ('none', ''))

    def owner_last_job(self, session_id):
        last = self._owner_last
        if last is None or last[0] != session_id:
            return 0
        return last[1]

    # ── helpers ───────────────────────────────────────────────────────────────
    def _new_job(self, kind, commission_id='', touch_counter=0, session_id=''):
        return Job(self._job_ids(), kind, commission_id, touch_counter, session_id)

    def _enqueue(self, job):
        self._queue = self._queue + (job,)
        return job

    def _note_owner_job(self, session_id, job):
        if session_id and job is not None:
            self._owner_last = (session_id, job.job_id)

    def _end(self, state, message=''):
        job = self._running.job
        self._running = None
        self._outcomes[job.job_id] = (state, message)
        self._outcome_order = (self._outcome_order + (job.job_id,))[-64:]
        if len(self._outcomes) > 64:
            keep = set(self._outcome_order)
            self._outcomes = {k: v for k, v in self._outcomes.items() if k in keep}
        self._on_job_end(job, state, message)
        return None

    def _schedule_retry(self, env, job):
        self._retry = self._retry + (Retry(job, env.now() + RETRY_DELAY_S),)

    def _set_record(self, **changes):
        self._record = self._record._replace(**changes)

    def _current(self, commission_id):
        rec = self._record
        return rec is not None and rec.commission_id == commission_id

    def _arm_idle(self, env):
        """Rule 9's "arm idle by its mode flags": on_calibration, every touch,
        on_workflow, on_manual and on_leader_teach all false."""
        return env.modes_idle() and not self.touch_active()

    def _session_ok(self, env, job):
        return env.session_active(job.session_id)

    def _touch_job_valid(self, job):
        """Rule 1 for TOUCH_OFF / TOUCH_SOLVE / RELOCK."""
        rec = self._record
        return (rec is not None and rec.commission_id == job.commission_id
                and rec.touch_counter == job.touch_counter
                and rec.touch_active and not rec.abort_marked)

    def _effect(self, pc, kind, *args, aux=None):
        self._running = self._running._replace(pc=pc, aux=aux if aux is not None
                                               else self._running.aux)
        return Effect(kind, args, kind != E_CLEANUP)

    # ── services (caller holds _mode_lock → _station_lock) ─────────────────────
    def request_claim(self, kind, session_id, mode_refusal=''):
        """Rule 9: ``begin`` / the re-solve / ``restore_previous``. Refused unless
        no station job of any commission is pending and the arm is idle by its
        mode flags (``mode_refusal`` is the node's sentence for the mode that
        holds the arm, '' when idle)."""
        if kind not in CLAIM_KINDS:
            return Answer('INVALID', 0, T.UNKNOWN_COMMAND_DE)
        if self.station_job_pending():
            return Answer('REFUSED', 0, T.JOB_PENDING_DE)
        if mode_refusal:
            return Answer('REFUSED', 0, mode_refusal)
        if self.touch_active():
            return Answer('REFUSED', 0, T.CALIBRATION_ACTIVE_DE)
        job = self._enqueue(self._new_job(kind, session_id=session_id))
        self._note_owner_job(session_id, job)
        return Answer('QUEUED', job.job_id, T.COMMISSION_QUEUED_DE)

    def note_owner_activity(self, now):
        """Every owner commission command (the wizard's status poll included)."""
        self._last_owner_activity = now

    def set_ready(self, commission_id, ready=True):
        """The commission's lens, board and world gates all passed (§6)."""
        if self._current(commission_id):
            self._set_record(ready=bool(ready))

    def touch_begin(self, commission_id, session_id=''):
        """Rule 2: flag before the torque-off, in one hold."""
        rec = self._record
        if rec is None or rec.commission_id != commission_id:
            return Answer('INVALID', 0, T.WRONG_COMMISSION_DE)
        if rec.touch_active:
            return Answer('REFUSED', 0, T.TOUCH_ALREADY_FREE_DE)
        if rec.abort_marked:
            return Answer('REFUSED', 0, T.ABORT_MARKED_DE)
        if self._jobs_of(commission_id):
            return Answer('REFUSED', 0, T.ROBOT_BUSY_DE)
        tc = rec.touch_counter + 1
        self._record = rec._replace(
            touch_counter=tc, touch_active=True, taps=(), off_ok=False,
            solved=False, relock_owed=False)
        job = self._enqueue(self._new_job(TOUCH_OFF, commission_id, tc, session_id))
        self._note_owner_job(session_id, job)
        return Answer('QUEUED', job.job_id, T.COMMISSION_QUEUED_DE)

    def _touch_refusal(self, rec, commission_id):
        """Rule 4's conditions, shared by touch_capture and touch_solve."""
        if rec is None or rec.commission_id != commission_id:
            return Answer('INVALID', 0, T.WRONG_COMMISSION_DE)
        if not rec.touch_active:
            return Answer('REFUSED', 0, T.TOUCH_NOT_ACTIVE_DE)
        if rec.relock_owed:
            return Answer('REFUSED', 0, T.RELEASE_ARM_DE)
        if self._jobs_of(commission_id):
            return Answer('REFUSED', 0, T.ROBOT_BUSY_DE)
        if not rec.off_ok:
            return Answer('REFUSED', 0, T.TOUCH_NOT_FREE_YET_DE)
        return None

    def touch_tap_count(self, commission_id):
        rec = self._record
        if rec is None or rec.commission_id != commission_id:
            return None
        return len(rec.taps)

    def touch_capture(self, commission_id, tap, expected_index):
        """Rule 4: reads FK only (the caller computed ``tap``), appends in order."""
        rec = self._record
        refusal = self._touch_refusal(rec, commission_id)
        if refusal is not None:
            return refusal
        if len(rec.taps) != expected_index:
            return Answer('INVALID', 0, T.DOT_ORDER_DE.format(dot=len(rec.taps) + 1))
        self._record = rec._replace(taps=rec.taps + (tap,))
        return Answer('OK', 0, T.DOT_CAPTURED_DE.format(dot=expected_index + 1))

    def touch_solve(self, commission_id, session_id=''):
        """Rule 5: refused under rule 4's conditions, else TOUCH_SOLVE."""
        rec = self._record
        refusal = self._touch_refusal(rec, commission_id)
        if refusal is not None:
            return refusal
        job = self._enqueue(self._new_job(TOUCH_SOLVE, commission_id,
                                          rec.touch_counter, session_id))
        self._note_owner_job(session_id, job)
        return Answer('QUEUED', job.job_id, T.COMMISSION_QUEUED_DE)

    def commit(self, commission_id, session_id=''):
        """Rule 8: enqueue COMMIT, so it runs after every torque job queued."""
        rec = self._record
        if rec is None or rec.commission_id != commission_id:
            return Answer('INVALID', 0, T.WRONG_COMMISSION_DE)
        for j in self._jobs_of(commission_id):
            if j.kind == COMMIT:
                self._note_owner_job(session_id, j)
                return Answer('QUEUED', j.job_id, T.COMMISSION_QUEUED_DE)
        job = self._enqueue(self._new_job(COMMIT, commission_id, 0, session_id))
        self._note_owner_job(session_id, job)
        return Answer('QUEUED', job.job_id, T.COMMISSION_QUEUED_DE)

    def cancel(self, commission_id, session_id=''):
        """Rule 7: mark the abort and enqueue one ABORT, never a second."""
        rec = self._record
        if rec is None or rec.commission_id != commission_id:
            return Answer('INVALID', 0, T.WRONG_COMMISSION_DE)
        for j in self._jobs_of(commission_id):
            if j.kind == ABORT:
                self._note_owner_job(session_id, j)
                return Answer('QUEUED', j.job_id, T.COMMISSION_QUEUED_DE)
        if any(j.kind == COMMIT for j in self._jobs_of(commission_id)):
            return Answer('REFUSED', 0, T.COMMIT_IN_PROGRESS_DE)
        job = self.mark_abort()
        self._note_owner_job(session_id, job)
        return Answer('QUEUED', job.job_id if job else 0, T.COMMISSION_QUEUED_DE)

    # ── timer (caller holds _mode_lock → _station_lock; never runs a job) ──────
    def mark_abort(self):
        """Rule 7 from the timer (session end, takeover, live leader) or cancel.
        Returns the ABORT job, or None when there is no record / it is marked."""
        rec = self._record
        if rec is None or rec.abort_marked:
            return None
        self._record = rec._replace(abort_marked=True)
        return self._enqueue(self._new_job(ABORT, rec.commission_id))

    def idle_tick(self, now):
        """Rule 6: one RELOCK when a touch idles 30 s with no job of it pending."""
        rec = self._record
        if (rec is None or not rec.touch_active or rec.abort_marked
                or self._touch_jobs(rec.commission_id, rec.touch_counter)):
            return None
        if now - self._last_owner_activity < IDLE_RELOCK_S:
            return None
        return self._enqueue(self._new_job(RELOCK, rec.commission_id, rec.touch_counter))

    def due_retries(self, now):
        return tuple(r for r in self._retry if r.due <= now)

    def promote_retry(self, entry):
        """Move one waiting retry back into the queue (its 2 s have passed)."""
        if entry not in self._retry:
            return False
        self._retry = tuple(r for r in self._retry if r != entry)
        self._queue = self._queue + (entry.job,)
        return True

    def promote_due(self, now):
        moved = 0
        for entry in self.due_retries(now):
            moved += int(self.promote_retry(entry))
        return moved

    # ── worker (caller holds the locks around take_next / step / deliver) ──────
    def take_next(self):
        """Start the queue head (jobs run strictly one at a time, in order)."""
        if self._running is not None or not self._queue:
            return False
        job = self._queue[0]
        self._queue = self._queue[1:]
        self._running = Running(job, 'check', 0, None, None)
        return True

    def deliver(self, ok, message='', payload=None):
        """The result of the effect the last step returned."""
        run = self._running
        if run is None or run.pc not in _WAIT_NEXT:
            raise RuntimeError('deliver without a pending effect')
        self._running = run._replace(pc=_WAIT_NEXT[run.pc],
                                     result=(bool(ok), message or '', payload))

    def step(self, env):
        """Advance the running job by one segment (rule 1: every job re-checks
        under the locks when it starts and never acts on a state it did not
        re-check). Returns an Effect for the worker to execute, or None."""
        run = self._running
        if run is None:
            return None
        if run.pc in _WAIT_NEXT:
            raise RuntimeError('step while an effect is pending')
        kind = run.job.kind
        if kind in CLAIM_KINDS:
            return self._step_claim(env, run)
        if kind == TOUCH_OFF:
            return self._step_touch_off(env, run)
        if kind == TOUCH_SOLVE:
            return self._step_touch_solve(env, run)
        if kind == RELOCK:
            return self._step_relock(env, run)
        if kind == COMMIT:
            return self._step_commit(env, run)
        if kind == ABORT:
            return self._step_abort(env, run)
        raise RuntimeError(f'unknown job kind {kind!r}')

    # Rule 9: check → re-lock (≤ 3 attempts, mode flags re-checked and the torque
    # state marked unknown before each) → claim (one hold: session, mode flags,
    # a confirmed True, on_calibration, and the record for CLAIM).
    def _step_claim(self, env, run):
        job = run.job
        if run.pc == 'check':
            if not (self._session_ok(env, job) and self._arm_idle(env)):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            self._running = run._replace(pc='relock', attempt=0)
            return None
        if run.pc == 'relock':
            if not self._arm_idle(env):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            self._running = run._replace(attempt=run.attempt + 1)
            return self._effect('relock_wait', E_TORQUE_ON, True)
        if run.pc == 'relocked':
            ok, message, _ = run.result
            if ok:
                self._running = run._replace(pc='claim')
                return None
            if run.attempt < RELOCK_ATTEMPTS:
                self._running = run._replace(pc='relock')
                return None
            return self._end(FAILED, message or T.RELOCK_FAILED_DE)
        if run.pc == 'claim':
            if not (self._session_ok(env, job) and self._arm_idle(env)
                    and env.torque_is_true()):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            if not env.claim_calibration():
                return self._end(STALE, T.COMMISSION_STALE_DE)
            if job.kind == CLAIM:
                self._record = Record(
                    env.new_commission_id(), 0, False, (), False, False, False,
                    False, '', None, False)
                return self._end(DONE, T.COMMISSION_STARTED_DE)
            return self._effect('work_wait', E_WORK, job.kind, job.session_id)
        if run.pc == 'worked':
            ok, message, payload = run.result
            env.finish_work(job.kind, ok, payload)
            env.release_calibration()
            return self._end(DONE if ok else FAILED, message)
        raise RuntimeError(run.pc)

    # Rule 3.
    def _step_touch_off(self, env, run):
        job = run.job
        if run.pc == 'check':
            if not self._touch_job_valid(job):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            return self._effect('off_wait', E_TORQUE_OFF)
        if run.pc == 'off_done':
            ok, message, _ = run.result
            if ok:
                if self._current(job.commission_id) and \
                        self._record.touch_counter == job.touch_counter:
                    self._set_record(off_ok=True)
                return self._end(DONE, T.TOUCH_FREE_DE)
            # A failed torque-off is maybe limp: keep the flag, mark the torque
            # state unknown (the hold is published first) and re-lock now.
            return self._effect('rt_wait', E_RETORQUE, True, aux=message)
        if run.pc == 'rt_done':
            ok, message, _ = run.result
            matches = (self._current(job.commission_id)
                       and self._record.touch_counter == job.touch_counter)
            if ok:
                if matches:
                    self._set_record(touch_active=False)
                return self._end(FAILED, run.aux or T.TOUCH_OFF_FAILED_DE)
            if matches:
                self._set_record(relock_owed=True)
                self._schedule_retry(env, self._new_job(
                    RELOCK, job.commission_id, job.touch_counter))
            return self._end(FAILED, message or T.RELOCK_FAILED_DE)
        raise RuntimeError(run.pc)

    # Rule 5.
    def _step_touch_solve(self, env, run):
        job = run.job
        if run.pc == 'check':
            if not self._touch_job_valid(job):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            return self._effect('solve_wait', E_SOLVE, job.commission_id,
                                self._record.taps)
        if run.pc == 'solved':
            return self._effect('rt_wait', E_RETORQUE, False, aux=run.result)
        if run.pc == 'rt_done':
            ok, message, _ = run.result
            gates_ok, gate_message, solution = run.aux
            matches = (self._current(job.commission_id)
                       and self._record.touch_counter == job.touch_counter)
            if ok:
                if not matches:
                    return self._end(STALE, T.COMMISSION_STALE_DE)
                if gates_ok:
                    self._set_record(touch_active=False, solved=True, solution=solution)
                    return self._end(DONE, gate_message or T.TOUCH_SOLVED_DE)
                self._set_record(touch_active=False, solved=False, taps=())
                return self._end(FAILED, gate_message)
            if matches:
                self._set_record(relock_owed=True)
                self._schedule_retry(env, self._new_job(
                    RELOCK, job.commission_id, job.touch_counter))
            return self._end(FAILED, message or T.RELOCK_FAILED_DE)
        raise RuntimeError(run.pc)

    # Rule 6.
    def _step_relock(self, env, run):
        job = run.job
        if run.pc == 'check':
            if not self._touch_job_valid(job):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            return self._effect('rt_wait', E_RETORQUE, False)
        if run.pc == 'rt_done':
            ok, message, _ = run.result
            if ok:
                if self._current(job.commission_id) and \
                        self._record.touch_counter == job.touch_counter:
                    self._set_record(touch_active=False, taps=(), relock_owed=False)
                return self._end(DONE, T.COMMISSION_DONE_DE)
            # Nothing changes; the same job waits 2 s and runs again (an abort
            # marked meanwhile makes that run end stale).
            self._running = None
            self._schedule_retry(env, job)
            return None
        raise RuntimeError(run.pc)

    # Rule 8.
    def _step_commit(self, env, run):
        job = run.job
        if run.pc == 'check':
            rec = self._record
            if not (rec is not None and rec.commission_id == job.commission_id
                    and not rec.touch_active and not rec.abort_marked
                    and rec.solved and rec.ready):
                return self._end(STALE, T.COMMIT_NOT_READY_DE)
            return self._effect('write_wait', E_WRITE, job.commission_id, rec.solution)
        if run.pc == 'written':
            ok, message, payload = run.result
            if not ok:
                return self._end(FAILED, message or T.WRITE_FAILED_DE)
            if not self._current(job.commission_id):
                return self._end(STALE, T.COMMISSION_STALE_DE)
            env.commit_swap(job.commission_id, payload)
            self._record = None
            env.release_calibration()
            self._end(DONE, T.COMMIT_DONE_DE)
            return Effect(E_CLEANUP, (job.commission_id,), False)
        raise RuntimeError(run.pc)

    # Rule 7.
    def _step_abort(self, env, run):
        job = run.job
        if run.pc == 'check':
            rec = self._record
            if rec is None or rec.commission_id != job.commission_id:
                return self._end(STALE, T.COMMISSION_STALE_DE)
            if rec.touch_active:
                return self._effect('rt_wait', E_RETORQUE, False)
            return self._abort_final(env, job)
        if run.pc == 'rt_done':
            ok, message, _ = run.result
            if ok:
                if not self._current(job.commission_id):
                    return self._end(STALE, T.COMMISSION_STALE_DE)
                return self._abort_final(env, job)
            if self._current(job.commission_id):
                self._set_record(abort_message=message or T.RELOCK_FAILED_DE)
            self._running = None
            self._schedule_retry(env, job)
            return None
        raise RuntimeError(run.pc)

    def _abort_final(self, env, job):
        """One hold of _mode_lock → _station_lock: clear the touch and the record,
        release on_calibration; the staging delete runs outside the locks."""
        self._set_record(touch_active=False)
        self._record = None
        env.release_calibration()
        self._end(DONE, T.CANCELLED_DE)
        return Effect(E_CLEANUP, (job.commission_id,), False)
