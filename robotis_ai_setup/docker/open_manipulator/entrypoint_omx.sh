#!/bin/bash
set -e

# Set up signal handling early — before any background processes are launched
PIDS=""
cleanup() {
    echo "[SHUTDOWN] Stopping all processes..."
    for pid in $PIDS; do
        kill "$pid" 2>/dev/null
    done
    wait
    echo "[SHUTDOWN] Done."
}
trap cleanup SIGTERM SIGINT

source /opt/ros/jazzy/setup.bash
source /root/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}

OFFLINE_MODE=${OFFLINE_MODE:-false}

echo "========================================"
echo "ROBOTIS Open Manipulator - AI Mode"
echo "Follower: ${FOLLOWER_PORT}"
echo "Leader:   ${LEADER_PORT}"
echo "Camera 1: ${CAMERA_DEVICE_1:-<none>} as ${CAMERA_NAME_1:-gripper}"
echo "Camera 2: ${CAMERA_DEVICE_2:-<none>} as ${CAMERA_NAME_2:-scene}"
echo "Offline:  ${OFFLINE_MODE}"
echo "========================================"

# --- Offline test mode: mock publishers instead of real hardware ---
if [ "$OFFLINE_MODE" = "true" ]; then
    echo "[OFFLINE] Running in offline test mode — no hardware required"
    echo "[OFFLINE] Starting mock ROS2 publishers..."

    python3 - << 'MOCK_SCRIPT' &
#!/usr/bin/env python3
"""Mock ROS2 publishers for offline testing.

Publishes fake joint states on /leader/joint_states and /joint_states
so the dashboard and physical_ai_server can operate without real hardware.
"""
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState


JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
HOME_POSITION = [0.0, -1.0, 0.3, 0.7, 0.0, 0.01]


class MockHardware(Node):
    def __init__(self):
        super().__init__('mock_hardware')
        self.leader_pub = self.create_publisher(JointState, '/leader/joint_states', 10)
        self.follower_pub = self.create_publisher(JointState, '/joint_states', 10)
        self.timer = self.create_timer(0.1, self._publish)  # 10 Hz
        self.get_logger().info('Mock hardware publishers started (10 Hz)')

    def _publish(self):
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = list(JOINTS)
        msg.position = list(HOME_POSITION)
        msg.velocity = [0.0] * 6
        msg.effort = [0.0] * 6
        self.leader_pub.publish(msg)
        self.follower_pub.publish(msg)


def main():
    rclpy.init()
    node = MockHardware()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
MOCK_SCRIPT
    PIDS="$!"

    echo "[OFFLINE] Mock publishers running (PID $PIDS)"
    echo "========================================"
    echo "Offline mode active — mock services running"
    echo "========================================"

    wait
    exit 0
fi

# --- Validate hardware (with retry for USB attach timing) ---
wait_for_device() {
    local device=$1 label=$2 max_wait=30 count=0
    while [ ! -e "$device" ] && [ $count -lt $max_wait ]; do
        echo "[INIT] Waiting for $label ($device)... ${count}s"
        sleep 1
        count=$((count + 1))
    done
    if [ ! -e "$device" ]; then
        echo "[ERROR] $label not found: $device"
        exit 1
    fi
    chmod 666 "$device" 2>/dev/null || true
    echo "[INIT] $label found: $device"
}

wait_for_device "$FOLLOWER_PORT" "Follower arm"
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
        self.sub = self.create_subscription(JointState, '/joint_states', self.cb, 10)
        # Publish to the same topic the leader uses — follower's arm_controller
        # is remapped to subscribe here
        qos = QoSProfile(depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self.pub = self.create_publisher(JointTrajectory, '/leader/joint_trajectory', qos)
        self.sent = False

    def cb(self, msg):
        if self.sent:
            return
        if not set(JOINTS).issubset(set(msg.name)):
            return
        self.follower_pos = [msg.position[msg.name.index(j)] for j in JOINTS]
        self.send_sync()

    def send_sync(self):
        self.sent = True
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        N = 50
        for i in range(N):
            t = (i + 1) / N
            s = 10*t**3 - 15*t**4 + 6*t**5  # quintic
            pt = JointTrajectoryPoint()
            pt.positions = [f + (l - f) * s for f, l in zip(self.follower_pos, LEADER_POS)]
            secs = DURATION * t
            pt.time_from_start.sec = int(secs)
            pt.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(pt)
        self.pub.publish(traj)
        self.get_logger().info(f'Published sync trajectory ({N} points, {DURATION}s)')
        # Wait for trajectory to finish then exit
        self.create_timer(DURATION + 1.0, lambda: sys.exit(0))

rclpy.init()
node = SyncNode()
try:
    rclpy.spin(node)
except SystemExit:
    pass
node.destroy_node()
rclpy.shutdown()
" 2>&1 || echo "[WARN] Sync failed — follower may snap on first leader move"
    echo "[LAUNCH] Sync complete."
else
    echo "[WARN] Could not read leader position — skipping sync"
fi

# --- Phase 4: Launch Cameras (up to 2) ---
for i in 1 2; do
    device_var="CAMERA_DEVICE_$i"
    name_var="CAMERA_NAME_$i"
    device="${!device_var}"
    default_names=("gripper" "scene")
    name="${!name_var:-${default_names[$((i-1))]}}"

    if [ -n "$device" ] && [ -e "$device" ]; then
        echo "[LAUNCH] Starting camera $i ($name on $device)..."
        ros2 launch open_manipulator_bringup camera_usb_cam.launch.py \
            name:="$name" \
            video_device:="$device" &
        PIDS="$PIDS $!"
    elif [ -n "$device" ]; then
        echo "[WARN] Camera $i device $device not found, skipping."
    fi
done

echo "========================================"
echo "All services running — teleportation active!"
echo "========================================"

wait
