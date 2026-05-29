#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
# Modified 2026-05 for EduBotics v2.5.0 — on-device training removed; Modal Cloud handles training.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stub TrainingManager — holds two static helpers used by inference callbacks.

On-device training was removed in EduBotics v2.5.0 because all classroom
training runs on Modal cloud workers. This module is kept ONLY to back
two static methods that other ROS service callbacks still call:

- ``get_weight_save_root_path()`` — local on-disk directory where downloaded
  LeRobot model checkpoints live (read by the inference / model-list paths).
- ``get_available_list()`` — list of policy types + execution devices that
  populate the React UI dropdown. Updated for LeRobot v0.5.1 (note the
  ``pi0_fast`` underscore rename and the new ``pi05`` policy).

The full Trainer / draccus / TrainPipelineConfig machinery is gone.
"""

from pathlib import Path

import lerobot


class TrainingManager:
    """Two static helpers — kept to avoid touching every call site in Phase 1.

    Will be flattened into a ``physical_ai_server/utils/lerobot_paths.py`` module
    in a follow-up cleanup once the Phase 4 Docker rebuild lands.
    """

    @staticmethod
    def get_available_list() -> tuple[list[str], list[str]]:
        policy_list = [
            'tdmpc',
            'diffusion',
            'act',
            'vqbet',
            'pi0',
            'pi0_fast',
            'pi05',
            'smolvla',
        ]
        device_list = ['cuda', 'cpu']
        return policy_list, device_list

    @staticmethod
    def get_weight_save_root_path() -> Path:
        """Return ``<lerobot_install_dir>/outputs/train`` resolved absolutely.

        This is the local directory where downloaded model checkpoints land.
        Inference / model-list callbacks consume it; training no longer writes here
        (Modal Cloud uploads checkpoints to HF Hub, students download via the
        HfApiWorker into the standard HuggingFace cache).
        """
        lerobot_file_path = Path(lerobot.__file__).resolve()
        lerobot_dirs = [p for p in lerobot_file_path.parents if p.name == 'lerobot']
        base = lerobot_dirs[-1] if lerobot_dirs else lerobot_file_path.parent.parent
        return (base / 'outputs' / 'train').resolve()
