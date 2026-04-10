#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Dongyun Kim

import os
import time

from lerobot.policies.pretrained import PreTrainedPolicy
import numpy as np
from physical_ai_server.utils.file_utils import read_json_file
import torch


class InferenceManager:

    def __init__(
            self,
            device: str = 'cuda'):

        self.device = device
        self.policy_type = None
        self.policy_path = None
        self.policy = None
        self._expected_image_keys = []
        self._expected_image_shapes = {}
        # Stale camera detection: track last-seen image hash per camera
        self._last_image_hashes: dict[str, int] = {}
        self._last_image_change_time: dict[str, float] = {}
        self._stale_warn_interval = 5.0  # warn every N seconds per camera
        self._stale_threshold = 2.0  # seconds before an image is considered stale
        self._last_stale_warn_time: dict[str, float] = {}

    def validate_policy(self, policy_path: str) -> bool:
        result_message = ''
        if not os.path.exists(policy_path) or not os.path.isdir(policy_path):
            result_message = f'Policy path {policy_path} does not exist or is not a directory.'
            return False, result_message

        config_path = os.path.join(policy_path, 'config.json')
        if not os.path.exists(config_path):
            result_message = f'config.json file does not exist in {policy_path}.'
            return False, result_message

        config = read_json_file(config_path)
        if (config is None or
                ('type' not in config and 'model_type' not in config)):
            result_message = f'config.json malformed or missing fields in {policy_path}.'
            return False, result_message

        available_policies = self.__class__.get_available_policies()
        policy_type = config.get('type') or config.get('model_type')
        if policy_type not in available_policies:
            result_message = f'Policy type {policy_type} is not supported.'
            return False, result_message

        self.policy_path = policy_path
        self.policy_type = policy_type
        return True, f'Policy {policy_type} is valid.'

    def load_policy(self):
        try:
            policy_cls = self._get_policy_class(self.policy_type)
            self.policy = policy_cls.from_pretrained(self.policy_path)
            self.policy.to(self.device)
            self.policy.eval()
            self.reset_policy()
            self._expected_image_keys = self._read_expected_image_keys()
            self._expected_image_shapes = self._read_expected_image_shapes()
            return True
        except Exception as e:
            print(f'Failed to load policy from {self.policy_path}: {e}')
            return False

    def reset_policy(self):
        """Reset policy state (action queue, temporal ensemble) between episodes."""
        if self.policy is not None and hasattr(self.policy, 'reset'):
            self.policy.reset()

    def _read_expected_image_keys(self) -> list[str]:
        """Read expected observation.images.* keys from the policy config."""
        try:
            config_path = os.path.join(self.policy_path, 'config.json')
            config = read_json_file(config_path)
            if config and 'input_features' in config:
                return [k for k in config['input_features'] if k.startswith('observation.images.')]
        except Exception:
            pass
        return []

    def _read_expected_image_shapes(self) -> dict[str, list[int]]:
        """Read expected image shapes from the policy config.

        Returns dict like {'observation.images.gripper': [3, 480, 640]}.
        """
        try:
            config_path = os.path.join(self.policy_path, 'config.json')
            config = read_json_file(config_path)
            if config and 'input_features' in config:
                return {
                    k: v.get('shape', [])
                    for k, v in config['input_features'].items()
                    if k.startswith('observation.images.') and isinstance(v, dict)
                }
        except Exception:
            pass
        return {}

    def _check_stale_cameras(self, images: dict[str, np.ndarray]) -> None:
        """Detect cameras that stopped publishing by comparing image hashes.

        If the same image bytes are received for longer than _stale_threshold
        seconds, print a warning. Does not block inference — the operator
        sees the warning and can investigate.
        """
        now = time.monotonic()
        for name, img in images.items():
            h = hash(img.data.tobytes()[:1024])  # hash first 1KB for speed
            prev = self._last_image_hashes.get(name)
            if prev != h:
                self._last_image_hashes[name] = h
                self._last_image_change_time[name] = now
            else:
                last_change = self._last_image_change_time.get(name, now)
                stale_duration = now - last_change
                if stale_duration > self._stale_threshold:
                    last_warn = self._last_stale_warn_time.get(name, 0)
                    if now - last_warn > self._stale_warn_interval:
                        print(
                            f'[WARNUNG] Kamera "{name}" liefert seit '
                            f'{stale_duration:.1f}s dasselbe Bild — '
                            f'Verbindung pruefen!',
                            flush=True,
                        )
                        self._last_stale_warn_time[name] = now

    def clear_policy(self):
        if hasattr(self, 'policy'):
            del self.policy
            self.policy = None
        else:
            print('No policy to clear.')

    def get_policy_config(self):
        return self.policy.config

    def predict(
            self,
            images: dict[str, np.ndarray],
            state: list[float],
            task_instruction: str = None) -> list:

        if self._expected_image_keys:
            provided = {f'observation.images.{k}' for k in images}
            missing = set(self._expected_image_keys) - provided

            if missing:
                expected_names = [k.replace('observation.images.', '') for k in self._expected_image_keys]
                connected_names = list(images.keys())
                raise RuntimeError(
                    f'Inferenz fehlgeschlagen: Das Modell erwartet die Kameras {expected_names}, '
                    f'aber verbunden sind nur {connected_names}. '
                    f'Bitte die Kamera-Namen in der Robot-Config an das Modell anpassen.'
                )

        self._check_stale_cameras(images)

        observation = self._preprocess(images, state, task_instruction)

        # Validate image shapes match what the model was trained on.
        # A resolution mismatch (e.g. camera swapped, setting changed) would
        # crash the model's convolutional layers with an opaque shape error.
        if self._expected_image_shapes:
            for key, expected_shape in self._expected_image_shapes.items():
                if key in observation:
                    actual_shape = list(observation[key].shape[1:])  # drop batch dim
                    if expected_shape and actual_shape != expected_shape:
                        raise RuntimeError(
                            f'Bildaufloesung stimmt nicht ueberein: '
                            f'{key} hat Form {actual_shape}, '
                            f'Modell erwartet {expected_shape}. '
                            f'Bitte Kamera-Aufloesung pruefen.'
                        )

        with torch.inference_mode():
            action = self.policy.select_action(observation)
            action = action.squeeze(0).to('cpu').numpy()

        return action

    def _preprocess(
            self,
            images: dict[str, np.ndarray],
            state: list,
            task_instruction: str = None) -> dict:

        observation = self._convert_images2tensors(images)
        observation['observation.state'] = self._convert_np2tensors(state)
        for key in observation.keys():
            observation[key] = observation[key].to(self.device)

        if task_instruction is not None:
            observation['task'] = [task_instruction]

        return observation

    def _convert_images2tensors(
            self,
            images: dict[str, np.ndarray]) -> dict[str, torch.Tensor]:

        processed_images = {}
        for key, value in images.items():
            image = torch.from_numpy(value)
            image = image.to(torch.float32) / 255
            image = image.permute(2, 0, 1)
            image = image.to(self.device, non_blocking=True)
            image = image.unsqueeze(0)
            processed_images['observation.images.' + key] = image

        return processed_images

    def _convert_np2tensors(
            self,
            data):
        if isinstance(data, list):
            data = np.array(data)
        tensor_data = torch.from_numpy(data)
        tensor_data = tensor_data.to(torch.float32)
        tensor_data = tensor_data.to(self.device, non_blocking=True)
        tensor_data = tensor_data.unsqueeze(0)

        return tensor_data

    def _get_policy_class(self, name: str) -> PreTrainedPolicy:
        if name == 'tdmpc':
            from lerobot.policies.tdmpc.modeling_tdmpc import TDMPCPolicy

            return TDMPCPolicy
        elif name == 'diffusion':
            from lerobot.policies.diffusion.modeling_diffusion import DiffusionPolicy

            return DiffusionPolicy
        elif name == 'act':
            from lerobot.policies.act.modeling_act import ACTPolicy

            return ACTPolicy
        elif name == 'vqbet':
            from lerobot.policies.vqbet.modeling_vqbet import VQBeTPolicy

            return VQBeTPolicy
        elif name == 'pi0':
            from lerobot.policies.pi0.modeling_pi0 import PI0Policy

            return PI0Policy
        elif name == 'pi0fast':
            from lerobot.policies.pi0fast.modeling_pi0fast import PI0FASTPolicy
            return PI0FASTPolicy
        elif name == 'smolvla':
            from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy
            return SmolVLAPolicy
        # TODO: Uncomment when GrootN1Policy is implemented
        # elif name == 'groot-n1':
        #     from Isaac.groot_n1.policies.groot_n1 import GrootN1Policy
        #     return GrootN1Policy
        else:
            raise NotImplementedError(
                f'Policy with name {name} is not implemented.')

    @staticmethod
    def get_available_policies() -> list[str]:
        return [
            'tdmpc',
            'diffusion',
            'act',
            'vqbet',
            'pi0',
            'pi0fast',
            'smolvla',
        ]

    @staticmethod
    def get_saved_policies():
        import os
        import json

        home_dir = os.path.expanduser('~')
        hub_dir = os.path.join(home_dir, '.cache/huggingface/hub')
        models_folder_list = [d for d in os.listdir(hub_dir) if d.startswith('models--')]

        saved_policy_path = []
        saved_policy_type = []

        for model_folder in models_folder_list:
            model_path = os.path.join(hub_dir, model_folder)
            snapshots_path = os.path.join(model_path, 'snapshots')

            # Check if snapshots directory exists
            if os.path.exists(snapshots_path) and os.path.isdir(snapshots_path):
                # Get list of folders inside snapshots directory
                snapshot_folders = [
                    d for d in os.listdir(snapshots_path)
                    if os.path.isdir(os.path.join(snapshots_path, d))
                ]

            # Check if pretrained_model folder exists in each snapshot folder
            for snapshot_folder in snapshot_folders:
                snapshot_path = os.path.join(snapshots_path, snapshot_folder)
                pretrained_model_path = os.path.join(snapshot_path, 'pretrained_model')

                # If pretrained_model folder exists, add to saved_policies
                if os.path.exists(pretrained_model_path) and os.path.isdir(pretrained_model_path):
                    config_path = os.path.join(pretrained_model_path, 'config.json')
                    if os.path.exists(config_path):
                        try:
                            with open(config_path, 'r') as f:
                                config = json.load(f)
                                if 'type' in config:
                                    saved_policy_path.append(pretrained_model_path)
                                    saved_policy_type.append(config['type'])
                                elif 'model_type' in config:
                                    saved_policy_path.append(pretrained_model_path)
                                    saved_policy_type.append(config['model_type'])
                        except (json.JSONDecodeError, IOError):
                            # If config.json cannot be read, store path only
                            print('File IO Errors : ', IOError)

        return saved_policy_path, saved_policy_type
