#!/bin/bash
set -e

# Classroom-Jetson short-circuit: when this container runs on the shared
# classroom Jetson, only the follower arm is physically connected (the
# leader stays at the student's desk for recording). Setting
# EDUBOTICS_FOLLOWER_ONLY=1 skips the leader port wait, the leader
# launch, the leader-pose read, and the quintic sync — the Jetson agent
# moves the follower to a safe home pose itself once the container is
# healthy.
FOLLOWER_ONLY="${EDUBOTICS_FOLLOWER_ONLY:-0}"

# Set up signal handling early — before any background processes are launched
PIDS=""
disable_torque() {
    # Best-effort: tell the Dynamixel hardware interface to drop torque so
    # the arm doesn't fall under gravity when our ROS nodes die. Both arms
    # expose set_dxl_torque services; try follower first, then leader.
    # 2s timeout each so we never block shutdown.
    echo "[SHUTDOWN] Disabling servo torque..."
    # Audit H1: log failures explicitly so a maintainer can distinguish
    # "service unreachable / 404" (catastrophic — arm stays torqued, will
    # slump under gravity once power loss removes holding torque) from
    # "torque actually dropped". Bare `|| true` swallowed every failure.
    if ! timeout 2 ros2 service call /dynamixel_hardware_interface/set_dxl_torque \
        std_srvs/srv/SetBool "{data: false}" >/dev/null 2>&1; then
        echo "[WARNUNG] Follower-Torque-Abschaltung fehlgeschlagen — Arm bleibt unter Strom"
    fi
    # Leader namespace pushes `leader/` and the xacro's `set_dxl_torque_srv_name`
    # parameter is `omx_l/set_dxl_torque` — resolved leader path is
    # `/leader/omx_l/set_dxl_torque`. Previously called the follower-style path
    # under `/leader/...`, which silently 404'd and left the leader torqued.
    if [ "$FOLLOWER_ONLY" = "1" ]; then
        echo "[SHUTDOWN] FOLLOWER_ONLY=1 — leader torque-disable skipped (no leader connected)."
    elif ! timeout 2 ros2 service call /leader/omx_l/set_dxl_torque \
        std_srvs/srv/SetBool "{data: false}" >/dev/null 2>&1; then
        echo "[WARNUNG] Leader-Torque-Abschaltung fehlgeschlagen — Arm bleibt unter Strom"
    fi
}
CLEANUP_DONE=0
cleanup() {
    # Idempotent — `trap ... EXIT` plus a SIGTERM both want to run this,
    # but disable_torque calling a ROS service after rclpy has been torn
    # down emits noisy errors. Sentinel guards against the double-run.
    if [ "$CLEANUP_DONE" = "1" ]; then
        return
    fi
    CLEANUP_DONE=1
    echo "[SHUTDOWN] Stopping all processes..."
    disable_torque
    for pid in $PIDS; do
        kill "$pid" 2>/dev/null
    done
    wait
    echo "[SHUTDOWN] Done."
}
# Audit E2: EXIT is mandatory — `set -e` aborts (wait_for_device 60s
# miss, sync-verifier exit 2, any other set-e fallthrough) take the
# script down via `exit`, NOT via a signal, so SIGTERM/SIGINT alone
# would have left both arms torqued while the container teardown
# proceeded. EXIT runs after every shell exit path.
trap cleanup SIGTERM SIGINT EXIT

source /opt/ros/jazzy/setup.bash
source /root/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}

