#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Roboter Studio's tower calibration service (spec 40 S4; WP-13).

:class:`SceneCalibration` runs beside the legacy calibration steps (WP-16
removes those). Like the rest of this package it imports NumPy, the standard
library and the fixture contract only: the node hands it the studio camera,
a tag detector, the measured follower joints, the arm model's FK, the
``guard_margin`` gate's arm model and guard, the start and stop of an
internal program and two sinks (a Protokoll line, the state topic), and the
tests hand it fakes.

* THE MAT CHECK (:meth:`run_mat_check`): three full-resolution looks of the
  tower camera through ``mat_check.run``. The result is kept in memory (the
  last ``ok`` one of this boot is the fallback below) and in
  ``mat_check_last.json``; a target tag seen clears the stored
  ``target_removed`` again. It runs on ``/calibration/start`` step
  ``mat_check``, after step ``target_removed`` and at the start of every real
  program (:meth:`program_start_gate`, called by the real ``WorkflowManager``
  before a run): there a result that fails only with ``tags_hidden`` or
  ``fixture_hidden`` falls back to the last ``ok`` result of this boot with
  one ``[WARNUNG]``; any other refusal refuses the start with its sentence.
  Under ``legacy`` the gate says nothing (today's path); with no statement
  (``None``) the mat check answers ``profile_unknown`` (D63).
* THE ARM CHECK (:meth:`start_arm_check`, decision D8; the arm moves on its
  own, approved by D34): an internal program (:func:`arm_check_program`)
  started through the node's ordinary program start, so mode arbitration, the
  leader gate, the torque assertion, the table floor and the tower guard all
  apply; the mat-check gate does not (a one-shot token lets exactly that
  program through). No tag has to be visible to start (review B4).
  A capture thread at 10 Hz (:meth:`capture_tick`) follows the program's own
  blocks (``/workflow/status``): while pose ``i``'s dwell runs and the arm has
  moved less than 0.002 rad for 0.3 s, the pose is a RESTED record (its
  encoder readings: every ``PoseRecord.measured`` is the arm's encoders,
  never the commanded joints, D84); it is CAPTURED when the encoders lie
  within 0.15 rad of the contract pose and three consecutive looks into the
  target's predicted image region (its nominal projection grown by 120 px)
  show at least six target tags whose centres (the mean of the four corners,
  D89-2) moved less than 0.4 px (``arm_check.still_between``). One
  full-frame look per pose collects the field and fixture tags. POSE 1 IS THE
  IDENTITY CHECK: still at AC01 for 3.0 s with fewer than six of this arm's
  target tags seen -> the program is stopped, a second program holding only
  the Grundstellung is started and the sentence is the state (the other
  arm's target, a covered mat or hidden fixture tags have their own
  sentences); nothing is written.
* THE SOLVE (:meth:`on_program_end` with ``finished``, on a worker thread):
  ``arm_check.run`` and then the last gate, :func:`guard_margin`, the box
  guard on the solved arm model with the ladder; on success
  ``arm_check.json`` with ``target_removed: false`` and the state
  ``confirm_target_removed``. The droop warning is a warning (D42). A
  pending handheld lens (``handheld.read``) is held fixed in the solve
  (``lens_prior``, D89-1), and a written arm check uses it up.
* THE HANDHELD STEP (:meth:`start_handheld`, decision D9's fallback): the
  node's calibration mode claimed (``claim_mode``: nothing else moves the arm
  or starts while the student holds the target in front of the camera), the
  studio picture held, a capture thread at 10 Hz (:meth:`handheld_tick`)
  hands every full-frame look to ``handheld.Capture`` with the arm check's
  stillness rule; 18 views in at least 9 cells -> ``handheld.solve`` on a
  worker thread -> ``intrinsics_handheld.json`` and the state
  ``needs_arm_check`` (code ``handheld_done`` / ``handheld_failed``).
  ``/calibration/cancel`` ends it: nothing written, the state the one before.
* THE STATE (:meth:`state`): ``STATE_TOPIC``'s JSON, ``{"v":1, "state",
  "code", "message_de", "notes", "camera", "arm_check", "handheld",
  "quality", "fixture"}`` as S4 gives it, plus ``"droop_warning"``
  (``{joint, deg, message_de}`` or ``null``) and ``"mat_check"`` (the last
  mat check's readings for the panel's „Details", or ``null``). Its codes:
  the mat check's and the arm check's (``texts_de.BY_CODE``), and
  ``unknown`` (nothing checked yet), ``arm_check_running``,
  ``confirm_target_removed`` (an arm check just stored), ``check_failed``,
  ``handheld_running``, ``handheld_done``, ``handheld_failed``; ``"handheld"``
  is ``handheld.Capture.progress()`` while that step runs, with the solve's
  ``rms_px`` after it.

A click on the outside stream (:meth:`SceneCalibration.click`) is mapped to
its studio pixel (:func:`studio_pixel`, A17); a detection of a run on the
studio picture goes back to the page's outside stream (:func:`outside_pixel`).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from typing import Callable, Optional

import numpy as np

from physical_ai_server.workflow import scene_fixture as _sf
from physical_ai_server.workflow.autocal import arm_check as _ac
from physical_ai_server.workflow.autocal import chain as _chain
from physical_ai_server.workflow.autocal import handheld as _hh
from physical_ai_server.workflow.autocal import mat_check as _mc
from physical_ai_server.workflow.autocal import store as _store
from physical_ai_server.workflow.autocal import tags as _tags
from physical_ai_server.workflow.autocal import texts_de as _t
from physical_ai_server.workflow.autocal.camera_model import KB

STATE_TOPIC = '/edubotics/calibration_state'
STATE_VERSION = 1
# /calibration/start's steps this service answers (the others stay the legacy wizard's).
STEPS = ('mat_check', 'arm_check', 'target_removed', 'handheld')
PROGRAM_PREFIX = 'armcheck-'
HOME_PROGRAM_PREFIX = 'armcheck-home-'
CAPTURE_THREAD = 'calibration-capture'
WORK_THREAD = 'calibration-work'

# ── the arm-check program (S4) ───────────────────────────────────────────────
ARM_CHECK_TEMPO = 0.5
FIRST_DWELL_S = 4.0          # after AC01: the identity check needs 3.0 s of rest
DWELL_S = 2.5
_MOVE_PREFIX = 'armcheck-move-'
_WAIT_PREFIX = 'armcheck-wait-'
_MOVE_ID = _MOVE_PREFIX + '{:02d}'
_WAIT_ID = _WAIT_PREFIX + '{:02d}'
_CLOSE_ID = 'armcheck-close'
_HOME_ID = 'armcheck-home'

# ── the capture rule (S4; arm_check.capturable is its static half, arm_check.still_between its stillness) ──
CAPTURE_PERIOD_S = 0.1
STILL_WINDOW_S = 0.3
STILL_TOL_RAD = 0.002
CAPTURE_LOOKS = 3
LOOK_STABLE_PX = 0.4
REGION_MARGIN_PX = 120.0
POSE1_CHECK_S = 3.0
MAT_CHECK_FRAMES = 3
# a look older than this is no look of the arm's rest
LOOK_MAX_AGE_S = 0.5
# the mat check waits this long in all for three distinct frames, then takes what it has
MAT_FRAMES_WAIT_S = 0.5
_FRAME_POLL_S = 0.01

# ── guard_margin (S4 revision 4) ─────────────────────────────────────────────
GUARD_HOVER_RADII = 6
GUARD_HOVER_AZIMUTHS = 13
GUARD_HOVER_ABOVE_MAT_M = 0.075

# The sentences of this service the spec does not word live in texts_de with
# the others; the names stay importable from here.
ARM_CHECK_BUSY_DE = _t.ARM_CHECK_BUSY_DE
CHECK_FAILED_DE = _t.CHECK_FAILED_DE
HANDHELD_BUSY_DE = _t.HANDHELD_BUSY_DE
HANDHELD_STOPPED_DE = _t.HANDHELD_STOPPED_DE

_FIXTURE_STATE = {'id': _sf.FIXTURE_ID, 'lift_m': _sf.LIFT_M}
_CAPTURE_SIZE = (int(_sf.CAPTURE['width']), int(_sf.CAPTURE['height']))


def _start_daemon(target, name):
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


# ── the outside stream's window (A17) ────────────────────────────────────────
def _window():
    o = _sf.OUTSIDE
    return (o['window_w'] / o['width'], o['window_h'] / o['height'], o['window_x0'], o['window_y0'])


def studio_pixel(u, v):
    """The studio (capture) pixel of a pixel of the outside stream: its window
    reduced 2:1, pixel centres on the integers on both sides (A17, guard G5):
    ``u_full = 2·u + 320.5``, ``v_full = 2·v + 60.5``."""
    sx, sy, x0, y0 = _window()
    return (sx * float(u) + x0 + (sx - 1.0) / 2.0, sy * float(v) + y0 + (sy - 1.0) / 2.0)


def outside_pixel(u_full, v_full):
    """The way back: the outside-stream pixel of a studio pixel."""
    sx, sy, x0, y0 = _window()
    return ((float(u_full) - x0 - (sx - 1.0) / 2.0) / sx, (float(v_full) - y0 - (sy - 1.0) / 2.0) / sy)


def outside_size(w_full, h_full):
    """A width and height in studio pixels, in outside-stream pixels."""
    sx, sy, _x0, _y0 = _window()
    return float(w_full) / sx, float(h_full) / sy


# ── the detector the node hands in ───────────────────────────────────────────
def detect_tags(perception, frame, roi=None) -> _tags.Frame:
    """Every AprilTag in ``frame`` (BGR, full resolution) or in its region
    ``roi = (x0, y0, x1, y1)``, as a ``tags.Frame`` in full-frame pixels; the
    decision margin of ``perception.Detection.confidence`` (``margin / 100``)."""
    h, w = frame.shape[:2]
    x0 = y0 = 0
    image = frame
    if roi is not None:
        x0 = max(0, int(math.floor(roi[0])))
        y0 = max(0, int(math.floor(roi[1])))
        x1 = min(w, int(math.ceil(roi[2])) + 1)
        y1 = min(h, int(math.ceil(roi[3])) + 1)
        image = frame[y0:y1, x0:x1]
    found = {}
    if image.size:
        for d in perception.detect(image, camera='scene', mode='apriltag'):
            if d.aruco_id is None or d.corners_px is None:
                continue
            corners = np.asarray(d.corners_px, dtype=np.float64).reshape(4, 2) + (x0, y0)
            found[int(d.aruco_id)] = _tags.TagObs(corners, float(d.confidence) * 100.0)
    grey = float(np.mean(frame)) if roi is None else 0.0
    return _tags.Frame(int(w), int(h), grey, found)


# ── the internal programs ────────────────────────────────────────────────────
def _block(btype, block_id, **extra):
    out = {'type': btype, 'id': block_id}
    out.update(extra)
    return out


def _wait(block_id, seconds):
    return _block('edubotics_wait_seconds', block_id, inputs={'SECONDS': {'shadow': {
        'type': 'math_number', 'fields': {'NUM': seconds}}}})


def _chain_blocks(blocks):
    for prev, nxt in zip(blocks, blocks[1:]):
        prev['next'] = {'block': nxt}
    return blocks[0]


def arm_check_program(profile, fk) -> dict:
    """The arm check as a Blockly program (S4): ``schließe Greifer``; per
    contract pose ``bewege zu AC01 … AC20`` (each a Position: the pose's joints
    on this arm, so every leg is ``motion._move_to_position`` with its table
    and tower checks) and ``warte`` 4.0 s after AC01, 2.5 s after the others;
    then ``Grundstellung``; tempo 0.5; no zone. ``fk(q)`` -> ``(R, t)`` gives
    each Position its point (the payload's ``x, y, z``)."""
    kind = _sf.arm_key(profile.profile_id)
    closed = float(profile.gripper_closed_rad)
    blocks = [_block('edubotics_close_gripper', _CLOSE_ID)]
    destinations = []
    for i, pose in enumerate(_sf.ARMS[kind]['arm_check_poses_rad'], start=1):
        name = f'AC{i:02d}'
        blocks.append(_block('edubotics_move_to', _MOVE_ID.format(i), inputs={'DESTINATION': {'block': {
            'type': 'edubotics_destination_ref', 'id': f'armcheck-ref-{i:02d}', 'fields': {'NAME': name}}}}))
        blocks.append(_wait(_WAIT_ID.format(i), FIRST_DWELL_S if i == 1 else DWELL_S))
        _R, t = fk(list(pose) + [closed])
        destinations.append({'name': name, 'kind': 'pose', 'x': float(t[0]), 'y': float(t[1]), 'z': float(t[2]),
                             'joints': [float(v) for v in pose] + [closed],
                             'joint_names': list(profile.joint_names), 'robot_type': profile.data_robot_type})
    blocks.append(_block('edubotics_home', _HOME_ID))
    return {'blocks': {'languageVersion': 0, 'blocks': [_chain_blocks(blocks)]},
            'tempo': ARM_CHECK_TEMPO, 'zones': [], 'destinations': destinations}


def home_program() -> dict:
    """The program the failed identity check starts: the Grundstellung only."""
    return {'blocks': {'languageVersion': 0, 'blocks': [_block('edubotics_home', _HOME_ID)]},
            'tempo': ARM_CHECK_TEMPO, 'zones': [], 'destinations': []}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _finite_or_none(value):
    """A non-finite float as ``None`` (the state is JSON a browser parses)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# ── guard_margin ─────────────────────────────────────────────────────────────
def guard_margin_envelope(calibration, kind):
    """The droop envelope (joints 2..4) the gate judges with: the kit's own,
    capped at the design envelope. The gate asks whether the arm's BUILD keeps
    its observation pose and its route clear of the tower; an arm that sags
    more than designed is a WARNING, never a refusal (D42), and its programs are
    judged with its own envelope by the runtime guard."""
    design = _sf.TOWER_DROOP_DESIGN_ENV_RAD[kind]
    own = list(calibration['droop_envelope_rad'])[1:4]
    return [min(float(e), float(cap)) for e, cap in zip(own, design)]


def guard_margin_hovers(model, profile_id) -> list:
    """(c)'s hovers: 6 radii from ``zone_inner_m`` to the reach × 13 azimuths
    −90 … +90°, roll 0, the fingertip ``GUARD_HOVER_ABOVE_MAT_M`` above the
    mat, solved through ``model``; the ones it solves, as joint lists."""
    kind = _sf.arm_key(profile_id)
    spec = _sf.ARMS[kind]
    z = _sf.MAT_THICKNESS_M + GUARD_HOVER_ABOVE_MAT_M + _sf.FINGERTIP_BELOW_EE_M[kind]
    out = []
    for r in np.linspace(spec['zone_inner_m'], spec['reach_m'][1], GUARD_HOVER_RADII):
        for az in np.linspace(-90.0, 90.0, GUARD_HOVER_AZIMUTHS):
            a = math.radians(float(az))
            q = model.solve((model.base_axis_x + r * math.cos(a), r * math.sin(a), z), roll=0.0)
            if q is not None:
                out.append([float(v) for v in q])
    return out


def guard_margin(calibration, profile, guard_parts):
    """``(ok, detail)``: the arm model of the solved parameters judged by the
    tower guard exactly as a program would be (the calibrated residual, the
    tower where the arm check placed it, :func:`guard_margin_envelope`): (a)
    HOME ↔ OBS with the claw open and closed, (b) the pinned route HOME → AC01
    … AC20 → HOME with the claw closed, (c) for every hover of the 78-pose grid
    with the claw open EITHER the direct line OBS ↔ hover OR hover ↔ HOME —
    with (a), the ladder of S6.4. ``guard_parts(calibration)`` -> ``(model,
    guard)`` builds the solved arm model and its guard (the node's: the arm
    model and the guard are not this package's)."""
    kind = _sf.arm_key(profile.profile_id)
    spec = _sf.ARMS[kind]
    model, guard = guard_parts(calibration)
    home = [float(v) for v in profile.home_joints_rad]
    obs = [float(v) for v in (profile.observe_pose_joints or spec['observe_joints_rad'])]
    g_open, g_closed = float(profile.gripper_open_rad), float(profile.gripper_closed_rad)

    def refused(a, b, g):
        return bool(guard.judge_leg(a, b, g, g).refused)

    detail = {'hovers': 0, 'via_home': 0}
    for g in (g_open, g_closed):
        if refused(home, obs, g):
            return False, dict(detail, failed='home_obs')
    route = [home] + [[float(v) for v in p] for p in spec['arm_check_poses_rad']] + [home]
    for k, (a, b) in enumerate(zip(route[:-1], route[1:])):
        if refused(a, b, g_closed):
            return False, dict(detail, failed='route', leg=k)
    for q in guard_margin_hovers(model, profile.profile_id):
        detail['hovers'] += 1
        if not refused(obs, q, g_open):
            continue
        if refused(q, home, g_open):
            return False, dict(detail, failed='hover', hover=q)
        detail['via_home'] += 1
    return True, detail


# ── one arm-check run ────────────────────────────────────────────────────────
class _Run:
    """What the capture thread and the program hooks share about one arm check."""

    def __init__(self, workflow_id, poses):
        self.workflow_id = workflow_id
        self.poses = [np.asarray(p, dtype=np.float64) for p in poses]
        self.pose = None             # 0-based pose of the program's current block
        self.dwell = False           # True while that pose's dwell (its wait block) runs
        self.samples = []            # (t, q (5,)) measured joints of the last STILL_WINDOW_S and a little more
        self.still_since = None
        self.rested = {}             # pose -> median encoder readings of its rest
        self.captured = {}           # pose -> (median encoder readings, {tag id: median corners})
        self.looks = {}              # pose -> {tag id: TagObs} of its full-frame look
        self.streak = []             # the consecutive target looks of the current dwell: ({id: corners}, q)
        self.last_frame = None
        self.pose1_target = 0        # the most of this arm's target tags seen in pose 1's region
        self.pose1_foreign = 0
        self.failure = None          # (code, sentence) of a failed identity check
        self.cancelled = False
        self.ended = False           # the arm-check program ended
        self.home_id = None          # the identity check's way home
        self.closed = False
        self.camera_profile_id = None


class _HandheldRun:
    """What the capture thread, the solve and a cancel share about one handheld step."""

    def __init__(self, capture, previous, camera_profile_id):
        self.capture = capture
        self.previous = previous     # the state before the step: a cancel returns to it
        self.camera_profile_id = camera_profile_id
        self.last_frame = None
        self.solving = False         # the capture ended; the solve is on its way
        self.cancelled = False
        self.closed = False


@dataclass
class Click:
    """A click of the outside stream for the tower calibration: the refusal
    (``message_de``), or the studio pixel to project with ``calibration`` onto
    the mat's top (``surface_z``) and the grasp height a Ziel there gets."""

    message_de: Optional[str] = None
    u_full: float = 0.0
    v_full: float = 0.0
    calibration: Optional[dict] = None
    surface_z: float = 0.0
    grasp_z: float = 0.0


class SceneCalibration:
    """See the module docstring. Thread-safe; nothing it calls back is called
    while its own lock is held."""

    STEPS = STEPS

    def __init__(self, profile, camera, calib_dir, *, detect: Callable, get_joints: Callable, fk: Callable,
                 guard_parts: Callable, start_program: Callable, stop_program: Callable,
                 claim_mode: Optional[Callable] = None, release_mode: Optional[Callable] = None,
                 log: Optional[Callable] = None, log_internal: Optional[Callable] = None,
                 on_state: Optional[Callable] = None, clock=time.monotonic, wall=time.time, sleep=time.sleep,
                 start_thread=_start_daemon) -> None:
        """``fk(q)`` -> ``(R, t)``: the real arm model's end effector (the
        program's Positions); ``guard_parts(calibration)`` -> ``(model,
        guard)``: :func:`guard_margin`'s; ``claim_mode()`` -> ``(ok,
        sentence)`` / ``release_mode()``: the node's calibration mode, which
        the handheld step owns while it runs; ``log``: a German Protokoll
        line; ``log_internal``: an English line for the server log only;
        ``on_state``: the state, at every change."""
        self.profile = profile
        self.kind = _sf.arm_key(getattr(profile, 'profile_id', None))
        self.calib_dir = str(calib_dir)
        self._camera = camera
        self._store = _store.Store(self.calib_dir)
        self._detect = detect
        self._get_joints = get_joints
        self._fk = fk
        self._guard_parts = guard_parts
        self._start_program = start_program
        self._stop_program = stop_program
        self._claim_mode = claim_mode or (lambda: (True, ''))
        self._release_mode = release_mode or (lambda: None)
        self._log = log or (lambda _text: None)
        self._log_internal = log_internal or (lambda _text: None)
        self._on_state = on_state or (lambda _state: None)
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._start_thread = start_thread
        self._lock = threading.RLock()
        self._run: Optional[_Run] = None
        self._handheld: Optional[_HandheldRun] = None
        self._internal = None        # (workflow_id, digest) of the program this service is starting
        self._approved = None        # the calibration the gate approved for the real run starting now
        self._gate_hold = None       # the workflow id of the real run holding the studio picture
        self._last_ok = None         # the last ok mat check of this boot
        self._state = self._state_dict('unknown', 'unknown', '')

    # ── the state ────────────────────────────────────────────────────────────
    def _state_dict(self, state, code, message, *, notes=(), arm_check=None, quality=None, droop=None,
                    mat=None, handheld=None):
        return {'v': STATE_VERSION, 'state': state, 'code': code, 'message_de': message, 'notes': list(notes),
                'camera': None, 'arm_check': arm_check, 'handheld': handheld, 'quality': quality,
                'fixture': dict(_FIXTURE_STATE), 'droop_warning': droop, 'mat_check': mat}

    def _set_state(self, state, code, message, **kw):
        with self._lock:
            self._state = self._state_dict(state, code, message, **kw)
        self._on_state(self.state())

    def state(self) -> dict:
        """The state topic's object (a copy; the camera's latest state filled in now)."""
        with self._lock:
            out = json.loads(json.dumps(self._state))
        try:
            out['camera'] = self._camera.camera_state()
        except Exception:  # noqa: BLE001 — an unreadable camera state is no state
            out['camera'] = None
        return out

    def state_json(self) -> str:
        return json.dumps(self.state(), separators=(',', ':'), ensure_ascii=False)

    def _arm_state(self, run, pose=None):
        n = len(run.poses)
        i = run.pose if pose is None else pose
        return {'pose': max(1, min(n, (i or 0) + 1)), 'poses': n, 'captured': len(run.captured)}

    def _set_running(self, run, pose=None):
        arm = self._arm_state(run, pose)
        self._set_state('arm_check_running', 'arm_check_running', _t.arm_check_running_de(arm['pose'], arm['poses']),
                        arm_check=arm)

    # ── what is stored ───────────────────────────────────────────────────────
    def _read(self):
        return self._store.read(self.profile.profile_id, _CAPTURE_SIZE)

    def status(self):
        """``(a valid arm check exists, the state's sentence)`` for ``/calibration/status``."""
        return self._read().status == 'ok', self.state()['message_de']

    def _busy_message(self):
        """The sentence of the arm check or the handheld step under way, or ``None``."""
        with self._lock:
            if self._run is not None and not self._run.closed:
                return ARM_CHECK_BUSY_DE
            if self._handheld is not None:
                return HANDHELD_BUSY_DE
        return None

    def _busy(self) -> bool:
        return self._busy_message() is not None

    # ── /calibration/start ───────────────────────────────────────────────────
    def start(self, step):
        """``(ok, sentence)`` of a step of ``STEPS``. Never raises (it answers
        a ROS service callback)."""
        try:
            if step == 'mat_check':
                busy = self._busy_message()
                if busy is not None:
                    return False, busy
                result = self.run_mat_check()
                return result.ok, result.message_de
            if step == 'arm_check':
                return self.start_arm_check()
            if step == 'target_removed':
                return self.confirm_target_removed()
            if step == 'handheld':
                return self.start_handheld()
        except Exception as e:  # noqa: BLE001 — logged; the student gets the sentence
            self._log_internal(f'calibration step {step!r} failed: {type(e).__name__}: {e}')
            return False, CHECK_FAILED_DE
        raise ValueError(f'not a step of this service: {step!r}')

    # ── the mat check ────────────────────────────────────────────────────────
    def _streaming(self):
        try:
            state = self._camera.camera_state()
        except Exception:  # noqa: BLE001
            state = None
        return isinstance(state, dict) and state.get('state') == 'streaming'

    def _frames(self, n):
        """Up to ``n`` distinct full-resolution looks (the detector's
        ``tags.Frame``), waiting at most ``MAT_FRAMES_WAIT_S`` for new frames."""
        frames, seen = [], []
        deadline = self._clock() + MAT_FRAMES_WAIT_S
        for _ in range(n + int(MAT_FRAMES_WAIT_S / _FRAME_POLL_S)):
            if len(frames) >= n:
                break
            got = self._camera.latest_frame(math.inf)
            if got is None:
                break
            frame = got[0]
            if any(frame is s for s in seen):
                if self._clock() >= deadline:
                    break
                self._sleep(_FRAME_POLL_S)
                continue
            seen.append(frame)
            frames.append(self._detect(frame, None))
        return frames

    def run_mat_check(self):
        """The mat check now (three looks, no motion); its result, kept. A
        check that raised is the refusal ``check_failed``."""
        profile = self._camera.profile()
        streaming = self._streaming()
        try:
            frames = self._frames(MAT_CHECK_FRAMES) if (profile == _sf.FIXTURE_ID and streaming) else []
            result = _mc.run(frames, self._store, _sf.ARMS[self.kind], profile_id=self.profile.profile_id,
                             profile=profile, camera_streaming=streaming, now=self._wall())
        except Exception as e:  # noqa: BLE001 — never into the caller (a ROS callback, a program start)
            self._log_internal(f'mat check failed: {type(e).__name__}: {e}')
            result = _mc.MatCheckResult(ok=False, code='check_failed', message_de=CHECK_FAILED_DE,
                                        stamp=float(self._wall()))
        try:
            self._store.write_mat_check_last(result.to_json())
        except (OSError, TypeError, ValueError) as e:
            self._log_internal(f'mat_check_last.json not written: {type(e).__name__}')
        if result.target_seen:
            got = self._read()
            if got.status == 'ok' and got.data.get('target_removed'):
                self._store.set_target_removed(self.profile.profile_id, False)
        with self._lock:
            if result.ok:
                self._last_ok = result
        if not self._busy():
            self._state_from_mat(result)
        return result

    def _state_from_mat(self, result):
        if result.ok:
            state = 'ok'
        elif result.code.startswith('needs_arm_check'):
            state = 'needs_arm_check'
        elif result.code == 'target_not_confirmed':
            state = 'confirm_target_removed'
        else:
            state = 'blocked'
        got = self._read()
        droop = quality = None
        if got.status == 'ok':
            quality = got.data.get('quality')
            droop = self._droop_state(got.data.get('droop_warning'))
        mat = {k: _finite_or_none(v) for k, v in result.to_json().items()
               if k not in ('T_cam_to_w', 'message_de', 'code', 'ok')}
        self._set_state(state, result.code, result.message_de, notes=result.notes, quality=quality, droop=droop,
                        mat=mat)

    @staticmethod
    def _droop_state(warning):
        if not isinstance(warning, dict):
            return None
        joint, deg = warning.get('joint'), warning.get('deg')
        return {'joint': joint, 'deg': deg, 'message_de': _t.droop_warning_de(joint, deg)}

    def _usable_calibration(self):
        """``(calibration, warnings, refusal)`` for a real use of the camera (a
        program, a click): the mat check now; ``tags_hidden`` /
        ``fixture_hidden`` fall back to the last ok result of this boot."""
        result = self.run_mat_check()
        warnings = []
        if not result.ok:
            with self._lock:
                last = self._last_ok
            if result.code not in ('tags_hidden', 'fixture_hidden') or last is None:
                return None, [], result.message_de
            result = last
            warnings.append(_t.LAST_RESULT_WARNING_DE)
        got = self._read()
        if got.status != 'ok':
            return None, [], _t.NEEDS_ARM_CHECK_NONE_DE
        kb = got.data['kb']
        lens = KB(kb['f'], kb['aspect'], kb['cx'], kb['cy'], kb['k1'], kb['k2'])
        droop = self._droop_state(got.data.get('droop_warning'))
        if droop is not None:
            warnings.append('[WARNUNG] ' + droop['message_de'])
        calibration = {'intrinsics': lens.to_intrinsics_dict(), 'T_cam_to_w': np.asarray(result.T_cam_to_w),
                       'image_size': _CAPTURE_SIZE}
        return calibration, warnings, None

    # ── the program-start gate ───────────────────────────────────────────────
    def program_start_gate(self, workflow_json, workflow_id):
        """``None`` = the real program may start, else the German refusal. The
        service's own internal program passes once (its token); under
        ``legacy`` nothing is judged. An approved start holds the studio
        picture until :meth:`on_program_end` names it."""
        with self._lock:
            internal = self._internal
            if internal is not None and internal == (workflow_id, _digest(str(workflow_json))):
                self._internal = None
                self._approved = None
                return None
            self._approved = None
        if self._camera.profile() == 'legacy':
            return None
        try:
            calibration, warnings, refusal = self._usable_calibration()
        except Exception as e:  # noqa: BLE001 — a gate that raised refuses, never passes
            self._log_internal(f'program start gate failed: {type(e).__name__}: {e}')
            return CHECK_FAILED_DE
        if refusal is not None:
            return refusal
        for text in warnings:
            self._log(text)
        with self._lock:
            self._approved = calibration
            hold, self._gate_hold = self._gate_hold is None, workflow_id
        if hold:
            self._camera.hold()
        return None

    def run_calibration(self):
        """The tower calibration the gate approved for the real run starting
        now and running until :meth:`on_program_end` (``_load_workflow_calibration``),
        or ``None``."""
        with self._lock:
            return self._approved

    def tower_scope(self) -> bool:
        """True unless the arm container states ``legacy`` (D63: ``None`` is the tower's scope too)."""
        return self._camera.profile() != 'legacy'

    # ── the click ────────────────────────────────────────────────────────────
    def click(self, camera, u, v):
        """A click on the outside stream for the tower calibration (A17): its
        studio pixel and the calibration to project it with, or the refusal;
        ``None`` when the click is not the tower's (another camera, ``legacy``)."""
        if camera != 'scene' or not self.tower_scope():
            return None
        calibration, _warnings, refusal = self._usable_calibration()
        if refusal is not None:
            return Click(message_de=refusal)
        u_full, v_full = studio_pixel(u, v)
        return Click(None, u_full, v_full, calibration, _sf.MAT_THICKNESS_M,
                     _sf.MAT_THICKNESS_M + _sf.FINGERTIP_BELOW_EE_M[self.kind])

    # ── the arm check ────────────────────────────────────────────────────────
    def _start_internal(self, payload, workflow_id):
        text = json.dumps(payload, separators=(',', ':'))
        with self._lock:
            self._internal = (workflow_id, _digest(text))
        try:
            return self._start_program(text, workflow_id)
        finally:
            with self._lock:
                self._internal = None

    def start_arm_check(self):
        """``(ok, sentence)``: start the arm-check program (see the module docstring)."""
        profile = self._camera.profile()
        if profile is None:
            return False, _t.PROFILE_UNKNOWN_DE
        if profile != _sf.FIXTURE_ID or self.kind is None:
            return False, _t.CAMERA_NOT_TOWER_DE
        if not self._streaming():
            return False, _t.CAMERA_MISSING_DE
        workflow_id = f'{PROGRAM_PREFIX}{int(self._wall())}'
        run = _Run(workflow_id, _sf.ARMS[self.kind]['arm_check_poses_rad'])
        try:
            run.camera_profile_id = (self._camera.camera_state() or {}).get('profile_id')
        except Exception:  # noqa: BLE001
            run.camera_profile_id = None
        with self._lock:
            busy = self._busy_message()
            if busy is not None:
                return False, busy
            self._run = run
            previous = self._state
        self._camera.hold()
        self._set_running(run, 0)
        try:
            ok, message = self._start_internal(arm_check_program(self.profile, self._fk), workflow_id)
        except Exception as e:  # noqa: BLE001 — a start that raised is a start that did not happen
            self._log_internal(f'arm check program not started: {type(e).__name__}: {e}')
            ok, message = False, _t.STOPPED_DE
        if not ok:
            with self._lock:
                run.closed = True
                if self._run is run:
                    self._run = None
                self._state = previous
            self._camera.release()
            self._on_state(self.state())
            return False, message
        self._start_thread(lambda: self._capture_loop(run), CAPTURE_THREAD)
        return True, _t.arm_check_running_de(1, len(run.poses))

    def confirm_target_removed(self):
        """Step ``target_removed``: set the stored flag, then the mat check."""
        busy = self._busy_message()
        if busy is not None:
            return False, busy
        if not self._store.set_target_removed(self.profile.profile_id, True):
            result = self.run_mat_check()
            return False, result.message_de
        result = self.run_mat_check()
        return result.ok, result.message_de

    def cancel(self):
        """``/calibration/cancel`` while an arm check is under way: stop its
        program; nothing is written. While the handheld step is under way: end
        it, nothing is written, the state is the one before the step. ``None``
        when neither is under way."""
        with self._lock:
            hand = self._handheld
            if hand is not None:
                hand.cancelled = True
                solving = hand.solving
                self._state = hand.previous
        if hand is not None:
            if not solving:          # else the solve sees the cancel, writes nothing and ends the step
                self._close_handheld(hand)
            self._on_state(self.state())
            return True, HANDHELD_STOPPED_DE
        with self._lock:
            run = self._run
            if run is None or run.closed:
                return None
            run.cancelled = True
            running = not run.ended
        if running:
            try:
                self._stop_program(run.workflow_id)
            except Exception as e:  # noqa: BLE001 — a cancel answers a ROS callback
                self._log_internal(f'arm check program not stopped: {type(e).__name__}: {e}')
        return True, _t.STOPPED_DE

    # ── the program's own progress ───────────────────────────────────────────
    def on_workflow_status(self, payload):
        """A ``/workflow/status`` payload: follows the arm-check program's blocks."""
        try:
            block = str(payload.get('current_block_id') or '')
            if payload.get('phase') != 'running' or not block.startswith(PROGRAM_PREFIX):
                return
            workflow_id = payload.get('workflow_id')
        except AttributeError:
            return
        with self._lock:
            run = self._run
            if run is None or run.workflow_id != workflow_id or run.ended:
                return
            pose, dwell = None, False
            for prefix, is_dwell in ((_MOVE_PREFIX, False), (_WAIT_PREFIX, True)):
                if block.startswith(prefix) and block[len(prefix):].isdigit():
                    pose, dwell = int(block[len(prefix):]) - 1, is_dwell
            if pose is None or not 0 <= pose < len(run.poses):
                run.dwell = False
                return
            if pose != run.pose or dwell != run.dwell:
                run.streak = []
            run.pose, run.dwell = pose, dwell
            failed = run.failure is not None
        if not failed:
            self._set_running(run)

    def on_program_end(self, workflow_id, phase):
        """A real program ended (``phase`` ``None``: it never started). Releases
        the studio picture of a gated run; for the arm check: the solve, the
        stopped sentence, or the identity check's way home."""
        release = False
        with self._lock:
            self._approved = None        # an approval is the run's, never the next caller's
            if self._gate_hold is not None and self._gate_hold == workflow_id:
                self._gate_hold = None
                release = True
            run = self._run
        if release:
            self._camera.release()
        if run is None or run.closed:
            return
        if workflow_id == run.home_id:
            self._close(run)
            return
        if workflow_id != run.workflow_id:
            return
        with self._lock:
            if run.ended:
                return
            run.ended = True
            failure, cancelled = run.failure, run.cancelled
        if failure is not None:
            self._start_thread(lambda: self._go_home(run), WORK_THREAD)
        elif phase == 'finished' and not cancelled:
            self._start_thread(lambda: self._solve(run), WORK_THREAD)
        else:
            self._set_state('needs_arm_check', 'stopped', _t.STOPPED_DE)
            self._close(run)

    def _close(self, run):
        with self._lock:
            if run.closed:
                return
            run.closed = True
            if self._run is run:
                self._run = None
        self._camera.release()

    def _go_home(self, run):
        home_id = f'{HOME_PROGRAM_PREFIX}{int(self._wall())}'
        with self._lock:
            run.home_id = home_id
        try:
            ok, message = self._start_internal(home_program(), home_id)
        except Exception as e:  # noqa: BLE001
            ok, message = False, f'{type(e).__name__}: {e}'
        if not ok:
            self._log_internal(f'arm check: way home not started: {message}')
            self._close(run)

    # ── the capture thread ───────────────────────────────────────────────────
    def _capture_loop(self, run):
        logged = False
        while True:
            with self._lock:
                if run.closed or run.ended or run.failure is not None:
                    return
            try:
                self.capture_tick()
            except Exception as e:  # noqa: BLE001 — logged once; the run's end decides
                if not logged:
                    logged = True
                    self._log_internal(f'arm check capture failed: {type(e).__name__}: {e}')
            self._sleep(CAPTURE_PERIOD_S)

    def _measured(self):
        try:
            q = self._get_joints()
        except Exception:  # noqa: BLE001
            return None
        if q is None or len(q) < 5:
            return None
        q = np.asarray([float(v) for v in list(q)[:5]], dtype=np.float64)
        return q if np.all(np.isfinite(q)) else None

    def _region(self, i):
        """The target's predicted image region at contract pose ``i``: the
        nominal arm and mount seen by the contract camera, grown by
        ``REGION_MARGIN_PX`` and cut to the image."""
        _ids, Xt = _ac.target_model(self.kind)
        M = _ac.mount_nominal(self.kind)
        E = _chain.ee(self.kind, np.asarray(_sf.ARMS[self.kind]['arm_check_poses_rad'][i], dtype=np.float64))
        X = (Xt @ M[:3, :3].T + M[:3, 3]) @ E[:3, :3].T + E[:3, 3]
        X[:, 2] += _sf.LIFT_M
        R0, t0 = _mc.contract_camera(_chain.BASE_AXIS_X[self.kind])
        lens = _sf.LENS_NOMINAL
        uv = _mc.project(X, R0, t0, (lens['f_px'], lens['aspect'], lens['cx'], lens['cy'], lens['k1'], lens['k2']))
        w, h = _CAPTURE_SIZE
        lo = np.maximum(uv.min(axis=0) - REGION_MARGIN_PX, 0.0)
        hi = np.minimum(uv.max(axis=0) + REGION_MARGIN_PX, (w - 1.0, h - 1.0))
        return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])

    def _look(self, run, roi):
        """A NEW frame's tags (``tags.Frame``), or ``None``."""
        got = self._camera.latest_frame(LOOK_MAX_AGE_S, 0.0)
        if got is None:
            return None
        frame = got[0]
        if frame is run.last_frame:
            return None
        run.last_frame = frame
        return self._detect(frame, roi)

    def capture_tick(self):
        """One step of the capture rule (the capture thread calls it at 10 Hz)."""
        with self._lock:
            run = self._run
            if run is None or run.closed or run.ended or run.failure is not None:
                return
        now = self._clock()
        q = self._measured()
        if q is None:
            return
        with self._lock:
            run.samples = [(t, s) for t, s in run.samples if t >= now - 2.0 * STILL_WINDOW_S] + [(now, q)]
            window = [s for t, s in run.samples if t >= now - STILL_WINDOW_S - 1e-9]
            span = now - min(t for t, _s in run.samples if t >= now - STILL_WINDOW_S - 1e-9)
            stack = np.stack(window)
            still = span >= STILL_WINDOW_S - 1e-9 and float(np.max(stack.max(axis=0) - stack.min(axis=0))) < \
                STILL_TOL_RAD
            if not still:
                run.still_since = None
                run.streak = []
                return
            if run.still_since is None:
                run.still_since = now - span
            i, dwell = run.pose, run.dwell
            if i is None or not dwell:
                return
            median = np.median(stack, axis=0)
            run.rested[i] = median
            need_look = i not in run.looks
            need_target = i not in run.captured
        if need_look:
            look = self._look(run, None)
            if look is not None:
                with self._lock:
                    run.looks[i] = dict(look.tags)
        if need_target:
            look = self._look(run, self._region(i))
            if look is not None:
                self._target_look(run, i, look, median)
        if i == 0:
            self._identity_check(run, now)

    def _target_look(self, run, i, look, q):
        groups = _tags.classify(look.tags, self.kind)
        own = {tid: np.asarray(look.tags[tid].corners, dtype=np.float64) for tid in groups['target']}
        with self._lock:
            if i == 0:
                run.pose1_target = max(run.pose1_target, len(own))
                run.pose1_foreign = max(run.pose1_foreign, len(groups['foreign_target']))
            if len(own) < _ac.CAPTURE_MIN_TARGET_TAGS:
                run.streak = []
                return
            if run.streak and not _ac.still_between(run.streak[-1][0], own, LOOK_STABLE_PX):
                run.streak = []
            run.streak.append((own, q))
            if len(run.streak) < CAPTURE_LOOKS:
                return
            looks = run.streak[-CAPTURE_LOOKS:]
            common = [tid for tid in looks[-1][0] if all(tid in lk for lk, _q in looks)]
            target = {tid: np.median(np.stack([lk[tid] for lk, _q in looks]), axis=0) for tid in common}
            measured = np.median(np.stack([s for _lk, s in looks]), axis=0)
            if _ac.capturable(run.poses[i], measured, len(target)):
                run.captured[i] = (measured, target)
            run.streak = []
        self._set_running(run)

    def _identity_check(self, run, now):
        with self._lock:
            if run.failure is not None or run.still_since is None or now - run.still_since < POSE1_CHECK_S - 1e-9:
                return
            failure = None
            if 0 not in run.captured and run.pose1_target < _ac.CAPTURE_MIN_TARGET_TAGS:
                failure = 'foreign_target' if run.pose1_foreign > 0 else 'target_not_seen'
            elif 0 in run.looks:
                groups = _tags.classify(run.looks[0], self.kind)
                if (len(groups['foreign_mat']) >= _mc.WRONG_MAT_TAGS
                        and len(groups['field']) < _mc.WRONG_MAT_TAGS):
                    failure = 'wrong_mat'
                elif len(groups['field']) < _mc.MIN_FIELD_TAGS:
                    failure = 'tags_hidden'
                elif len(groups['fixture']) < len(_tags.FIXTURE_IDS):
                    failure = 'fixture_hidden'
            if failure is None:
                return
            run.failure = (failure, _t.BY_CODE[failure])
        self._set_state('needs_arm_check', failure, _t.BY_CODE[failure])
        self._log(_t.BY_CODE[failure])
        try:
            self._stop_program(run.workflow_id)
        except Exception as e:  # noqa: BLE001 — the program then ends on its own; the failure stands
            self._log_internal(f'arm check program not stopped: {type(e).__name__}: {e}')

    # ── the solve ────────────────────────────────────────────────────────────
    def _records(self, run):
        with self._lock:
            records = []
            for i in sorted(set(run.rested) | set(run.captured)):
                commanded = tuple(float(v) for v in run.poses[i])
                if i in run.captured:
                    measured, target = run.captured[i]
                else:
                    measured, target = run.rested[i], None
                records.append(_ac.PoseRecord(i, commanded, tuple(float(v) for v in measured), target))
            looks = [run.looks[i] for i in sorted(run.looks)]
        return records, looks

    def _solve(self, run):
        try:
            records, looks = self._records(run)
            handheld = _hh.read(self.calib_dir, _CAPTURE_SIZE)
            result = _ac.run(records, looks, profile_id=self.profile.profile_id, image_size=_CAPTURE_SIZE,
                             camera_profile_id=run.camera_profile_id, lens_prior=handheld)
            refusal = None if result.ok else (result.code, result.message_de)
            if refusal is None:
                ok, detail = guard_margin(result.calibration, self.profile, self._guard_parts)
                if not ok:
                    self._log_internal(f'arm check refused by guard_margin: {detail}')
                    refusal = ('guard_margin', _t.GUARD_MARGIN_DE)
            with self._lock:
                cancelled = run.cancelled
            droop = self._droop_state(result.droop_warning)
            for text in result.warnings_de:
                self._log('[WARNUNG] ' + text)
            if cancelled:
                self._set_state('needs_arm_check', 'stopped', _t.STOPPED_DE)
            elif refusal is not None:
                self._log(refusal[1])
                self._set_state('needs_arm_check', refusal[0], refusal[1], notes=result.notes, droop=droop)
            else:
                self._store.write(result.calibration)
                if handheld is not None:
                    self._use_up_handheld()
                with self._lock:
                    self._last_ok = None
                self._set_state('confirm_target_removed', 'confirm_target_removed', _t.CONFIRM_TARGET_REMOVED_DE,
                                notes=result.notes, quality=result.calibration['quality'], droop=droop)
        except Exception as e:  # noqa: BLE001 — a solve that raised writes nothing
            self._log_internal(f'arm check solve failed: {type(e).__name__}: {e}')
            self._set_state('needs_arm_check', 'stopped', _t.STOPPED_DE)
        finally:
            self._close(run)

    def _use_up_handheld(self):
        """The written arm check holds the handheld lens now: the result is used
        up. A file left behind is held by the next arm check too (the same lens)."""
        try:
            _hh.discard(self.calib_dir)
        except OSError as e:
            self._log_internal(f'{_hh.FILE_NAME} not removed: {type(e).__name__}')

    # ── the handheld step (decision D9's fallback) ───────────────────────────
    def start_handheld(self):
        """``(ok, sentence)``: claim the calibration mode, hold the studio
        camera and start the capture thread (see the module docstring)."""
        profile = self._camera.profile()
        if profile is None:
            return False, _t.PROFILE_UNKNOWN_DE
        if profile != _sf.FIXTURE_ID or self.kind is None:
            return False, _t.CAMERA_NOT_TOWER_DE
        if not self._streaming():
            return False, _t.CAMERA_MISSING_DE
        try:
            camera_profile_id = (self._camera.camera_state() or {}).get('profile_id')
        except Exception:  # noqa: BLE001
            camera_profile_id = None
        capture = _hh.Capture(self.kind, looks=CAPTURE_LOOKS, stable_px=LOOK_STABLE_PX, image_size=_CAPTURE_SIZE)
        with self._lock:
            busy = self._busy_message()
            if busy is not None:
                return False, busy
            run = _HandheldRun(capture, self._state, camera_profile_id)
            self._handheld = run         # from here on a second start is busy
        try:
            ok, message = self._claim_mode()
        except Exception as e:  # noqa: BLE001 — a claim that raised is a claim refused
            self._log_internal(f'calibration mode not claimed: {type(e).__name__}: {e}')
            ok, message = False, CHECK_FAILED_DE
        if not ok:
            with self._lock:
                run.closed = True
                self._handheld = None
            return False, message
        self._camera.hold()
        self._set_handheld_running(run)
        self._start_thread(lambda: self._handheld_loop(run), CAPTURE_THREAD)
        return True, _t.handheld_running_de(0)

    def _set_handheld_running(self, run):
        with self._lock:
            progress = run.capture.progress()
        self._set_state('handheld_running', 'handheld_running', _t.handheld_running_de(progress['views']),
                        handheld=progress)

    def _handheld_loop(self, run):
        logged = False
        while True:
            with self._lock:
                if run.closed or run.solving:
                    return
            try:
                self.handheld_tick()
            except Exception as e:  # noqa: BLE001 — logged once; a cancel ends the step
                if not logged:
                    logged = True
                    self._log_internal(f'handheld capture failed: {type(e).__name__}: {e}')
            self._sleep(CAPTURE_PERIOD_S)

    def handheld_tick(self):
        """One full-frame look of the handheld capture (the capture thread calls
        it at 10 Hz); the capture complete, the solve on a worker thread."""
        with self._lock:
            run = self._handheld
            if run is None or run.closed or run.solving:
                return
        seen = self._look(run, None)
        if seen is None:
            return
        with self._lock:
            if run.closed or run.solving:
                return
            captured = run.capture.offer(seen.tags)
            run.solving = solving = run.capture.finished
        if captured:
            self._set_handheld_running(run)
        if solving:
            self._start_thread(lambda: self._handheld_solve(run), WORK_THREAD)

    def _handheld_solve(self, run):
        try:
            with self._lock:
                views = list(run.capture.views)
            result = _hh.solve(views)
            with self._lock:
                cancelled = run.cancelled
                handheld = dict(run.capture.progress(), rms_px=_finite_or_none(float(result.rms_px)))
            if cancelled:
                return
            if result.ok:
                _hh.write(self.calib_dir, _hh.result_dict(
                    result, run.capture, camera_profile_id=run.camera_profile_id, image_size=_CAPTURE_SIZE,
                    created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._wall()))))
                self._set_state('needs_arm_check', 'handheld_done', _t.HANDHELD_DONE_DE, handheld=handheld)
            else:
                self._log(_t.HANDHELD_FAILED_DE)
                self._set_state('needs_arm_check', 'handheld_failed', _t.HANDHELD_FAILED_DE, handheld=handheld)
        except Exception as e:  # noqa: BLE001 — a solve that raised writes nothing
            self._log_internal(f'handheld solve failed: {type(e).__name__}: {e}')
            self._log(_t.HANDHELD_FAILED_DE)
            self._set_state('needs_arm_check', 'handheld_failed', _t.HANDHELD_FAILED_DE)
        finally:
            self._close_handheld(run)

    def _close_handheld(self, run):
        with self._lock:
            if run.closed:
                return
            run.closed = True
            if self._handheld is run:
                self._handheld = None
        self._camera.release()
        self._release_mode()
