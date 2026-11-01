#!/usr/bin/env python3
"""OMX camera station end-to-end smoke (plan v6.13 §12.1), inside the SERVER image.

Started by ``docker-publish.yml::smoke-test`` (amd64 leg) with ROS sourced and,
mounted next to this file:

* ``camera_ingest_node.py`` — the checkout's arm-container ingest node (the arm
  image is not pulled; ``image_source_parity.sh`` proves it ships those bytes);
* ``station_fixture.py`` — the synthetic mat render and station YAML
  (``physical_ai_server/test/station_fixture.py``);
* a fixture ``layout_data.py`` (a RELEASED test layout) bind-mounted read-only
  over the installed module — the shipped one keeps ``OMX-STATION-A`` draft.

A fake GUI sender streams 640×480 JPEGs as cam 1 (``scene``) and 1080p renders as
cam 200 into the real ingest node and server; ``/joint_states`` is faked at a
touch dot. Steps: enter → ACTIVE → camera and robot check → verified → resume
with the same client id (camera re-checked, robot_ok kept) → leave → IDLE.
``/scene`` keeps arriving throughout; a recording start is refused while ACTIVE
and accepted after IDLE. Prints one JSON line; exits non-zero on any failure.
"""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CALIB = '/tmp/station-e2e-calib'
CLIENT = 'e2e-' + uuid.uuid4().hex[:8]
RESULT = {'ok': False, 'steps': []}


def step(name, **data):
    RESULT['steps'].append(dict(name=name, **data))
    print(f'[station-e2e] {name} {json.dumps(data, default=str)}', flush=True)


def fail(msg):
    RESULT['error'] = msg
    print(json.dumps(RESULT, default=str), flush=True)
    sys.exit(1)


def prepare():
    import station_fixture as F
    from physical_ai_server.station import layout_data, store
    layout = layout_data.LAYOUTS.get(F.TEST_LAYOUT_ID)
    if layout is None or layout.get('layout_state') != 'released':
        fail('the fixture layout_data.py is not mounted over the installed module')
    frame = F.render(layout, F.camera_pose(), seed=1)
    fields = F.station_fields(layout, F.camera_pose(), frames=[frame])
    store.write_station(fields, CALIB)
    cal, reason = store.load(store.yaml_path(CALIB))
    if cal is None:
        fail(f'fixture station YAML does not load: {reason}')
    q = F.dot_tip_joints((0.100, 0.120))
    scene = F.render(layout, F.camera_pose(), seed=2, ss=1)
    return {
        'station_jpeg': F.encode_jpeg(scene, 90),
        'scene_jpeg': F.encode_jpeg(__import__('cv2').resize(scene, (640, 480)), 85),
        'joints': q,
    }


def sender(cam_id, jpeg, fps, stop):
    header = struct.Struct('>BIQ')
    period = 1.0 / fps
    while not stop.is_set():
        try:
            with socket.create_connection(('127.0.0.1', 5557), timeout=2) as s:
                while not stop.is_set():
                    s.sendall(header.pack(cam_id, len(jpeg), time.time_ns()) + jpeg)
                    time.sleep(period)
        except OSError:
            time.sleep(0.3)


def main():
    env = dict(os.environ, EDUBOTICS_CALIB_DIR=CALIB, EDUBOTICS_STATION_ENABLED='1',
               EDUBOTICS_ROBOT_TYPE='omx_full', EDUBOTICS_CAMERA_NAMES='gripper,scene',
               EDUBOTICS_FORCE_RECALIBRATION='1')
    os.environ.update(env)
    fx = prepare()
    procs = [
        subprocess.Popen([sys.executable, os.path.join(HERE, 'camera_ingest_node.py')], env=env),
        subprocess.Popen(['ros2', 'launch', 'physical_ai_server', 'physical_ai_server.launch.py'],
                         env=env),
    ]
    stop = threading.Event()
    threads = [threading.Thread(target=sender, args=(1, fx['scene_jpeg'], 30.0, stop), daemon=True),
               threading.Thread(target=sender, args=(200, fx['station_jpeg'], 15.0, stop),
                                daemon=True)]
    for t in threads:
        t.start()
    try:
        run(fx)
        RESULT['ok'] = True
    finally:
        stop.set()
        for p in procs:
            p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
    print(json.dumps(RESULT, default=str), flush=True)


