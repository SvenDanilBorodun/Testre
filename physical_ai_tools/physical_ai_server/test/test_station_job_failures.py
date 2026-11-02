"""A station job or a station service call that raises (fix round 1, F3 and F5).

F3: a job that raises never wedges the robot. The re-solve and the restore catch
their own failures and end FAILED with a German sentence; the worker's catch-all
(``StationService._fail_running_job`` → ``CommissionWorker.fail_running``)
releases a claim the job holds past its claim and re-schedules a live record's
ABORT or RELOCK, so plan §6 invariant 3 (liveness) holds. The model checker's EXC
configuration (``test_station_commission_worker.py``) explores the same catch-all
through every interleaving; these cases run it on the real service and its
worker thread with a stub node.

F5: an exception never leaves a ``/station/*`` service callback (it would kill
the node): the call is answered FAILED with a German sentence.
"""

from __future__ import annotations

import os
import stat
import time

import pytest

cv2 = pytest.importorskip('cv2')

import station_fixture as F  # noqa: E402
from physical_ai_server.station import commission as CM  # noqa: E402
from physical_ai_server.station import commission_worker as CW  # noqa: E402
from physical_ai_server.station import store  # noqa: E402
from physical_ai_server.station import texts_de as T  # noqa: E402
from test_station_service import _Resp, _activate, _req, _session, _svc  # noqa: E402


def _wait(pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _check(svc, cmd, cid, sid):
    return svc.check_command(_req(command=cmd, client_id=cid, session_id=sid), _Resp())


def _commission(svc, cmd, sid, **kw):
    return svc.commission_command(_req(command=cmd, client_id='A', session_id=sid, **kw),
                                  _Resp())


def _idle(svc):
    return not svc.worker.station_job_pending()


def _begin_and_free_the_arm(svc, sid):
    """begin → CLAIM done → touch_begin → TOUCH_OFF done (the arm limp, touch on)."""
    r = _commission(svc, 'begin', sid, layout_id=F.TEST_LAYOUT_ID)
    assert r.result == 'QUEUED', r.message
    assert _wait(lambda: svc.worker.record is not None and _idle(svc))
    cid = svc.worker.record.commission_id
    r = _commission(svc, 'touch_begin', sid, commission_id=cid)
    assert r.result == 'QUEUED', r.message
    assert _wait(lambda: _idle(svc) and svc.worker.record.off_ok)
    assert svc.worker.record.touch_active
    return cid


# ── F3: the re-solve and the restore catch their own failures ─────────────────
def test_a_raising_re_solve_releases_the_claim_and_ends_failed_in_german(tmp_path, monkeypatch):
    """Verifier A: an OpenCV error inside the board solve after the claim."""
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)

    def boom(*a, **k):
        raise cv2.error('cv2 blew up')
    monkeypatch.setattr(CM, 'solve_board_burst', boom)
    r = _check(svc, 'resolve_camera', 'A', sid)
    assert r.result == 'QUEUED', r.message
    svc.start_worker()
    try:
        assert _wait(lambda: _idle(svc))
    finally:
        svc.stop()
    assert node.on_calibration is False
    assert svc.worker.job_state(r.job_id) == ('failed', T.JOB_INTERNAL_ERROR_DE)
    assert not (tmp_path / 'station' / '.staging-resolve').exists()
    # The robot is usable again: a new claim is accepted.
    assert _check(svc, 'resolve_camera', 'A', sid).result == 'QUEUED'