# --- Host USB-bridge detection for camera pixel-format default ---
# The same physical_ai_server / open_manipulator image runs on two
# wildly different USB host stacks:
#
#   1. Windows 11 student PC → bundled WSL2 distro → kernel-mode
#      `vhci_hcd` driver forwards usbipd-attached devices. The bridge
#      cannot sustain a 18.4 MB/s uncompressed YUYV stream from TWO
#      Innomaker U20CAM-720P (640×480×30 each = 36.8 MB/s combined);
#      both cameras crash within ~5 s with `VIDIOC_DQBUF: Select
#      timeout` and the container restarts. Verified empirically
#      2026-05-22 on Sven's classroom rig. The compressed-on-wire
#      `mjpeg2rgb` mode keeps each stream at ~1 MB/s (the camera
#      hardware encodes JPEG before the USB transfer; usb_cam decodes
#      to RGB on the CPU) which the vhci_hcd bridge handles fine. CPU
#      cost is ~30 % per camera at 30 Hz, well within budget on the
#      i5-class student PCs.
#
#   2. Classroom Jetson Orin Nano → NATIVE USB host controller (no
#      WSL bridge). The wire bandwidth is real USB 2.0 isoch, ~25 MB/s
#      per controller. Two yuyv cameras still fit (18.4 MB/s × 2 fits
#      across two controllers; usb_cam does no decode → minimal CPU).
#      mjpeg2rgb would burn ~60 % of one ARM core for no benefit.
#
# We therefore default to `mjpeg2rgb` on WSL2 and `yuyv` on real
# hardware (Jetson, or any non-WSL Linux). The override path is
# unchanged: setting EDUBOTICS_CAMERA_PIXEL_FORMAT in the compose env
# (forwarded through docker-compose.yml::environment) wins.
#
# Detection method: Microsoft's WSL2 kernel reports a build string
# containing `microsoft` (case-insensitive). `uname -r` examples:
#   WSL2:   "5.15.167.4-microsoft-standard-WSL2"
#   Jetson: "5.15.148-tegra"
# We grep case-insensitive to defend against future Microsoft kernel
# string drift (e.g. "Microsoft" capitalised). Falls back to the
# compose default (yuyv) if uname fails for any reason.
detect_host_usb_bridge() {
    if uname -r 2>/dev/null | grep -iq 'microsoft'; then
        echo "vhci_hcd"
    else
        echo "native"
    fi
}
EDUBOTICS_HOST_USB_BRIDGE="${EDUBOTICS_HOST_USB_BRIDGE:-$(detect_host_usb_bridge)}"
export EDUBOTICS_HOST_USB_BRIDGE

# --- Camera source selection: native_bridge vs usb_cam ---
# native_bridge: the cameras are NOT attached to this distro via usbipd.
#   The Windows GUI captures them natively (full 30 fps) and streams JPEG
#   frames over localhost TCP to camera_ingest_node.py, which republishes
#   them as CompressedImage on /<name>/image_raw/compressed. This is the
#   WSL2 student path — the vhci_hcd USB/IP bridge caps in-container UVC
#   capture at ~6-10 Hz per camera (per-device isochronous latency,
#   benchmarked 2026-05-23) and that traffic also jitters the 100 Hz
#   Dynamixel reads. Moving cameras off the bridge fixes both.
# usb_cam: in-container V4L2 capture (Jetson Orin Nano / native Linux —
#   real USB host, no bridge). Preserves the existing behaviour exactly,
#   including the mjpeg2rgb-vs-yuyv split below.
# Default follows the detected host bridge (WSL2 ⇒ native_bridge). An
# explicit EDUBOTICS_CAMERA_SOURCE in the compose env always wins and is
# the one-variable rollback to the old usbipd camera path.
if [ -z "${EDUBOTICS_CAMERA_SOURCE:-}" ]; then
    if [ "$EDUBOTICS_HOST_USB_BRIDGE" = "vhci_hcd" ]; then
        EDUBOTICS_CAMERA_SOURCE="native_bridge"
    else
        EDUBOTICS_CAMERA_SOURCE="usb_cam"
    fi
fi
export EDUBOTICS_CAMERA_SOURCE
echo "[INIT] Camera source: ${EDUBOTICS_CAMERA_SOURCE} (host USB bridge: ${EDUBOTICS_HOST_USB_BRIDGE})"

