# Copyright 2026 EduBotics
#
# Teleoperation force/collision detector — PURE LOGIC, no rclpy/ROS imports.
#
# EduBotics teleop force/collision e-stop (a Rule §2 software safety guard, scoped to
# teleop/recording only). On the OpenMANIPULATOR-X follower the arm joints (dxl11-15) run
# position control with NO current limit, so a student pressing the arm into an object winds
# the motors toward stall current (~2.3 A on XM430-W350) with nothing stopping them but a slow
# firmware overload trip. This class detects that condition from per-joint Present Current.
#
# Detection rule (per joint, per tick):
#   * PRIMARY: |current| >= threshold AND |velocity| <= velocity_gate, sustained for
#     `debounce_ticks` consecutive ticks. The velocity gate is the crux — a blocked joint
#     draws high current while barely moving, whereas fast free teleop motion also draws
#     current but the joint IS moving. The debounce rejects transient acceleration spikes.
#   * BACKSTOP: the firmware Hardware Error Status "Overload" bit (0x20) → immediate hard trip.
#
# Gating: returns "not tripped" (and resets counters) when mode_is_inference is True (Rule §2:
# the inference action distribution must never be reshaped) or when disabled (master rollback).
#
# Kept free of rclpy so it unit-tests without the ROS stack (mirrors test_data_manager_*).

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

# Dynamixel X-series Hardware Error Status bitmask (control table addr 70).
OVERLOAD_BIT = 0x20

# Present Current raw count -> Ampere for XM430-W350 (2.69 mA / LSB, e-manual).
PRESENT_CURRENT_A_PER_LSB = 0.00269

# Conservative per-joint defaults in Amps (dxl11..dxl15). Base joints carry more gravity load
# than wrist joints, so they tolerate a higher floor before "forced against an object" is
# declared. e-manual-derived starting points (no-load ~0.07 A, stall ~2.3 A); the operator
# refines them per-rig with calibrate_collision_currents.py.
DEFAULT_CURRENT_THRESHOLDS_A = (1.5, 1.5, 1.2, 1.0, 1.0)
# Per-joint threshold env var names, spelled out in full (NOT built with an f-string) so
# ci.yml::env-forwarding-guard, which greps source for literal env-var tokens, sees each
# complete name and matches it against the compose `environment:` list. A computed/concatenated
# name would be seen by the guard as an incomplete token and wrongly flagged as un-forwarded.
CURRENT_ENV_VARS = (
    'EDUBOTICS_COLLISION_CURRENT_J1',
    'EDUBOTICS_COLLISION_CURRENT_J2',
    'EDUBOTICS_COLLISION_CURRENT_J3',
    'EDUBOTICS_COLLISION_CURRENT_J4',
    'EDUBOTICS_COLLISION_CURRENT_J5',
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

    Args:
        joint_names: gpio joint identifiers in canonical order, e.g. ['dxl11'..'dxl15'].
        current_thresholds: mapping joint -> trip threshold in Amps (absolute value).
        velocity_gate: rad/s; a joint with |velocity| <= this is treated as "not moving".
        debounce_ticks: consecutive bad ticks required before a current trip (>= 1).
        use_overload_bit: honor the firmware Overload bit as an immediate hard trip.
        enabled: master switch; when False, update() is a no-op (one-variable rollback).
    """

    def __init__(self, joint_names, current_thresholds, velocity_gate,
                 debounce_ticks, use_overload_bit=True, enabled=True):
        self._joint_names = list(joint_names)
        self._thresholds = dict(current_thresholds)
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
               currents: Mapping[str, float],
               velocities: Mapping[str, float],
               hw_error_bits: Mapping[str, int],
               mode_is_inference: bool) -> CollisionResult:
        """Process one sample of per-joint (current[A], velocity[rad/s], hw_error_bits).

        A joint missing from `velocities` defaults to 0.0 (treated as not-moving) — this
        fails toward protection, never toward a silent miss. Returns a CollisionResult.
        """
        # Rule §2: never act during inference; also honor the master kill switch.
        if not self._enabled or mode_is_inference:
            self.reset()
            return CollisionResult(tripped=False)

        tripped_joints: List[str] = []
        latched_overload: List[str] = []
        reasons: List[str] = []

        for joint in self._joint_names:
            cur = abs(float(currents.get(joint, 0.0)))
            vel = abs(float(velocities.get(joint, 0.0)))
            threshold = float(self._thresholds.get(joint, float('inf')))

            overload = (self._use_overload_bit
                        and bool(int(hw_error_bits.get(joint, 0)) & OVERLOAD_BIT))

            bad_tick = (cur >= threshold) and (vel <= self._velocity_gate)
            if bad_tick:
                self._bad_ticks[joint] += 1
            else:
                self._bad_ticks[joint] = 0
            debounced = self._bad_ticks[joint] >= self._debounce_ticks

            if overload:
                latched_overload.append(joint)
                tripped_joints.append(joint)
                reasons.append(f'{joint}: Overload hardware error ({cur:.2f} A)')
            elif debounced:
                tripped_joints.append(joint)
                reasons.append(
                    f'{joint}: overcurrent {cur:.2f} A >= {threshold:.2f} A '
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
    unit-testable with a dict-backed getter. Per-joint thresholds come from CURRENT_ENV_VARS,
    mapped positionally onto `joint_names`. debounce_ms is converted to ticks against
    `update_rate_hz` (the gpio broadcaster's publish rate, normally the 100 Hz
    controller_manager update_rate).
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
        default = (DEFAULT_CURRENT_THRESHOLDS_A[idx]
                   if idx < len(DEFAULT_CURRENT_THRESHOLDS_A) else 1.0)
        if idx < len(CURRENT_ENV_VARS):
            thresholds[joint] = _get_float(CURRENT_ENV_VARS[idx], default)
        else:
            thresholds[joint] = default

    if update_rate_hz <= 0:
        update_rate_hz = DEFAULT_UPDATE_RATE_HZ
    debounce_ticks = max(1, round((debounce_ms / 1000.0) * update_rate_hz))

    return CollisionDetector(
        joint_names=joint_names,
        current_thresholds=thresholds,
        velocity_gate=velocity_gate,
        debounce_ticks=debounce_ticks,
        use_overload_bit=use_overload,
        enabled=enabled,
    )
