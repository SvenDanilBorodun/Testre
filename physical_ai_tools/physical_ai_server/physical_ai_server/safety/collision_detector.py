# Copyright 2026 EduBotics
#
# Teleoperation force/collision detector — PURE LOGIC, no rclpy/ROS imports.
#
# EduBotics teleop force/collision e-stop (a Rule §2 software safety guard, scoped to
# teleop/recording only). On the OpenMANIPULATOR-X follower the arm joints (dxl11-15) run
# position control with NO current limit, so a student pressing the arm into an object winds
# the motors toward stall with nothing stopping them but a slow firmware overload trip. This
# class detects that from each joint's per-tick motor-effort signal.
#
# MIXED SERVO MODELS (this is why the signal is a normalized "effort fraction", not Amps):
# the follower is not a uniform arm. dxl11-13 are XL430-W250, whose control table has NO
# 'Present Current' register — it exposes 'Present Load' (addr 126, signed, +/-1000 = +/-100%
# of applied PWM). dxl14-15 are XL330-M288, which DO have 'Present Current' (addr 126,
# ~1 mA/LSB). Declaring 'Present Current' on an XL430 aborts the follower's hardware init
# ("Cannot find control item in model file : Present Current") — which is exactly why an
# earlier all-'Present Current' version bricked the follower on the standard OMX-X BOM. The
# collision_monitor reads each joint's NATIVE signal and normalizes it to a 0..1 effort
# fraction (|signal| / full-scale) via effort_fraction_from_values() below; this detector then
# works purely on fractions, so a single threshold concept covers both models.
#
# Detection rule (per joint, per tick):
#   * PRIMARY: effort_fraction >= threshold AND |velocity| <= velocity_gate, sustained for
#     `debounce_ticks` consecutive ticks. The velocity gate is the crux — a blocked joint
#     strains hard while barely moving, whereas fast free teleop motion also strains but the
#     joint IS moving. The debounce rejects transient acceleration spikes.
#   * BACKSTOP: the firmware Hardware Error Status "Overload" bit (0x20) -> immediate hard trip.
#
# Gating: returns "not tripped" (and resets counters) when mode_is_inference is True (Rule §2:
# the inference action distribution must never be reshaped) or when disabled (master rollback).
#
# Kept free of rclpy so it unit-tests without the ROS stack.

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

# Dynamixel X-series Hardware Error Status bitmask (control table addr 70).
OVERLOAD_BIT = 0x20

# Per-model full-scale normalizers used to turn a raw force register into a 0..1 effort
# fraction. These cancel out for a calibrated rig (the calibrate helper measures the same
# fraction through the same constants), so their exact value only sets the meaning of the
# pre-calibration DEFAULT thresholds below.
PRESENT_LOAD_FULLSCALE = 1000.0           # XL430-W250 'Present Load': +/-1000 = +/-100% PWM.
XL330_PRESENT_CURRENT_FULLSCALE = 1750.0  # XL330-M288 ~Current Limit max (raw counts, 1 mA/LSB).

# Per-joint native force signal for the OMX-X follower, matching omx_f.ros2_control.xacro's
# per-joint servo model (XL430-W250 base joints expose Present Load; XL330-M288 wrist joints
# expose Present Current). If the follower BOM changes, update this map AND the xacro/yaml
# together. Value = (gpio state-interface name, full-scale used to normalize to a fraction).
JOINT_EFFORT_SIGNAL = {
    'joint1': ('Present Load', PRESENT_LOAD_FULLSCALE),
    'joint2': ('Present Load', PRESENT_LOAD_FULLSCALE),
    'joint3': ('Present Load', PRESENT_LOAD_FULLSCALE),
    'joint4': ('Present Current', XL330_PRESENT_CURRENT_FULLSCALE),
    'joint5': ('Present Current', XL330_PRESENT_CURRENT_FULLSCALE),
}


