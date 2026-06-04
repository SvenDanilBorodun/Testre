#!/usr/bin/env python3
#
# Unit tests for the teleop force/collision detector (EduBotics teleop e-stop).
#
# CollisionDetector is pure logic with NO rclpy/ROS imports, so we load it directly via
# importlib from the COPY-wholesale physical_ai_server package (same source-of-truth path
# convention as test_data_manager_finalize.py) and exercise every branch without the ROS
# stack: the normalized effort-fraction signal (mixed XL430 'Present Load' / XL330 'Present
# Current' servo models, incl. the unsigned-int16 wire bug caught on-rig 2026-06-03),
# per-joint threshold, the velocity gate, debounce, per-joint independence, the Overload
# hardware-bit backstop, inference mode-gating, the master enable switch, reset(), and the
# env-driven factory.

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
# The monitor feeds the detector ARM JOINT names (it maps gpio names dxl11..dxl15 -> joint1..
# joint5 and normalizes the per-model force signal to an effort fraction before update()).
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5']


def _make(thresholds=None, velocity_gate=0.05, debounce_ticks=3,
          use_overload_bit=True, enabled=True):
    if thresholds is None:
        thresholds = {j: 0.50 for j in JOINTS}
    return CD.CollisionDetector(
        joint_names=JOINTS,
        effort_thresholds=thresholds,
        velocity_gate=velocity_gate,
        debounce_ticks=debounce_ticks,
        use_overload_bit=use_overload_bit,
        enabled=enabled,
    )


def _all(value):
    return {j: value for j in JOINTS}


class TestEffortFraction(unittest.TestCase):
    """effort_fraction_from_values(): the per-model 0..1 normalization layer."""

    def test_present_load_normalizes_to_fraction(self):
        # XL430-W250 base joint: Present Load 500 == 50% PWM.
        self.assertAlmostEqual(
            CD.effort_fraction_from_values('joint1', {'Present Load': 500.0}), 0.5)

    def test_present_current_normalizes_to_fraction(self):
        # XL330-M288 wrist joint: Present Current 875 raw == half the 1750 full-scale.
        self.assertAlmostEqual(
            CD.effort_fraction_from_values('joint4', {'Present Current': 875.0}), 0.5)

    def test_unsigned_int16_mapped_back_to_negative(self):
        # The gpio broadcaster publishes the SIGNED 16-bit register UNSIGNED: -5 arrives as
        # 65531 (confirmed on-rig 2026-06-03, dxl11 at rest). Naive |65531|/1000 = 65.5 would
        # instantly false-trip; the sign fix must yield |−5|/1000 = 0.005.
        self.assertAlmostEqual(
            CD.effort_fraction_from_values('joint1', {'Present Load': 65531.0}), 0.005)

    def test_unsigned_int16_present_current(self):
        # Same fix for Present Current arriving unsigned: -17 -> 65519.
        self.assertAlmostEqual(
            CD.effort_fraction_from_values('joint4', {'Present Current': 65519.0}),
            17.0 / 1750.0)

    def test_missing_signal_returns_none(self):
        # No recognized force signal -> None ("no reading", NOT zero / NOT a trip).
        self.assertIsNone(
            CD.effort_fraction_from_values('joint1', {'Present Velocity': 1.0}))

    def test_fallback_to_other_signal_on_model_swap(self):
        # joint1 expects Present Load; if the servo model changed and only Present Current is
        # published, fall back to it (with the matching full-scale).
        self.assertAlmostEqual(
            CD.effort_fraction_from_values('joint1', {'Present Current': 875.0}), 0.5)

    def test_joint_effort_signal_map_matches_omx_bom(self):
        # dxl11-13 = XL430-W250 -> Present Load; dxl14-15 = XL330-M288 -> Present Current.
        for j in ('joint1', 'joint2', 'joint3'):
            self.assertEqual(CD.JOINT_EFFORT_SIGNAL[j][0], 'Present Load')
        for j in ('joint4', 'joint5'):
            self.assertEqual(CD.JOINT_EFFORT_SIGNAL[j][0], 'Present Current')


