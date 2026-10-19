"""A calibrated rig, emulated on the simulator — for the tests whose promise
depends on the camera (the „Greife" wait, the put-back reclaim, the hat re-arm).

Not a test module. A test imports what it needs::

    from rig_emulation import Rig, B, chain, program, ref, num, text

Why an emulation and not the plain simulator: the simulator has no camera, so
``perception_blocks._tag_table_xy`` answers ``None`` there and the recycled-
object reclaim (a person moved a placed cube) can never see a move. On a real,
calibrated rig it projects the tag onto the table. ``rig_projection`` patches it
to the object's TRUE table (x, y), which is exactly what a perfect projection
answers — the same trick the round-1 harness ``putback.py`` used.

``Rig(simulated=False)`` (the default) builds the manager the way the REAL node
does for the arm (``simulated`` off, so „Greife" waits for an object, Q8);
``simulated=True`` builds it the way ``_get_or_create_sim_workflow_manager`` does
(no wait, the sim wording, the sim table at the solver's fingertip height).

``real_time=False`` (the default) skips the inter-chunk pacing of
``trajectory_builder`` through ``monkeypatch``, like ``test_sim_runtime``; the
published waypoints are byte-identical. The put-back and hat cases that depend
on how long a body takes run with ``real_time=True``.
"""

from __future__ import annotations

import json
import threading
import time
import types

import numpy as np

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.sim_world import SimWorld
from physical_ai_server.workflow.workflow_manager import WorkflowManager


def true_table_xy(ctx, d, recipe):
    """A perfect camera projection: the detection's own table (x, y)."""
    w = getattr(d, 'world_xyz_m', None)
    return None if w is None else (float(w[0]), float(w[1]))