def effort_fraction_from_values(joint, name_to_val):
    """Normalize a joint's native gpio force signal to a 0..1 effort fraction.

    `name_to_val` is the per-joint mapping {interface_name: value} taken from one
    DynamicJointState sample. Reads the joint's expected signal from JOINT_EFFORT_SIGNAL and
    returns |raw| / full_scale. If that signal is absent it falls back to whichever force
    signal the joint actually published (robust to a servo-model swap). Returns None when the
    joint has no recognized force signal at all — the caller treats a missing effort as "no
    reading", not as zero, so a sensor dropout cannot itself trip a stop.

    Pure function (no ROS); kept here so the monitor and the unit tests share one definition.
    NOTE: calibrate_collision_currents.py duplicates this logic (it runs in the
    open_manipulator image, which has no physical_ai_server package to import) — keep in sync.
    """
    sig, fullscale = JOINT_EFFORT_SIGNAL.get(joint, (None, None))
    raw = name_to_val.get(sig) if sig else None
    if raw is None:
        for alt_sig, alt_fs in (
            ('Present Current', XL330_PRESENT_CURRENT_FULLSCALE),
            ('Present Load', PRESENT_LOAD_FULLSCALE),
        ):
            if alt_sig in name_to_val:
                raw, fullscale = name_to_val[alt_sig], alt_fs
                break
    if raw is None or not fullscale:
        return None
    # Present Load is a SIGNED 16-bit register that the gpio broadcaster publishes UNSIGNED
    # (e.g. -5 arrives as 65531); Present Current may arrive either way. Map the upper half
    # [32768, 65535] back to negative before taking the magnitude, else a tiny negative load
    # reads as a huge effort and false-trips a stationary joint. Confirmed on-rig 2026-06-03:
    # dxl11 Present Load at rest = 65531 (== -5).
    v = float(raw)
    if v >= 32768.0:
        v -= 65536.0
    return abs(v) / float(fullscale)


# Per-joint default effort-fraction thresholds (joint1..joint5 == dxl11..dxl15), CALIBRATED
# 2026-06-03 on the reference OMX-X rig (35 s no-contact full-workspace sweep incl. extended
# gravity holds; threshold = clamp(p95 * 1.5, 0.30, 0.95)). Measured p95 envelope: J1=0.08,
# J2=0.44 (shoulder carries the whole arm -> by far the highest), J3=0.26, J4=0.04, J5=0.02.
# Servos + friction are identical across classroom rigs, so this calibration generalizes; an
# operator can still refine per-rig with calibrate_collision_currents.py (runbook Step 3).
DEFAULT_EFFORT_THRESHOLDS = (0.30, 0.65, 0.40, 0.30, 0.30)
# Per-joint threshold env var names, spelled out in full (NOT built with an f-string) so
# ci.yml::env-forwarding-guard, which greps source for literal env-var tokens, sees each
# complete name and matches it against the compose `environment:` list.
EFFORT_ENV_VARS = (
    'EDUBOTICS_COLLISION_EFFORT_J1',
    'EDUBOTICS_COLLISION_EFFORT_J2',
    'EDUBOTICS_COLLISION_EFFORT_J3',
    'EDUBOTICS_COLLISION_EFFORT_J4',
    'EDUBOTICS_COLLISION_EFFORT_J5',
)
DEFAULT_VELOCITY_GATE = 0.05      # rad/s — "joint is effectively not moving" below this
DEFAULT_DEBOUNCE_MS = 150
DEFAULT_USE_OVERLOAD_BIT = True
DEFAULT_ENABLED = True
DEFAULT_UPDATE_RATE_HZ = 100.0


@dataclass
class CollisionResult:
    """Outcome of one detector tick. `latched_overload` joints need a reboot_dxl on resume."""
    tripped: bool
    reason: str = ''
    joints: List[str] = field(default_factory=list)
    latched_overload: List[str] = field(default_factory=list)