def run(fx):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from physical_ai_interfaces.msg import TaskInfo
    from physical_ai_interfaces.srv import SendCommand, StationCheck, StationSession
    from sensor_msgs.msg import CompressedImage, JointState

    rclpy.init()
    node = rclpy.create_node('station_e2e_smoke')
    scene_arrivals = []
    station_seen = []
    node.create_subscription(CompressedImage, '/scene/image_raw/compressed',
                             lambda m: scene_arrivals.append(time.monotonic()),
                             QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT))
    node.create_subscription(CompressedImage, '/station_scene/image_raw/compressed',
                             lambda m: station_seen.append(m.header.frame_id),
                             QoSProfile(depth=2, reliability=ReliabilityPolicy.BEST_EFFORT))
    js_pub = node.create_publisher(JointState, '/joint_states', 10)
    names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']

    def publish_joints():
        msg = JointState()
        msg.header.stamp = node.get_clock().now().to_msg()
        msg.name = names
        msg.position = [float(v) for v in fx['joints']]
        js_pub.publish(msg)
    node.create_timer(0.02, publish_joints)
    session = node.create_client(StationSession, '/station/session')
    check = node.create_client(StationCheck, '/station/check')
    command = node.create_client(SendCommand, '/task/command')

    def call(client, req, timeout=20.0):
        if not client.wait_for_service(timeout_sec=60.0):
            fail(f'service {client.srv_name} not available')
        fut = client.call_async(req)
        end = time.monotonic() + timeout
        while not fut.done() and time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.05)
        if not fut.done():
            fail(f'{client.srv_name} timed out')
        return fut.result()

    def sess(cmd, sid=''):
        r = StationSession.Request()
        r.command, r.client_id, r.session_id = cmd, CLIENT, sid
        return call(session, r)

    def chk(cmd, sid):
        r = StationCheck.Request()
        r.command, r.client_id, r.session_id = cmd, CLIENT, sid
        return call(check, r)

    def spin(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.05)

    def record_start():
        r = SendCommand.Request()
        r.command = SendCommand.Request.START_RECORD
        ti = TaskInfo()
        ti.task_name, ti.task_instruction, ti.fps = 'stationsmoke', ['x'], 30
        ti.warmup_time_s, ti.episode_time_s, ti.reset_time_s, ti.num_episodes = 1, 2, 1, 1
        ti.push_to_hub, ti.user_id, ti.private_mode = False, 'smoke', True
        ti.use_optimized_save_mode = True
        r.task_info = ti
        return call(command, r)

    def record_finish():
        r = SendCommand.Request()
        r.command = SendCommand.Request.FINISH
        call(command, r)

    status = sess('status')
    if status.state != 'IDLE' or not status.station_configured or status.station_protocol != 6:
        fail(f'not a configured station rig: state={status.state} '
             f'configured={status.station_configured} reason={status.message}')
    step('status', state=status.state, digest=status.calibration_digest[:12])
    entered = sess('enter')
    if not entered.accepted:
        fail(f'enter refused: {entered.reason_code} {entered.message}')
    sid = entered.session_id
    for _ in range(80):
        spin(0.25)
        st = sess('keepalive', sid)
        if st.state == 'ACTIVE' and st.source_ok:
            break
    else:
        fail(f'never ACTIVE (state={st.state}, source_ok={st.source_ok})')
    step('active', generation=st.generation)
    if set(station_seen) != {'station_scene'}:
        fail(f'station frames carry frame_id {sorted(set(station_seen))}')
    refused = record_start()
    if refused.success or 'Stationskamera' not in refused.message:
        fail(f'recording start not refused during ACTIVE: {refused.message!r}')
    step('recording_refused', message=refused.message)
    cam = chk('camera', sid)
    if cam.result != 'OK' or not cam.camera_ok:
        fail(f'camera check: {cam.result} {cam.message} rms={cam.rms_mm:.2f}')
    rob = chk('robot', sid)
    if rob.result != 'OK' or not rob.verified:
        fail(f'robot check: {rob.result} {rob.message} dxy={rob.dot_dxy_mm:.2f}')
    step('verified', camera_rms_mm=round(cam.rms_mm, 3), dot=rob.dot_id)
    resumed = sess('enter')
    if resumed.reason_code != 'RESUMED' or resumed.camera_ok or not resumed.robot_ok:
        fail(f'resume: {resumed.reason_code} camera_ok={resumed.camera_ok} '
             f'robot_ok={resumed.robot_ok}')
    cam2 = chk('camera', sid)
    if cam2.result != 'OK' or not cam2.verified:
        fail(f'camera re-check after resume: {cam2.result} {cam2.message}')
    step('resumed', epoch=resumed.camera_check_epoch)
    left = sess('leave', sid)
    if not left.accepted or left.state != 'IDLE':
        fail(f'leave: {left.reason_code} {left.message} state={left.state}')
    step('left')
    n0 = len(scene_arrivals)
    spin(2.0)
    rate = (len(scene_arrivals) - n0) / 2.0
    if rate < 10.0:
        fail(f'/scene stopped arriving ({rate:.1f} fps)')
    accepted = record_start()
    if not accepted.success:
        fail(f'recording start refused after IDLE: {accepted.message!r}')
    step('recording_accepted', scene_fps=round(rate, 1))
    spin(1.0)
    record_finish()
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
