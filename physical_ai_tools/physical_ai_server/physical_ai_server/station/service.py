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

"""The node side of the OMX camera station (plan v6.13 §5–§8), rclpy-free.

``PhysicalAIServer`` builds one :class:`StationService` and delegates the three
station services, the 5 Hz timer, the D4 motion gate, the per-run binding and
the claim refusals to it. Everything the node owns is reached through ``node``
(duck-typed, so the tests drive it with a stub): the mode flags,
``_assert_no_other_active``, ``leader_appears_active``, the torque switch, the
calibration claim, FK, perception and the Communicator's station frame store.

Locks: the node's ``_mode_lock`` → ``_station_lock`` only, each hold O(1). Every
torque change of a commission runs on the ONE station worker thread (plan §6),
never on the timer and never under a lock.
"""

import shutil
import threading
import time
import types
import uuid

import numpy as np

from physical_ai_server.station import check as C
from physical_ai_server.station import commission as CM
from physical_ai_server.station import commission_worker as CW
from physical_ai_server.station import store
from physical_ai_server.station import texts_de as T
from physical_ai_server.station.state import SessionMachine

OMX_PROFILES = ('omx_full', 'omx_follower')
CHECK_FRAME_MAX_AGE_S = 0.5
CHECK_RETRY_WINDOW_S = 2.0
BOARD_BURST_TIMEOUT_S = 3.0


def parse_station_flag(raw):
    """One parsing rule (§3.1): whitespace stripped, exactly '1' is on."""
    if raw is None:
        return False
    return ''.join(str(raw).split()) == '1'


class _Env:
    """The worker's view of the node, valid only under _mode_lock → _station_lock."""

    def __init__(self, svc):
        self.svc = svc

    def now(self):
        return self.svc.clock()

    def modes_idle(self):
        n = self.svc.node
        return not (getattr(n, 'on_calibration', False) or getattr(n, 'on_workflow', False)
                    or getattr(n, 'on_manual', False) or getattr(n, 'on_leader_teach', False))

    def session_active(self, sid):
        m = self.svc.machine
        return bool(sid) and m.state == 'ACTIVE' and m.session_id == sid

    def torque_is_true(self):
        return getattr(self.svc.node, '_follower_torque_on', None) is True

    def claim_calibration(self):
        ok, _ = self.svc.node._assert_no_other_active('calibration')
        if not ok:
            return False
        self.svc.node._set_calibration_active(True)
        return True

    def release_calibration(self):
        self.svc.node._set_calibration_active(False)

    def new_commission_id(self):
        return uuid.uuid4().hex[:12]

    def finish_work(self, kind, ok, payload):
        self.svc._finish_work_locked(kind, ok, payload)

    def commit_swap(self, commission_id, payload):
        self.svc._swap_locked(payload)


