#!/usr/bin/env python3
#
# Unit tests for the teleop force/collision detector (EduBotics teleop e-stop).
#
# CollisionDetector is pure logic with NO rclpy/ROS imports, so we load it directly via
# importlib from the COPY-wholesale physical_ai_server package (same source-of-truth path
# convention as test_data_manager_finalize.py) and exercise every branch without the ROS
# stack: per-joint threshold, the velocity gate, debounce, per-joint independence, the
# Overload hardware-bit backstop, inference mode-gating, the master enable switch, reset(),
# and the env-driven factory.

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DETECTOR_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'safety' / 'collision_detector.py'
)


def _load_module():
    spec = importlib.util.spec_from_file_location('collision_detector', DETECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CD = _load_module()
JOINTS = ['dxl11', 'dxl12', 'dxl13', 'dxl14', 'dxl15']


def _make(thresholds=None, velocity_gate=0.05, debounce_ticks=3,
          use_overload_bit=True, enabled=True):
    if thresholds is None:
        thresholds = {j: 1.0 for j in JOINTS}
    return CD.CollisionDetector(
        joint_names=JOINTS,
        current_thresholds=thresholds,
        velocity_gate=velocity_gate,
        debounce_ticks=debounce_ticks,
        use_overload_bit=use_overload_bit,
        enabled=enabled,
    )


def _all(value):
    return {j: value for j in JOINTS}


class TestThreshold(unittest.TestCase):
    def test_high_current_zero_velocity_trips_after_debounce(self):
        d = _make(debounce_ticks=3)
        currents = {**_all(0.0), 'dxl11': 1.5}
        vels = _all(0.0)
        errs = _all(0)
        # First two ticks: building debounce, not yet tripped.
        self.assertFalse(d.update(currents, vels, errs, False).tripped)
        self.assertFalse(d.update(currents, vels, errs, False).tripped)
        # Third consecutive bad tick trips.
        res = d.update(currents, vels, errs, False)
        self.assertTrue(res.tripped)
        self.assertIn('dxl11', res.joints)

    def test_just_below_threshold_never_trips(self):
        d = _make(thresholds=_all(1.0), debounce_ticks=2)
        currents = {**_all(0.0), 'dxl11': 0.99}
        for _ in range(20):
            self.assertFalse(d.update(currents, _all(0.0), _all(0), False).tripped)


class TestVelocityGate(unittest.TestCase):
    def test_high_current_while_moving_does_not_trip(self):
        # Fast free teleop motion draws current but the joint IS moving -> gate rejects.
        d = _make(velocity_gate=0.05, debounce_ticks=2)
        currents = {**_all(0.0), 'dxl11': 5.0}
        vels = {**_all(0.0), 'dxl11': 1.2}  # well above the gate
        for _ in range(20):
            self.assertFalse(d.update(currents, vels, _all(0), False).tripped)

    def test_missing_velocity_defaults_to_not_moving(self):
        # A joint absent from velocities is treated as 0.0 (fails toward protection).
        d = _make(debounce_ticks=1)
        currents = {'dxl11': 2.0}
        res = d.update(currents, {}, {}, False)
        self.assertTrue(res.tripped)


class TestDebounce(unittest.TestCase):
    def test_single_spike_resets(self):
        d = _make(debounce_ticks=3)
        bad = ({**_all(0.0), 'dxl11': 2.0}, _all(0.0), _all(0))
        good = (_all(0.0), _all(0.0), _all(0))
        self.assertFalse(d.update(*bad, False).tripped)   # 1
        self.assertFalse(d.update(*bad, False).tripped)   # 2
        self.assertFalse(d.update(*good, False).tripped)  # reset
        self.assertFalse(d.update(*bad, False).tripped)   # 1 again
        self.assertFalse(d.update(*bad, False).tripped)   # 2
        self.assertTrue(d.update(*bad, False).tripped)    # 3 -> trip

    def test_debounce_one_trips_immediately(self):
        d = _make(debounce_ticks=1)
        res = d.update({**_all(0.0), 'dxl11': 2.0}, _all(0.0), _all(0), False)
        self.assertTrue(res.tripped)


class TestPerJoint(unittest.TestCase):
    def test_per_joint_thresholds_independent(self):
        d = _make(thresholds={'dxl11': 1.5, 'dxl12': 1.5, 'dxl13': 1.2,
                              'dxl14': 1.0, 'dxl15': 1.0}, debounce_ticks=1)
        # dxl14 at 1.1 trips (>=1.0); dxl11 at 1.1 does NOT (<1.5).
        res = d.update({**_all(0.0), 'dxl14': 1.1, 'dxl11': 1.1}, _all(0.0), _all(0), False)
        self.assertTrue(res.tripped)
        self.assertIn('dxl14', res.joints)
        self.assertNotIn('dxl11', res.joints)


class TestOverloadBackstop(unittest.TestCase):
    def test_overload_bit_trips_immediately(self):
        d = _make(debounce_ticks=100)  # debounce would never trip on its own
        errs = {**_all(0), 'dxl12': CD.OVERLOAD_BIT}
        res = d.update(_all(0.0), _all(0.0), errs, False)
        self.assertTrue(res.tripped)
        self.assertIn('dxl12', res.joints)
        self.assertIn('dxl12', res.latched_overload)

    def test_overload_ignored_when_disabled(self):
        d = _make(debounce_ticks=100, use_overload_bit=False)
        errs = {**_all(0), 'dxl12': CD.OVERLOAD_BIT}
        res = d.update(_all(0.0), _all(0.0), errs, False)
        self.assertFalse(res.tripped)

    def test_non_overload_error_bits_ignored(self):
        d = _make(debounce_ticks=1)
        errs = {**_all(0), 'dxl12': 0x04}  # overheating bit, not overload
        res = d.update(_all(0.0), _all(0.0), errs, False)
        self.assertFalse(res.tripped)


class TestModeGating(unittest.TestCase):
    def test_inference_never_trips(self):
        d = _make(debounce_ticks=1)
        currents = {**_all(0.0), 'dxl11': 5.0}
        errs = {**_all(0), 'dxl11': CD.OVERLOAD_BIT}
        for _ in range(10):
            self.assertFalse(d.update(currents, _all(0.0), errs, True).tripped)

    def test_inference_resets_debounce(self):
        d = _make(debounce_ticks=3)
        bad = ({**_all(0.0), 'dxl11': 2.0}, _all(0.0), _all(0))
        d.update(*bad, False)  # 1
        d.update(*bad, False)  # 2
        d.update(*bad, True)   # inference tick resets counters
        # Back to collection: must re-debounce from zero, so this is only tick 1.
        self.assertFalse(d.update(*bad, False).tripped)
        self.assertFalse(d.update(*bad, False).tripped)
        self.assertTrue(d.update(*bad, False).tripped)


class TestMasterEnable(unittest.TestCase):
    def test_disabled_never_trips(self):
        d = _make(debounce_ticks=1, enabled=False)
        currents = {**_all(0.0), 'dxl11': 5.0}
        errs = {**_all(0), 'dxl11': CD.OVERLOAD_BIT}
        for _ in range(10):
            self.assertFalse(d.update(currents, _all(0.0), errs, False).tripped)


class TestReset(unittest.TestCase):
    def test_reset_requires_fresh_debounce(self):
        d = _make(debounce_ticks=3)
        bad = ({**_all(0.0), 'dxl11': 2.0}, _all(0.0), _all(0))
        d.update(*bad, False)  # 1
        d.update(*bad, False)  # 2
        d.reset()
        self.assertFalse(d.update(*bad, False).tripped)  # 1
        self.assertFalse(d.update(*bad, False).tripped)  # 2
        self.assertTrue(d.update(*bad, False).tripped)   # 3


class TestEnvFactory(unittest.TestCase):
    def test_defaults(self):
        env = {}
        d = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS,
                                       update_rate_hz=100.0)
        self.assertTrue(d.enabled)
        # 150 ms at 100 Hz -> 15 ticks.
        self.assertEqual(d.debounce_ticks, 15)

    def test_env_overrides_and_disable(self):
        env = {
            'EDUBOTICS_COLLISION_ENABLED': '0',
            'EDUBOTICS_COLLISION_CURRENT_J1': '0.8',
            'EDUBOTICS_COLLISION_DEBOUNCE_MS': '100',
            'EDUBOTICS_COLLISION_VELOCITY_GATE': '0.1',
        }
        d = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS,
                                       update_rate_hz=100.0)
        self.assertFalse(d.enabled)
        self.assertEqual(d.debounce_ticks, 10)  # 100 ms at 100 Hz
        # Disabled -> never trips even with a clear overcurrent.
        self.assertFalse(
            d.update({'dxl11': 5.0}, {'dxl11': 0.0}, {'dxl11': 0}, False).tripped)

    def test_bad_env_value_falls_back(self):
        env = {'EDUBOTICS_COLLISION_CURRENT_J1': 'not-a-number'}
        d = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS)
        # Falls back to the default threshold for J1 (1.5 A): 1.4 does not trip, 1.6 does.
        d1 = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS)
        d1._debounce_ticks = 1
        self.assertFalse(d1.update({'dxl11': 1.4}, {'dxl11': 0.0}, {}, False).tripped)
        self.assertTrue(d1.update({'dxl11': 1.6}, {'dxl11': 0.0}, {}, False).tripped)


if __name__ == '__main__':
    unittest.main()
