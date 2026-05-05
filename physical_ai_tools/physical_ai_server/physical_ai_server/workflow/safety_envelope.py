#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Safety envelope shared by the inference path and the Roboter Studio
workflow runtime.

Extracted from ``inference/inference_manager.py`` so the workflow path can
reuse the exact same NaN-guard / joint-clamp / per-tick-delta-cap logic
without re-implementing it. Both call sites instantiate their own
``SafetyEnvelope`` and configure it via ``set_action_limits``; state
(``_last_action``) is intentionally per-instance so the two paths can be
in different modes simultaneously.

Behaviour MUST stay byte-identical to the pre-extraction version of
``_apply_safety_envelope`` — the parity test in
``test_safety_envelope_parity.py`` records a reference action sequence
against the original implementation and replays it through the extracted
class.
"""

from __future__ import annotations

import numpy as np


class SafetyEnvelope:
    """Stateful action validator + clamper. Call order per tick:

    1. ``apply(action)`` returns either the clamped action or None to
       skip publishing.
    2. The caller publishes the returned ndarray.
    3. The next call sees ``_last_action`` set to the previous return.
    """

    def __init__(self) -> None:
        self._action_min: np.ndarray | None = None
        self._action_max: np.ndarray | None = None
        self._action_max_delta: np.ndarray | None = None
        self._last_action: np.ndarray | None = None
        self._warned_action_shape: bool = False

    def set_action_limits(
        self,
        joint_min: list[float] | None = None,
        joint_max: list[float] | None = None,
        max_delta_per_tick: list[float] | None = None,
    ) -> None:
        """Configure the per-joint clamp and per-tick delta cap.

        ``None`` for any of the three lists disables that specific check.
        Reset on every reconfigure so a robot type change with a new joint
        count emits a fresh shape-mismatch warning.
        """
        self._action_min = np.asarray(joint_min, dtype=np.float32) if joint_min else None
        self._action_max = np.asarray(joint_max, dtype=np.float32) if joint_max else None
        self._action_max_delta = (
            np.asarray(max_delta_per_tick, dtype=np.float32) if max_delta_per_tick else None
        )
        self._warned_action_shape = False

    def reset(self) -> None:
        """Drop the last-action memory (e.g. when starting a new run)."""
        self._last_action = None

    def apply(self, action: np.ndarray) -> np.ndarray | None:
        """Validate + clamp ``action``. Returns the (possibly clamped)
        action, or ``None`` to skip publishing this tick."""
        if not np.all(np.isfinite(action)):
            print(
                '[STOPP] Modell hat NaN/Inf-Werte ausgegeben. Tick verworfen.',
                flush=True,
            )
            return None

        if self._action_min is not None and self._action_max is not None:
            if len(action) == len(self._action_min):
                clipped = np.clip(action, self._action_min, self._action_max)
                if not np.allclose(clipped, action, atol=1e-6):
                    diff = np.where(~np.isclose(clipped, action, atol=1e-6))[0]
                    print(
                        f'[WARNUNG] Vorhergesagte Aktion verletzt Gelenklimits '
                        f'an Indizes {diff.tolist()} — wird begrenzt.',
                        flush=True,
                    )
                action = clipped
            else:
                if not self._warned_action_shape:
                    print(
                        f'[WARNUNG] Aktion hat {len(action)} Werte, '
                        f'Gelenklimits sind fuer {len(self._action_min)} '
                        f'konfiguriert — Limits werden NICHT erzwungen. '
                        f'Bitte set_action_limits() in physical_ai_server.py '
                        f'auf den aktiven Roboter abstimmen.',
                        flush=True,
                    )
                    self._warned_action_shape = True

        if self._action_max_delta is not None and self._last_action is not None:
            # Pre-existing audit fix: previously this branch only
            # guarded ``len(action) == len(self._last_action)``, which
            # let the broadcast against ``_action_max_delta`` blow up
            # when those two shapes differed. Skip the delta cap if
            # ANY of the three shapes mismatch — same reasoning as
            # the joint-limit branch above.
            if (
                len(action) == len(self._last_action)
                and len(action) == len(self._action_max_delta)
            ):
                delta = action - self._last_action
                abs_delta = np.abs(delta)
                mask = abs_delta > self._action_max_delta
                if np.any(mask):
                    delta = np.where(
                        mask,
                        np.sign(delta) * self._action_max_delta,
                        delta,
                    )
                    action = self._last_action + delta
                    print(
                        f'[WARNUNG] Aktions-Schrittweite begrenzt an Indizes '
                        f'{np.where(mask)[0].tolist()}.',
                        flush=True,
                    )

        self._last_action = action.copy()
        return action
