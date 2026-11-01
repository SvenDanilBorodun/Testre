"""The node side of the station (plan v6.13 §5–§8, §12.1) with a stub node:
arbitration with the REAL ``_assert_no_other_active`` (extracted by ``ast``),
the D4 gate and the per-run binding, the worker thread, a full commission on
synthetic renders, the session checks, the zone mask and the bound getter."""

from __future__ import annotations

import ast
import textwrap
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip('cv2')

import station_fixture as F  # noqa: E402
from physical_ai_server.station import check as C  # noqa: E402
from physical_ai_server.station import commission_worker as CW  # noqa: E402
from physical_ai_server.station import layout_data, store  # noqa: E402
from physical_ai_server.station import texts_de as T  # noqa: E402
from physical_ai_server.station.service import StationService, parse_station_flag  # noqa: E402
from physical_ai_server.station.zone_perception import ZonePerception  # noqa: E402

_SERVER_PY = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
LAY = F.released_layout()
LAYOUTS = {F.TEST_LAYOUT_ID: LAY}


def _method(name):
    src = _SERVER_PY.read_text(encoding='utf-8')
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            ns = {'math': __import__('math')}
            exec(compile(textwrap.dedent(ast.get_source_segment(src, n)), str(_SERVER_PY),
                         'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(name)


_ASSERT = _method('_assert_no_other_active')


class _Log:
    def info(self, *a, **k):
        pass

    warning = error = info


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeComm:
    def __init__(self, clock=None):
        self.clock = clock
        self.frame = None
        self.seq = 0
        self.arrival = 0.0
        self.size = (1920, 1080)
        self.joints_age = 0.1

    def push(self, frame, size=(1920, 1080)):
        self.frame, self.size = frame, size
        self.seq += 1
        self.arrival = time.monotonic()

    def station_arrivals(self):
        now = self.clock() if self.clock else time.monotonic()
        return [(now - 1.5 + i * 0.05, self.size) for i in range(31)] if self.frame is not None else []

    def get_camera_msg_age_s(self, name):
        return None if self.frame is None else time.monotonic() - self.arrival

    def get_station_frame(self):
        if self.frame is None:
            return None, None, None, self.seq
        # every call is a new distinct frame (the stream keeps arriving)
        self.seq += 1
        return self.frame, 0.05, self.size, self.seq

    def get_follower_joints_age_s(self):
        return self.joints_age


class StubNode:
    def __init__(self, torque=True):
        self.on_recording = self.on_inference = self.is_training = False
        self.on_calibration = self.on_workflow = self.on_manual = False
        self.on_leader_teach = False
        self._collision_active = False
        self._follower_torque_on = None
        self._dxl_torque_lock = threading.Lock()
        self.torque_script = []
        self.torque_calls = []
        self.leader = False
        self.clock = FakeClock()
        self.communicator = FakeComm(self.clock)
        self.workflow_manager = None
        self.pose = None
        self._station = None
        self.torque_delay = 0.0

    def get_logger(self):
        return _Log()

    def _assert_no_other_active(self, mode):
        return _ASSERT(self, mode)

    def leader_appears_active(self):
        return self.leader

    def _set_calibration_active(self, active):
        self.on_calibration = active

    def _set_follower_torque(self, enabled):
        self.torque_calls.append(enabled)
        if self.torque_delay:
            time.sleep(self.torque_delay)
        ok = self.torque_script.pop(0) if self.torque_script else True
        if ok:
            self._follower_torque_on = bool(enabled)
        elif not enabled and self._follower_torque_on is True:
            self._follower_torque_on = None
        return ok

    def _last_torque_reason(self):
        return ''

    def _retorque_follower_or_keep_locked(self, response):
        for _ in range(3):
            if self._set_follower_torque(True):
                return True
        response.message = T.RELOCK_FAILED_DE
        response.success = False
        return False

    def _build_ik_solver(self):
        from physical_ai_server.workflow.ik_solver import IKSolver
        return IKSolver()

    def _get_current_gripper_pose(self):
        return self.pose

    def _build_perception(self):
        return None

    def _load_object_catalog(self):
        return None


def _svc(tmp_path, node=None, configured=False, **kw):
    node = node or StubNode()
    svc = StationService(node, threading.Lock(), threading.Lock(), True, 'omx_full',
                         base=tmp_path, layouts=LAYOUTS, clock=node.clock, **kw)
    node._station = svc
    if configured:
        f = F.station_fields(LAY)
        store.write_station(f, tmp_path, LAYOUTS)
    svc.boot()
    return svc, node


def _req(**kw):
    base = dict(command='', session_id='', client_id='', layout_id='', commission_id='',
                dot_id=0, mark_id='', cube_turn=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Resp(types.SimpleNamespace):
    def __getattr__(self, name):
        return None


def _session(svc, cmd, cid='A', sid=''):
    return svc.session_command(_req(command=cmd, client_id=cid, session_id=sid), _Resp())


def _activate(svc, node, cid='A'):
    node.communicator.push(F.render(LAY, F.camera_pose(), ss=1))
    r = _session(svc, 'enter', cid)
    assert r.accepted, r.message
    node.clock.t += 1.6
    _session(svc, 'keepalive', cid, r.session_id)
    svc.tick()
    assert svc.machine.state == 'ACTIVE'
    return r.session_id


# ── flag, rig, capability ─────────────────────────────────────────────────────
@pytest.mark.parametrize('raw,on', [('1', True), (' 1\n', True), ('"1"', False), ('0', False),
                                    ('', False), (None, False), ('true', False), ('11', False)])
def test_one_flag_parsing_rule(raw, on):
    assert parse_station_flag(raw) is on


def test_rig_capability_and_off(tmp_path):
    svc, _ = _svc(tmp_path)
    assert svc.rig and svc.capability_extra() == {'station_protocol': 6}
    off = StationService(StubNode(), threading.Lock(), threading.Lock(), True, 'v1_studio')
    assert not off.rig and off.capability_extra() == {} and off.machine.state == 'OFF'
    r = off.session_command(_req(command='status'), _Resp())
    assert r.state == 'OFF' and r.reason_code == 'NOT_STATION_RIG'


# ── arbitration (§5.3) with the real _assert_no_other_active ──────────────────
def test_enter_refusals_and_resume_takeover_do_not_rerun_arbitration(tmp_path):
    svc, node = _svc(tmp_path)
    node.on_recording = True
    r = _session(svc, 'enter')
    assert not r.accepted and r.reason_code == 'OTHER_MODE_ACTIVE'
    assert r.message == 'Aufnahme läuft gerade — bitte zuerst stoppen.'
    node.on_recording = False
    node.leader = True
    r = _session(svc, 'enter')
    assert r.reason_code == 'LEADER_ACTIVE' and r.message == T.LEADER_ACTIVE_ENTER_DE
    node.leader = False
    sid = _activate(svc, node)
    node.on_workflow = True                     # a Studio mode inside the session
    assert _session(svc, 'enter').reason_code == 'RESUMED'
    node.clock.t += 5.0
    r = _session(svc, 'enter', 'B')
    assert r.accepted and svc.machine.owner == 'B'


def test_recording_and_inference_refused_while_the_claim_is_held(tmp_path):
    svc, node = _svc(tmp_path)
    _activate(svc, node)
    for mode in ('recording', 'inference'):
        ok, msg = node._assert_no_other_active(mode)
        assert not ok and msg == T.CLAIM_REFUSAL_DE
    for mode in ('workflow', 'manual', 'capture', 'calibration'):
        assert node._assert_no_other_active(mode)[0], mode
    svc.machine._end('LEFT')
    assert node._assert_no_other_active('recording')[0]


def test_retorque_pending_only_for_an_enter_that_opens_a_new_session(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    svc.worker._record = CW.Record('c1', 1, True, (), True, False, False, False, '', None, False)
    node.on_calibration = True
    _session(svc, 'leave', 'A', sid)
    r = _session(svc, 'enter')
    assert r.reason_code == 'RETORQUE_PENDING' and r.message == T.RETORQUE_IN_PROGRESS_DE
    svc.worker._record = svc.worker._record._replace(abort_message='Arm konnte nicht …')
    assert _session(svc, 'enter').message == 'Arm konnte nicht …'


# ── the D4 gate and the per-run binding (§5.4) ────────────────────────────────
def test_gate_reasons_and_binding(tmp_path):
    svc, node = _svc(tmp_path)
    assert svc.gate_motion() == T.GATE_NOT_CONFIGURED_DE
    svc2, node2 = _svc(tmp_path / 'b', configured=True)
    assert svc2.gate_motion() == T.GATE_NOT_VERIFIED_DE
    _activate(svc2, node2)
    svc2.machine.camera_ok = svc2.machine.robot_ok = True
    assert svc2.gate_motion() == ''
    binding, reason = svc2.bind_run()
    assert reason == '' and binding['camera'] == 'station_scene'
    assert binding['calibration'] is svc2.calibration
    assert svc2.continue_refusal(svc2.digest()) == ''
    assert svc2.continue_refusal('other') == T.GATE_DIGEST_CHANGED_DE
    svc2.machine.digest_changed()
    assert svc2.continue_refusal(svc2.digest()) == T.GATE_NOT_VERIFIED_DE


def test_a_corrupt_yaml_never_weakens_the_gate(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    store.yaml_path(tmp_path).write_bytes(b'broken')
    svc.reload_calibration()
    assert not svc.configured and svc.gate_motion() == T.GATE_NOT_CONFIGURED_DE


def test_status_answers_workflow_active_for_a_real_run(tmp_path):
    svc, node = _svc(tmp_path)
    node.workflow_manager = types.SimpleNamespace(is_running=True)
    assert _session(svc, 'status').workflow_active is True
    node.workflow_manager = types.SimpleNamespace(is_running=False)
    assert _session(svc, 'status').workflow_active is False


# ── the worker thread ─────────────────────────────────────────────────────────
def _run_jobs(svc):
    for _ in range(40):
        with svc.mode_lock, svc.lock:
            started = svc.worker.take_next()
        if not started and svc.worker.running is None and not svc.worker.queue:
            break
        svc.run_current_job()


def test_begin_relocks_first_after_a_respawn_and_claims(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    node._follower_torque_on = None              # a respawn left the flag unknown
    r = svc.commission_command(_req(command='begin', session_id=sid, client_id='A',
                                    layout_id=F.TEST_LAYOUT_ID), _Resp())
    assert r.result == 'QUEUED', r.message
    _run_jobs(svc)
    assert node.torque_calls[0] is True and node.on_calibration
    st = svc.commission_command(_req(command='status', session_id=sid, client_id='A'), _Resp())
    assert st.commission_id and st.step == 'lens' and st.job_state == 'done'


def test_begin_refuses_a_draft_or_unknown_layout_and_a_failed_relock_claims_nothing(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    for lid in ('OMX-STATION-A', 'NOPE'):
        r = svc.commission_command(_req(command='begin', session_id=sid, client_id='A',
                                        layout_id=lid), _Resp())
        assert r.result == 'REFUSED' and r.message == T.LAYOUT_NOT_RELEASED_DE
    node.torque_script = [False, False, False]
    svc.commission_command(_req(command='begin', session_id=sid, client_id='A',
                                layout_id=F.TEST_LAYOUT_ID), _Resp())
    _run_jobs(svc)
    assert not node.on_calibration and svc.worker.record is None


def test_a_keepalive_is_answered_while_a_slow_job_runs_and_the_timer_never_blocks(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    svc.commission_command(_req(command='begin', session_id=sid, client_id='A',
                                layout_id=F.TEST_LAYOUT_ID), _Resp())
    node.torque_delay = 1.5                      # a re-torque stub that hangs
    t = threading.Thread(target=_run_jobs, args=(svc,), daemon=True)
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    assert _session(svc, 'keepalive', 'A', sid).accepted
    svc.tick()
    assert time.monotonic() - t0 < 0.3
    t.join(5)


def test_a_session_end_aborts_the_commission_on_the_worker(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    svc.commission_command(_req(command='begin', session_id=sid, client_id='A',
                                layout_id=F.TEST_LAYOUT_ID), _Resp())
    _run_jobs(svc)
    cid = svc.worker.record.commission_id
    svc.commission_command(_req(command='touch_begin', session_id=sid, client_id='A',
                                commission_id=cid), _Resp())
    _run_jobs(svc)
    assert svc.worker.touch_active() and node._follower_torque_on is False
    node.torque_script = [False] * 3
    _session(svc, 'leave', 'A', sid)
    _run_jobs(svc)
    assert node.on_calibration and svc.worker.record.abort_marked     # kept over a limp arm
    svc.worker.promote_due(node.clock() + 3)
    _run_jobs(svc)
    assert not node.on_calibration and svc.worker.record is None
    assert node._follower_torque_on is True


# ── a full commission on renders, then the session checks ─────────────────────
def test_commission_end_to_end_then_camera_and_robot_check_verify(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    own = dict(session_id=sid, client_id='A')
    svc.commission_command(_req(command='begin', layout_id=F.TEST_LAYOUT_ID, **own), _Resp())
    _run_jobs(svc)
    cid = svc.worker.record.commission_id
    K, dist = F.ftheta_intrinsics()
    svc.commission.intrinsics = {'K': K, 'dist': dist, 'fit_rms': 0.4, 'held_rms': 0.5,
                                 'held_max': 1.0}
    r = svc.commission_command(_req(command='board_solve', commission_id=cid, **own), _Resp())
    assert r.result == 'OK', r.message

    class Det:
        def __init__(self, i, c):
            self.aruco_id, self.corners_px, self.centroid_px = i, c, c.mean(axis=0)

    node._build_perception = lambda: types.SimpleNamespace(
        detect=lambda img, camera, mode, aruco_id=None: [Det(i, c) for i, c in C.detect_tags(img)])
    for mark_id, turn in svc.commission.order():
        x, y = dict(svc.commission.targets)[mark_id]
        node.communicator.push(F.render(LAY, F.camera_pose(), ss=1,
                                        cubes=[(x, y, 20, 90 * turn)]))
        r = svc.commission_command(_req(command='mark_capture', commission_id=cid,
                                        mark_id=mark_id, cube_turn=turn, **own), _Resp())
        assert r.result == 'OK', (mark_id, r.message)
    assert svc.commission_command(_req(command='mark_solve', commission_id=cid, **own),
                                  _Resp()).result == 'OK'
    node.communicator.push(F.render(LAY, F.camera_pose(), ss=1))
    svc.commission_command(_req(command='touch_begin', commission_id=cid, **own), _Resp())
    _run_jobs(svc)
    ik = node._build_ik_solver()
    for d in LAY['dots']:
        q = ik.solve((d['centre_mm'][0] / 1000, d['centre_mm'][1] / 1000, ik.tool_tip_below_ee_m))
        node.pose = ik.fk(list(q) + [0.0])
        r = svc.commission_command(_req(command='touch_capture', commission_id=cid,
                                        dot_id=d['id'], **own), _Resp())
        assert r.result == 'OK', r.message
    svc.commission_command(_req(command='touch_solve', commission_id=cid, **own), _Resp())
    _run_jobs(svc)
    assert svc.worker.record.solved, svc.worker.job_state(svc.worker.owner_last_job(sid))
    r = svc.commission_command(_req(command='commit', commission_id=cid, **own), _Resp())
    assert r.result == 'QUEUED', r.message
    _run_jobs(svc)
    assert svc.configured and not node.on_calibration and svc.worker.record is None
    st = svc.commission_command(_req(command='status', **own), _Resp())
    assert st.job_state == 'done'
    # camera check, robot check → verified
    c = svc.check_command(_req(command='camera', **own), _Resp())
    assert c.result == 'OK' and c.camera_ok, c.message
    node.pose = ik.fk(list(ik.solve((0.1, 0.12, ik.tool_tip_below_ee_m))) + [0.0])
    rb = svc.check_command(_req(command='robot', **own), _Resp())
    assert rb.result == 'OK' and rb.verified, rb.message
    assert not svc.check_command(_req(command='camera', session_id=sid, client_id='X'),
                                 _Resp()).accepted
    # resume: camera re-checked, robot kept
    _session(svc, 'enter', 'A')
    assert svc.machine.robot_ok and not svc.machine.camera_ok
    # a re-solve revokes robot_ok; restore_previous twice is refused without a previous
    svc.machine.camera_ok = True
    r = svc.check_command(_req(command='resolve_camera', **own), _Resp())
    assert r.result == 'QUEUED'
    _run_jobs(svc)
    assert not svc.machine.robot_ok and svc.machine.camera_ok
    status = svc.check_command(_req(command='status'), _Resp())
    assert status.job_state == 'done'


def test_a_run_jog_or_replay_during_a_resolve_is_refused_by_on_calibration(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    _activate(svc, node)
    node.on_calibration = True
    for mode in ('workflow', 'manual'):
        ok, msg = node._assert_no_other_active(mode)
        assert not ok and msg.startswith('Kalibrierung läuft gerade')


def test_the_owner_status_poll_resets_the_idle_clock_a_non_owner_does_not(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    before = svc.worker._last_owner_activity
    svc.commission_command(_req(command='status', session_id=sid, client_id='X'), _Resp())
    assert svc.worker._last_owner_activity == before
    svc.commission_command(_req(command='status', session_id=sid, client_id='A'), _Resp())
    assert svc.worker._last_owner_activity > before


def test_a_commission_step_without_a_frame_answers_no_frame_and_keeps_its_step(tmp_path):
    svc, node = _svc(tmp_path)
    sid = _activate(svc, node)
    svc.commission_command(_req(command='begin', session_id=sid, client_id='A',
                                layout_id=F.TEST_LAYOUT_ID), _Resp())
    _run_jobs(svc)
    cid = svc.worker.record.commission_id
    node.communicator.frame = None
    r = svc.commission_command(_req(command='lens_capture', session_id=sid, client_id='A',
                                    commission_id=cid), _Resp())
    assert r.result == 'NO_FRAME' and svc.commission.step_name() == 'lens'


def test_calibration_status_fields(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    resp = types.SimpleNamespace(station_protocol=0, station_rig=False, station_configured=False,
                                 station_verified=False, station_state='', station_reason='')
    svc.fill_calibration_status(resp)
    assert resp.station_protocol == 6 and resp.station_rig and resp.station_configured
    assert resp.station_state == 'IDLE' and not resp.station_verified


# ── the zone mask and the bound getter ────────────────────────────────────────
class _Det:
    def __init__(self, tag_id, centre):
        self.aruco_id = tag_id
        self.corners_px = np.asarray(centre, dtype=float).reshape(1, 2)
        self.centroid_px = tuple(centre)


def _project(cal, x, y, z):
    K = cal['scene_intrinsics']['K']
    dist = cal['scene_intrinsics']['dist']
    T_inv = np.linalg.inv(cal['scene_extrinsics'])
    p = T_inv @ np.array([x, y, z, 1.0])
    px, _ = cv2.projectPoints(p[:3].reshape(1, 3), np.zeros(3), np.zeros(3), K, dist)
    return px.reshape(2)


def test_zone_mask_drops_a_cube_12mm_outside_with_an_8mm_1_5deg_registration():
    import math
    f = F.station_fields(LAY, reg=(math.radians(1.5), 0.0057, 0.0057))
    store.yaml_path  # noqa: B018
    cal = store.calibration_dict(f, 'd')
    poly = store.polygon_to_base(f)

    class Catalog:
        def recipe_for_tag(self, tid):
            return types.SimpleNamespace(object_height_m=0.030) if tid in (20, 21) else (
                types.SimpleNamespace(object_height_m=0.060) if tid == 30 else None)
    inner = types.SimpleNamespace(detect=lambda *a, **k: dets)
    zone = ZonePerception(inner, cal, poly, lambda: Catalog())
    # the zone's outer arc in the base frame, 12 mm outside and 30 mm inside
    th = math.radians(1.5)
    Rr = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    centre = Rr @ np.array([-0.01125, 0.0]) + 0.0057
    out_xy = centre + np.array([0.250 + 0.012 + 0.010, 0.0])
    in_xy = centre + np.array([0.200, 0.0])
    dets = [_Det(20, _project(cal, *out_xy, 0.030)), _Det(21, _project(cal, *in_xy, 0.030)),
            _Det(30, _project(cal, *in_xy, 0.060))]
    kept = zone.detect(None, 'scene', 'apriltag')
    assert [d.aruco_id for d in kept] == [21, 30]      # the A13 object at its own height


def test_the_simulator_never_wraps_and_the_bound_getter_guards_the_size():
    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    logs = []
    for simulated in (False, True):
        wm = WorkflowManager(publisher=lambda pts: None,
                             emit_status=lambda p: logs.append(p), simulated=simulated)
        frame = np.zeros((480, 640, 3), np.uint8)
        get, _ = wm._bound_getters({'get_scene_frame': lambda: frame,
                                    'get_scene_frame_age': lambda: 0.0},
                                   {'image_size': (1920, 1080)})
        assert get() is None and get() is None
    assert [p['log_message'] for p in logs].count(
        '[WARNUNG] Kamerabild passt nicht zur Kalibrierung.') == 2       # once per run
    get, _ = WorkflowManager(publisher=lambda p: None)._bound_getters(
        {'get_scene_frame': lambda: np.zeros((1080, 1920, 3), np.uint8)},
        {'image_size': (1920, 1080)})
    assert get() is not None
    src = (Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'workflow'
           / 'workflow_manager.py').read_text(encoding='utf-8')
    assert "if wrap is not None and perception_instance is not None and not self._simulated" in src


def test_a_station_run_is_bound_to_station_scene_never_scene():
    src = _SERVER_PY.read_text(encoding='utf-8')
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == '_station_run_binding')
    seg = ast.get_source_segment(src, fn)
    assert "'scene'" not in seg and "binding['camera']" in seg


# ── manual calibration refused, cancel inert, routing (§5.4) ──────────────────
@pytest.mark.parametrize('name', ['calibration_start_callback', 'calibration_capture_callback',
                                  'calibration_solve_callback', 'calibration_preview_callback',
                                  'calibration_verify_callback'])
def test_manual_calibration_is_refused_on_a_station_rig(name):
    fn = _method(name)
    node = types.SimpleNamespace(_station=types.SimpleNamespace(rig=True))
    resp = fn(node, types.SimpleNamespace(), types.SimpleNamespace())
    assert resp.success is False and resp.message == T.MANUAL_CALIBRATION_REFUSED_DE


def test_manual_cancel_is_inert_on_a_station_rig():
    fn = _method('calibration_cancel_callback')
    touched = []
    node = types.SimpleNamespace(
        _station=types.SimpleNamespace(rig=True),
        _calibration_stop_event=types.SimpleNamespace(set=lambda: touched.append('stop')),
        _set_calibration_active=lambda a: touched.append(('cal', a)))
    resp = fn(node, types.SimpleNamespace(camera=''), types.SimpleNamespace())
    assert resp.success is True and touched == []


def test_load_workflow_calibration_routes_to_the_station_and_never_to_disk(tmp_path):
    fn = _method('_load_workflow_calibration')
    cal = types.SimpleNamespace(calibration={'digest': 'x', 'z_table': 0.04})
    node = types.SimpleNamespace(_station=types.SimpleNamespace(rig=True, calibration=cal))
    assert fn(node) == {'digest': 'x', 'z_table': 0.04}
    node = types.SimpleNamespace(_station=types.SimpleNamespace(rig=True, calibration=None))
    assert fn(node) == {}


def test_mark_destination_routes_and_refuses_scene_on_a_station_rig(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    _activate(svc, node)
    svc.machine.camera_ok = svc.machine.robot_ok = True
    fn = _method('_station_mark_destination')
    import math as _math
    stored = []
    node.get_logger = lambda: _Log()
    node._station_lock = svc.lock
    node._get_or_create_workflow_manager = lambda: types.SimpleNamespace(
        set_destination=lambda *a, **k: stored.append(a))
    resp = fn(node, types.SimpleNamespace(camera='scene', pixel_x=10, pixel_y=10, label='A'),
              types.SimpleNamespace())
    assert resp.success is False and resp.message == T.MARK_SCENE_REFUSED_DE
    cal = svc.calibration.calibration
    px = _project(cal, 0.2, 0.05, 0.0)
    resp = fn(node, types.SimpleNamespace(camera='station_scene', pixel_x=float(px[0]),
                                          pixel_y=float(px[1]), label='A'),
              types.SimpleNamespace())
    assert resp.success, resp.message
    assert abs(resp.world_x - 0.2) < 0.002 and abs(resp.world_y - 0.05) < 0.002
    assert stored and _math.isfinite(resp.world_z)
    svc.machine.robot_ok = False
    resp = fn(node, types.SimpleNamespace(camera='station_scene', pixel_x=float(px[0]),
                                          pixel_y=float(px[1]), label='A'),
              types.SimpleNamespace())
    assert resp.success is False and resp.message == T.GATE_NOT_VERIFIED_DE


def _segment(name):
    src = _SERVER_PY.read_text(encoding='utf-8')
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, fn)


@pytest.mark.parametrize('name,claim', [
    ('workshop_jog_callback', "self._assert_no_other_active('manual')"),
    ('workshop_replay_callback', "self._assert_no_other_active('manual')"),
])
def test_the_d4_gate_runs_at_operation_start_before_the_claim(name, claim):
    seg = _segment(name)
    i_gate = seg.index('_station.gate_motion()')
    assert i_gate < seg.index(claim), name


def test_a_real_run_binds_before_the_torque_assert_and_continue_step_check_the_digest():
    seg = _segment('_launch_workflow')
    i_bind = seg.index('station.bind_run()')
    assert i_bind < seg.index("torque_on(True)") < seg.index("'run_binding': run_binding")
    for name in ('workflow_continue_callback', 'workflow_step_callback'):
        seg = _segment(name)
        assert seg.index('continue_refusal') < seg.index('manager.')


def test_not_gated_paths_stay_ungated():
    for name in ('workshop_hand_guide_callback', 'workshop_record_callback',
                 'workflow_stop_callback', 'capture_pose_callback'):
        assert 'gate_motion' not in _segment(name), name