class StationService:
    def __init__(self, node, mode_lock, station_lock, station_enabled, profile_id,
                 base=None, clock=time.monotonic, layouts=None):
        self.node = node
        self.mode_lock = mode_lock
        self.lock = station_lock
        self.clock = clock
        self.base = base
        self.layouts = layouts
        self.profile_id = profile_id
        self.rig = bool(station_enabled) and profile_id in OMX_PROFILES
        self.machine = SessionMachine(self.rig)
        self.worker = CW.CommissionWorker()
        self.env = _Env(self)
        self.calibration = None
        self.calibration_reason = T.GATE_NOT_CONFIGURED_DE
        self.commission = None
        self._pending_layout = None
        self._last_check = None          # (generation, dict)
        self._last_check_job = 0
        self._check_running = False
        self._resolve_token = None
        self._wake = threading.Event()
        self._stop = False
        self._thread = None
        self._mark_unknown_hook = None

    # ── boot ──────────────────────────────────────────────────────────────────
    def boot(self):
        """At server start: delete leftover staging dirs, parse the YAML once."""
        if not self.rig:
            return
        try:
            store.cleanup_staging(self.base)
        except Exception as e:  # noqa: BLE001
            self._log('warning', f'station staging cleanup failed: {e!r}')
        self.reload_calibration()

    def reload_calibration(self):
        cal, reason = store.load(store.yaml_path(self.base), self.layouts)
        self.calibration = cal
        self.calibration_reason = '' if cal is not None else reason

    def start_worker(self):
        if self._thread is not None or not self.rig:
            return
        self._thread = threading.Thread(target=self._worker_loop, name='station-worker',
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _log(self, level, msg):
        try:
            getattr(self.node.get_logger(), level)(msg)
        except Exception:  # noqa: BLE001
            pass

    # ── state views ───────────────────────────────────────────────────────────
    @property
    def configured(self):
        return self.rig and self.calibration is not None

    def digest(self):
        return self.calibration.digest if self.calibration is not None else ''

    def verified_locked(self):
        return self.machine.verified(self.configured)

    def holds_claim(self):
        return self.rig and self.machine.holds_claim

    def workflow_active(self):
        """A real (non-sim) program is running or paused."""
        try:
            mgr = getattr(self.node, 'workflow_manager', None)
            return bool(mgr is not None and mgr.is_running)
        except Exception:  # noqa: BLE001
            return False

    # ── frames ────────────────────────────────────────────────────────────────
    def _comm(self):
        return getattr(self.node, 'communicator', None)

    def _arrivals(self):
        comm = self._comm()
        fn = getattr(comm, 'station_arrivals', None) if comm is not None else None
        return fn() if callable(fn) else []

    def _frame_age(self):
        comm = self._comm()
        fn = getattr(comm, 'get_camera_msg_age_s', None) if comm is not None else None
        return fn(T.STATION_CAMERA) if callable(fn) else None

    def station_frame(self):
        """(bgr | None, arrival_age_s | None, (w, h) | None, seq)."""
        comm = self._comm()
        fn = getattr(comm, 'get_station_frame', None) if comm is not None else None
        if not callable(fn):
            return None, None, None, -1
        return fn()

    def fresh_frame(self, max_age=CHECK_FRAME_MAX_AGE_S, after_seq=-1):
        """('NO_FRAME' | 'WRONG_SIZE' | 'OK', bgr, seq)."""
        bgr, age, size, seq = self.station_frame()
        if bgr is None or age is None or age > max_age or seq <= after_seq:
            return 'NO_FRAME', None, seq
        if tuple(size or ()) != T.STATION_IMAGE_SIZE or bgr.shape[:2] != (1080, 1920):
            return 'WRONG_SIZE', None, seq
        return 'OK', bgr, seq

    # ── timer (5 Hz, its own MutuallyExclusive group) ─────────────────────────
    def tick(self):
        if not self.rig:
            return
        now = self.clock()
        arrivals = self._arrivals()
        age = self._frame_age()
        try:
            leader = bool(self.node.leader_appears_active())
        except Exception:  # noqa: BLE001
            leader = False
        with self.mode_lock, self.lock:
            ended = self.machine.tick(now, arrivals, age, leader)
            if ended:
                self.worker.mark_abort()
            self.worker.idle_tick(now)
            self.worker.promote_due(now)
            work = self.worker.station_job_pending()
        if work:
            self._wake.set()

    # ── the one station worker thread ─────────────────────────────────────────
    def _worker_loop(self):
        while not self._stop:
            with self.mode_lock, self.lock:
                self.worker.promote_due(self.clock())
                started = self.worker.take_next()
                self._sync_commission_locked()
            if not started:
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            try:
                self.run_current_job()
            except Exception as e:  # noqa: BLE001 — a job must never kill the worker
                self._log('error', f'station job failed: {e!r}')
                with self.mode_lock, self.lock:
                    if self.worker.running is not None:
                        self.worker._end(CW.FAILED, T.COMMISSION_STALE_DE)

    def run_current_job(self):
        """Step the running job to its end, executing effects outside the locks."""
        while True:
            with self.mode_lock, self.lock:
                if self.worker.running is None:
                    self._sync_commission_locked()
                    return
                job = self.worker.running.job
                eff = self.worker.step(self.env)
                token = (self.machine.check_token(self.digest())
                         if eff is not None and eff.kind == CW.E_WORK else None)
                self._sync_commission_locked()
            if eff is None:
                continue
            result = self._execute(eff, job, token)
            if eff.needs_result:
                with self.mode_lock, self.lock:
                    self.worker.deliver(*result)

    def _mark_torque_unknown(self):
        """Rule 3 / rule 9: a True becomes None (a False stays), so the next
        torque-on publishes the measured-pose hold first."""
        lock = getattr(self.node, '_dxl_torque_lock', None)
        if lock is None:
            if getattr(self.node, '_follower_torque_on', None) is True:
                self.node._follower_torque_on = None
            return
        with lock:
            if getattr(self.node, '_follower_torque_on', None) is True:
                self.node._follower_torque_on = None

    def _execute(self, eff, job, token):
        n = self.node
        if eff.kind == CW.E_TORQUE_ON:
            if eff.args[0]:
                self._mark_torque_unknown()
            ok = bool(n._set_follower_torque(True))
            return ok, '' if ok else (T.RELOCK_FAILED_DE + n._last_torque_reason()), None
        if eff.kind == CW.E_TORQUE_OFF:
            ok = bool(n._set_follower_torque(False))
            return ok, '' if ok else (T.TOUCH_OFF_FAILED_DE + n._last_torque_reason()), None
        if eff.kind == CW.E_RETORQUE:
            if eff.args[0]:
                self._mark_torque_unknown()
            resp = types.SimpleNamespace(success=True, message='')
            ok = bool(n._retorque_follower_or_keep_locked(resp))
            return ok, '' if ok else resp.message, None
        if eff.kind == CW.E_SOLVE:
            return True, '', self._solve_touch(eff.args[1])
        if eff.kind == CW.E_WRITE:
            return self._write_commit(eff.args[0], eff.args[1])
        if eff.kind == CW.E_WORK:
            return self._work(eff.args[0], token)
        if eff.kind == CW.E_CLEANUP:
            shutil.rmtree(store.staging_dir(eff.args[0], self.base), ignore_errors=True)
            return True, '', None
        raise RuntimeError(eff.kind)

    def _sync_commission_locked(self):
        rec = self.worker.record
        if rec is None:
            self.commission = None
            return
        if self.commission is None or self.commission.commission_id != rec.commission_id:
            layout = self._pending_layout
            self.commission = CM.Commission(rec.commission_id, layout,
                                            store.staging_dir(rec.commission_id, self.base))
            self.worker.set_ready(rec.commission_id, False)

    def _tip_below(self):
        ik = self.node._build_ik_solver()
        return float(getattr(ik, 'tool_tip_below_ee_m', 0.0) or 0.0) if ik else 0.0

    def _solve_touch(self, taps):
        com = self.commission
        if com is None:
            return False, T.COMMISSION_STALE_DE, None
        board_z = 0.0
        ok, msg, sol = CM.solve_touch(list(taps), com.layout, self._tip_below(), board_z)
        return ok, msg, sol

    def _write_commit(self, cid, solution):
        com = self.commission
        if com is None or com.commission_id != cid or solution is None:
            return False, T.WRITE_FAILED_DE, None
        try:
            fields = com.yaml_fields(solution, self.profile_id)
            store.write_station(fields, self.base, self.layouts)
            cal, reason = store.load(store.yaml_path(self.base), self.layouts)
            if cal is None:
                return False, reason or T.WRITE_FAILED_DE, None
            try:
                store.write_evidence(cid, {
                    'commission_id': cid, 'world_rms_mm': com.world['rms_mm'],
                    'world_max_mm': com.world['max_mm'], 'marks': {
                        f'{k[0]}@{90 * k[1]}': v for k, v in com.marks.items()},
                    'registration': {k: solution[k] for k in (
                        'registration_rms_mm', 'registration_max_mm',
                        'registration_shift_mm', 'registration_rot_deg')},
                }, base=self.base)
            except Exception as e:  # noqa: BLE001 — evidence is best-effort
                self._log('warning', f'station evidence not written: {e!r}')
            return True, T.COMMIT_DONE_DE, cal
        except Exception as e:  # noqa: BLE001
            self._log('error', f'station write failed: {e!r}')
            return False, T.WRITE_FAILED_DE, None

    def _work(self, kind, token):
        if kind == CW.RESTORE:
            try:
                ok, msg = store.restore_previous(self.base, self.layouts)
            except Exception as e:  # noqa: BLE001
                self._log('error', f'station restore failed: {e!r}')
                return False, T.RESTORE_INVALID_DE, None
            if not ok:
                return False, msg, None
            cal, reason = store.load(store.yaml_path(self.base), self.layouts)
            return (cal is not None), (msg if cal is not None else reason), {'cal': cal}
        return self._resolve(token)

    def _burst(self, count=CM.BOARD_BURST, timeout=BOARD_BURST_TIMEOUT_S):
        frames, seq = [], -1
        deadline = self.clock() + timeout
        while len(frames) < count and self.clock() < deadline:
            status, bgr, s = self.fresh_frame(after_seq=seq)
            if status == 'OK':
                frames.append(bgr)
                seq = s
            else:
                time.sleep(0.03)
        return frames

    def _resolve(self, token):
        cal = self.calibration
        if cal is None:
            return False, T.NOT_CONFIGURED_DE, None
        f = cal.fields
        frames = self._burst()
        if len(frames) < CM.BOARD_MIN_PASS:
            return False, T.COMMISSION_NO_FRAME_DE, None
        from physical_ai_server.workflow.calibration_manager import CalibrationManager
        layout = store.layout_entry(cal.layout_id, self.layouts)
        stage = store.station_dir(self.base) / '.staging-resolve'
        mgr = CalibrationManager(root=stage, board_to_base=store.board_to_mat(layout))
        K, dist = f['camera_matrix'], f['distortion_coefficients']
        T_new, _, _, msg = CM.solve_board_burst(mgr, frames, K, dist, layout)
        shutil.rmtree(stage, ignore_errors=True)
        if T_new is None:
            return False, msg, None
        check_cal = {'K': K, 'dist': dist, 'T_cam_mat': T_new, 'ref_ids': f['ref_ids'],
                     'ref_corners_commissioned': f['ref_corners_commissioned']}
        res = C.camera_check(frames[:3], check_cal)
        within, shift, rot = C.resolve_within_bounds(T_new, f['T_cam_mat_commissioned'])
        if res['result'] != 'OK' or not within:
            return False, T.RESOLVE_REFUSED_DE, {'check': res}
        fields = CM.resolve_fields(f, T_new)
        try:
            store.write_station(fields, self.base, self.layouts)
        except Exception as e:  # noqa: BLE001
            self._log('error', f'station re-solve write failed: {e!r}')
            return False, T.WRITE_FAILED_DE, None
        new_cal, reason = store.load(store.yaml_path(self.base), self.layouts)
        if new_cal is None:
            return False, reason, None
        return True, T.RESOLVE_OK_DE, {'cal': new_cal, 'check': res, 'token': token}

    # Called by the worker's env under _mode_lock → _station_lock.
    def _swap_locked(self, cal):
        if cal is None:
            return
        self.calibration = cal
        self.calibration_reason = ''
        self.machine.digest_changed()

    def _finish_work_locked(self, kind, ok, payload):
        if kind == CW.RESOLVE:
            payload = payload or {}
            check = payload.get('check')
            if check is not None:
                self._last_check = (self.machine.generation, dict(check, result=(
                    'OK' if ok else 'RESOLVE_REFUSED')))
        if not ok or not payload or payload.get('cal') is None:
            return
        token = payload.get('token')
        self._swap_locked(payload['cal'])
        if kind == CW.RESOLVE and token is not None:
            gen, rev, _ = token
            m = self.machine
            # §7.3: camera_ok only if the counter moved by the writer's own bump.
            if m.state == 'ACTIVE' and m.generation == gen and m.revocations == rev + 1:
                m.camera_ok = bool(m.source_ok)

    # ── arbitration (§5.3) ────────────────────────────────────────────────────
    def claim_refusal(self, requested_mode):
        """Recording / inference starts while the claim is held."""
        if requested_mode in ('recording', 'inference') and self.holds_claim():
            return T.CLAIM_REFUSAL_DE
        return ''

    def _arm_idle_refusal(self):
        """Rule 9's "arm idle by its mode flags" for the claim services."""
        n = self.node
        if (getattr(n, 'on_calibration', False) or self.worker.touch_active()
                or getattr(n, 'on_workflow', False) or getattr(n, 'on_manual', False)
                or getattr(n, 'on_leader_teach', False)):
            if getattr(n, 'on_calibration', False) or self.worker.touch_active():
                return T.CALIBRATION_ACTIVE_DE
            _, msg = n._assert_no_other_active('calibration')
            return msg or T.CALIBRATION_ACTIVE_DE
        return ''

    def _new_session_refusal(self):
        """Called by the machine only for an enter that opens a NEW session."""
        try:
            if self.node.leader_appears_active():
                return ('LEADER_ACTIVE', T.LEADER_ACTIVE_ENTER_DE)
        except Exception:  # noqa: BLE001
            pass
        pending = self.worker.retorque_pending_message()
        if pending:
            return ('RETORQUE_PENDING', pending)
        ok, msg = self.node._assert_no_other_active('station')
        if not ok:
            return ('OTHER_MODE_ACTIVE', msg)
        return None

    # ── D4 motion gate and the per-run binding (§5.4) ──────────────────────────
    def gate_refusal_locked(self):
        if not self.configured:
            return T.GATE_NOT_CONFIGURED_DE
        if not self.verified_locked():
            return T.GATE_NOT_VERIFIED_DE
        return ''

    def gate_motion(self):
        """'' when real motion may start, else the German reason (D4)."""
        if not self.rig:
            return ''
        with self.lock:
            return self.gate_refusal_locked()

    def bind_run(self):
        """(binding, '') or (None, reason): checks verified and captures the
        camera name and the in-memory StationCalibration in ONE hold."""
        if not self.rig:
            return None, ''
        with self.lock:
            reason = self.gate_refusal_locked()
            if reason:
                return None, reason
            return {'camera': T.STATION_CAMERA, 'calibration': self.calibration}, ''

    def continue_refusal(self, bound_digest):
        if not self.rig:
            return ''
        with self.lock:
            reason = self.gate_refusal_locked()
            if reason:
                return reason
            if bound_digest is not None and bound_digest != self.digest():
                return T.GATE_DIGEST_CHANGED_DE
            return ''

    # ── /station/session ──────────────────────────────────────────────────────
    def _fill_session(self, resp, client_id):
        m = self.machine
        resp.state = m.state
        resp.camera_check_epoch = int(m.revocations) & 0xFFFFFFFF
        resp.session_id = m.visible_session_id(client_id)
        resp.generation = int(m.generation) & 0xFFFFFFFF
        resp.station_configured = bool(self.configured)
        resp.source_ok = bool(m.source_ok)
        resp.camera_ok = bool(m.camera_ok)
        resp.robot_ok = bool(m.robot_ok)
        resp.verified = bool(self.verified_locked())
        resp.layout_id = self.calibration.layout_id if self.calibration else ''
        resp.calibration_digest = self.digest()
        resp.station_protocol = T.STATION_PROTOCOL

    def session_command(self, req, resp):
        cmd = (req.command or '').strip()
        cid = (req.client_id or '').strip()
        sid = (req.session_id or '').strip()
        now = self.clock()
        resp.end_reason = ''
        resp.accepted = False
        resp.reason_code = ''
        resp.message = ''
        wf = self.workflow_active()
        resp.workflow_active = wf
        if not self.rig:
            self._fill_session(resp, cid)
            resp.reason_code = 'NOT_STATION_RIG'
            resp.message = T.NOT_STATION_RIG_DE
            return resp
        with self.mode_lock, self.lock:
            if cmd == 'enter':
                ans, event = self.machine.enter(cid, now, self._new_session_refusal)
                if event == 'takeover':
                    self.worker.mark_abort()
            elif cmd == 'leave':
                ans, ended = self.machine.leave(sid, cid, wf)
                if ended:
                    self.worker.mark_abort()
            elif cmd == 'keepalive':
                ans = self.machine.keepalive(sid, cid, now)
            elif cmd == 'status':
                ans = None
            else:
                ans = None
                resp.message = T.UNKNOWN_COMMAND_DE
            if ans is not None:
                resp.accepted = bool(ans.accepted)
                resp.reason_code = ans.reason_code
                resp.message = ans.message
                resp.end_reason = ans.end_reason
            elif cmd == 'status':
                resp.accepted = True
            self._fill_session(resp, cid)
        self._wake.set()
        return resp

    def _owner_locked(self, req):
        return self.machine.is_owner((req.session_id or '').strip(),
                                     (req.client_id or '').strip())

    # ── /station/check ────────────────────────────────────────────────────────
    def _fill_check_common(self, resp):
        m = self.machine
        resp.generation = int(m.generation) & 0xFFFFFFFF
        resp.camera_ok = bool(m.camera_ok)
        resp.robot_ok = bool(m.robot_ok)
        resp.verified = bool(self.verified_locked())

    @staticmethod
    def _fill_metrics(resp, d):
        resp.ref_ids = [int(i) for i in d.get('ref_ids', [])]
        resp.ref_residual_mm = [float(v) for v in d.get('per_ref_rms_mm', [])]
        resp.rms_mm = float(d.get('rms_mm', 0.0))
        resp.max_mm = float(d.get('max_mm', 0.0))
        resp.centroid_mm = float(d.get('centroid_mm', 0.0))
        resp.rotation_deg = float(d.get('rotation_deg', 0.0))
        resp.dot_id = int(d.get('dot_id', 0))
        resp.dot_dxy_mm = float(d.get('dot_dxy_mm', 0.0))
        resp.dot_dz_mm = float(d.get('dot_dz_mm', 0.0))

    def check_command(self, req, resp):
        cmd = (req.command or '').strip()
        resp.accepted = False
        resp.success = False
        resp.result = ''
        resp.job_id = 0
        resp.job_state = 'none'
        resp.message = ''
        resp.action_de = ''
        if cmd == 'status':
            with self.mode_lock, self.lock:
                self._fill_check_common(resp)
                resp.accepted = True
                last = self._last_check
                if last is not None and last[0] == self.machine.generation:
                    self._fill_metrics(resp, last[1])
                    resp.result = last[1].get('result', '')
                    resp.message = last[1].get('message', '')
                    resp.success = resp.result == 'OK'
                owner_job = self.worker.owner_last_job(self.machine.session_id)
                job = self._last_check_job if owner_job == self._last_check_job else 0
                if job:
                    state, message = self.worker.job_state(job)
                    resp.job_id, resp.job_state = job, state
                    if message and not resp.message:
                        resp.message = message
            return resp
        if cmd not in ('camera', 'robot', 'resolve_camera'):
            resp.result, resp.message = 'INVALID', T.CHECK_INVALID_DE
            return resp
        with self.mode_lock, self.lock:
            self._fill_check_common(resp)
            if not self._owner_locked(req):
                resp.result, resp.message = 'NOT_OWNER', T.NOT_OWNER_DE
                return resp
            resp.accepted = True
            if not self.configured:
                resp.result, resp.message = 'NOT_CONFIGURED', T.NOT_CONFIGURED_DE
                return resp
            if self.machine.state != 'ACTIVE':
                resp.result, resp.message = 'BUSY', T.CHECK_NOT_ACTIVE_DE
                return resp
            if cmd == 'resolve_camera':
                ans = self.worker.request_claim(CW.RESOLVE, self.machine.session_id,
                                                self._arm_idle_refusal())
                resp.result = 'QUEUED' if ans.result == 'QUEUED' else 'FAILED'
                resp.job_id = ans.job_id
                resp.job_state = 'queued' if ans.job_id else 'none'
                resp.message = T.RESOLVE_QUEUED_DE if ans.job_id else ans.message
                resp.success = ans.result == 'QUEUED'
                if ans.job_id:
                    self._last_check_job = ans.job_id
                self._wake.set()
                return resp
            if self._check_running:
                resp.result, resp.message = 'BUSY', T.CHECK_BUSY_DE
                return resp
            if cmd == 'camera' and not self.machine.source_ok:
                resp.result, resp.message = 'NO_FRAME', T.NO_FRAME_DE
                return resp
            self._check_running = True
            token = self.machine.check_token(self.digest())
            cal = self.calibration
        try:
            result = self._camera_check(cal) if cmd == 'camera' else self._robot_check(cal)
        finally:
            with self.mode_lock, self.lock:
                self._check_running = False
                applied = False
                if result.get('result') in ('OK', 'MOVED', 'WRONG_MAT', 'REFS_MISSING',
                                            'ROBOT_MOVED', 'NO_DOT', 'NOT_VERTICAL'):
                    ok = result['result'] == 'OK'
                    if cmd == 'camera':
                        applied = self.machine.apply_camera(token, self.digest(), ok)
                    else:
                        applied = self.machine.apply_robot(token, self.digest(), ok)
                    if applied:
                        self._last_check = (self.machine.generation, result)
                self._fill_check_common(resp)
        resp.result = result.get('result', 'FAILED')
        resp.message = result.get('message', '')
        resp.success = resp.result == 'OK' and applied
        if resp.result == 'MOVED':
            resp.action_de = T.CAMERA_ACTION_DE
        self._fill_metrics(resp, result)
        return resp

    def _camera_check(self, cal):
        f = cal.fields
        status, frame, seq = self.fresh_frame()
        if status != 'OK':
            return {'result': status,
                    'message': T.NO_FRAME_DE if status == 'NO_FRAME' else T.WRONG_SIZE_DE}
        check_cal = {'K': f['camera_matrix'], 'dist': f['distortion_coefficients'],
                     'T_cam_mat': f['T_cam_mat'], 'ref_ids': f['ref_ids'],
                     'ref_corners_commissioned': f['ref_corners_commissioned']}
        res = C.camera_check([frame], check_cal)
        deadline = self.clock() + CHECK_RETRY_WINDOW_S
        tries = 1
        while res['result'] == 'REFS_MISSING' and tries < 3 and self.clock() < deadline:
            status, frame2, s2 = self.fresh_frame(after_seq=seq)
            if status != 'OK':
                time.sleep(0.05)
                continue
            seq = s2
            tries += 1
            res = C.camera_check([frame2], check_cal)
        res = dict(res)
        res.pop('measured', None)
        res['ref_ids'] = list(f['ref_ids'])
        return res

    def _robot_check(self, cal):
        f = cal.fields
        comm = self._comm()
        age_fn = getattr(comm, 'get_follower_joints_age_s', None) if comm is not None else None
        age = age_fn() if callable(age_fn) else None
        pose = self.node._get_current_gripper_pose()
        if pose is None:
            return {'result': 'NO_FRAME', 'message': T.NO_JOINTS_DE}
        R, t = pose
        ik = self.node._build_ik_solver()
        axis = getattr(ik, 'approach_axis_local', (1.0, 0.0, 0.0)) if ik else (1.0, 0.0, 0.0)
        tip = C.tool_tip(R, t, axis, self._tip_below())
        return C.robot_check(tip, C.tilt_deg(R, axis), age, f['dot_tips'])

    # ── /station/commission ───────────────────────────────────────────────────
    def _fill_commission_status(self, resp):
        com = self.commission
        rec = self.worker.record
        resp.commission_id = rec.commission_id if rec else ''
        if com is None or rec is None:
            resp.step = ''
            return
        resp.step = com.step_name(rec.touch_active, rec.solved)
        resp.lens_views_fit = len(com.fit_views())
        resp.lens_views_heldout = len(com.heldout_views())
        resp.coverage_cells = com.coverage_cells()
        if com.intrinsics:
            resp.lens_fit_rms_px = float(com.intrinsics['fit_rms'])
            resp.lens_heldout_rms_px = float(com.intrinsics['held_rms'])
            resp.lens_heldout_max_px = float(com.intrinsics['held_max'])
        if com.board:
            resp.board_reproj_px = float(com.board['reproj'])
        if com.world:
            resp.world_rms_mm = float(com.world['rms_mm'])
            resp.world_max_mm = float(com.world['max_mm'])
        resp.world_points = len(com.marks)
        sol = rec.solution
        if sol:
            resp.registration_rms_mm = float(sol['registration_rms_mm'])
            resp.registration_max_mm = float(sol['registration_max_mm'])
            resp.registration_shift_mm = float(sol['registration_shift_mm'])
            resp.registration_rot_deg = float(sol['registration_rot_deg'])
        nxt = com.next_mark()
        resp.next_mark_id = nxt[0] if (nxt and com.board) else ''
        resp.next_cube_turn = int(nxt[1]) if (nxt and com.board) else 0
        resp.next_dot_id = (len(rec.taps) + 1) if (rec.touch_active and rec.off_ok
                                                   and len(rec.taps) < 4) else 0

    def _answer(self, resp, ans):
        resp.result = ans.result
        resp.message = ans.message
        resp.job_id = int(ans.job_id or 0)
        resp.success = ans.result in ('OK', 'QUEUED')
        if ans.job_id:
            resp.job_state = self.worker.job_state(ans.job_id)[0]
        return resp

    def commission_command(self, req, resp):
        cmd = (req.command or '').strip()
        resp.accepted = False
        resp.success = False
        resp.result = ''
        resp.step = ''
        resp.job_id = 0
        resp.job_state = 'none'
        resp.job_message = ''
        resp.message = ''
        now = self.clock()
        with self.mode_lock, self.lock:
            owner = self._owner_locked(req)
            if owner:
                self.worker.note_owner_activity(now)
            if cmd == 'status':
                resp.accepted = True
                resp.success = True
                resp.result = 'OK'
                if owner:
                    self._fill_commission_status(resp)
                    job = self.worker.owner_last_job(self.machine.session_id)
                    if job:
                        state, message = self.worker.job_state(job)
                        resp.job_id, resp.job_state, resp.job_message = job, state, message
                return resp
            if not owner:
                resp.result, resp.message = 'NOT_OWNER', T.NOT_OWNER_DE
                return resp
            resp.accepted = True
            sid = self.machine.session_id
            rec = self.worker.record
            if cmd == 'begin':
                layout = store.released_layout((req.layout_id or '').strip(), self.layouts)
                if layout is None:
                    resp.result, resp.message = 'REFUSED', T.LAYOUT_NOT_RELEASED_DE
                    return resp
                if self.machine.state != 'ACTIVE':
                    resp.result, resp.message = 'REFUSED', T.CHECK_NOT_ACTIVE_DE
                    return resp
                ans = self.worker.request_claim(CW.CLAIM, sid, self._arm_idle_refusal())
                if ans.result == 'QUEUED':
                    self._pending_layout = layout
                self._wake.set()
                return self._answer(resp, ans)
            if cmd == 'restore_previous':
                if rec is not None:
                    resp.result, resp.message = 'REFUSED', T.JOB_PENDING_DE
                    return resp
                ans = self.worker.request_claim(CW.RESTORE, sid, self._arm_idle_refusal())
                self._wake.set()
                return self._answer(resp, ans)
            cid = (req.commission_id or '').strip()
            if rec is None or cid != rec.commission_id:
                resp.result = 'INVALID'
                resp.message = T.WRONG_COMMISSION_DE if rec is not None else T.NO_COMMISSION_DE
                return resp
            if cmd in ('touch_begin', 'touch_solve', 'commit', 'cancel'):
                fn = {'touch_begin': self.worker.touch_begin,
                      'touch_solve': self.worker.touch_solve,
                      'commit': self.worker.commit, 'cancel': self.worker.cancel}[cmd]
                if cmd == 'commit' and self.commission is not None:
                    self.worker.set_ready(cid, self.commission.gates_passed())
                ans = fn(cid, sid)
                self._wake.set()
                self._fill_commission_status(resp)
                return self._answer(resp, ans)
            if cmd == 'touch_capture':
                expected = int(req.dot_id) - 1
                count = self.worker.touch_tap_count(cid)
                if count is None or not 0 <= expected <= 3 or expected != count:
                    resp.result = 'INVALID'
                    resp.message = T.DOT_ORDER_DE.format(dot=(count or 0) + 1)
                    return resp
                layout = self.commission.layout
            elif cmd not in ('lens_capture', 'lens_solve', 'board_solve', 'mark_capture',
                             'mark_solve'):
                resp.result, resp.message = 'INVALID', T.UNKNOWN_COMMAND_DE
                return resp
            com = self.commission
        # Outside the locks: FK and frame work (bounded; this callback group only).
        if cmd == 'touch_capture':
            return self._touch_capture(req, resp, cid, expected, layout)
        return self._camera_step(cmd, req, resp, com)

    def _touch_capture(self, req, resp, cid, expected, layout):
        pose = self.node._get_current_gripper_pose()
        if pose is None:
            resp.result, resp.message = 'FAILED', T.NO_JOINTS_DE
            return resp
        R, t = pose
        ik = self.node._build_ik_solver()
        axis = getattr(ik, 'approach_axis_local', (1.0, 0.0, 0.0)) if ik else (1.0, 0.0, 0.0)
        tap, msg = CM.check_tap(int(req.dot_id), R, t, axis, self._tip_below(), layout)
        if tap is None:
            resp.result, resp.message = 'FAILED', msg
            return resp
        with self.mode_lock, self.lock:
            ans = self.worker.touch_capture(cid, tap, expected)
            self._fill_commission_status(resp)
        return self._answer(resp, ans)

    def _camera_step(self, cmd, req, resp, com):
        if com is None:
            resp.result, resp.message = 'INVALID', T.NO_COMMISSION_DE
            return resp
        if cmd in ('lens_capture', 'mark_capture'):
            status, frame, _ = self.fresh_frame()
            if status != 'OK':
                resp.result, resp.message = 'NO_FRAME', T.COMMISSION_NO_FRAME_DE
                return resp
        if cmd == 'lens_capture':
            ok, msg = com.lens_capture(frame)
            resp.result = 'OK' if ok else 'FAILED'
        elif cmd == 'lens_solve':
            ok, msg = com.lens_solve()
            resp.result = 'OK' if ok else 'FAILED'
        elif cmd == 'board_solve':
            frames = self._burst()
            if len(frames) < CM.BOARD_MIN_PASS:
                resp.result, resp.message = 'NO_FRAME', T.COMMISSION_NO_FRAME_DE
                return resp
            from physical_ai_server.workflow.calibration_manager import CalibrationManager
            mgr = CalibrationManager(root=com.staging,
                                     board_to_base=store.board_to_mat(com.layout))
            ok, msg = com.board_solve(frames, mgr)
            resp.result = 'OK' if ok else 'FAILED'
        elif cmd == 'mark_capture':
            perception = self.node._build_perception()
            catalog = None
            try:
                catalog = self.node._load_object_catalog()
            except Exception:  # noqa: BLE001
                catalog = None
            ids = set(catalog.all_tag_ids()) if catalog is not None else set()

            def detect(img):
                if perception is None:
                    return []
                out = []
                for d in perception.detect(img, camera=T.STATION_CAMERA, mode='apriltag'):
                    corners = getattr(d, 'corners_px', None)
                    px = (np.asarray(corners, dtype=np.float64).reshape(-1, 2).mean(axis=0)
                          if corners is not None else np.asarray(d.centroid_px, dtype=np.float64))
                    out.append((int(d.aruco_id), px))
                return out
            ok, code, msg = com.mark_capture((req.mark_id or '').strip(), int(req.cube_turn),
                                             frame, detect, ids)
            resp.result = code
        else:  # mark_solve
            ok, msg = com.mark_solve()
            resp.result = 'OK' if ok else 'FAILED'
        resp.message = msg
        resp.success = resp.result == 'OK'
        with self.mode_lock, self.lock:
            if self.worker.record is not None and self.commission is com:
                self.worker.set_ready(com.commission_id, com.gates_passed())
            self._fill_commission_status(resp)
        return resp

    # ── CalibrationStatus (§8.4) and capability (§5.6) ────────────────────────
    def fill_calibration_status(self, resp):
        with self.lock:
            fields = {
                'station_protocol': T.STATION_PROTOCOL if self.rig else 0,
                'station_rig': self.rig,
                'station_configured': bool(self.configured),
                'station_verified': bool(self.verified_locked()),
                'station_state': self.machine.state,
                'station_reason': self.calibration_reason if self.rig else '',
            }
        for k, v in fields.items():
            if hasattr(resp, k):
                setattr(resp, k, v)
        return resp

    def capability_extra(self):
        return {'station_protocol': T.STATION_PROTOCOL} if self.rig else {}