# The mjpeg2rgb-vs-yuyv pixel-format split only matters for the usb_cam
# path (native_bridge re-encodes JPEG on the Windows host and never touches
# usb_cam). Apply the WSL2 default ONLY if the operator hasn't overridden it
# explicitly. Compose's `EDUBOTICS_CAMERA_PIXEL_FORMAT=${...:-yuyv}` always
# sets the var to something non-empty, so we compare against the bare default
# (`yuyv`) rather than `-z`. An operator .env override wins.
if [ "$EDUBOTICS_CAMERA_SOURCE" = "usb_cam" ] && \
   [ "$EDUBOTICS_HOST_USB_BRIDGE" = "vhci_hcd" ] && \
   [ "${EDUBOTICS_CAMERA_PIXEL_FORMAT:-}" = "yuyv" ]; then
    echo "[INIT] WSL2 detected (uname -r matches 'microsoft') — switching pixel_format default yuyv → mjpeg2rgb."
    echo "[INIT] Rationale: WSL2 vhci_hcd bandwidth cannot sustain 2×YUYV at 640x480x30."
    EDUBOTICS_CAMERA_PIXEL_FORMAT="mjpeg2rgb"
    export EDUBOTICS_CAMERA_PIXEL_FORMAT
fi

echo "========================================"
echo "ROBOTIS Open Manipulator - AI Mode"
echo "Follower: ${FOLLOWER_PORT}"
if [ "$FOLLOWER_ONLY" = "1" ]; then
    echo "Leader:   <skipped — EDUBOTICS_FOLLOWER_ONLY=1>"
else
    echo "Leader:   ${LEADER_PORT}"
fi
echo "Camera 1: ${CAMERA_DEVICE_1:-<none>} as ${CAMERA_NAME_1:-gripper}"
echo "Camera 2: ${CAMERA_DEVICE_2:-<none>} as ${CAMERA_NAME_2:-scene}"
echo "========================================"

# --- Validate hardware (with retry for USB attach timing) ---
# 60s is generous enough for slow USB hubs and in-flight `usbipd attach` from
# the Windows host; below that we were occasionally racing the enumeration.
wait_for_device() {
    local device=$1 label=$2 max_wait=60 count=0
    while [ ! -e "$device" ] && [ $count -lt $max_wait ]; do
        echo "[INIT] Waiting for $label ($device)... ${count}s"
        sleep 1
        count=$((count + 1))
    done
    if [ ! -e "$device" ]; then
        echo "[ERROR] $label not found after ${max_wait}s: $device"
        echo "[ERROR] Check usbipd attach on the Windows host, then restart."
        exit 1
    fi
    chmod 666 "$device" 2>/dev/null || true
    echo "[INIT] $label found: $device"
}

wait_for_device "$FOLLOWER_PORT" "Follower arm"

if [ "$FOLLOWER_ONLY" = "1" ]; then
    echo "[LAUNCH] FOLLOWER_ONLY=1 — skipping leader port wait, leader launch, and quintic sync."
    LEADER_POS=""
