#!/usr/bin/env python3
"""EduBotics robot activation agent — the arm moves when a student says so.

WHY THIS EXISTS
---------------
Until 2026-09-07 „Umgebung starten" was itself a powered motion. Which motion
depended on three branches in ``entrypoint_omx.sh`` Phase 3 (quintic sync to
the leader / follower-only HOME / nothing at all, when the leader read came
back empty) plus a fourth inside ``edu6_arm_node.start_boot_home``, and on a
both-arms rig the follower additionally began MIRRORING the leader the instant
``joint_trajectory_command_broadcaster`` spawned. All of that happened while
the container came up: before any browser was open, before anybody had logged
in, and with nobody's hand on the arm.

Now the container comes up SILENT and this node holds the motion until
``/edubotics/activate`` is called. In the product the only caller is the Start
page's „Roboter aktivieren" button, which lives inside StudentApp's
authenticated ``<main>`` — so in the ordinary case a moving arm means a
logged-in student asked for it, in front of the robot.

Two honest limits on that sentence, neither of which this node can close:
``utils/authGate.js`` offers an ATTEMPT-GATED „Ohne Anmeldung fortfahren"
escape when the auth service itself proves unreachable (a rig with no cloud has
to stay usable), and rosbridge authenticates nobody at all — anything that
reaches it could already publish to ``/leader/joint_trajectory`` directly, long
before this service existed. CLAUDE.md's rule applies verbatim: never record
the login gate as closing a wire-level hole. What this node removes is the
UNATTENDED motion, which is the actual defect.

WHAT „ACTIVATION" IS AND IS NOT
-------------------------------
It gates MOTION, not POWER. Servo torque still comes up with the container:
both Feetech arms backdrive and the OMX follower is held only by its servo
torque, so an un-torqued arm does not wait politely — it falls. This is the
same call ``start_boot_home`` already makes when its floor guard refuses (the
refusal LEAVES THE ARM TORQUED, CLAUDE.md Rule §2). Torque-on itself is
motionless: the Feetech driver seeds ``Goal = Present`` before energising, and
the OMX JointTrajectoryController holds the pose it booted in.

THE CONTRACT (deliberately interfaces-free)
-------------------------------------------
``/edubotics/activate``        std_srvs/Trigger  — returns IMMEDIATELY; the
                               sequence runs on a worker thread, so no
                               rosbridge call sits open for ~10 s.
``/edubotics/activation_state`` std_msgs/String   — JSON, latched + 1 Hz.
``/edubotics/home_arm``        std_srvs/Trigger  — served by the Feetech
                               driver, called by this node. Family-neutral
                               alias for /edu6/home | /edu1/home.

Both use stock message types on purpose: ``physical_ai_interfaces`` is built
only in the SERVER image, and a new .srv there would mean an interfaces
rebuild, an enum-parity entry and a CMakeLists edit for a two-field message.
``/sim/objects`` is the standing precedent for JSON-over-``std_msgs/String``.

THE SEQUENCE, PER RIG
---------------------
Feetech (edu6_studio / edu1_studio)
    call ``/edubotics/home_arm`` and let the DRIVER home itself. It owns the serial bus,
    the floor pre-check, the torque-on retry ladder and the single-writer
    trajectory rail; publishing a home trajectory at it from outside would walk
    straight past all four.
Follower-only OMX (omx_follower, Jetson, a flipped omx_full)
    3 s quintic to HOME, then a soft arrival check.
OMX with a leader
    HOME, then read the leader pose (BOUNDED — the entrypoint's version could
    block forever), 3 s quintic sync onto it, soft verify, and only then spawn
    the leader's ``joint_trajectory_command_broadcaster``.

    The sync leg is not decoration. That broadcaster republishes the leader's
    live pose at 100 Hz with ``time_from_start = 0``; spawning it while the
    follower sits at HOME and the leader lies wherever gravity left it is a
    step command into a 0.10 rad goal tolerance. home → sync → spawn is the
    only order that both "drives to home" and starts teleop without a snap.

HOW TELEOP IS ACTUALLY HELD OFF
-------------------------------
``omx_l_leader_ai.launch.py`` drops the broadcaster from the boot spawner's
argument list (gated on the same env var as everything here), so before
activation NOTHING PUBLISHES A COMMAND on ``/leader/joint_trajectory``. (Bare
publishers do exist from boot — this node's own, and the collision monitor's —
they simply never write.) The teleop half of the gate is therefore structural:
the controller that would stream the leader's pose at 100 Hz is not loaded, so
there is no flag for a code path to forget to consult. Reusing
``/collision_flag`` — which the broadcaster already honours — was rejected: it
would conflate „not activated" with „collision", and the collision monitor's
5 Hz watchdog owns that topic.

ROLLBACK
--------
``EDUBOTICS_REQUIRE_ACTIVATION=0`` restores the pre-gate behaviour exactly
(boot home, boot sync, broadcaster in the boot spawner). This node still runs
and reports ``active`` from the start, so the Start page tells the truth in
both modes.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from std_srvs.srv import Trigger
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

# ── constants ────────────────────────────────────────────────────────────────

#: The OMX joint order on the wire. Identical to the list the entrypoint's
#: Phase-3 heredocs used, and to what the follower's arm_controller expects.
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']

#: Follower HOME. MUST stay equal to workflow/handlers/motion.py
#: HOME_JOINTS_RAD + gripper open — this is the ±pi/2 boot/workflow family, NOT
#: the deliberately more-folded ±pi/4 collision/Jetson safe home
#: (collision_monitor.SAFE_HOME_ARM, jetson_agent SAFE_HOME_JOINTS). Two HOME
#: families exist on purpose; reconciling them is an ask-first safety change.
HOME_JOINTS_RAD = [0.0, -1.5707963267948966, 1.5707963267948966, 0.0, 0.0, 0.8]

#: Quintic duration for both the home leg and the sync leg, and the number of
#: waypoints in each. Same 3 s / 50 points the entrypoint published.
MOVE_DURATION_S = 3.0
MOVE_POINTS = 50

#: Arrival tolerance, and the joints it is judged on. Carried over verbatim
#: from entrypoint_omx.sh: 0.30 rad because the arm_controller routinely aborts
#: the 3 s quintic mid-flight on its own goal tolerance, and the GRIPPER is
#: exempt because the leader's trigger sits at ~-0.70 rad while the follower's
#: gripper starts open — a ~1.4 rad error that is expected, not a dropout.
VERIFY_TOL_RAD = 0.30
ARM_IDX = (0, 1, 2, 3, 4)

#: How long to wait for the first usable /joint_states and /leader/joint_states.
#: The entrypoint's leader read had NO timeout and would spin forever if the
#: leader never published a complete message; that is fixed here.
JOINT_WAIT_S = 10.0
LEADER_WAIT_S = 5.0

#: How old a cached joint sample may be and still be used to BUILD A MOTION.
#: Without this the caches were never invalidated by age, so a feed that
#: delivered one sample at boot and then died still produced a quintic from
#: that stale pose — and the only symptom was the soft „did not quite arrive"
#: warning. /joint_states runs at 100 Hz, so 2 s is ~200 missed messages;
#: matching the SPA's own 3 s liveness window in spirit, tighter because this
#: value becomes a commanded trajectory rather than a dot on a page.
JOINT_MAX_AGE_S = 2.0

#: Settle margin after a move before the arrival check, and how long to keep
#: re-checking. Mirrors the entrypoint (DURATION + 0.5, then a 2 s window).
SETTLE_S = 0.5
VERIFY_WINDOW_S = 2.0

#: Controller spawn. The leader's controller_manager lives under the `leader`
#: namespace (PushRosNamespace in omx_l_leader_ai.launch.py), so the spawner
#: needs an explicit -c; inside the launch it inherited the namespace.
TELEOP_CONTROLLER = 'joint_trajectory_command_broadcaster'
LEADER_CONTROLLER_MANAGER = '/leader/controller_manager'
SPAWNER_TIMEOUT_S = 60.0
#: Spawner output that means „the controller_manager already has it", which is
#: a SUCCESS for us. See _start_teleop.
_ALREADY_LOADED = ('already loaded', 'already active', 'already exists')

STATE_TOPIC = '/edubotics/activation_state'
ACTIVATE_SERVICE = '/edubotics/activate'
#: The FAMILY-NEUTRAL alias edu6_arm_node.py exposes alongside its own
#: /edu6/home | /edu1/home. Calling the neutral name keeps the family branch in
#: exactly one file (the driver) instead of two.
FEETECH_HOME_SERVICE = '/edubotics/home_arm'
FEETECH_HOME_WAIT_S = 20.0

#: Feetech families. Same `case` split entrypoint_omx.sh makes — one place per
#: layer, never a chain of `!=` tests that can drift apart.
FEETECH_ROBOT_TYPES = ('edu6_studio', 'edu1_studio')

STATE_IDLE = 'idle'
STATE_ACTIVATING = 'activating'
STATE_ACTIVE = 'active'
STATE_FAILED = 'failed'

# ── German, because a student reads every one of these ───────────────────────

MSG_IDLE = ('Der Roboter ist noch nicht aktiviert. Er steht still, '
            'bis du ihn aktivierst.')
MSG_STEP_HOME = 'Der Roboter fährt in die Grundstellung …'
MSG_STEP_SYNC = 'Der Follower-Arm gleicht sich an den Leader-Arm an …'
MSG_STEP_TELEOP = 'Die Teleoperation wird eingeschaltet …'
MSG_ACTIVE = 'Der Roboter ist aktiv.'
MSG_ACTIVE_TELEOP = ('Der Roboter ist aktiv. Der Follower-Arm folgt jetzt '
                     'dem Leader-Arm.')
MSG_BUSY = 'Der Roboter wird bereits aktiviert. Bitte warten.'
#: A SECOND activation on a leader rig is refused, and this is a correctness
#: refusal, not politeness. Once the broadcaster is spawned it republishes the
#: leader's pose onto /leader/joint_trajectory at 100 Hz; a home trajectory
#: published onto the same rail is overwritten within 10 ms and the follower
#: snaps straight back to the leader. Homing a teleoperating rig means moving
#: the LEADER, which is a thing only hands can do.
MSG_TELEOP_ALREADY_RUNNING = (
    'Die Teleoperation läuft bereits — der Follower-Arm folgt dem Leader-Arm. '
    'Führe den Leader-Arm von Hand in die Grundstellung.')
MSG_NO_JOINTS = ('[FEHLER] Vom Roboterarm kommen keine Gelenkdaten. '
                 'Bitte prüfe Kabel und Stromversorgung und starte die '
                 'Umgebung neu.')
MSG_NO_LEADER = ('[FEHLER] Vom Leader-Arm kommen keine Gelenkdaten. '
                 'Bitte prüfe das USB-Kabel des Leader-Arms und starte die '
                 'Umgebung neu.')
MSG_HOME_INCOMPLETE = ('[WARNUNG] Der Roboter hat die Grundstellung nicht '
                       'ganz erreicht. Bitte prüfe, ob der Arm irgendwo '
                       'ansteht.')
MSG_SYNC_INCOMPLETE = ('[WARNUNG] Der Follower-Arm hat den Leader-Arm nicht '
                       'ganz erreicht. Die erste Bewegung des Leader-Arms '
                       'kann darum größer ausfallen als sonst.')
MSG_TELEOP_FAILED = ('[FEHLER] Die Teleoperation konnte nicht eingeschaltet '
                     'werden. Bitte starte die Umgebung neu.')
MSG_FEETECH_UNREACHABLE = ('[FEHLER] Der Arm-Treiber antwortet nicht. Bitte '
                           'starte die Umgebung neu.')

# ── pure helpers (unit-tested without rclpy — see tests/test_activation_agent) ─


def env_flag(value, default=True):
    """Interpret an env string as a boolean the compose way.

    Only a literal ``0`` turns a flag off. An UNSET or empty value keeps the
    default, because compose writes ``KEY=`` for an unset ``${KEY:-}`` and an
    empty string there means „not configured", never „disabled"."""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return text != '0'


def is_feetech(robot_type):
    """True for the arms whose driver owns its own bus and its own home."""
    return (robot_type or '').strip() in FEETECH_ROBOT_TYPES


def activation_steps(robot_type, follower_only):
    """The ordered step ids this rig runs on activation.

    One place decides the shape of the sequence, so the status topic, the log
    and the code below cannot disagree about what is going to happen."""
    if is_feetech(robot_type):
        return ('home',)
    if follower_only:
        return ('home',)
    return ('home', 'sync', 'teleop')


def has_leader(robot_type, follower_only):
    """True when this rig teleoperates, i.e. when a leader arm is launched."""
    return not is_feetech(robot_type) and not follower_only


def quintic_waypoints(start, target, duration_s=MOVE_DURATION_S,
                      count=MOVE_POINTS):
    """Zero-velocity, zero-acceleration quintic blend from ``start`` to
    ``target``.

    Returns ``[(positions, velocities, accelerations, t_s), …]``. Byte-for-byte
    the same polynomial the entrypoint published (s = 10t³ − 15t⁴ + 6t⁵), kept
    as a pure function so the arithmetic is testable without a robot: the
    controller needs explicit velocities and accelerations or it interpolates
    numerically and can overshoot."""
    if len(start) != len(target):
        raise ValueError('start and target must have the same length')
    if duration_s <= 0 or count <= 0:
        raise ValueError('duration and count must be positive')
    deltas = [t - s for s, t in zip(start, target)]
    points = []
    for i in range(count):
        t = (i + 1) / count
        s = 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5
        s_dot = (30 * t ** 2 - 60 * t ** 3 + 30 * t ** 4) / duration_s
        s_ddot = (60 * t - 180 * t ** 2 + 120 * t ** 3) / (duration_s ** 2)
        points.append((
            [p + d * s for p, d in zip(start, deltas)],
            [d * s_dot for d in deltas],
            [d * s_ddot for d in deltas],
            duration_s * t,
        ))
    return points


def arrival_decision(start_pos, current_pos, target, tol=VERIFY_TOL_RAD,
                     arm_idx=ARM_IDX):
    """Did the arm get there, and did it actually move getting there?

    Returns ``(ok, stale_joint_names, max_err, max_motion)``.

    TWO conditions, both from the entrypoint's Audit-E3 verifier, because
    either alone passes vacuously:

    * every judged joint within ``tol`` of the target, AND
    * every judged joint whose COMMANDED delta was meaningful (> tol) has
      traversed at least half of it.

    Without the second, a ``/joint_states`` feed that stops mid-move leaves a
    stale snapshot that can match the target while the arm never left its start
    pose. Without the first, an arm that thrashes and stops short passes."""
    err = [abs(c - t) for c, t in zip(current_pos, target)]
    motion = [abs(c - s) for c, s in zip(current_pos, start_pos)]
    commanded = [t - s for s, t in zip(start_pos, target)]
    stale = [
        i for i in arm_idx
        if abs(commanded[i]) > tol and motion[i] < 0.5 * abs(commanded[i])
    ]
    reached = all(err[i] < tol for i in arm_idx)
    max_err = max(err[i] for i in arm_idx) if arm_idx else 0.0
    max_motion = max(motion[i] for i in arm_idx) if arm_idx else 0.0
    return (reached and not stale), stale, max_err, max_motion


def state_payload(state, step, message, robot_type, leader, required, seq):
    """The JSON the Start page reads. Keys are the wire contract with
    ``hooks/useRobotActivation.js`` — additive only."""
    return {
        'state': state,
        'step': step or '',
        'message': message or '',
        'robot_type': robot_type or '',
        'has_leader': bool(leader),
        'required': bool(required),
        'seq': int(seq),
    }


def fresh_pose(sample, now, max_age_s=JOINT_MAX_AGE_S):
    """A cached ``(pose, stamp)`` if it is recent enough, else None.

    „Recent enough" is the difference between „the arm is not talking to us"
    and „the arm told us once, ten minutes ago" — and the second one used to
    build a trajectory. ``max_age_s <= 0`` disables the check, which is the
    behaviour this replaced."""
    if not sample:
        return None
    pose, stamp = sample
    if max_age_s > 0 and (now - stamp) > max_age_s:
        return None
    return pose


def joint_positions(names, positions, wanted=JOINTS):
    """Pull ``wanted`` out of a JointState in OUR order, or None.

    None means "this message does not describe the joints we command" — a
    partial message must never be padded or reordered into a trajectory."""
    if not names or positions is None:
        return None
    index = {}
    for i, name in enumerate(names):
        if name not in index:
            index[name] = i
    if not all(j in index for j in wanted):
        return None
    if any(index[j] >= len(positions) for j in wanted):
        return None
    return [float(positions[index[j]]) for j in wanted]


# ── the node ─────────────────────────────────────────────────────────────────


class ActivationAgent(Node):

    def __init__(self):
        super().__init__('edubotics_activation')
        self._robot_type = os.environ.get('EDUBOTICS_ROBOT_TYPE', 'omx_full')
        self._follower_only = (
            os.environ.get('EDUBOTICS_FOLLOWER_ONLY', '0').strip() == '1')
        self._required = env_flag(
            os.environ.get('EDUBOTICS_REQUIRE_ACTIVATION'), default=True)
        self._feetech = is_feetech(self._robot_type)
        self._leader = has_leader(self._robot_type, self._follower_only)

        self._group = ReentrantCallbackGroup()
        self._lock = threading.Lock()
        self._seq = 0
        self._worker = None
        # With activation NOT required the boot path already homed and already
        # spawned the broadcaster, so the honest state is `active` and teleop
        # must not be spawned a second time.
        self._state = STATE_IDLE if self._required else STATE_ACTIVE
        self._step = ''
        self._message = MSG_IDLE if self._required else MSG_ACTIVE
        self._teleop_started = not self._required

        # (pose, monotonic timestamp). The timestamp is what makes a dead
        # feed distinguishable from a silent one — see JOINT_MAX_AGE_S.
        self._follower_sample = None
        self._leader_sample = None

        self.create_subscription(
            JointState, '/joint_states', self._follower_cb, 10,
            callback_group=self._group)
        if self._leader:
            self.create_subscription(
                JointState, '/leader/joint_states', self._leader_cb, 10,
                callback_group=self._group)

        # Latched: a browser that connects after activation must learn the
        # state from the first message, not from the next 1 Hz tick. The timer
        # is belt-and-braces for rosbridge subscriptions, which do not always
        # ask for TRANSIENT_LOCAL.
        self._state_pub = self.create_publisher(
            String, STATE_TOPIC,
            QoSProfile(depth=1,
                       reliability=ReliabilityPolicy.RELIABLE,
                       durability=DurabilityPolicy.TRANSIENT_LOCAL))
        # Same QoS the entrypoint's Phase-3 publishers used. The follower's
        # arm_controller subscribes VOLATILE, which a TRANSIENT_LOCAL offer
        # satisfies.
        self._traj_pub = self.create_publisher(
            JointTrajectory, '/leader/joint_trajectory',
            QoSProfile(depth=10,
                       reliability=ReliabilityPolicy.RELIABLE,
                       durability=DurabilityPolicy.TRANSIENT_LOCAL))

        self._home_client = (
            self.create_client(Trigger, FEETECH_HOME_SERVICE,
                               callback_group=self._group)
            if self._feetech else None)

        self.create_service(Trigger, ACTIVATE_SERVICE, self._activate_cb,
                            callback_group=self._group)
        self.create_timer(1.0, self._publish_state, callback_group=self._group)
        self._publish_state()
        self.get_logger().info(
            f'Aktivierung bereit ({self._robot_type}, '
            f'Leader={"ja" if self._leader else "nein"}, '
            f'erforderlich={"ja" if self._required else "nein"}).')

    # ── subscriptions ────────────────────────────────────────────────────────

    def _follower_cb(self, msg):
        pos = joint_positions(list(msg.name), list(msg.position))
        if pos is not None:
            self._follower_sample = (pos, time.monotonic())

    def _leader_cb(self, msg):
        pos = joint_positions(list(msg.name), list(msg.position))
        if pos is not None:
            self._leader_sample = (pos, time.monotonic())

    @property
    def _follower_pos(self):
        return fresh_pose(self._follower_sample, time.monotonic())

    @property
    def _leader_pos(self):
        return fresh_pose(self._leader_sample, time.monotonic())

    # ── state ────────────────────────────────────────────────────────────────

    def _set_state(self, state, step='', message=''):
        with self._lock:
            self._state = state
            self._step = step
            self._message = message
        self._publish_state()

    def _publish_state(self):
        """Serialise AND publish under the lock.

        The seq allocation alone is not enough: with json.dumps + publish()
        outside, two threads (the 1 Hz timer and the worker's transitions) can
        allocate 5 then 6 and publish 6 then 5, so a subscriber sees a stale
        state AFTER a newer one — measured at 0.02 % under hammering, including
        messages landing after an `active` with an older seq. Holding the lock
        across the publish makes seq order and wire order the same order. The
        publish is a non-blocking DDS write, so the critical section stays
        short; nothing in here can call back into a lock-taking method."""
        with self._lock:
            self._seq += 1
            payload = state_payload(
                self._state, self._step, self._message, self._robot_type,
                self._leader, self._required, self._seq)
            msg = String()
            msg.data = json.dumps(payload, ensure_ascii=False)
            self._state_pub.publish(msg)

    # ── the service ──────────────────────────────────────────────────────────

    def _activate_cb(self, _request, response):
        """Start the sequence and return AT ONCE.

        A blocking implementation would hold a rosbridge service call open for
        the whole ~10 s sequence, and the React service timeout is 10 s. The
        state topic is how progress gets back."""
        with self._lock:
            if self._state == STATE_ACTIVATING:
                response.success = False
                response.message = MSG_BUSY
                return response
            if self._leader and self._teleop_started:
                # Not a lock, a physics refusal — see MSG_TELEOP_ALREADY_RUNNING.
                # React hides the re-home button in this state; this is the half
                # that holds when something else calls the service.
                response.success = False
                response.message = MSG_TELEOP_ALREADY_RUNNING
                return response
            self._state = STATE_ACTIVATING
            self._step = ''
            self._message = MSG_STEP_HOME
            worker = threading.Thread(target=self._run_sequence,
                                      name='activation', daemon=True)
            self._worker = worker
        self._publish_state()
        worker.start()
        response.success = True
        response.message = MSG_STEP_HOME
        return response

    # ── the sequence ─────────────────────────────────────────────────────────

    def _run_sequence(self):
        """Walk the steps activation_steps() declares for this rig.

        The loop reads that helper rather than re-deciding the shape inline:
        the status topic, the log and the code have to agree about what is
        going to happen, and two `if self._feetech` chains in two places is
        exactly how they stop agreeing. An unknown step id is a programming
        error, not a runtime condition, so it raises."""
        try:
            steps = activation_steps(self._robot_type, self._follower_only)
            # Carried from leg to leg: a SOFT warning (moved, but not all the
            # way there) does not stop the sequence, but it must still reach
            # the student on the final message.
            warning = ''
            for step in steps:
                if step == 'home':
                    self._set_state(STATE_ACTIVATING, 'home', MSG_STEP_HOME)
                    ok, message = (self._home_feetech() if self._feetech
                                   else self._home_omx())
                elif step == 'sync':
                    self._set_state(STATE_ACTIVATING, 'sync', MSG_STEP_SYNC)
                    ok, message = self._sync_to_leader()
                elif step == 'teleop':
                    self._set_state(STATE_ACTIVATING, 'teleop', MSG_STEP_TELEOP)
                    ok = self._start_teleop()
                    message = '' if ok else MSG_TELEOP_FAILED
                else:
                    raise ValueError(f'unknown activation step {step!r}')
                if not ok:
                    self._set_state(STATE_FAILED, step, message)
                    self.get_logger().error(message)
                    return
                if message:
                    self.get_logger().warning(message)
                    warning = message

            final = MSG_ACTIVE_TELEOP if self._leader else MSG_ACTIVE
            if warning:
                final = f'{final} {warning}'
            self._set_state(STATE_ACTIVE, '', final)
            self.get_logger().info('Roboter aktiviert.')
        except Exception as exc:  # noqa: BLE001 - a dead worker must not
            # leave the card spinning forever; report and allow a retry.
            self._set_state(STATE_FAILED, '',
                            f'[FEHLER] Die Aktivierung ist fehlgeschlagen: {exc}')
            self.get_logger().error(f'Aktivierung fehlgeschlagen: {exc}')

    # ── legs ─────────────────────────────────────────────────────────────────

    def _wait_for(self, getter, timeout_s):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = getter()
            if value is not None:
                return value
            time.sleep(0.05)
        return getter()

    def _home_feetech(self):
        """Ask the Feetech driver to home ITSELF.

        The driver owns the serial bus, the table-floor pre-check
        (``boot_home_floor_decision``), the torque-on retry ladder and the
        single-writer trajectory rail. Publishing a home trajectory at it from
        out here would bypass every one of those."""
        if not self._home_client.wait_for_service(timeout_sec=5.0):
            return False, MSG_FEETECH_UNREACHABLE
        future = self._home_client.call_async(Trigger.Request())
        deadline = time.monotonic() + FEETECH_HOME_WAIT_S
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not future.done():
            return False, MSG_FEETECH_UNREACHABLE
        result = future.result()
        if result is None:
            return False, MSG_FEETECH_UNREACHABLE
        if not result.success:
            # The driver's own German reason — cable, 12 V, a joint parked on
            # the position-map edge, a floor refusal. Never overwrite it.
            return False, result.message or MSG_FEETECH_UNREACHABLE
        # The driver returns as soon as the glide is on the rail and verifies
        # it on its own thread; give the motion time before we call it active.
        time.sleep(MOVE_DURATION_S + SETTLE_S)
        return True, ''

    def _home_omx(self):
        start = self._wait_for(lambda: self._follower_pos, JOINT_WAIT_S)
        if start is None:
            return False, MSG_NO_JOINTS
        self._publish_trajectory(start, HOME_JOINTS_RAD)
        ok, stale, max_err, max_motion = self._await_arrival(
            start, HOME_JOINTS_RAD)
        self.get_logger().info(
            f'Grundstellung: max. Abweichung {max_err:.3f} rad, '
            f'max. Bewegung {max_motion:.3f} rad.')
        if not ok:
            return True, MSG_HOME_INCOMPLETE
        return True, ''

    def _sync_to_leader(self):
        leader = self._wait_for(lambda: self._leader_pos, LEADER_WAIT_S)
        if leader is None:
            return False, MSG_NO_LEADER
        start = self._wait_for(lambda: self._follower_pos, JOINT_WAIT_S)
        if start is None:
            return False, MSG_NO_JOINTS
        self._publish_trajectory(start, leader)
        ok, stale, max_err, max_motion = self._await_arrival(start, leader)
        self.get_logger().info(
            f'Leader-Abgleich: max. Abweichung {max_err:.3f} rad, '
            f'max. Bewegung {max_motion:.3f} rad.')
        if not ok:
            # Soft on purpose. Hard-failing here is what put compose into a
            # restart storm in 2026-05: the arm_controller routinely aborts the
            # 3 s quintic on its own goal tolerance before the follower is
            # within 0.30 rad of an extreme leader pose.
            return True, MSG_SYNC_INCOMPLETE
        return True, ''

    def _publish_trajectory(self, start, target):
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        for positions, velocities, accelerations, t in quintic_waypoints(
                start, target):
            point = JointTrajectoryPoint()
            point.positions = list(positions)
            point.velocities = list(velocities)
            point.accelerations = list(accelerations)
            point.time_from_start.sec = int(t)
            point.time_from_start.nanosec = int((t % 1) * 1e9)
            traj.points.append(point)
        self._traj_pub.publish(traj)

    def _await_arrival(self, start, target):
        time.sleep(MOVE_DURATION_S + SETTLE_S)
        deadline = time.monotonic() + VERIFY_WINDOW_S
        result = (False, list(ARM_IDX), 0.0, 0.0)
        while True:
            current = self._follower_pos
            if current is not None:
                result = arrival_decision(start, current, target)
                if result[0]:
                    return result
            if time.monotonic() > deadline:
                return result
            time.sleep(0.1)

    def _start_teleop(self):
        """Spawn the leader's trajectory broadcaster — teleop begins HERE.

        Idempotent: a second activation (the „Grundstellung erneut anfahren"
        path) must re-home without asking the controller_manager to load a
        controller it already has, which the spawner reports as a failure."""
        with self._lock:
            if self._teleop_started:
                return True
        cmd = ['ros2', 'run', 'controller_manager', 'spawner',
               TELEOP_CONTROLLER,
               '-c', LEADER_CONTROLLER_MANAGER,
               '--controller-manager-timeout', '30']
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=SPAWNER_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.get_logger().error(f'spawner konnte nicht starten: {exc}')
            return False
        output = f'{proc.stdout or ""}\n{proc.stderr or ""}'
        if proc.returncode != 0:
            # A controller the manager ALREADY has is a success for us, not a
            # failure. Without this, a spawner that activated the controller
            # and only THEN hit our subprocess timeout would leave
            # _teleop_started False, and every retry would re-home and re-sync
            # the arm before the spawner refused again — motion on each press,
            # forever. Substring match because the spawner has no exit code
            # for it.
            if any(marker in output.lower() for marker in _ALREADY_LOADED):
                self.get_logger().warning(
                    'Der Leader-Broadcaster lief bereits — Teleoperation ist aktiv.')
            else:
                self.get_logger().error(
                    f'spawner rc={proc.returncode}: {output.strip()[:500]}')
                return False
        # Written under the lock that _activate_cb reads it under. One worker
        # runs at a time, so this cannot actually race today - but an
        # asymmetric flag is how that stops being true.
        with self._lock:
            self._teleop_started = True
        self.get_logger().info('Teleoperation aktiv (Leader-Broadcaster läuft).')
        return True


def main():
    rclpy.init()
    node = ActivationAgent()

    # Same shape as edu6_arm_node.main(): docker stop sends SIGTERM, and
    # without this the interpreter dies where it stands — mid-glide, with a
    # worker thread holding state nobody unwinds. Turning it into a
    # KeyboardInterrupt lets the executor drop out of spin() and the finally
    # block below run. Torque-off on teardown is NOT this node's job: the
    # entrypoint's own EXIT trap calls disable_torque().
    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
            rclpy.shutdown()
        except Exception:  # noqa: BLE001 — shutdown is best-effort
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