class CollisionDetector:
    """Stateful per-joint over-force detector. One instance per follower arm.

    Works on a normalized effort fraction (0..1) per joint, so it is servo-model agnostic —
    the monitor does the per-model normalization (Present Load vs Present Current) upstream.

    Args:
        joint_names: arm joint identifiers in canonical order, e.g. ['joint1'..'joint5'].
        effort_thresholds: mapping joint -> trip threshold as an effort fraction in [0, 1].
        velocity_gate: rad/s; a joint with |velocity| <= this is treated as "not moving".
        debounce_ticks: consecutive bad ticks required before an over-force trip (>= 1).
        use_overload_bit: honor the firmware Overload bit as an immediate hard trip.
        enabled: master switch; when False, update() is a no-op (one-variable rollback).
    """

    def __init__(self, joint_names, effort_thresholds, velocity_gate,
                 debounce_ticks, use_overload_bit=True, enabled=True):
        self._joint_names = list(joint_names)
        self._thresholds = dict(effort_thresholds)
        self._velocity_gate = abs(float(velocity_gate))
        self._debounce_ticks = max(1, int(debounce_ticks))
        self._use_overload_bit = bool(use_overload_bit)
        self._enabled = bool(enabled)
        self._bad_ticks = {j: 0 for j in self._joint_names}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def debounce_ticks(self) -> int:
        return self._debounce_ticks

    def reset(self) -> None:
        """Clear the per-joint debounce counters (call after a resume)."""
        for joint in self._bad_ticks:
            self._bad_ticks[joint] = 0

    def update(self,
               efforts: Mapping[str, float],
               velocities: Mapping[str, float],
               hw_error_bits: Mapping[str, int],
               mode_is_inference: bool) -> CollisionResult:
        """Process one sample of per-joint (effort_fraction[0..1], velocity[rad/s], hw_error_bits).

        A joint missing from `velocities` defaults to 0.0 (treated as not-moving) — this fails
        toward protection. A joint missing from `efforts` contributes no over-force for that
        tick (its debounce counter resets), so a sensor dropout cannot by itself trip a stop.
        Returns a CollisionResult.
        """
        # Rule §2: never act during inference; also honor the master kill switch.
        if not self._enabled or mode_is_inference:
            self.reset()
            return CollisionResult(tripped=False)

        tripped_joints: List[str] = []
        latched_overload: List[str] = []
        reasons: List[str] = []

        for joint in self._joint_names:
            eff = efforts.get(joint)
            vel = abs(float(velocities.get(joint, 0.0)))
            threshold = float(self._thresholds.get(joint, float('inf')))

            overload = (self._use_overload_bit
                        and bool(int(hw_error_bits.get(joint, 0)) & OVERLOAD_BIT))

            bad_tick = (eff is not None
                        and float(eff) >= threshold
                        and vel <= self._velocity_gate)
            if bad_tick:
                self._bad_ticks[joint] += 1
            else:
                self._bad_ticks[joint] = 0
            debounced = self._bad_ticks[joint] >= self._debounce_ticks

            if overload:
                latched_overload.append(joint)
                tripped_joints.append(joint)
                reasons.append(f'{joint}: Overload hardware error')
            elif debounced:
                tripped_joints.append(joint)
                reasons.append(
                    f'{joint}: over-force effort {float(eff):.2f} >= {threshold:.2f} '
                    f'while v={vel:.3f} rad/s'
                )

        return CollisionResult(
            tripped=bool(tripped_joints),
            reason='; '.join(reasons),
            joints=tripped_joints,
            latched_overload=latched_overload,
        )


def build_detector_from_env(getenv: Callable[[str, object], object],
                            joint_names: Sequence[str],
                            update_rate_hz: float = DEFAULT_UPDATE_RATE_HZ) -> CollisionDetector:
    """Build a CollisionDetector from the collision-guard environment variables.

    `getenv` is a (name, default) -> value callable (e.g. os.environ.get) injected so this is
    unit-testable with a dict-backed getter. Per-joint thresholds come from EFFORT_ENV_VARS
    (effort fractions in [0, 1]), mapped positionally onto `joint_names`. debounce_ms is
    converted to ticks against `update_rate_hz` (the gpio broadcaster's publish rate, normally
    the 100 Hz controller_manager update_rate).
    """
    def _get_float(name, default):
        raw = getenv(name, None)
        if raw is None or str(raw).strip() == '':
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning('Invalid %s=%r; using default %s', name, raw, default)
            return default

    def _get_bool(name, default):
        raw = getenv(name, None)
        if raw is None or str(raw).strip() == '':
            return default
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

    enabled = _get_bool('EDUBOTICS_COLLISION_ENABLED', DEFAULT_ENABLED)
    velocity_gate = _get_float('EDUBOTICS_COLLISION_VELOCITY_GATE', DEFAULT_VELOCITY_GATE)
    debounce_ms = _get_float('EDUBOTICS_COLLISION_DEBOUNCE_MS', DEFAULT_DEBOUNCE_MS)
    use_overload = _get_bool('EDUBOTICS_COLLISION_USE_OVERLOAD_BIT', DEFAULT_USE_OVERLOAD_BIT)

    thresholds: Dict[str, float] = {}
    for idx, joint in enumerate(joint_names):
        default = (DEFAULT_EFFORT_THRESHOLDS[idx]
                   if idx < len(DEFAULT_EFFORT_THRESHOLDS) else 0.60)
        if idx < len(EFFORT_ENV_VARS):
            thresholds[joint] = _get_float(EFFORT_ENV_VARS[idx], default)
        else:
            thresholds[joint] = default

    if update_rate_hz <= 0:
        update_rate_hz = DEFAULT_UPDATE_RATE_HZ
    debounce_ticks = max(1, round((debounce_ms / 1000.0) * update_rate_hz))

    return CollisionDetector(
        joint_names=joint_names,
        effort_thresholds=thresholds,
        velocity_gate=velocity_gate,
        debounce_ticks=debounce_ticks,
        use_overload_bit=use_overload,
        enabled=enabled,
    )