else
    wait_for_device "$LEADER_PORT" "Leader arm"

    # --- Phase 1: Launch Leader FIRST ---
    # Leader must start first so we know its position before the follower moves.
    echo "[LAUNCH] Starting leader..."
    ros2 launch open_manipulator_bringup omx_l_leader_ai.launch.py \
        port_name:=${LEADER_PORT} &
    PIDS="$!"

    # Wait for leader joint states
    count=0
    while ! ros2 topic list 2>/dev/null | grep -q "/leader/joint_states" && [ $count -lt 30 ]; do
        sleep 1
        count=$((count + 1))
    done
    sleep 2
    echo "[LAUNCH] Leader ready."

    # Read leader's current position
    LEADER_POS=$(python3 -c "
import rclpy, json
from rclpy.node import Node
from sensor_msgs.msg import JointState

class ReadOnce(Node):
    def __init__(self):
        super().__init__('read_leader')
        self.sub = self.create_subscription(JointState, '/leader/joint_states', self.cb, 10)
        self.joints = ['joint1','joint2','joint3','joint4','joint5','gripper_joint_1']
        self.done = False
    def cb(self, msg):
        if self.done:
            return
        if set(self.joints).issubset(set(msg.name)):
            pos = [msg.position[msg.name.index(j)] for j in self.joints]
            print(json.dumps(pos))
            self.done = True
            raise SystemExit

rclpy.init()
node = ReadOnce()
try:
    rclpy.spin(node)
except SystemExit:
    pass
node.destroy_node()
rclpy.shutdown()
" 2>/dev/null)

    echo "[LAUNCH] Leader position: ${LEADER_POS}"

    # NOTE: Roboter Studio runs FOLLOWER-ONLY (the GUI starts it with
    # EDUBOTICS_FOLLOWER_ONLY=1), so this leader-present path is the
    # recording/teleop session only. The 2026-06-17 teleop-suspend bridge that
    # used to launch here was removed — there is no Roboter Studio motion to
    # arbitrate against the leader broadcaster when the leader is present.
fi

# --- Phase 2: Launch Follower ---
echo "[LAUNCH] Starting follower..."
ros2 launch open_manipulator_bringup omx_f_follower_ai.launch.py \
    port_name:=${FOLLOWER_PORT} &
PIDS="$PIDS $!"

# Wait for follower to be ready
count=0
while ! ros2 topic list 2>/dev/null | grep -q "/joint_states" && [ $count -lt 60 ]; do
    sleep 1
    count=$((count + 1))
done
echo "[LAUNCH] Follower ready (/joint_states detected)."
# Wait for arm_controller to be fully active
sleep 3

# --- Phase 3: Move follower to leader position smoothly ---
# Publish trajectory directly to /leader/joint_trajectory (the topic the
# follower's arm_controller subscribes to via remapping).
# Uses quintic smoothing over 3s so the follower glides to the leader position.
if [ -n "$LEADER_POS" ] && [ "$LEADER_POS" != "null" ]; then
    echo "[LAUNCH] Moving follower to match leader (3s smooth trajectory)..."
    python3 -c "
import rclpy, sys, json, time
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from sensor_msgs.msg import JointState

LEADER_POS = json.loads('${LEADER_POS}')
JOINTS = ['joint1','joint2','joint3','joint4','joint5','gripper_joint_1']
DURATION = 3.0

class SyncNode(Node):
    def __init__(self):
        super().__init__('sync_follower')
        self.follower_pos = None
        # Subscription stays live throughout — verify step reads the latest
        # follower pose from here, not a stale snapshot.
        self.sub = self.create_subscription(JointState, '/joint_states', self.cb, 10)
        # Publish to the same topic the leader uses — follower's arm_controller
        # is remapped to subscribe here
        qos = QoSProfile(depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self.pub = self.create_publisher(JointTrajectory, '/leader/joint_trajectory', qos)
        self.sent = False

    def cb(self, msg):
        if not set(JOINTS).issubset(set(msg.name)):
            return
        self.follower_pos = [msg.position[msg.name.index(j)] for j in JOINTS]
        if not self.sent:
            self.send_sync()

    def send_sync(self):
        self.sent = True
        # Audit E3: capture the pose at sync-publish time so the verifier
        # can prove the arm actually moved. Before this snapshot, a stale
        # follower_pos (callback stopped publishing mid-sync) could match
        # LEADER_POS vacuously and the 0.08 rad tolerance would pass even
        # though the arm never moved at all.
        self._sync_start_pos = list(self.follower_pos)
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        N = 50
        # Quintic smoothing with explicit velocities + accelerations. Zero
        # at both endpoints, no snap. Without these the controller has to
        # numerically interpolate and can overshoot.
        deltas = [l - f for f, l in zip(self.follower_pos, LEADER_POS)]
        self._sync_initial_deltas = list(deltas)
        for i in range(N):
            t = (i + 1) / N
            s = 10*t**3 - 15*t**4 + 6*t**5
            s_dot = (30*t**2 - 60*t**3 + 30*t**4) / DURATION
            s_ddot = (60*t - 180*t**2 + 120*t**3) / (DURATION * DURATION)
            pt = JointTrajectoryPoint()
            pt.positions = [f + d * s for f, d in zip(self.follower_pos, deltas)]
            pt.velocities = [d * s_dot for d in deltas]
            pt.accelerations = [d * s_ddot for d in deltas]
            secs = DURATION * t
            pt.time_from_start.sec = int(secs)
            pt.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(pt)
        self.pub.publish(traj)
        self.get_logger().info(f'Published sync trajectory ({N} points, {DURATION}s)')
        # After the motion should be done, verify the follower actually
        # reached the target. If it didn't, that signals a servo dropout or
        # a blocked arm — fail loud so the first real inference command
        # doesn't come in on top of a mispositioned robot.
        self._verify_t = None
        self.create_timer(
            DURATION + 0.5, lambda: self._start_verify())

    def _start_verify(self):
        self._verify_deadline = time.monotonic() + 2.0
        self._verify_timer = self.create_timer(0.1, self._verify_tick)

    def _verify_tick(self):
        if self.follower_pos is None:
            return
        err = [abs(a - b) for a, b in zip(self.follower_pos, LEADER_POS)]
        # 2026-05-18: bumped 0.08 → 0.30 rad. The pre-bump value was a tight
        # post-sync correctness check; in practice the follower's
        # arm_controller aborts the 3s quintic sync mid-flight (via
        # JointTrajectoryController state_tolerance) before the follower
        # gets close enough to the leader, so this verify always failed →
        # exit 2 → docker compose restart-loop. 0.30 still catches a real
        # servo dropout (the arm not moving at all on a joint with a 0.7
        # rad commanded delta still trips the >=50% motion check below),
        # but accepts the routine ~10-20° finishing lag from rest pose.
        tol = 0.30  # rad — was 0.08; see commit log for rationale
        # 2026-05-24: the gripper (gripper_joint_1, index 5) is EXEMPT from
        # verification. The leader gripper sits at its held trigger position
        # (e.g. ~-0.70 rad) while the follower gripper starts open (~0.0), so
        # the follower legitimately does NOT track the leader gripper at boot
        # — a ~1.4 rad err + 0.0 motion that is benign and expected, not a
        # dropout. Including it made the verifier soft-fail on EVERY boot,
        # masking its real job (catching an actual arm-joint dropout). We
        # verify the 5 arm joints only — err AND motion — so a truly stuck
        # joint1..joint5 still hard-detects.
        ARM_IDX = [0, 1, 2, 3, 4]  # joint1..joint5; exclude gripper_joint_1 (5)
        # Audit E3: also require the arm to have actually moved for any
        # joint whose initial delta was meaningful. The pre-E3 check passed
        # vacuously when /joint_states stopped publishing mid-sync: a stale
        # follower_pos snapshot can match LEADER_POS without the arm ever
        # leaving its start pose. We require >=50% of the commanded delta to
        # have been traversed on every joint that had a meaningful initial
        # offset (|delta| > tol).
        motion = [abs(a - b) for a, b in zip(self.follower_pos, self._sync_start_pos)]
        motion_ok = True
        for i in ARM_IDX:
            d = self._sync_initial_deltas[i]
            if abs(d) > tol and motion[i] < 0.5 * abs(d):
                motion_ok = False
                break
        if all(err[i] < tol for i in ARM_IDX) and motion_ok:
            self.get_logger().info(
                f'Sync verified (arm max err {max(err[i] for i in ARM_IDX):.3f} rad, '
                f'arm max motion {max(motion[i] for i in ARM_IDX):.3f} rad; '
                f'gripper exempt).'
            )
            sys.exit(0)
        if time.monotonic() > self._verify_deadline:
            stale_joints = [
                JOINTS[i] for i in ARM_IDX
                if abs(self._sync_initial_deltas[i]) > tol
                and motion[i] < 0.5 * abs(self._sync_initial_deltas[i])
            ]
            reason = (
                f'follower stale (no motion on: {stale_joints})'
                if stale_joints
                else 'follower not at leader'
            )
            self.get_logger().error(
                f'Sync verification FAILED: {reason}. '
                f'Per-joint err (rad): {[round(e, 3) for e in err]}. '
                f'Per-joint motion (rad): {[round(m, 3) for m in motion]}. '
                f'Refusing to proceed — check for mechanical block or servo dropout.'
            )
            sys.exit(2)

rclpy.init()
node = SyncNode()
_exit_code = 0
try:
    rclpy.spin(node)
except SystemExit as _se:
    # Capture the code so we can re-raise AFTER clean shutdown. A bare
    # 'pass' here silently ate sys.exit(2) and the shell saw rc=0, making
    # the whole verification-hard-exit path dead code.
    _exit_code = _se.code if isinstance(_se.code, int) else 0
node.destroy_node()
rclpy.shutdown()
sys.exit(_exit_code)
" || sync_rc=$?
    sync_rc=${sync_rc:-0}
    if [ $sync_rc -eq 2 ]; then
        # 2026-05-18: soft-fail instead of `exit 2`. Hard-failing here put
        # docker compose's restart-policy into a tight crash-loop whenever
        # the follower couldn't reach the leader pose within 0.30 rad in
        # 3+2 seconds — a common scenario in classroom setups where the
        # human positions the leader arm at an extreme pose before clicking
        # Start, or where servos are torque-disabled from a prior shutdown.
        # The trade-off: the first leader move can now drive the follower
        # through a larger initial delta. The arm_controller's own state
        # tolerance still catches a runaway trajectory.
        echo "[WARN] Sync verification did not pass — continuing anyway."
        echo "[WARN] First leader move may produce a larger-than-usual follower"
        echo "[WARN] motion as the controller catches up to the leader pose."
    elif [ $sync_rc -ne 0 ]; then
        echo "[WARN] Sync script exited with status $sync_rc — follower may snap on first leader move"
    else
        echo "[LAUNCH] Sync complete."
    fi
elif [ "$FOLLOWER_ONLY" = "1" ]; then
    # FOLLOWER_ONLY (Roboter Studio on the student PC, and the Jetson): there is
    # no leader to sync to, so move the follower to a DETERMINISTIC safe HOME.
    # On the student PC nothing else homes the follower, so without this it
    # would sit at its power-up pose and the first Roboter Studio command would
    # jump from there. HOME must match workflow/handlers/motion.py
    # HOME_JOINTS_RAD + gripper open. Soft: a failed home never kills boot.
    # (On the Jetson the agent also moves the follower home afterwards — a
    # harmless idempotent confirm.)
    echo "[LAUNCH] FOLLOWER_ONLY=1 — moving follower to safe HOME (3s smooth trajectory)..."
    python3 -c "
import rclpy, sys, math
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from sensor_msgs.msg import JointState

JOINTS = ['joint1','joint2','joint3','joint4','joint5','gripper_joint_1']
HOME = [0.0, -math.pi/2, math.pi/2, 0.0, 0.0, 0.8]
DURATION = 3.0

class HomeNode(Node):
    def __init__(self):
        super().__init__('home_follower')
        self.follower_pos = None
        self.sent = False
        self.sub = self.create_subscription(JointState, '/joint_states', self.cb, 10)
        qos = QoSProfile(depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self.pub = self.create_publisher(JointTrajectory, '/leader/joint_trajectory', qos)
        # Bail if /joint_states never delivers all joints within ~10s.
        self.create_timer(10.0, self._bail)

    def cb(self, msg):
        if self.sent or not set(JOINTS).issubset(set(msg.name)):
            return
        self.follower_pos = [msg.position[msg.name.index(j)] for j in JOINTS]
        self._send_home()

    def _send_home(self):
        self.sent = True
        deltas = [h - f for f, h in zip(self.follower_pos, HOME)]
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        N = 50
        for i in range(N):
            t = (i + 1) / N
            s = 10*t**3 - 15*t**4 + 6*t**5
            s_dot = (30*t**2 - 60*t**3 + 30*t**4) / DURATION
            s_ddot = (60*t - 180*t**2 + 120*t**3) / (DURATION * DURATION)
            pt = JointTrajectoryPoint()
            pt.positions = [f + d * s for f, d in zip(self.follower_pos, deltas)]
            pt.velocities = [d * s_dot for d in deltas]
            pt.accelerations = [d * s_ddot for d in deltas]
            secs = DURATION * t
            pt.time_from_start.sec = int(secs)
            pt.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(pt)
        self.pub.publish(traj)
        self.get_logger().info(f'Published HOME trajectory ({N} points, {DURATION}s)')
        # Let the trajectory finish (publisher must stay alive so the
        # TRANSIENT_LOCAL message is delivered to the controller) then exit.
        self.create_timer(DURATION + 0.5, lambda: sys.exit(0))

    def _bail(self):
        if not self.sent:
            self.get_logger().warn('No /joint_states for follower — skipping startup home.')
            sys.exit(0)

rclpy.init()
node = HomeNode()
try:
    rclpy.spin(node)
except SystemExit:
    pass
node.destroy_node()
rclpy.shutdown()
" || echo '[WARN] Startup home move failed (non-fatal) — follower stays at its power-up pose.'
    echo '[LAUNCH] Follower startup home complete.'
else
    echo "[WARN] Could not read leader position — skipping sync"
fi

# --- Phase 4: Launch Cameras ---
if [ "$EDUBOTICS_CAMERA_SOURCE" = "native_bridge" ]; then
    # Native-bridge path (WSL2 student PC): the cameras are NOT attached to
    # this distro. The Windows GUI captures them natively at 30 fps and
    # streams JPEG frames over localhost TCP to camera_ingest_node.py, which
    # republishes them as CompressedImage on /<name>/image_raw/compressed.
    # No usbipd cameras, no usb_cam, no /dev/video* here.
    echo "[LAUNCH] Native camera bridge mode — starting camera_ingest_node.py (TCP :${EDUBOTICS_CAMERA_INGEST_PORT:-5557})..."
    python3 /usr/local/bin/camera_ingest_node.py &
    PIDS="$PIDS $!"
    echo "[LAUNCH] Camera ingest running — awaiting JPEG frames from the Windows GUI."
else
# --- usb_cam path (Jetson Orin Nano / native Linux): in-container V4L2 ---
#
# Audit F21: a single `[ -e $device ]` check at the top would race
# usbipd's WSL forwarding on cold boot — the test fails, [WARN] is
# logged, and the container proceeds WITHOUT cameras. Mirror the
# arm-side wait_for_device by polling for the camera node briefly
# before giving up. 30 s matches the existing arm waits.
wait_for_camera() {
    local dev="$1" name="$2" timeout="${3:-30}" t=0
    while [ ! -e "$dev" ] && [ "$t" -lt "$timeout" ]; do
        sleep 1
        t=$((t + 1))
    done
    if [ -e "$dev" ]; then
        return 0
    fi
    echo "[WARN] Camera $name ($dev) not present after ${timeout}s"
    return 1
}

# Audit Gap-D 2026-05-23: refuse to launch when two configured cameras
# resolve to the same underlying /dev/videoN. The Innomaker U20CAM-720P
# pair both report USB serial "SN0001" and udev creates one shared
# `/dev/v4l/by-id/usb-Innomaker_..._SN0001-...` symlink — pointing at
# whichever device enumerated last. The GUI's wsl_bridge.list_video_devices
# already de-dups by Bus info to avoid handing the same by-id path to both
# camera slots, but a stale .env from a prior install (or a manual edit)
# can still encode the colliding path. Worst-case: the student records
# 100 episodes with the gripper feed labelled as "scene" and the trained
# policy then drives the wrong camera at inference time → silent corpus
# rot.
#
# Detection happens AFTER readlink resolution (below) so we compare the
# real `/dev/videoN`, not the symlink names. If a collision is detected
# we hard-exit with a German [STOPP] message — failing loud matches Rule
# §3 ("overlays must fail loudly on missing target") and is consistent
# with the post-sync verification hard-exit at the top of this script.
declare -A SEEN_REAL_DEV   # bash 4 associative array, real_dev → "i:name"
for i in 1 2; do
    device_var="CAMERA_DEVICE_$i"
    name_var="CAMERA_NAME_$i"
    device="${!device_var}"
    default_names=("gripper" "scene")
    name="${!name_var:-${default_names[$((i-1))]}}"

    if [ -z "$device" ]; then
        continue
    fi
    if ! wait_for_camera "$device" "$name" 30; then
        # Camera path never appeared (usbipd not forwarding, driver
        # crash, replug mid-boot). Skip — the new compose healthcheck
        # (audit F7) will report the container unhealthy if a
        # configured camera is missing.
        continue
    fi
    # Resolve /dev/v4l/by-id/* symlinks to the underlying /dev/videoN.
    # usb_cam's V4L2 wrapper strips the path components naively and
    # ends up trying to open `/dev/../../video2`, which fails with
    # "Device specified is not available or is not a valid V4L2 device".
    # The GUI writes by-id paths into .env when a camera exposes a USB
    # serial (stable across replug); the resolution stays per-launch.
    real_dev="$device"
    if [ -L "$device" ]; then
        resolved="$(readlink -f "$device" 2>/dev/null || true)"
        if [ -e "$resolved" ]; then
            echo "[LAUNCH] Camera $i: resolved $device → $resolved"
            device="$resolved"
            real_dev="$resolved"
        fi
    fi
    # Audit Gap-D collision check — see the block above this loop.
    if [ -n "${SEEN_REAL_DEV[$real_dev]:-}" ]; then
        prev="${SEEN_REAL_DEV[$real_dev]}"
        echo "[STOPP] Beide konfigurierte Kameras zeigen auf dasselbe Gerät: $real_dev"
        echo "[STOPP]   Slot $prev"
        echo "[STOPP]   Slot $i:$name"
        echo "[STOPP] Ursache: Zwei UVC-Kameras mit identischer USB-Seriennummer (häufig bei Innomaker U20CAM-720P)"
        echo "[STOPP] teilen sich denselben /dev/v4l/by-id/-Symlink. Die GUI sollte beim nächsten"
        echo "[STOPP] 'Geräte aktualisieren' automatisch by-path verwenden. Bis dahin:"
        echo "[STOPP]   1. EduBotics neu starten (GUI → 'Hardware neu erkennen')"
        echo "[STOPP]   2. Oder CAMERA_DEVICE_2 in .env manuell auf /dev/v4l/by-path/... setzen."
        exit 3
    fi
    SEEN_REAL_DEV[$real_dev]="$i:$name"
    echo "[LAUNCH] Starting camera $i ($name on $device)..."
    # Audit F22: declare an explicit resolution + format here instead
    # of relying on whatever upstream `params_1.yaml` defaults to. Two
    # webcams with different native modes used to share params_1.yaml,
    # producing `VIDIOC_S_FMT: Invalid argument` on the second camera
    # (silenced into stderr, healthcheck used to miss it).
    #
    # pixel_format default is split by host USB bridge — see the
    # detect_host_usb_bridge() block near the top of this script.
    #   - WSL2 student PC (vhci_hcd): default flips to `mjpeg2rgb`. The
    #     bridge cannot sustain 2×YUYV at 640×480×30 (~37 MB/s combined)
    #     and both cameras crash with `Select timeout` within ~5 s.
    #     mjpeg2rgb keeps each stream at ~1 MB/s on the wire (camera
    #     hardware does JPEG encode); usb_cam decodes to RGB on the CPU
    #     at roughly 30 %/cam, which the i5-class student PCs handle.
    #     Verified 2026-05-22 on Sven's classroom rig.
    #   - Native USB host (Jetson Orin Nano, real desktop Linux): default
    #     stays `yuyv` (compose default). Real USB 2.0 isoch fits two
    #     YUYV streams; usb_cam does no decode → minimal CPU. mjpeg2rgb
    #     would saturate one ARM core on the Jetson for no benefit.
    #
    # `raw_mjpeg` is intentionally NOT a supported value: usb_cam 0.8.1
    # tags the message `encoding="yuv422"` while passing MJPG bytes
    # straight through, and every downstream consumer (recording,
    # browser preview, perception, training) gets either green tiles
    # or RNG noise (ros-drivers/usb_cam#346). Confirmed by saving a
    # snapshot JPEG via web_video_server — the file was 30 KB of
    # garbage. The previous documentation here claimed mjpeg2rgb caused
    # "94 % CPU saturation"; that was measured against the older
    # upstream usb_cam that lacked the libjpeg-turbo fast path. Empirical
    # 2026-05-23 measurement on our pinned 0.8.1: ~30 %/cam at 30 Hz.
    # Override via EDUBOTICS_CAMERA_PIXEL_FORMAT — keep `raw_mjpeg` out
    # of the supported set.
    ros2 launch open_manipulator_bringup camera_usb_cam.launch.py \
        name:="$name" \
        video_device:="$device" \
        image_width:="${EDUBOTICS_CAMERA_WIDTH:-640}" \
        image_height:="${EDUBOTICS_CAMERA_HEIGHT:-480}" \
        framerate:="${EDUBOTICS_CAMERA_FRAMERATE:-30.0}" \
        pixel_format:="${EDUBOTICS_CAMERA_PIXEL_FORMAT:-yuyv}" &
    PIDS="$PIDS $!"
done
fi

echo "========================================"
echo "All services running — ready for teleoperation and inference."
echo "========================================"

wait
