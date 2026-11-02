"""The station bench toolkit (``tools/station_bench/``, dev only): its pure parts
and the fence that keeps it out of every shipped artifact.

No hardware, no ROS, no OpenCV: the tools import ``rclpy`` / ``cv2`` only inside
``main()``, so every module here loads with the standard library. The mirrored
constants (check thresholds, the lease, the topics, the hat and yaw cadence) are
compared with the shipped sources as TEXT / AST, so a change there flags the
bench tools too.
"""

import ast
import contextlib
import csv
import io
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
BENCH = REPO / 'tools' / 'station_bench'
PAS = REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'


def _load(rel, name):
    path = BENCH / rel
    if not path.is_file():
        raise AssertionError(f'required file missing: {path.relative_to(REPO)}')
    for p in (str(BENCH), str(path.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load('bench_common.py', 'bench_common')


def _assign(path, name):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name) and t.id == name
                        and isinstance(node.value, ast.Constant)):
                    return node.value.value
    raise AssertionError(f'{name} not found in {path.relative_to(REPO)}')


class Statistics(unittest.TestCase):
    def test_percentile_is_numpys_linear(self):
        self.assertIsNone(B.percentile([], 50))
        self.assertEqual(B.percentile([3.0], 95), 3.0)
        xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertAlmostEqual(B.percentile(xs, 50), 5.5)
        self.assertAlmostEqual(B.percentile(xs, 95), 9.55)
        self.assertAlmostEqual(B.percentile([10, 0, float('nan'), 5], 50), 5.0)

    def test_summarize_rate_gaps_and_verdict(self):
        s = B.summarize([1.0, 2.0, None, 3.0])
        self.assertEqual((s['n'], s['min'], s['max'], s['mean']), (3, 1.0, 3.0, 2.0))
        self.assertEqual(B.summarize([])['n'], 0)
        self.assertAlmostEqual(B.rate_hz([0.0, 0.5, 1.0]), 2.0)
        self.assertIsNone(B.rate_hz([1.0]))
        self.assertEqual(B.gaps([0.0, 0.25, 1.0]), [0.25, 0.75])
        self.assertEqual(B.verdict(30.0, 29.0, at_least=True), 'PASS')
        self.assertEqual(B.verdict(0.31, 0.3, at_least=False), 'FAIL')
        self.assertEqual(B.verdict(None, 0.3, at_least=False), 'UNKNOWN')


class OutputFiles(unittest.TestCase):
    def test_out_path_names_and_creates_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            p = B.out_path(os.path.join(d, 'neu'), 'run logger', '20261102-101010', '.csv')
            self.assertEqual(os.path.basename(p), 'run_logger_20261102-101010.csv')
            self.assertTrue(os.path.isdir(os.path.join(d, 'neu')))
        with self.assertRaises(ValueError):
            B.out_path('', 'x', 's', '.csv')
        self.assertRegex(B.stamp(), r'^\d{8}-\d{6}$')

    def test_csv_log_writes_a_header_and_flushes_every_row(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'x.csv')
            log = B.CsvLog(path, ['a', 'b', 'c'])
            log.write({'a': 1.23456789, 'b': None, 'c': [1, 2.5], 'ignored': 9})
            log.write({'a': float('nan'), 'b': 'Größe', 'c': ''})
            with open(path, encoding='utf-8') as fh:          # readable before close
                rows = list(csv.DictReader(fh))
            log.close()
        self.assertEqual(rows[0], {'a': '1.23457', 'b': '', 'c': '1 2.5'})
        self.assertEqual(rows[1], {'a': '', 'b': 'Größe', 'c': ''})

    def test_write_json_is_whole_and_keeps_umlauts(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 's.json')
            B.write_json(p, {'msg': 'Prüfung', 'v': 1.5})
            self.assertEqual(json.loads(pathlib.Path(p).read_text(encoding='utf-8')),
                             {'msg': 'Prüfung', 'v': 1.5})
            self.assertFalse(os.path.exists(p + '.tmp'))


class ClockOffset(unittest.TestCase):
    def test_the_fastest_round_trip_bounds_the_true_offset(self):
        true = 5_000_000_000            # the container is 5 s ahead of Windows
        samples = []
        for i, (up, down) in enumerate([(4e6, 1e6), (2e5, 3e5), (9e6, 9e6), (5e5, 1e5)]):
            t_send = 1_000_000_000_000 + i * 50_000_000
            remote = t_send + int(up) + true
            t_recv = t_send + int(up) + int(down)
            samples.append((t_send, remote, t_recv))
        est = B.estimate_clock_offset(samples)
        self.assertEqual(est['rtt_min_ns'], 500_000)
        self.assertLessEqual(abs(est['offset_ns'] - true), est['uncertainty_ns'])
        self.assertEqual(est['n'], 4)
        with self.assertRaises(ValueError):
            B.estimate_clock_offset([])

    def test_corrected_age_and_the_offset_json(self):
        # stamp on Windows at 100 ms; received 250 ms later on a clock 5 s ahead
        self.assertEqual(B.corrected_age_ns(5_350_000_000, 100_000_000, 5_000_000_000),
                         250_000_000)
        text = json.dumps({'estimate': {'offset_ns': -12, 'uncertainty_ns': 3400}})
        self.assertEqual(B.parse_offset_json(text), (-12, 3400))


