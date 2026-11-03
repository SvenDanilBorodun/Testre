"""The station session state machine (plan v6.13 §5.2) and the review scenarios
of §12.1, on the real ``station/state.py``."""

from __future__ import annotations

import itertools

import pytest

from physical_ai_server.station import state as S
from physical_ai_server.station.state import SessionMachine, stream_healthy_since

FHD = (1920, 1080)


def _ids():
    c = itertools.count(1)
    return lambda: f'sid{next(c)}'


def _frames(t0, t1, fps=15.0, size=FHD):
    n = int((t1 - t0) * fps)
    return [(t0 + i / fps, size) for i in range(1, n + 1)]


def _active(m, cid='A', t=0.0):
    ans, ev = m.enter(cid, t)
    assert ans.accepted and ev == 'new' and m.state == 'ENTERING'
    m.tick(t + 1.2, _frames(t, t + 1.2), 0.05, False)
    assert m.state == 'ACTIVE' and m.source_ok
    return m.session_id


def test_off_when_not_a_station_rig():
    m = SessionMachine(False)
    assert m.state == 'OFF'
    ans, _ = m.enter('A', 0.0)
    assert not ans.accepted and ans.reason_code == 'NOT_STATION_RIG'


def test_stream_healthy_rule():
    assert stream_healthy_since(_frames(0, 1.2), 0.0, 1.2)
    assert not stream_healthy_since(_frames(0, 0.6), 0.0, 0.6)           # < 1 s span
    assert not stream_healthy_since(_frames(0, 2, size=(640, 480)), 0.0, 2.0)
    assert not stream_healthy_since(_frames(0, 2, fps=8), 0.0, 2.0)      # < 10 fps
    assert not stream_healthy_since(_frames(0, 1.2), 1.0, 1.2)           # before the event


def test_native_timeout_and_its_end_reason():
    m = SessionMachine(True, _ids())
    m.enter('A', 0.0)
    sid = m.session_id
    for t in range(1, 31):
        assert m.tick(float(t), [], None, False) == ''
        m.keepalive(sid, 'A', float(t))
    assert m.tick(30.5, [], None, False) == 'NATIVE_TIMEOUT'
    ans = m.keepalive(sid, 'A', 31.0)
    assert ans.reason_code == 'SESSION_ENDED' and ans.end_reason == 'NATIVE_TIMEOUT'
    assert 'kein 1080p' in ans.message


def test_resume_by_the_same_client_keeps_robot_ok_and_revokes_the_camera():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    m.camera_ok = m.robot_ok = True
    epoch, gen = m.revocations, m.generation
    ans, ev = m.enter('A', 2.0)
    assert ans.accepted and ans.reason_code == 'RESUMED' and ev == 'resumed'
    assert m.session_id == sid and m.generation == gen
    assert not m.camera_ok and m.robot_ok and m.revocations == epoch + 1


@pytest.mark.parametrize('gap', [1.0, 5.0, 8.0])
def test_reload_landing_on_home_resumes(gap):
    m = SessionMachine(True, _ids())
    sid = _active(m)
    m.keepalive(sid, 'A', 2.0)
    m.tick(2.0 + gap - 0.1, _frames(1.2, 2.0 + gap - 0.1), 0.05, False)
    ans, _ = m.enter('A', 2.0 + gap)
    assert ans.reason_code == 'RESUMED' and m.session_id == sid


def test_reload_after_9s_with_a_2_5s_capability_delay_ends_keepalive_lost():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    m.keepalive(sid, 'A', 2.0)
    assert m.tick(2.0 + 10.1, _frames(1.2, 12.1), 0.05, False) == 'KEEPALIVE_LOST'
    ans, ev = m.enter('A', 2.0 + 9 + 2.5)
    assert ev == 'new' and m.session_id != sid
    assert m.keepalive(sid, 'A', 14.0).end_reason == 'KEEPALIVE_LOST'


def test_second_tab_busy_takeover_and_owner_mismatch():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    m.keepalive(sid, 'A', 3.0)
    ans, _ = m.enter('B', 6.9)
    assert not ans.accepted and ans.reason_code == 'BUSY_OTHER_TAB'
    m.robot_ok = m.camera_ok = True
    gen = m.generation
    ans, ev = m.enter('B', 7.1)
    assert ans.accepted and ev == 'takeover' and m.owner == 'B'
    assert m.generation == gen + 1 and m.session_id != sid
    assert not m.robot_ok and not m.camera_ok
    old = m.keepalive(sid, 'A', 7.2)
    assert old.reason_code == 'OWNER_MISMATCH'
    leave, _ = m.leave(sid, 'A', False)
    assert leave.reason_code == 'OWNER_MISMATCH'
    new_sid = m.session_id
    m.leave(new_sid, 'B', False)
    after = m.keepalive(sid, 'A', 8.0)
    assert after.reason_code == 'SESSION_ENDED' and after.end_reason == ''