def fast_pacing(monkeypatch) -> None:
    """Skip the real-time chunk pacing (waypoints unchanged)."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_monotonic,
                                              sleep=lambda _s: None))


class Rig:
    """One arm, one SimWorld, one WorkflowManager wired like the node."""

    def __init__(self, monkeypatch, pid: str = 'omx_full', objects=None, *,
                 simulated: bool = False, real_time: bool = False,
                 rig_projection: bool = True, catalog=None,
                 z_table: float | None = None, scene_frame=True,
                 scene_frame_age=None, code_rpc=None):
        self.pid = pid
        self.profile = rp.ROBOT_PROFILES[pid]
        self.objects = [dict(o) for o in (objects or [])]
        self.world = SimWorld([dict(o) for o in self.objects])
        self.catalog = catalog if catalog is not None else fixed_catalog(pid)
        if not real_time:
            fast_pacing(monkeypatch)
        if rig_projection:
            monkeypatch.setattr(pb, '_tag_table_xy', true_table_xy)
        p = self.profile
        home = [float(v) for v in p.home_joints_rad] + [float(p.gripper_open_rad)]
        self.ik = p.build_ik()
        self.frames: list = []
        self.arm = SimArm(
            joint_state_sink=self.frames.append,
            ik=p.build_ik(),
            objects=[dict(o) for o in self.objects],
            num_arm_joints=p.num_arm_joints,
            home_full_joints=home,
            close_threshold_rad=p.sim_close_threshold_rad,
            held_block_offset_rad=p.sim_held_block_offset_rad,
            held_floor_rad=p.sim_held_floor_rad,
            world=self.world,
        )
        tip = float(getattr(self.ik, 'tool_tip_below_ee_m', 0.0) or 0.0)
        if z_table is None:
            # The simulator's table (Q13) sits at the fingertip height; the
            # emulated rig keeps the same table, as a touch-off would measure.
            z_table = tip
        self.z_table = float(z_table)
        self.statuses: list[dict] = []
        self.done = threading.Event()
        self.final = None
        dummy = np.zeros((1, 1, 3), dtype=np.uint8)
        self._frame = dummy if scene_frame else None
        self._frame_age = scene_frame_age
        cat = self.catalog
        self.mgr = WorkflowManager(
            publisher=self.arm.publish,
            ik_factory=p.build_ik,
            perception_factory=lambda: SimPerception(
                [dict(o) for o in self.objects], cat, self.world,
                table_z=self.z_table),
            load_destinations=lambda: {},
            load_calibration=lambda: {'z_table': self.z_table},
            emit_status=self.statuses.append,
            on_finished=self._finished,
            simulated=simulated,
            get_scene_frame=lambda: self._frame,
            get_scene_frame_age=lambda: (self._frame_age() if callable(self._frame_age)
                                         else 0.0),
            get_current_pose_xyz=lambda: self.arm.fk_xyz(),
            get_follower_joints=self.arm.get_joints,
            load_object_catalog=lambda: cat,
            arm_profile=p,
            code_rpc=code_rpc,
        )

    # ── running ──────────────────────────────────────────────────────────
    def _finished(self, phase):
        self.final = {'phase': phase,
                      'joints': list(self.arm.get_joints()),
                      'world': self.world.snapshot()}
        self.done.set()

    def start(self, payload: dict, wid: str = 'wf-rig'):
        self.done.clear()
        self.final = None
        ok, msg, warnings = self.mgr.start(json.dumps(payload), wid)
        assert ok, msg
        return warnings

    def run(self, payload: dict, timeout: float = 60.0, stop_after: float | None = None):
        """Run ``payload``; ``stop_after`` presses Stop after that many seconds.
        Returns the terminal phase ('finished' | 'stopped' | 'error')."""
        self.start(payload)
        timer = None
        if stop_after is not None:
            timer = threading.Timer(stop_after, self.mgr.stop)
            timer.daemon = True
            timer.start()
        if not self.done.wait(timeout):
            self.mgr.stop()
            self.done.wait(10)
            raise AssertionError(f'workflow did not end within {timeout} s; logs: {self.logs()[-12:]}')
        if timer is not None:
            timer.cancel()
        return self.final['phase']

    def logs(self) -> list[str]:
        return [s['log_message'] for s in self.statuses
                if isinstance(s, dict) and s.get('log_message')]

    def errors(self) -> list[str]:
        return [s['error'] for s in self.statuses
                if isinstance(s, dict) and s.get('error')]

    def grasps(self) -> int:
        """„„Würfel" gegriffen." lines, counted from the direct emit trace."""
        return sum(1 for m in self.logs() if m.endswith('" gegriffen.'))

    def ee_z(self, q=None) -> float:
        q = list(self.arm.get_joints()) if q is None else list(q)
        _R, t = self.ik.fk(q[:self.profile.num_arm_joints])
        return float(t[2])

    # ── a student who puts objects back ──────────────────────────────────
    def put_back(self, key: int, x: float, y: float) -> bool:
        """Move world object ``key`` to (x, y) unless it is in the jaws."""
        with self.world._lock:
            if self.world._held_key == key:
                return False
            self.world._objects[key]['x'] = float(x)
            self.world._objects[key]['y'] = float(y)
        return True

    def student(self, near_xy, back_xy, putbacks: int = 2, delay_s: float = 1.0):
        """A background student: whenever an object lies released near
        ``near_xy`` (a release spot), wait ``delay_s`` and put it back at
        ``back_xy[key]``, at most ``putbacks`` times per object. Returns
        ``(stop_event, events)``."""
        stop = threading.Event()
        events: list[str] = []
        done: dict[int, int] = {}
        t0 = time.monotonic()

        def _loop():
            while not stop.is_set():
                time.sleep(0.05)
                snap = self.world.snapshot()
                for o in snap['objects']:
                    k = o['key']
                    if snap.get('held') == k or done.get(k, 0) >= putbacks:
                        continue
                    if abs(o['x'] - near_xy[0]) < 0.03 and abs(o['y'] - near_xy[1]) < 0.03:
                        time.sleep(delay_s)
                        bx, by = back_xy[k % len(back_xy)]
                        if self.put_back(k, bx, by):
                            done[k] = done.get(k, 0) + 1
                            events.append(f't={time.monotonic() - t0:.1f}s cube {k} back')

        th = threading.Thread(target=_loop, daemon=True)
        th.start()
        return stop, events


# ── Blockly JSON builders ────────────────────────────────────────────────
_ids = [0]


def _id():
    _ids[0] += 1
    return f'b{_ids[0]}'


def B(btype, fields=None, inputs=None, nxt=None, extra=None):
    b = {'type': btype, 'id': _id()}
    if fields:
        b['fields'] = dict(fields)
    if inputs:
        b['inputs'] = {k: ({'block': v} if not (isinstance(v, dict) and ('block' in v or 'shadow' in v)) else v)
                       for k, v in inputs.items()}
    if nxt is not None:
        b['next'] = {'block': nxt}
    if extra:
        b['extraState'] = extra
    return b


def chain(*blocks):
    blocks = [b for b in blocks if b is not None]
    for a, b in zip(blocks, blocks[1:]):
        a['next'] = {'block': b}
    return blocks[0] if blocks else None


def num(v):
    return B('math_number', fields={'NUM': v})


def text(s):
    return B('text', fields={'TEXT': s})


def ref(name):
    return B('edubotics_destination_ref', fields={'NAME': name})


def var(name):
    return {'name': name, 'id': f'v_{name}'}


def vget(name):
    return B('variables_get', fields={'VAR': {'id': f'v_{name}'}})


def vset(name, value_block):
    return B('variables_set', fields={'VAR': {'id': f'v_{name}'}}, inputs={'VALUE': value_block})


def program(*roots, variables=None, destinations=None, trajectories=None, zones=None):
    p = {'blocks': {'languageVersion': 0, 'blocks': list(roots)}}
    if variables is not None:
        p['variables'] = variables
    p['destinations'] = destinations if destinations is not None else []
    if trajectories is not None:
        p['trajectories'] = trajectories
    if zones is not None:
        p['zones'] = zones
    return p


def grasp(t='wuerfel'):
    return B('edubotics_grasp_object', fields={'OBJECT_TYPE': t})


def drop(dest='Ablage'):
    return B('edubotics_drop_at', inputs={'DESTINATION': ref(dest)})


def sees(t='wuerfel'):
    return B('edubotics_see_object', fields={'OBJECT_TYPE': t})


def log(s):
    return B('edubotics_log', inputs={'MESSAGE': text(s)})


def wait(s):
    return B('edubotics_wait_seconds', inputs={'SECONDS': num(s)})


def forever(body):
    return B('edubotics_forever', inputs={'DO': body})


def hat(body, t='wuerfel'):
    return B('edubotics_when_object_seen', fields={'OBJECT_TYPE': t}, nxt=body)


def cube(x, y, yaw=0.2, typ='wuerfel'):
    return {'type': typ, 'x': x, 'y': y, 'yaw': yaw}


ABLAGE = {'name': 'Ablage', 'kind': 'pin', 'x': 0.10, 'y': 0.15, 'z': 0.0}
ABLAGE2 = {'name': 'Ablage2', 'kind': 'pin', 'x': 0.10, 'y': -0.15, 'z': 0.0}
PICK = [(0.18, 0.0), (0.16, -0.08), (0.20, 0.08), (0.14, 0.06)]