class SignalStatus(unittest.TestCase):
    def test_parses_the_servers_own_payload(self):
        path = PAS / 'signal_status.py'
        spec = importlib.util.spec_from_file_location('station_bench_signal_status', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        payload = mod.build_payload(
            seq=7, uptime_s=3.0, recording=False, free_bytes=10 ** 10,
            sources=[{'kind': 'camera', 'name': 'gripper', 'topic': '/gripper/image_raw',
                      'hz': 24.8, 'age_s': 0.04},
                     {'kind': 'camera', 'name': 'scene', 'topic': '/scene/image_raw',
                      'hz': None, 'age_s': None},
                     {'kind': 'joints', 'name': 'follower', 'topic': '/joint_states',
                      'hz': 100.0, 'age_s': 0.01}],
            ingest={'alive': True, 'age_s': 0.1})
        d = B.parse_signal_status(mod.encode_payload(payload))
        self.assertEqual(d['seq'], 7)
        self.assertEqual(d['cameras']['gripper'], {'hz': 24.8, 'age_s': 0.04,
                                                   'topic': '/gripper/image_raw'})
        self.assertIsNone(d['cameras']['scene']['hz'])
        self.assertEqual(d['follower']['hz'], 100.0)
        self.assertEqual(B.SIGNAL_TOPIC, mod.TOPIC)
        with self.assertRaises(ValueError):
            B.parse_signal_status('[1, 2]')


class Ros2CliText(unittest.TestCase):
    def test_reads_a_field_from_a_response_repr(self):
        text = ("requester: making request: ...\n\nresponse:\n"
                "physical_ai_interfaces.srv.StationSession_Response(end_reason='', "
                "camera_check_epoch=3, accepted=True, state='ACTIVE', reason_code='', "
                "message='', session_id='', generation=4)\n")
        self.assertEqual(B.parse_ros2_response_field(text, 'state'), 'ACTIVE')
        self.assertEqual(B.parse_ros2_response_field(text, 'generation'), '4')
        self.assertEqual(B.parse_ros2_response_field(text, 'reason_code'), '')
        self.assertIsNone(B.parse_ros2_response_field(text, 'verified'))


class ToggleGuard(unittest.TestCase):
    def test_only_with_no_session_and_the_stream_off(self):
        ok_bridge = {'enabled': True, 'stream': 'off'}
        self.assertEqual(B.toggle_guard('IDLE', ok_bridge, 0.5), (True, ''))
        for state in ('ACTIVE', 'ENTERING', 'OFF'):
            self.assertFalse(B.toggle_guard(state, ok_bridge, 0.5)[0], state)
        self.assertFalse(B.toggle_guard(None, ok_bridge, 0.5)[0])         # unknown refuses
        self.assertFalse(B.toggle_guard('IDLE', ok_bridge, None)[0])      # no age refuses
        self.assertFalse(B.toggle_guard('IDLE', ok_bridge, 30.0)[0])      # stale refuses
        self.assertFalse(B.toggle_guard('IDLE', None, 0.5)[0])
        self.assertFalse(B.toggle_guard('IDLE', {'enabled': False, 'stream': 'off'}, 0.5)[0])
        self.assertFalse(B.toggle_guard('IDLE', {'enabled': True, 'stream': 'on'}, 0.5)[0])

    def test_the_on_phase_stays_inside_the_lease(self):
        self.assertTrue(B.toggle_timing_ok(2.0, 2.0)[0])
        self.assertFalse(B.toggle_timing_ok(9.5, 2.0)[0])
        self.assertFalse(B.toggle_timing_ok(2.0, 0.0)[0])
        self.assertLess(B.TOGGLE_ON_MAX_S, B.STATION_LEASE_S)

    def test_the_reopen_hint_is_the_bridges_own_sentence(self):
        src = (REPO / 'robotis_ai_setup' / 'gui' / 'app' / 'camera_bridge.py').read_text(
            encoding='utf-8')
        joined = re.sub(r'"\s*\n\s*f?"', '', src)
        self.assertIn('Stationskamera liefert keine Bilder — wird neu geöffnet', joined)
        self.assertTrue(B.reopen_suspected(
            'Stationskamera liefert keine Bilder — wird neu geöffnet (Index 1).'))
        self.assertFalse(B.reopen_suspected(''))


class Checks(unittest.TestCase):
    CAM_OK = {'result': 'OK', 'generation': 3, 'rms_mm': 1.2, 'max_mm': 2.9,
              'centroid_mm': 1.0, 'rotation_deg': 0.2, 'dot_id': 0, 'dot_dxy_mm': 0.0,
              'dot_dz_mm': 0.0, 'ref_ids': [100, 101, 102, 103],
              'ref_residual_mm': [1.0, 1.1, 1.2, 1.3]}

    def test_kind_and_half_threshold(self):
        self.assertEqual(B.check_kind(self.CAM_OK), 'camera')
        self.assertTrue(B.half_threshold_ok('camera', self.CAM_OK))
        loose = dict(self.CAM_OK, max_mm=3.5)
        self.assertFalse(B.half_threshold_ok('camera', loose))
        robot = {'result': 'OK', 'dot_id': 2, 'dot_dxy_mm': 1.9, 'dot_dz_mm': 1.6}
        self.assertEqual(B.check_kind(robot), 'robot')
        self.assertFalse(B.half_threshold_ok('robot', robot))
        self.assertTrue(B.half_threshold_ok('robot', dict(robot, dot_dz_mm=1.5)))
        self.assertEqual(B.check_kind({'result': 'ROBOT_MOVED', 'dot_id': 1}), 'robot')
        self.assertEqual(B.check_kind({'result': 'MOVED', 'ref_ids': []}), 'camera')
        self.assertEqual(B.check_kind({'result': ''}), '')
        self.assertIsNone(B.half_threshold_ok('camera', dict(self.CAM_OK, result='MOVED')))

    def test_tally_and_s1_verdict(self):
        t = B.CheckTally()
        for _ in range(49):
            t.add('camera', self.CAM_OK)
        self.assertEqual(t.summary()['camera']['s1_static_verdict'], 'PENDING')
        t.add('camera', self.CAM_OK)
        self.assertEqual(t.summary()['camera']['s1_static_verdict'], 'PASS')
        t.add('camera', dict(self.CAM_OK, result='MOVED'))
        self.assertEqual(t.summary()['camera']['s1_static_verdict'], 'FAIL')
        self.assertIn('camera: 51 checks', t.line())

    def test_s1_robot_verdict_is_all_ok_and_the_half_count_only_a_report(self):
        # Spec §12.3: „30 static robot checks, all OK"; the half-threshold rule
        # is the camera's. A robot OK at dxy 3.0 mm (OK <= 4.0, half 2.0) passes
        # S1 and is merely not counted at half the thresholds.
        t = B.CheckTally()
        robot_ok = {'result': 'OK', 'dot_id': 1, 'dot_dxy_mm': 3.0, 'dot_dz_mm': 2.5}
        for _ in range(B.S1_ROBOT_CHECKS):
            t.add('robot', robot_ok)
        s = t.summary()['robot']
        self.assertEqual(s['s1_static_verdict'], 'PASS')
        self.assertEqual(s['ok_at_half_thresholds'], 0)
        self.assertIn('not a gate', s['ok_at_half_thresholds_note'])
        t.add('robot', dict(robot_ok, result='ROBOT_MOVED'))
        self.assertEqual(t.summary()['robot']['s1_static_verdict'], 'FAIL')
        # The camera keeps both halves of its criterion.
        c = B.CheckTally()
        for _ in range(B.S1_CAMERA_CHECKS):
            c.add('camera', dict(self.CAM_OK, max_mm=3.5))       # OK, not at half
        self.assertEqual(c.summary()['camera']['s1_static_verdict'], 'FAIL')

    def test_the_camera_half_thresholds_are_pinned(self):
        self.assertEqual((B.CAMERA_OK_RMS_MM / 2, B.CAMERA_OK_MAX_MM / 2,
                          B.CAMERA_OK_CENTROID_MM / 2, B.CAMERA_OK_ROTATION_DEG / 2),
                         (1.5, 3.0, 1.5, 0.25))
        at = dict(self.CAM_OK, rms_mm=1.5, max_mm=3.0, centroid_mm=1.5, rotation_deg=0.25)
        self.assertTrue(B.half_threshold_ok('camera', at))
        for key, over in (('rms_mm', 1.5001), ('max_mm', 3.0001), ('centroid_mm', 1.5001),
                          ('rotation_deg', 0.2501)):
            self.assertFalse(B.half_threshold_ok('camera', dict(at, **{key: over})), key)

    def test_a_re_solve_result_is_its_own_kind(self):
        # station/service.py::_finish_work_locked stores the re-solve's own
        # camera_check dict as OK / RESOLVE_REFUSED; camera_check adds no ref_ids
        # (only _camera_check does) and no dot_id.
        resolve_ok = {'result': 'OK', 'generation': 3, 'rms_mm': 0.8, 'max_mm': 1.4,
                      'centroid_mm': 0.5, 'rotation_deg': 0.1, 'dot_id': 0,
                      'dot_dxy_mm': 0.0, 'dot_dz_mm': 0.0, 'ref_ids': [],
                      'ref_residual_mm': [0.7, 0.8, 0.9, 0.8]}
        self.assertEqual(B.check_kind(resolve_ok), 'resolve')
        self.assertEqual(B.check_kind({'result': 'RESOLVE_REFUSED', 'dot_id': 0,
                                       'ref_ids': []}), 'resolve')
        self.assertIsNone(B.half_threshold_ok('resolve', resolve_ok))
        self.assertEqual(B.check_kind(self.CAM_OK), 'camera')
        t = B.CheckTally()
        t.add('resolve', resolve_ok)
        t.add('resolve', dict(resolve_ok, result='RESOLVE_REFUSED'))
        self.assertEqual(t.summary()['resolve']['by_result'], {'OK': 1, 'RESOLVE_REFUSED': 1})
        self.assertEqual(t.total('camera'), 0)
        self.assertIn('resolve: 2', t.line())

    def test_the_resolve_shape_is_the_servers(self):
        svc = (PAS / 'station' / 'service.py').read_text(encoding='utf-8')
        finish = svc.split('def _finish_work_locked', 1)[1].split('\n    def ', 1)[0]
        self.assertIn("'OK' if ok else 'RESOLVE_REFUSED'", finish)
        steps = svc.split('def _resolve_steps', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('C.camera_check(', steps)
        self.assertNotIn("res['ref_ids']", steps)               # no ref_ids on a re-solve
        cam = svc.split('def _camera_check', 1)[1].split('\n    def ', 1)[0]
        self.assertIn("res['ref_ids'] = list(f['ref_ids'])", cam)   # every camera check has them
        srv = (REPO / 'physical_ai_tools' / 'physical_ai_interfaces' / 'srv'
               / 'StationCheck.srv').read_text(encoding='utf-8')
        self.assertIn('RESOLVE_REFUSED', srv)

    def test_the_fingerprint_tells_two_checks_apart(self):
        a = B.check_fingerprint(self.CAM_OK)
        self.assertEqual(a, B.check_fingerprint(dict(self.CAM_OK)))
        self.assertNotEqual(a, B.check_fingerprint(dict(self.CAM_OK, rms_mm=1.2000001)))
        self.assertNotEqual(a, B.check_fingerprint(dict(self.CAM_OK, generation=4)))

    def test_mirrored_thresholds_equal_the_servers(self):
        check = PAS / 'station' / 'check.py'
        for mine, theirs in (('CAMERA_OK_RMS_MM', 'OK_RMS_MM'), ('CAMERA_OK_MAX_MM', 'OK_MAX_MM'),
                             ('CAMERA_OK_CENTROID_MM', 'OK_CENTROID_MM'),
                             ('CAMERA_OK_ROTATION_DEG', 'OK_ROTATION_DEG'),
                             ('ROBOT_OK_DXY_MM', 'ROBOT_DXY_MM'),
                             ('ROBOT_OK_DZ_MM', 'ROBOT_DZ_MM')):
            self.assertEqual(getattr(B, mine), _assign(check, theirs), mine)

    def test_entry_timer(self):
        e = B.EntryTimer()
        self.assertIsNone(e.update(0.0, 'IDLE', 1, False))
        self.assertIsNone(e.update(1.0, 'ENTERING', 2, False))
        self.assertIsNone(e.update(5.0, 'ACTIVE', 2, False))
        self.assertEqual(e.update(61.0, 'ACTIVE', 2, True), 60.0)
        self.assertIsNone(e.update(62.0, 'ACTIVE', 2, True))      # counted once
        self.assertIsNone(e.update(70.0, 'IDLE', 2, False))
        self.assertIsNone(e.update(80.0, 'ACTIVE', 3, False))     # a new session
        self.assertEqual(e.update(100.0, 'ACTIVE', 3, True), 20.0)
        self.assertEqual(e.durations, [60.0, 20.0])

    def test_entry_timer_ignores_a_session_running_when_logging_started(self):
        e = B.EntryTimer()
        self.assertIsNone(e.update(0.0, 'ACTIVE', 7, False))     # already running
        self.assertIsNone(e.update(30.0, 'ACTIVE', 7, True))     # not an entry we saw
        self.assertIsNone(e.update(40.0, 'IDLE', 7, False))
        self.assertIsNone(e.update(50.0, 'ENTERING', 8, False))  # entered after the start
        self.assertEqual(e.update(65.0, 'ACTIVE', 8, True), 15.0)
        self.assertEqual(e.durations, [15.0])
        # A first poll in ENTERING is the same: that entry began before we looked.
        f = B.EntryTimer()
        self.assertIsNone(f.update(0.0, 'ENTERING', 2, False))
        self.assertIsNone(f.update(5.0, 'ACTIVE', 2, True))
        self.assertEqual(f.durations, [])


class MirroredContracts(unittest.TestCase):
    def test_lease_port_and_topics(self):
        bridge = REPO / 'robotis_ai_setup' / 'gui' / 'app' / 'roboter_studio_control.py'
        self.assertEqual(B.STATION_LEASE_S, _assign(bridge, 'STATION_LEASE_S'))
        W = _load('wsl_docker.py', 'wsl_docker')
        self.assertEqual(W.BRIDGE_URL, f"http://127.0.0.1:{_assign(bridge, 'DEFAULT_PORT')}")
        texts = PAS / 'station' / 'texts_de.py'
        self.assertEqual(B.STATION_TOPIC, _assign(texts, 'STATION_TOPIC'))

    def test_image_qos_is_the_servers_own_subscription(self):
        """B2 counts frames TO THE SERVER: a RELIABLE bench subscriber gets the
        retransmissions the server's BEST_EFFORT subscription loses."""
        comm = PAS / 'communication' / 'communicator.py'
        src = comm.read_text(encoding='utf-8')
        self.assertEqual(B.SERVER_SENSOR_QOS_DEPTH, _assign(comm, 'SENSOR_QOS_DEPTH'))
        station = src.split('def enable_station_store', 1)[1].split('\n    def ', 1)[0]
        m = re.search(r'QoSProfile\(depth=(\d+), reliability=ReliabilityPolicy\.(\w+)', station)
        self.assertIsNotNone(m, 'enable_station_store no longer builds its QoS as expected')
        self.assertEqual((B.SERVER_STATION_QOS_DEPTH, 'BEST_EFFORT'), (int(m.group(1)),
                                                                       m.group(2)))
        subs = src.split('def init_subscribers', 1)[1].split('\n    def ', 1)[0]
        cam = subs.split('camera_qos = QoSProfile(', 1)[1].split(')', 1)[0]
        self.assertIn('depth=SENSOR_QOS_DEPTH', cam)
        self.assertIn('ReliabilityPolicy.BEST_EFFORT', cam)
        self.assertEqual(B.image_qos(B.STATION_TOPIC)[:2], ('best_effort', 2))
        self.assertEqual(B.image_qos(B.SCENE_TOPIC)[:2], ('best_effort', 32))
        self.assertEqual(B.image_qos(B.GRIPPER_TOPIC)[:2], ('best_effort', 32))
        # The comparison flag: the publisher's own QoS (camera_ingest_node.py).
        ingest = (REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator'
                  / 'camera_ingest_node.py').read_text(encoding='utf-8')
        self.assertRegex(ingest, r'QoSProfile\(\s*depth=5,\s*reliability=ReliabilityPolicy'
                                 r'\.RELIABLE')
        self.assertEqual(B.image_qos(B.STATION_TOPIC, reliable=True)[:2], ('reliable', 5))

    def test_the_container_tools_subscribe_images_with_the_server_qos(self):
        for rel in ('container/arrival_rate.py', 'container/watch_stream.py',
                    'container/perception_load.py'):
            src = (BENCH / rel).read_text(encoding='utf-8')
            self.assertTrue('B.image_qos(' in src, f'{rel}: not built by B.image_qos')
            self.assertIsNone(re.search(r'depth=5,\s*reliability=ReliabilityPolicy\.RELIABLE',
                                        src), f'{rel}: a hard-coded RELIABLE image QoS')
        A = _load('container/arrival_rate.py', 'station_bench_arrival_rate_qos')
        self.assertTrue(A.build_parser().parse_args(['--out', '/o', '--reliable']).reliable)

    def test_the_hat_and_yaw_cadence_the_b4_tool_mirrors(self):
        P = _load('container/perception_load.py', 'station_bench_perception_load')
        wm = (PAS / 'workflow' / 'workflow_manager.py').read_text(encoding='utf-8')
        body = wm.split('def _wait_object_visible', 1)[1].split('\n    def ', 1)[0]
        self.assertIn(f'time.sleep({P.HAT_POLL_SLEEP_S})', body)
        pb = PAS / 'workflow' / 'handlers' / 'perception_blocks.py'
        self.assertEqual(P.YAW_INTERVAL_DEFAULT_S, _assign(pb, '_TAG_YAW_FRAME_INTERVAL_S'))
        self.assertRegex(pb.read_text(encoding='utf-8'),
                         rf"_safe_float\('[A-Z_]+', {float(P.YAW_FRAMES_DEFAULT)}\)")

    def test_the_check_log_reads_only_fields_the_srv_answers(self):
        C = _load('container/check_log.py', 'station_bench_check_log')
        srv = REPO / 'physical_ai_tools' / 'physical_ai_interfaces' / 'srv'
        for name, fields in (('StationCheck.srv', C.CHECK_RESPONSE_FIELDS),
                             ('StationSession.srv', C.SESSION_RESPONSE_FIELDS)):
            response = (srv / name).read_text(encoding='utf-8').split('---', 1)[1]
            declared = set(re.findall(r'^\s*[\w\[\]]+\s+(\w+)', response, re.M))
            self.assertEqual(sorted(set(fields) - declared), [], name)


class Tools(unittest.TestCase):
    def test_every_tool_parses_its_arguments_without_ros(self):
        for rel in ('camera_open.py', 'run_logger.py', 'clock_offset.py', 'time_target.py',
                    'container_run.py', 'pick_tally.py', 'container/watch_stream.py',
                    'container/arrival_rate.py', 'container/perception_load.py',
                    'container/check_log.py'):
            mod = _load(rel, 'station_bench_' + rel.replace('/', '_')[:-3])
            parser = mod.build_parser()
            out = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
                parser.parse_args(['--help'])
            self.assertEqual(cm.exception.code, 0, rel)
            self.assertIn('usage:', out.getvalue())

    def test_run_logger_summary_and_pid_pick(self):
        R = _load('run_logger.py', 'station_bench_run_logger')
        rows = ([{'stream': 'off', 'scene_fps': 29.6, 'gripper_hz': 24.9,
                  'gui_cpu_pct_one_core': 12.0, 'error_de': ''}] * 5
                + [{'stream': 'on', 'scene_fps': 28.5, 'station_fps': 14.8, 'gripper_hz': 23.5,
                    'gui_cpu_pct_one_core': 40.0, 'error_de': ''}] * 5)
        args = types.SimpleNamespace(label='x', gripper_baseline_hz=25.0)
        s = R.summarize(rows, [], args)
        v = s['verdicts']
        self.assertEqual(v['B1 crop >= 29 fps, stream off (mean)'], 'PASS')
        self.assertEqual(v['B1 crop >= 29 fps, stream on (mean)'], 'FAIL')
        self.assertEqual(v['B1 Studio >= 14 fps under id 200 (mean)'], 'PASS')
        self.assertEqual(v['B1 gripper within 1 fps of baseline, stream off'], 'PASS')
        self.assertEqual(v['B1 gripper within 1 fps of baseline, stream on'], 'FAIL')
        self.assertEqual(s['stream_on']['crop_windows_below_29'], 5)
        toggles = [{'reopen_suspected': False, 'on_ok': True, 'scene_max_gap_ms': 40.0}] * 3
        s = R.summarize(rows, toggles, args)
        self.assertEqual(s['verdicts']['B1 200 switches without a reopen (bridge evidence)'],
                         'PENDING')
        cim = [{'ProcessId': 10, 'Name': 'EduBotics.exe',
                'CommandLine': '"C:\\EduBotics\\EduBotics.exe" --webview http://localhost'},
               {'ProcessId': 11, 'Name': 'EduBotics.exe',
                'CommandLine': '"C:\\EduBotics\\EduBotics.exe"'}]
        self.assertEqual(R.pick_gui_pid(cim), 11)
        dev = [{'ProcessId': 20, 'Name': 'python.exe',
                'CommandLine': 'python C:/src/robotis_ai_setup/gui/main.py'}]
        self.assertEqual(R.pick_gui_pid(dev), 20)
        self.assertIsNone(R.pick_gui_pid([]))

    def test_camera_open_frame_summary(self):
        C = _load('camera_open.py', 'station_bench_camera_open')
        frames = [(0.0, False, 0, 0)] + [(0.1 + i / 30.0, True, 1920, 1080) for i in range(31)]
        frames.append((1.5, True, 1280, 720))
        s = C.summarize_frames(frames)
        self.assertEqual(s['frames_native_size'], 31)
        self.assertEqual(s['read_failures'], 1)
        self.assertAlmostEqual(s['measured_fps_native'], 30.0, places=6)
        self.assertEqual(s['sizes_seen'], {'1920x1080': 31, '1280x720': 1})
        self.assertAlmostEqual(s['first_frame_s'], 0.1)

    def test_perception_trial_numbers(self):
        P = _load('container/perception_load.py', 'station_bench_perception_load')
        ends = [0.0, 0.45, 0.9, 1.35, 1.8, 2.25]
        r = P.trial_numbers(0.5, 1.7, ends)
        self.assertEqual(r['hat_polls_in_window'], 2)
        self.assertAlmostEqual(r['hat_polls_per_s_interval'], 1 / 0.45)
        self.assertAlmostEqual(r['hat_max_gap_s'], 0.45)
        starved = P.trial_numbers(0.5, 1.7, [0.0, 1.9])
        self.assertAlmostEqual(starved['hat_max_gap_s'], 1.9)
        self.assertAlmostEqual(starved['hat_polls_per_s_interval'], 1 / 1.9)

    def test_arrival_summaries(self):
        A = _load('container/arrival_rate.py', 'station_bench_arrival_rate')
        off = 2_000_000_000
        recs = [(int(1e12 + i * 1e9 / 15) + off + 100_000_000, int(1e12 + i * 1e9 / 15))
                for i in range(30)]
        s = A.summarize_topic(recs, off, 5_000_000, True)
        self.assertAlmostEqual(s['rate_fps'], 15.0, places=3)
        self.assertAlmostEqual(s['age_ms']['p95'], 100.0, places=3)
        self.assertEqual(s['B2 rate >= 14 fps'], 'PASS')
        self.assertEqual(s['B2 age p95 <= 0.3 s'], 'PASS')
        self.assertTrue(A.summarize_topic(recs, None, None, True)['B2 age p95 <= 0.3 s']
                        .startswith('UNKNOWN'))
        edge = [(r[0] + 198_000_000, r[1]) for r in recs]       # p95 298 ms, ±5 ms
        self.assertTrue(A.summarize_topic(edge, off, 5_000_000, True)['B2 age p95 <= 0.3 s']
                        .startswith('UNDECIDED'))
        q = A.summarize_qr([{'latency_ms': 200.0, 'shown_to_stamp_ms': 80.0}] * 10, 1_000_000)
        self.assertEqual(q['B3 latency p95 <= 0.35 s'], 'PASS')
        self.assertTrue(A.summarize_qr([], None)['B3 latency p95 <= 0.35 s'].startswith('UNKNOWN'))

    def test_check_log_recorder(self):
        C = _load('container/check_log.py', 'station_bench_check_log')
        r = C.Recorder()
        self.assertIsNone(r.check({'result': ''}))
        self.assertEqual(r.check(Checks.CAM_OK), 'camera')
        self.assertIsNone(r.check(dict(Checks.CAM_OK)))          # the same stored check
        self.assertEqual(r.check(dict(Checks.CAM_OK, rms_mm=1.3)), 'camera')
        self.assertEqual(r.tally.total('camera'), 2)
        resolved = dict(Checks.CAM_OK, ref_ids=[], rms_mm=0.9)
        self.assertEqual(r.check(resolved), 'resolve')           # never 'unknown'
        self.assertEqual(r.check(dict(resolved, result='RESOLVE_REFUSED')), 'resolve')
        self.assertEqual(r.tally.total('camera'), 2)             # not a camera check
        self.assertEqual(r.tally.total('resolve'), 2)
        # The owner's last re-solve job: a change is logged once.
        self.assertFalse(r.job({'job_id': 0, 'job_state': 'none', 'message': ''}))
        self.assertTrue(r.job({'job_id': 4, 'job_state': 'running', 'message': ''}))
        self.assertFalse(r.job({'job_id': 4, 'job_state': 'running', 'message': ''}))
        self.assertTrue(r.job({'job_id': 4, 'job_state': 'failed', 'message': 'x'}))
        # Logging starts while no session runs; the entry is seen from its start.
        changed, dur = r.session(0.0, {'state': 'IDLE', 'generation': 4, 'verified': False})
        self.assertTrue(changed)
        changed, dur = r.session(1.0, {'state': 'ENTERING', 'generation': 5, 'verified': False})
        self.assertTrue(changed)
        self.assertIsNone(dur)
        changed, dur = r.session(2.0, {'state': 'ENTERING', 'generation': 5, 'verified': False})
        self.assertFalse(changed)
        changed, dur = r.session(41.0, {'state': 'ACTIVE', 'generation': 5, 'verified': True})
        self.assertEqual((changed, dur), (True, 40.0))

    def test_pick_tally(self):
        T = _load('pick_tally.py', 'station_bench_pick_tally')
        t = T.Tally()
        for _ in range(48):
            t.key('s', 1.0, 'x')
        t.key('f', 2.0, 'x')
        t.key('x', 2.0, 'x')                                    # ignored
        self.assertEqual(t.summary()['g8_verdict'], 'PENDING')
        t.key('S', 3.0, 'x')
        s = t.summary()
        self.assertEqual((s['picks'], s['success'], s['fail']), (50, 49, 1))
        self.assertEqual(s['g8_verdict'], 'PASS')
        t.key('u', 4.0, 'x')
        self.assertEqual(t.summary()['g8_verdict'], 'FAIL')     # any unsafe contact fails
        self.assertIn('undo', t.key('z', 5.0, 'x'))
        self.assertEqual(t.summary()['g8_verdict'], 'PASS')
        self.assertEqual(B.pick_summary(['fail'] * 3 + ['success'] * 47)['g8_verdict'], 'FAIL')

    def test_qr_payload_round_trip(self):
        self.assertEqual(B.parse_qr_payload(B.qr_payload(1762077600123456789)),
                         1762077600123456789)
        for bad in ('', 'EBT1:', 'EBT1:12a', 'XYZ:1', None, 'EBT1:' + '9' * 21):
            self.assertIsNone(B.parse_qr_payload(bad), bad)


class WslPlumbing(unittest.TestCase):
    def setUp(self):
        self.W = _load('wsl_docker.py', 'wsl_docker')

    def test_every_wsl_word_is_plain(self):
        W = self.W
        self.assertEqual(W.docker_exec_argv(['ls', '-1', '/tmp/station_bench/out']),
                         ['wsl', '-d', 'EduBotics', '--', 'docker', 'exec', 'physical_ai_server',
                          'ls', '-1', '/tmp/station_bench/out'])
        self.assertEqual(W.bash_stdin_argv()[-4:], ['-i', 'physical_ai_server', 'bash', '-s'])
        for bad in ('a b', '$(x)', "'q'", 'a;b', 'a|b', 'a>b', ''):
            with self.assertRaises(ValueError, msg=bad):
                W.wsl_argv(['echo', bad])

    def test_scripts_travel_on_stdin(self):
        W = self.W
        s = W.python_tool_script('/tmp/station_bench/check_log.py',
                                 ['--out', '/tmp/station_bench/out/x', '--label', "Zone A's"],
                                 '/tmp/station_bench/check_log.pid').decode('utf-8')
        self.assertTrue(s.startswith('source /opt/ros/jazzy/setup.bash && source '
                                     '/root/ros2_ws/install/setup.bash\n'))
        self.assertIn('echo $$ > /tmp/station_bench/check_log.pid\nexec python3 -u '
                      '/tmp/station_bench/check_log.py --out /tmp/station_bench/out/x '
                      "--label 'Zone A'\"'\"'s'", s)
        cli = W.service_status_script('/station/session',
                                      'physical_ai_interfaces/srv/StationSession').decode()
        self.assertIn('ros2 service call /station/session physical_ai_interfaces/srv/'
                      'StationSession "{command: status}"', cli)

    def test_container_run_passes_the_offset_only_to_arrival_rate(self):
        R = _load('container_run.py', 'station_bench_container_run')
        self.assertEqual(R.tool_args('arrival_rate', '/o', ['--qr'], (5, 7), 's1'),
                         ['--out', '/o', '--stamp', 's1', '--offset-ns', '5',
                          '--offset-unc-ns', '7', '--qr'])
        self.assertEqual(R.tool_args('check_log', '/o', [], (5, 7)), ['--out', '/o'])
        self.assertEqual(R.split_argv(['check_log', '--out', 'C:/b', '--', '--duration', '9']),
                         (['check_log', '--out', 'C:/b'], ['--duration', '9']))
        self.assertEqual(R.split_argv(['check_log', '--out', 'x']),
                         (['check_log', '--out', 'x'], []))


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class _FakeProc:
    """A wsl client. ``script`` lists what each TIMED wait does: 'timeout',
    'ctrl_c' or an exit code. An UNTIMED wait fails the test: on Windows it is
    the uninterruptible ``WaitForSingleObject(INFINITE)``, so Ctrl+C is never
    seen while a long tool (check_log, the README's Ctrl+C case) runs."""

    def __init__(self, script, events, stdout=b''):
        self.script = list(script)
        self.events = events
        self.waits = []
        self.killed = False
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)

    def wait(self, timeout=None):
        if timeout is None:
            raise AssertionError('an untimed proc.wait(): Ctrl+C cannot interrupt it on Windows')
        self.waits.append(timeout)
        step = self.script.pop(0) if self.script else 0
        if step == 'timeout':
            raise subprocess.TimeoutExpired('wsl', timeout)
        if step == 'ctrl_c':
            raise KeyboardInterrupt
        self.events.append(('exit', step))
        return step

    def kill(self):
        self.killed = True


class ContainerRun(unittest.TestCase):
    def setUp(self):
        self.R = _load('container_run.py', 'station_bench_container_run_sup')

    def test_waits_in_short_slices_so_ctrl_c_is_seen(self):
        events = []
        proc = _FakeProc(['timeout', 'timeout', 0], events)
        rc, _ = self.R.supervise(proc, interrupt=lambda: events.append('interrupt'),
                                 tool_state=lambda: 'gone', say=lambda *_: None)
        self.assertEqual(rc, 0)
        self.assertTrue(proc.waits and all(t <= 0.5 for t in proc.waits), proc.waits)
        self.assertNotIn('interrupt', events)

    def test_ctrl_c_waits_for_the_summary_before_the_copy(self):
        # The race reproduced: after the interrupt the local client is gone at
        # once, while the container tool is still writing its summary.
        events, states = [], ['running', 'running', 'running', 'summary']
        clock = _FakeClock()
        proc = _FakeProc(['timeout', 'ctrl_c', 130], events)

        def state():
            s = states.pop(0)
            events.append(('state', s))
            return s

        rc, end = self.R.supervise(proc, interrupt=lambda: events.append('interrupt'),
                                   tool_state=state, clock=clock, sleep=clock.sleep,
                                   say=lambda *_: None)
        self.assertEqual(end, 'summary')
        self.assertEqual(events[0], 'interrupt')
        self.assertEqual([e for e in events if e[0] == 'state'][-1], ('state', 'summary'))
        self.assertEqual(states, [])                    # it waited for the summary
        self.assertNotIn(None, proc.waits)

    def test_the_wait_for_the_summary_is_bounded(self):
        clock = _FakeClock()
        proc = _FakeProc(['ctrl_c', 130], [])
        rc, end = self.R.supervise(proc, interrupt=lambda: None,
                                   tool_state=lambda: 'running', done_timeout_s=60.0,
                                   clock=clock, sleep=clock.sleep, say=lambda *_: None)
        self.assertEqual(end, 'timeout')
        self.assertGreaterEqual(clock.t, 60.0)
        self.assertLess(clock.t, 62.0)

    def test_main_relays_the_console_and_copies_after_the_summary(self):
        R, W = self.R, self.R.W
        events = []
        popen_kwargs = {}
        proc = _FakeProc(['timeout', 'ctrl_c', 130], events,
                         stdout='listening …\nwrote /tmp/x_summary.json\n'.encode('utf-8'))
        states = ['running', 'summary']

        def fake_popen(argv, **kw):
            popen_kwargs.update(kw)
            return proc

        saved = {n: getattr(W, n, None) for n in (
            'push_file', 'pull_dir', 'interrupt', 'container_tool_state', 'distro_name')}
        saved_popen = R.subprocess.Popen
        out = io.StringIO()
        try:
            W.push_file = lambda *a, **k: None
            W.distro_name = lambda: 'EduBotics'
            W.interrupt = lambda *a, **k: events.append('interrupt')
            W.container_tool_state = lambda *a, **k: (events.append('state'), states.pop(0))[1]
            W.pull_dir = lambda *a, **k: (events.append('pull'), ['a_summary.json'])[1]
            R.subprocess.Popen = fake_popen
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
                R.main(['check_log', '--out', d])
        finally:
            for n, v in saved.items():
                if v is None:
                    if hasattr(W, n):
                        delattr(W, n)
                else:
                    setattr(W, n, v)
            R.subprocess.Popen = saved_popen
        order = [e for e in events if e in ('interrupt', 'state', 'pull')]
        self.assertEqual(order, ['interrupt', 'state', 'state', 'pull'])
        self.assertIs(popen_kwargs.get('stdout'), subprocess.PIPE)   # relayed, never lost
        self.assertIn('wrote /tmp/x_summary.json', out.getvalue())

    def test_the_tool_state_script_and_its_answer(self):
        W = _load('wsl_docker.py', 'wsl_docker')
        s = W.tool_state_script('/tmp/station_bench/check_log.pid',
                                '/tmp/station_bench/out/check_log_1').decode()
        self.assertIn('/tmp/station_bench/out/check_log_1/*_summary.json', s)
        self.assertIn('kill -0', s)
        self.assertEqual(W.parse_tool_state('summary\n'), 'summary')
        self.assertEqual(W.parse_tool_state('noise\nrunning\n'), 'running')
        self.assertEqual(W.parse_tool_state('gone'), 'gone')
        self.assertIsNone(W.parse_tool_state(''))
        self.assertIsNone(W.parse_tool_state('Error: No such container'))


class NeverShipped(unittest.TestCase):
    """Nothing under tools/ reaches an image, the installer or the GUI build."""

    def _dockerfiles(self):
        return [p for p in REPO.rglob('Dockerfile*')
                if 'node_modules' not in p.parts and p.is_file()]

    def test_no_dockerfile_copies_from_tools(self):
        files = self._dockerfiles()
        self.assertGreater(len(files), 5)
        for df in files:
            for line in df.read_text(encoding='utf-8').splitlines():
                m = re.match(r'\s*(COPY|ADD)\s+(.*)$', line, re.I)
                if not m or '--from' in m.group(2):
                    continue
                srcs = [w for w in m.group(2).split() if not w.startswith('--')][:-1]
                for s in srcs:
                    self.assertFalse(s.startswith(('tools/', './tools/', '../tools')),
                                     f'{df.relative_to(REPO)}: {line.strip()}')
                    self.assertNotIn('station_bench', s, f'{df.relative_to(REPO)}')

    def test_the_repo_root_build_context_copies_named_files_only(self):
        df = REPO / 'robotis_ai_setup' / 'runpod_training' / 'Dockerfile'
        for line in df.read_text(encoding='utf-8').splitlines():
            m = re.match(r'\s*COPY\s+(.*)$', line)
            if m and '--from' not in m.group(1):
                self.assertTrue(m.group(1).split()[0].startswith('robotis_ai_setup/'), line)

    def test_build_scripts_installer_and_gui_spec_name_no_tools_path(self):
        for rel in ('robotis_ai_setup/docker/build-images.sh',
                    'robotis_ai_setup/installer/robotis_ai_setup.iss',
                    'robotis_ai_setup/gui/build.spec',
                    '.github/scripts/image_source_parity.sh'):
            text = (REPO / rel).read_text(encoding='utf-8', errors='replace')
            self.assertNotRegex(text, r'(^|[\s"\'(=/\\])tools[/\\]', rel)
            self.assertNotIn('station_bench', text, rel)

    def test_the_toolkit_reads_no_edubotics_env(self):
        names = set()
        for p in BENCH.rglob('*.py'):
            names |= set(re.findall(r'EDUBOTICS_[A-Z0-9_]+', p.read_text(encoding='utf-8')))
        self.assertEqual(sorted(names), [])


if __name__ == '__main__':
    unittest.main()
