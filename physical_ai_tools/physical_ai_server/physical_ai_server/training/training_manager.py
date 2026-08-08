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

from physical_ai_server.data_processing import dataset_paths


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
        """Return the directory downloaded model checkpoints actually land in.

        DELEGATES to ``dataset_paths.model_root()``, which is the single source
        of truth shared with the downloader and with React's
        ``POLICY_MODEL_PATH``.

        It used to derive ``<lerobot_install_dir>/outputs/train`` from
        ``lerobot.__file__``. That named the pip site-packages install, which
        NOTHING writes to — ``data_manager.download_huggingface_repo`` has
        written to ``~/ros2_ws/outputs/train`` since v2.5.0, precisely because
        the image build ``rm -rf``'s the vendored lerobot tree. Both readers
        here (``get_model_weight_list_callback`` and
        ``get_training_info_callback``) were therefore looking in an empty
        directory, and once the browse confinement derived its model root from
        this function too, „Modellpfad auswählen" started refusing outright.
        """
        return dataset_paths.model_root()