class TestThreshold(unittest.TestCase):
    def test_high_effort_zero_velocity_trips_after_debounce(self):
        d = _make(debounce_ticks=3)
        efforts = {**_all(0.0), 'joint1': 0.75}
        vels = _all(0.0)
        errs = _all(0)
        # First two ticks: building debounce, not yet tripped.
        self.assertFalse(d.update(efforts, vels, errs, False).tripped)
        self.assertFalse(d.update(efforts, vels, errs, False).tripped)
        # Third consecutive bad tick trips.
        res = d.update(efforts, vels, errs, False)
        self.assertTrue(res.tripped)
        self.assertIn('joint1', res.joints)

    def test_just_below_threshold_never_trips(self):
        d = _make(thresholds=_all(0.50), debounce_ticks=2)
        efforts = {**_all(0.0), 'joint1': 0.49}
        for _ in range(20):
            self.assertFalse(d.update(efforts, _all(0.0), _all(0), False).tripped)

    def test_missing_effort_is_no_reading_not_a_trip(self):
        # A joint absent from `efforts` (sensor dropout) contributes no over-force and resets
        # its debounce counter — a dropout can never itself stop the arm.
        d = _make(debounce_ticks=2)
        bad = {**_all(0.0), 'joint1': 0.9}
        self.assertFalse(d.update(bad, _all(0.0), _all(0), False).tripped)        # 1
        self.assertFalse(d.update({}, _all(0.0), _all(0), False).tripped)         # dropout
        self.assertFalse(d.update(bad, _all(0.0), _all(0), False).tripped)        # 1 again
        self.assertTrue(d.update(bad, _all(0.0), _all(0), False).tripped)         # 2 -> trip


class TestVelocityGate(unittest.TestCase):
    def test_high_effort_while_moving_does_not_trip(self):
        # Fast free teleop motion strains the motors but the joint IS moving -> gate rejects.
        d = _make(velocity_gate=0.05, debounce_ticks=2)
        efforts = {**_all(0.0), 'joint1': 0.95}
        vels = {**_all(0.0), 'joint1': 1.2}  # well above the gate
        for _ in range(20):
            self.assertFalse(d.update(efforts, vels, _all(0), False).tripped)

    def test_missing_velocity_defaults_to_not_moving(self):
        # A joint absent from velocities is treated as 0.0 (fails toward protection).
        d = _make(debounce_ticks=1)
        efforts = {'joint1': 0.9}
        res = d.update(efforts, {}, {}, False)
        self.assertTrue(res.tripped)


class TestDebounce(unittest.TestCase):
    def test_single_spike_resets(self):
        d = _make(debounce_ticks=3)
        bad = ({**_all(0.0), 'joint1': 0.9}, _all(0.0), _all(0))
        good = (_all(0.0), _all(0.0), _all(0))
        self.assertFalse(d.update(*bad, False).tripped)   # 1
        self.assertFalse(d.update(*bad, False).tripped)   # 2
        self.assertFalse(d.update(*good, False).tripped)  # reset
        self.assertFalse(d.update(*bad, False).tripped)   # 1 again
        self.assertFalse(d.update(*bad, False).tripped)   # 2
        self.assertTrue(d.update(*bad, False).tripped)    # 3 -> trip

    def test_debounce_one_trips_immediately(self):
        d = _make(debounce_ticks=1)
        res = d.update({**_all(0.0), 'joint1': 0.9}, _all(0.0), _all(0), False)
        self.assertTrue(res.tripped)


class TestPerJoint(unittest.TestCase):
    def test_per_joint_thresholds_independent(self):
        d = _make(thresholds={'joint1': 0.65, 'joint2': 0.65, 'joint3': 0.40,
                              'joint4': 0.30, 'joint5': 0.30}, debounce_ticks=1)
        # joint4 at 0.35 trips (>=0.30); joint1 at 0.35 does NOT (<0.65).
        res = d.update({**_all(0.0), 'joint4': 0.35, 'joint1': 0.35},
                       _all(0.0), _all(0), False)
        self.assertTrue(res.tripped)
        self.assertIn('joint4', res.joints)
        self.assertNotIn('joint1', res.joints)