def test_a_re_solve_on_an_unwritable_station_dir_releases_the_claim(tmp_path):
    """Verifier A: the staging mkdir fails (permissions; a full disk alike)."""
    if os.geteuid() == 0:
        pytest.skip('root ignores the directory mode')
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)
    sdir = tmp_path / 'station'
    os.chmod(sdir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = _check(svc, 'resolve_camera', 'A', sid)
        assert r.result == 'QUEUED', r.message
        svc.start_worker()
        assert _wait(lambda: _idle(svc), timeout=10)
    finally:
        svc.stop()
        os.chmod(sdir, stat.S_IRWXU)
    assert node.on_calibration is False
    assert svc.worker.job_state(r.job_id) == ('failed', T.JOB_INTERNAL_ERROR_DE)


def test_the_re_solve_itself_never_raises_and_always_removes_its_staging(tmp_path, monkeypatch):
    svc, node = _svc(tmp_path, configured=True)
    _activate(svc, node)

    def boom(*a, **k):
        raise ValueError('degenerate solve')
    monkeypatch.setattr(CM, 'solve_board_burst', boom)
    assert svc._resolve(None) == (False, T.JOB_INTERNAL_ERROR_DE, None)
    assert not (tmp_path / 'station' / '.staging-resolve').exists()


@pytest.mark.parametrize('where', ['restore_previous', 'load'])
def test_the_restore_itself_never_raises(tmp_path, monkeypatch, where):
    svc, node = _svc(tmp_path, configured=True)
    if where == 'load':
        monkeypatch.setattr(store, 'restore_previous', lambda *a, **k: (True, T.RESTORE_OK_DE))

    def boom(*a, **k):
        raise OSError('disk gone')
    monkeypatch.setattr(store, where, boom)
    assert svc._work(CW.RESTORE, None) == (False, T.JOB_INTERNAL_ERROR_DE, None)


# ── F3: the worker's catch-all ────────────────────────────────────────────────
def test_the_catch_all_releases_a_claim_the_raising_job_holds(tmp_path, monkeypatch):
    """Whatever raises past the claim (here the work itself), the claim is released:
    the claim's own re-lock confirmed the arm locked, and the work switches no
    torque. The sentence is German and says what happened (not „übersprungen“)."""
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)

    def boom(kind, token):
        raise RuntimeError('unexpected')
    monkeypatch.setattr(svc, '_work', boom)
    r = _commission(svc, 'restore_previous', sid)
    assert r.result == 'QUEUED', r.message
    svc.start_worker()
    try:
        assert _wait(lambda: _idle(svc))
        assert node.on_calibration is False
        state, message = svc.worker.job_state(r.job_id)
        assert (state, message) == ('failed', T.JOB_INTERNAL_ERROR_DE)
        assert 'übersprungen' not in message
        # The worker thread is alive and takes the next job.
        monkeypatch.setattr(svc, '_work', lambda kind, token: (True, 'ok', None))
        r2 = _commission(svc, 'restore_previous', sid)
        assert r2.result == 'QUEUED', r2.message
        assert _wait(lambda: svc.worker.job_state(r2.job_id)[0] == 'done')
    finally:
        svc.stop()
    assert node.on_calibration is False


def test_a_raising_abort_re_torque_waits_and_runs_again_until_it_locks(tmp_path):
    """Verifier A: the ABORT of a session end raises in its re-torque. The abort mark
    never stays without its job: the same ABORT waits 2 s and runs again, and once
    the re-torque works the record is cleared and the claim released."""
    svc, node = _svc(tmp_path, configured=False)
    sid = _activate(svc, node)
    svc.start_worker()
    try:
        _begin_and_free_the_arm(svc, sid)
        working = node._retorque_follower_or_keep_locked

        def raising(resp):
            raise RuntimeError('rclpy context invalid')
        node._retorque_follower_or_keep_locked = raising
        _session(svc, 'leave', 'A', sid)            # session end → abort marked
        assert _wait(lambda: svc.worker.retries)
        rec = svc.worker.record
        assert rec is not None and rec.abort_marked and rec.touch_active
        assert [r.job.kind for r in svc.worker.retries] == [CW.ABORT]
        assert svc.worker.station_job_pending()    # liveness: a job is left
        assert node.on_calibration is True          # never released over a limp arm
        node._retorque_follower_or_keep_locked = working
        node.clock.t += CW.RETRY_DELAY_S + 0.1
        svc._wake.set()
        assert _wait(lambda: svc.worker.record is None and _idle(svc))
    finally:
        svc.stop()
    assert node.on_calibration is False