def test_takeover_during_entering_restarts_the_native_timeout_clock():
    m = SessionMachine(True, _ids())
    m.enter('A', 0.0)
    m.enter('B', 25.0)
    m.keepalive(m.session_id, 'B', 25.0)
    assert m.tick(31.0, [], None, False) == ''
    m.keepalive(m.session_id, 'B', 31.0)
    assert m.tick(55.5, [], None, False) == 'KEEPALIVE_LOST' or m.state == 'IDLE'


def test_duplicated_tab_shares_the_id_until_re_minted():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    ans, ev = m.enter('A', 1.5)        # the duplicate before its re-mint: a resume
    assert ev == 'resumed' and m.session_id == sid
    ans, ev = m.enter('A-reminted', 2.0)
    assert ans.reason_code == 'BUSY_OTHER_TAB'


def test_rosbridge_outage_6s_survives_11s_ends():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    m.keepalive(sid, 'A', 2.0)
    assert m.tick(8.0, _frames(1.2, 8.0), 0.05, False) == ''
    assert m.keepalive(sid, 'A', 8.0).accepted
    assert m.tick(19.1, _frames(1.2, 19.1), 0.05, False) == 'KEEPALIVE_LOST'


def test_server_restart_forgets_everything():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    fresh = SessionMachine(True, _ids())
    ans = fresh.keepalive(sid, 'A', 1.0)
    assert fresh.state == 'IDLE' and ans.reason_code == 'SESSION_ENDED' and ans.end_reason == ''


def test_gui_closed_and_stream_gap_revoke_camera_and_source_keep_robot():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    m.camera_ok = m.robot_ok = True
    epoch = m.revocations
    m.keepalive(sid, 'A', 3.0)
    m.tick(3.0, [], 2.5, False)
    assert m.state == 'ACTIVE' and not m.source_ok
    assert not m.camera_ok and m.robot_ok and m.revocations == epoch + 1
    m.keepalive(sid, 'A', 5.0)
    m.tick(5.0, _frames(3.5, 5.0), 0.05, False)
    assert m.source_ok and not m.camera_ok


def test_a_live_leader_ends_the_session():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    assert m.tick(2.0, _frames(0, 2.0), 0.05, True) == 'LEADER_ACTIVE'
    assert m.keepalive(sid, 'A', 2.1).end_reason == 'LEADER_ACTIVE'


def test_leave_refused_during_a_program_and_left_after():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    ans, ended = m.leave(sid, 'A', True)
    assert not ans.accepted and ans.reason_code == 'WORKFLOW_ACTIVE' and not ended
    ans, ended = m.leave(sid, 'A', False)
    assert ans.accepted and ended and m.state == 'IDLE'
    assert m.keepalive(sid, 'A', 3.0).end_reason == 'LEFT'


def test_a_check_in_flight_across_a_revocation_is_discarded():
    m = SessionMachine(True, _ids())
    sid = _active(m)
    token = m.check_token('d1')
    m.enter('A', 2.0)                   # a resume revokes the camera meanwhile
    assert not m.apply_camera(token, 'd1', True) and not m.camera_ok
    token = m.check_token('d1')
    assert not m.apply_camera(token, 'd2', True)          # digest changed
    token = m.check_token('d1')
    m.digest_changed()
    assert not m.apply_robot(token, 'd1', True) and not m.robot_ok
    token = m.check_token('d1')
    assert m.apply_robot(token, 'd1', True) and m.robot_ok


def test_new_session_refusal_runs_only_for_a_new_session():
    m = SessionMachine(True, _ids())
    calls = []

    def refusal():
        calls.append(1)
        return ('RETORQUE_PENDING', 'Die Einrichtung wird gerade beendet – bitte kurz warten.')
    ans, _ = m.enter('A', 0.0, refusal)
    assert ans.reason_code == 'RETORQUE_PENDING' and calls == [1]
    sid = _active(m, t=1.0)
    m.enter('A', 2.0, refusal)           # resume
    m.keepalive(sid, 'A', 2.0)
    m.enter('B', 7.0, refusal)           # takeover
    assert calls == [1]


def test_end_reasons_are_kept_for_16_sessions():
    m = SessionMachine(True, _ids())
    sids = []
    for i in range(18):
        sid = _active(m, t=10.0 * i)
        sids.append(sid)
        m.leave(sid, 'A', False)
    assert m.end_reason_for(sids[0]) == '' and m.end_reason_for(sids[-1]) == 'LEFT'
    assert len(m._ended) == S.ENDED_MEMORY