class TestOverloadBackstop(unittest.TestCase):
    def test_overload_bit_trips_immediately(self):
        d = _make(debounce_ticks=100)  # debounce would never trip on its own
        errs = {**_all(0), 'joint2': CD.OVERLOAD_BIT}
        res = d.update(_all(0.0), _all(0.0), errs, False)
        self.assertTrue(res.tripped)
        self.assertIn('joint2', res.joints)
        self.assertIn('joint2', res.latched_overload)

    def test_overload_ignored_when_disabled(self):
        d = _make(debounce_ticks=100, use_overload_bit=False)
        errs = {**_all(0), 'joint2': CD.OVERLOAD_BIT}
        res = d.update(_all(0.0), _all(0.0), errs, False)
        self.assertFalse(res.tripped)

    def test_non_overload_error_bits_ignored(self):
        d = _make(debounce_ticks=1)
        errs = {**_all(0), 'joint2': 0x04}  # overheating bit, not overload
        res = d.update(_all(0.0), _all(0.0), errs, False)
        self.assertFalse(res.tripped)


class TestModeGating(unittest.TestCase):
    def test_inference_never_trips(self):
        d = _make(debounce_ticks=1)
        efforts = {**_all(0.0), 'joint1': 1.0}
        errs = {**_all(0), 'joint1': CD.OVERLOAD_BIT}
        for _ in range(10):
            self.assertFalse(d.update(efforts, _all(0.0), errs, True).tripped)

    def test_inference_resets_debounce(self):
        d = _make(debounce_ticks=3)
        bad = ({**_all(0.0), 'joint1': 0.9}, _all(0.0), _all(0))
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
        efforts = {**_all(0.0), 'joint1': 1.0}
        errs = {**_all(0), 'joint1': CD.OVERLOAD_BIT}
        for _ in range(10):
            self.assertFalse(d.update(efforts, _all(0.0), errs, False).tripped)


class TestReset(unittest.TestCase):
    def test_reset_requires_fresh_debounce(self):
        d = _make(debounce_ticks=3)
        bad = ({**_all(0.0), 'joint1': 0.9}, _all(0.0), _all(0))
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

    def test_default_thresholds_are_calibrated_fractions(self):
        # The shipped defaults are effort FRACTIONS calibrated on the reference rig — they
        # must stay in (0, 1] (an Amps-style value like 1.5 here means a unit regression).
        self.assertEqual(len(CD.DEFAULT_EFFORT_THRESHOLDS), 5)
        for value in CD.DEFAULT_EFFORT_THRESHOLDS:
            self.assertGreater(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_env_overrides_and_disable(self):
        env = {
            'EDUBOTICS_COLLISION_ENABLED': '0',
            'EDUBOTICS_COLLISION_EFFORT_J1': '0.45',
            'EDUBOTICS_COLLISION_DEBOUNCE_MS': '100',
            'EDUBOTICS_COLLISION_VELOCITY_GATE': '0.1',
        }
        d = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS,
                                       update_rate_hz=100.0)
        self.assertFalse(d.enabled)
        self.assertEqual(d.debounce_ticks, 10)  # 100 ms at 100 Hz
        # Disabled -> never trips even with a clear over-force.
        self.assertFalse(
            d.update({'joint1': 1.0}, {'joint1': 0.0}, {'joint1': 0}, False).tripped)

    def test_effort_env_var_overrides_threshold(self):
        env = {'EDUBOTICS_COLLISION_EFFORT_J1': '0.45'}
        d = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS)
        d._debounce_ticks = 1
        self.assertFalse(d.update({'joint1': 0.44}, {'joint1': 0.0}, {}, False).tripped)
        self.assertTrue(d.update({'joint1': 0.46}, {'joint1': 0.0}, {}, False).tripped)

    def test_bad_env_value_falls_back(self):
        env = {'EDUBOTICS_COLLISION_EFFORT_J1': 'not-a-number'}
        d = CD.build_detector_from_env(lambda k, default=None: env.get(k, default), JOINTS)
        # Falls back to the J1 default (DEFAULT_EFFORT_THRESHOLDS[0]): just below stays
        # quiet, just above trips.
        default_j1 = CD.DEFAULT_EFFORT_THRESHOLDS[0]
        d._debounce_ticks = 1
        self.assertFalse(
            d.update({'joint1': default_j1 - 0.01}, {'joint1': 0.0}, {}, False).tripped)
        self.assertTrue(
            d.update({'joint1': default_j1 + 0.01}, {'joint1': 0.0}, {}, False).tripped)


if __name__ == '__main__':
    unittest.main()