def test_a_raising_touch_solve_owes_a_relock_that_ends_the_touch(tmp_path, monkeypatch):
    """The registration solve raises with the arm limp: the job ends FAILED in
    German and a RELOCK is owed (touch capture refused meanwhile), which re-locks
    the arm and ends the touch 2 s later, as after a failed re-torque (rule 5)."""
    svc, node = _svc(tmp_path, configured=False)
    sid = _activate(svc, node)
    svc.start_worker()
    try:
        cid = _begin_and_free_the_arm(svc, sid)

        def boom(*a, **k):
            raise ValueError('SVD did not converge')
        monkeypatch.setattr(CM, 'solve_touch', boom)
        r = _commission(svc, 'touch_solve', sid, commission_id=cid)
        assert r.result == 'QUEUED', r.message
        assert _wait(lambda: svc.worker.job_state(r.job_id)[0] == 'failed')
        assert svc.worker.job_state(r.job_id)[1] == T.JOB_INTERNAL_ERROR_DE
        rec = svc.worker.record
        assert rec.touch_active and rec.relock_owed
        assert [x.job.kind for x in svc.worker.retries] == [CW.RELOCK]
        with svc.mode_lock, svc.lock:
            ans = svc.worker.touch_capture(cid, 'tap', 0)
        assert (ans.result, ans.message) == ('REFUSED', T.RELEASE_ARM_DE)
        node.clock.t += CW.RETRY_DELAY_S + 0.1
        svc._wake.set()
        assert _wait(lambda: not svc.worker.record.touch_active and _idle(svc))
        assert svc.worker.record.taps == () and not svc.worker.record.relock_owed
    finally:
        svc.stop()
    assert node._follower_torque_on is True


def test_the_worker_thread_survives_a_raise_outside_any_job(tmp_path, monkeypatch):
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)
    real = svc._sync_commission_locked
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('once')
        return real()
    monkeypatch.setattr(svc, '_sync_commission_locked', flaky)
    monkeypatch.setattr(svc, '_work', lambda kind, token: (True, 'ok', None))
    svc.start_worker()
    try:
        assert _wait(lambda: calls['n'] >= 1)
        r = _commission(svc, 'restore_previous', sid)
        assert r.result == 'QUEUED', r.message
        assert _wait(lambda: svc.worker.job_state(r.job_id)[0] == 'done')
    finally:
        svc.stop()


# ── F5: no exception leaves a /station/* service callback ─────────────────────
def test_board_solve_on_an_unwritable_staging_dir_answers_failed(tmp_path):
    """Verifier A: the board solve's staging mkdir raises PermissionError."""
    if os.geteuid() == 0:
        pytest.skip('root ignores the directory mode')
    svc, node = _svc(tmp_path, configured=False)
    sid = _activate(svc, node)
    svc.start_worker()
    try:
        r = _commission(svc, 'begin', sid, layout_id=F.TEST_LAYOUT_ID)
        assert _wait(lambda: svc.worker.record is not None and _idle(svc))
    finally:
        svc.stop()
    cid = svc.worker.record.commission_id
    svc.commission.intrinsics = {'K': F.ftheta_intrinsics()[0],
                                 'dist': F.ftheta_intrinsics()[1]}
    sdir = tmp_path / 'station'
    sdir.mkdir(exist_ok=True)
    os.chmod(sdir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = _commission(svc, 'board_solve', sid, commission_id=cid)
    finally:
        os.chmod(sdir, stat.S_IRWXU)
    assert (r.success, r.result, r.message) == (False, 'FAILED', T.COMMAND_INTERNAL_ERROR_DE)


def test_a_raising_camera_check_answers_failed_and_frees_the_next_check(tmp_path, monkeypatch):
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)

    def boom(cal):
        raise cv2.error('decode')
    monkeypatch.setattr(svc, '_camera_check', boom)
    r = _check(svc, 'camera', 'A', sid)
    assert (r.success, r.result, r.message) == (False, 'FAILED', T.COMMAND_INTERNAL_ERROR_DE)
    monkeypatch.undo()
    assert _check(svc, 'camera', 'A', sid).result != 'BUSY'


def test_a_raising_session_command_answers_not_accepted_in_german(tmp_path, monkeypatch):
    svc, node = _svc(tmp_path, configured=True)

    def boom(*a, **k):
        raise RuntimeError('state machine')
    monkeypatch.setattr(svc.machine, 'enter', boom)
    r = _session(svc, 'enter', 'A')
    assert (r.accepted, r.message) == (False, T.COMMAND_INTERNAL_ERROR_DE)


def test_a_raising_commission_command_answers_failed(tmp_path, monkeypatch):
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)

    def boom(*a, **k):
        raise RuntimeError('worker')
    monkeypatch.setattr(svc.worker, 'request_claim', boom)
    r = _commission(svc, 'restore_previous', sid)
    assert (r.success, r.result, r.message) == (False, 'FAILED', T.COMMAND_INTERNAL_ERROR_DE)
    # The locks were released on the way out.
    assert svc.lock.acquire(timeout=1) and svc.mode_lock.acquire(timeout=1)
    svc.lock.release()
    svc.mode_lock.release()
