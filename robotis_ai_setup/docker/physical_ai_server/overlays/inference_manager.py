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

        # If the caller asked for CUDA but it's unavailable, fail loud instead of
        # letting PyTorch silently no-op `.to('cuda')` and run inference on CPU
        # (which drops 30 Hz to <5 Hz and makes the arm lag its tick tolerance).
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError(
                'Inferenz benoetigt eine CUDA-faehige GPU, aber keine wurde '
                'gefunden. Bitte pruefen: NVIDIA-Treiber auf dem Windows-Host, '
                '`nvidia-smi` in der WSL2-Distro, docker-compose.gpu.yml aktiv.'
            )
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
        # After this many seconds of frozen frames we halt inference entirely —
        # otherwise the policy keeps commanding motion based on a dead camera.
        self._stale_halt_threshold = 5.0
        self._last_stale_warn_time: dict[str, float] = {}
        # Per-joint safety envelope for the predicted action. Overridden by
        # `set_action_limits()` once the policy's joint_order is known.
        self._action_min: np.ndarray | None = None
        self._action_max: np.ndarray | None = None
        self._action_max_delta: np.ndarray | None = None  # max change per tick
        self._last_action: np.ndarray | None = None

    def set_action_limits(
            self,
            joint_min: list[float] | None = None,
            joint_max: list[float] | None = None,
            max_delta_per_tick: list[float] | None = None) -> None:
        """Configure the safety envelope applied to every predicted action.

        All values are per-joint in the same order the policy produces. None for
        a given array skips that check. Called by the ROS node once it knows
        the robot's joint_order.
        """
        self._action_min = np.asarray(joint_min, dtype=np.float32) if joint_min else None
        self._action_max = np.asarray(joint_max, dtype=np.float32) if joint_max else None
        self._action_max_delta = (
            np.asarray(max_delta_per_tick, dtype=np.float32) if max_delta_per_tick else None
        )

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

    def _check_stale_cameras(self, images: dict[str, np.ndarray]) -> str | None:
        """Detect cameras that stopped publishing by comparing image hashes.

        Returns the name of the first camera that's been frozen past
        `_stale_halt_threshold` so the caller can halt inference; returns None
        otherwise. Warnings are still printed at the lower `_stale_threshold`
        so the operator gets an early signal before the halt fires.
        """
        now = time.monotonic()
        halt_on: str | None = None
        for name, img in images.items():
            h = hash(img.data.tobytes()[:1024])  # hash first 1KB for speed
            prev = self._last_image_hashes.get(name)
            if prev != h:
                self._last_image_hashes[name] = h
                self._last_image_change_time[name] = now
                continue
            last_change = self._last_image_change_time.get(name, now)
            stale_duration = now - last_change
            if stale_duration > self._stale_halt_threshold and halt_on is None:
                halt_on = name
            elif stale_duration > self._stale_threshold:
                last_warn = self._last_stale_warn_time.get(name, 0)
                if now - last_warn > self._stale_warn_interval:
                    print(
                        f'[WARNUNG] Kamera "{name}" liefert seit '
                        f'{stale_duration:.1f}s dasselbe Bild — '
                        f'Verbindung pruefen!',
                        flush=True,
                    )
                    self._last_stale_warn_time[name] = now
        return halt_on

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
                # Previously raised — but raising from a ROS timer callback
                # may tear down the executor. Log, skip this tick, return None
                # so the caller doesn't publish stale actions to the arm.
                expected_names = [k.replace('observation.images.', '') for k in self._expected_image_keys]
                connected_names = list(images.keys())
                print(
                    f'[FEHLER] Kamera-Namen passen nicht: Modell erwartet '
                    f'{expected_names}, verbunden {connected_names}. '
                    f'Inferenz-Tick uebersprungen.',
                    flush=True,
                )
                return None

        stale_camera = self._check_stale_cameras(images)
        if stale_camera is not None:
            # Frozen camera = policy is acting on a dead scene. Refuse to
            # publish a command rather than drive the arm blind.
            print(
                f'[STOPP] Kamera "{stale_camera}" ist seit >'
                f'{self._stale_halt_threshold:.0f}s eingefroren. '
                f'Inferenz angehalten — Kamera pruefen, dann neu starten.',
                flush=True,
            )
            return None

        observation = self._preprocess(images, state, task_instruction)

        # Validate image shapes match what the model was trained on.
        # A resolution mismatch (e.g. camera swapped, setting changed) would
        # crash the model's convolutional layers with an opaque shape error.
        if self._expected_image_shapes:
            for key, expected_shape in self._expected_image_shapes.items():
                if key in observation:
                    actual_shape = list(observation[key].shape[1:])  # drop batch dim
                    if expected_shape and actual_shape != expected_shape:
                        print(
                            f'[FEHLER] Bildaufloesung stimmt nicht ueberein: '
                            f'{key} hat Form {actual_shape}, '
                            f'Modell erwartet {expected_shape}. Tick uebersprungen.',
                            flush=True,
                        )
                        return None

        with torch.inference_mode():
            action = self.policy.select_action(observation)
            action = action.squeeze(0).to('cpu').numpy()

        # Safety envelope: NaN/inf guard + joint-limit clamp + per-tick delta
        # cap. A diverging policy (bad checkpoint, OOD observation) can emit
        # huge or NaN values; publishing those to /arm_controller causes a
        # violent hardware motion.
        action = self._apply_safety_envelope(action)
        if action is None:
            return None

        return action

    def _apply_safety_envelope(self, action: np.ndarray) -> np.ndarray | None:
        """Validate + clamp the predicted action before it reaches the arm.

        Returns the (possibly clamped) action, or None to skip publishing.
        """
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
                    # Announce once per distinct offending pattern to avoid
                    # spamming at 30 Hz.
                    diff = np.where(~np.isclose(clipped, action, atol=1e-6))[0]
                    print(
                        f'[WARNUNG] Vorhergesagte Aktion verletzt Gelenklimits '
                        f'an Indizes {diff.tolist()} — wird begrenzt.',
                        flush=True,
                    )
                action = clipped

        if self._action_max_delta is not None and self._last_action is not None:
            if len(action) == len(self._last_action):
                delta = action - self._last_action
                abs_delta = np.abs(delta)
                mask = abs_delta > self._action_max_delta
                if np.any(mask):
                    # Cap magnitude while preserving sign.
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
                        except (json.JSONDecodeError, IOError) as e:
                            # If config.json cannot be read, log the actual
                            # exception so the operator has a debugging trail.
                            print(f'[WARNUNG] config.json lesbar nicht in '
                                  f'{pretrained_model_path}: {e}', flush=True)

        return saved_policy_path, saved_policy_type
