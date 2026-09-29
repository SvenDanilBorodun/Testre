#!/usr/bin/env python3
#
# F1 (Aufnahme 2.0, owner-approved collision-path change): FORCE_RESUME_TELEOP
# („Trotzdem fortsetzen“) during a recording ENDS the session like FINISH — the
# DataManager finalizes and hands the upload off per its own guards — and the
# terminating READY carries the saved count plus a `[WARNUNG]` iff the upload was
# blocked. Before, the forced path left the dataset unfinalized, never uploaded
# it and kept the crash marker. A plain RESUME_TELEOP still re-arms the SAME
# session (unchanged).
#
# The fake node host and the collision stubs are the ones of
# test_collision_monitor_contract.py, loaded from that file by path (tests/ has
# no __init__.py) so both files drive the monitor through one harness. The
# existing contract test is deliberately not edited: its SimpleNamespace
# DataManager has no end_session_now, which keeps the plain READY path covered.

import importlib.util
import types
import unittest
from pathlib import Path

_CONTRACT_PATH = Path(__file__).with_name('test_collision_monitor_contract.py')
H = None


def _harness():
    global H
    if H is None:
        spec = importlib.util.spec_from_file_location(
            '_edubotics_collision_contract_harness', str(_CONTRACT_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        H = module
    return H


class ForceResumeEndsRecordingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.h = _harness()

    def _host(self, *, end=None):
        h = self.h
        host = h._Host()
        calls = []
        status = h._TaskStatus()
        status.current_episode_number = 2
        status.total_time = 5
        status.proceed_time = 3

        def _end():
            calls.append('end')
            if end is not None:
                return end()
            return True

        host.data_manager = types.SimpleNamespace(
            re_record=lambda: calls.append('re_record'),
            end_session_now=_end,
            get_current_record_status=lambda: status,
            _upload_blocked_reason_de='')
        host.calls = calls
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(h.CONTACT_POSE)
        host.on_recording = True
        h._trip(host)
        host._collision_leader_pos = {**h.CONTACT_POSE}
        return host

    def test_force_resume_mid_recording_finishes_the_session(self):
        host = self._host()
        self.assertTrue(host.force_resume_teleop()[0])
        host.fire_pending_timers()
        self.assertEqual(host.calls, ['re_record', 'end'])
        self.assertEqual(host.timer_start_calls, [])      # NOT re-armed
        self.assertFalse(host.on_recording)
        last = host.pub_for('/task/status').published[-1]
        self.assertEqual(last.phase, self.h._TaskStatus.READY)
        self.assertEqual(last.current_episode_number, 2)
        self.assertEqual(last.total_time, 0)
        self.assertEqual(last.proceed_time, 0)
        self.assertEqual(last.error, '')
        self.assertFalse(host._collision_end_recording)   # consumed

    def test_blocked_upload_reason_rides_the_terminating_tick(self):
        host = self._host()
        host.data_manager._upload_blocked_reason_de = 'Upload abgelehnt: …'
        host.force_resume_teleop()
        host.fire_pending_timers()
        self.assertEqual(host.pub_for('/task/status').published[-1].error,
                         '[WARNUNG] Upload abgelehnt: …')

    def test_plain_resume_still_rearms_the_same_session(self):
        h = self.h
        host = self._host()
        host._collision_homed = True
        host._collision_leader_pos = {
            j: v for j, v in zip(h.CM.ARM_JOINT_NAMES, h.CM.SAFE_HOME_ARM)}
        host._collision_leader_pos.update(
            {j: 0.0 for j in h.CM.LEADER_JOINTS if j not in host._collision_leader_pos})
        self.assertTrue(host.resume_teleop()[0])
        host.fire_pending_timers()
        self.assertNotIn('end', host.calls)
        self.assertEqual(host.timer_start_calls, ['collection'])

    def test_force_resume_without_a_recording_publishes_plain_ready(self):
        h = self.h
        host = h._Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(h.CONTACT_POSE)
        host.on_recording = False
        h._trip(host)
        host._collision_leader_pos = {**h.CONTACT_POSE}
        self.assertTrue(host.force_resume_teleop()[0])
        self.assertFalse(host._collision_end_recording)
        host.fire_pending_timers()
        last = host.pub_for('/task/status').published[-1]
        self.assertEqual(last.phase, h._TaskStatus.READY)

    def test_a_failing_end_falls_back_to_plain_ready_and_never_raises(self):
        def _boom():
            raise RuntimeError('finalize exploded')

        host = self._host(end=_boom)
        host.force_resume_teleop()
        host.fire_pending_timers()               # must not raise
        self.assertIn('end', host.calls)
        last = host.pub_for('/task/status').published[-1]
        self.assertEqual(last.phase, self.h._TaskStatus.READY)
        self.assertFalse(host.on_recording)
        self.assertEqual(host.timer_start_calls, [])


if __name__ == '__main__':
    unittest.main()
