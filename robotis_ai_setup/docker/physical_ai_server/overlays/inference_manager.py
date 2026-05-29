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
import platform

from lerobot.policies.factory import make_pre_post_processors
from lerobot.policies.pretrained import PreTrainedPolicy
from lerobot.utils.control_utils import predict_action
import numpy as np
from physical_ai_server.utils.file_utils import read_json_file
import torch


class InferenceManager:

    def __init__(
            self,
            device: str = 'cuda'):

        # NOTE: CUDA availability is checked in load_policy(), NOT here.
        # InferenceManager is constructed unconditionally in
        # PhysicalAIServer.init_ros_params() — including when the user is
        # only recording. Failing the constructor on a CPU-only/dev box
        # would also break recording mode, which doesn't need a GPU.
        self.device = device
        self.policy_type = None
        self.policy_path = None
        self.policy = None
        self.preprocessor = None
        self.postprocessor = None
        self._expected_image_keys = []

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
        # CUDA check moved here from __init__: failing the constructor
        # would also break recording-mode init (see __init__ comment).
        # Returning False keeps the ROS node alive so the operator can
        # see the German error in TaskStatus instead of a hard crash.
        if self.device == 'cuda' and not torch.cuda.is_available():
            print(
                '[FEHLER] Inferenz benötigt eine CUDA-fähige GPU, aber '
                'keine wurde gefunden. Bitte prüfen: NVIDIA-Treiber auf '
                'dem Windows-Host, `nvidia-smi` in der WSL2-Distro, '
                'docker-compose.gpu.yml aktiv.',
                flush=True,
            )
            return False
        try:
            policy_cls = self._get_policy_class(self.policy_type)
            self.policy = policy_cls.from_pretrained(self.policy_path)
            self.policy.to(self.device)
            self.policy.eval()
            # v0.5.1: load processor pipelines that were saved alongside the
            # checkpoint (policy_preprocessor.json + policy_postprocessor.json).
            # Without these, predict_action's preprocessor would have no
            # normalization stats and the policy output would be garbage
            # (upstream bug #3645 symptom: "mean is infinity").
            self.preprocessor, self.postprocessor = make_pre_post_processors(
                policy_cfg=self.policy.config,
                pretrained_path=self.policy_path,
            )
            print(
                f'Loaded processor pipeline: '
                f'pre={len(self.preprocessor.steps)} steps, '
                f'post={len(self.postprocessor.steps)} steps',
                flush=True,
            )
            self.reset_policy()
            self._expected_image_keys = self._read_expected_image_keys()
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

    def clear_policy(self):
        if hasattr(self, 'policy'):
            del self.policy
            self.policy = None
            self.preprocessor = None
            self.postprocessor = None
        else:
            print('No policy to clear.')

    def get_policy_config(self):
        return self.policy.config

    def predict(
            self,
            images: dict[str, np.ndarray],
            state: list[float],
            task_instruction: str = None) -> list:

        # Audit F10: drop EXTRA cameras the policy wasn't trained on so a
        # 1-camera-trained policy with 2 connected cameras doesn't crash
        # the policy on an unknown observation key. Pure correctness fix
        # — the policy still runs with the cameras it expects.
        if self._expected_image_keys:
            provided = {f'observation.images.{k}' for k in images}
            # A camera the policy REQUIRES but that isn't present would fail
            # deep inside predict_action with an opaque tensor-shape error.
            # Surface a clear German message instead so the operator knows to
            # check the camera connection and restart inference. This is input
            # validation (wrong observation keys), not a removed safety
            # envelope — the run fails either way; we only make it legible.
            missing = set(self._expected_image_keys) - provided
            if missing:
                missing_names = [
                    k.replace('observation.images.', '') for k in sorted(missing)
                ]
                raise RuntimeError(
                    f'Modell erwartet Kamera(s) {missing_names}, die nicht '
                    f'verfügbar sind. Bitte Kamera-Verbindung prüfen und die '
                    f'Inferenz neu starten.'
                )
            unexpected = provided - set(self._expected_image_keys)
            if unexpected:
                expected_names = [k.replace('observation.images.', '') for k in self._expected_image_keys]
                unexpected_names = [k.replace('observation.images.', '') for k in unexpected]
                print(
                    f'[WARNUNG] Zusätzliche Kameras werden ignoriert: '
                    f'{unexpected_names}, Modell erwartet nur {expected_names}.',
                    flush=True,
                )
                images = {
                    k: v for k, v in images.items()
                    if f'observation.images.{k}' in self._expected_image_keys
                }

        # v0.5.1: predict_action runs prepare_observation_for_inference +
        # preprocessor + policy.select_action + postprocessor under
        # torch.inference_mode() with optional autocast for CUDA. Pass raw
        # ndarrays keyed with the canonical 'observation.images.<cam>' /
        # 'observation.state' names; predict_action handles channel-first,
        # /255, batch dim, and device move. Returns a torch.Tensor of shape
        # (1, action_dim).
        observation = {}
        for key, value in images.items():
            observation[f'observation.images.{key}'] = value
        observation['observation.state'] = (
            np.asarray(state, dtype=np.float32)
            if not isinstance(state, np.ndarray) else state.astype(np.float32)
        )

        action = predict_action(
            observation=observation,
            policy=self.policy,
            device=torch.device(self.device) if isinstance(self.device, str) else self.device,
            preprocessor=self.preprocessor,
            postprocessor=self.postprocessor,
            use_amp=getattr(self.policy.config, 'use_amp', False),
            task=task_instruction,
            robot_type=getattr(self, '_robot_type', None),
        )
        action = action.squeeze(0).cpu().numpy()

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
        elif name == 'pi0_fast':
            from lerobot.policies.pi0_fast.modeling_pi0_fast import PI0FastPolicy
            return PI0FastPolicy
        elif name == 'pi05':
            from lerobot.policies.pi05.modeling_pi05 import PI05Policy
            return PI05Policy
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
        # v0.5.1: pi0_fast (underscore rename) + new pi05 (Pi-0.5).
        # SmolVLA is hidden on aarch64 hosts (Jetson) due to upstream bug #3636:
        # SmolVLA on Jetson Orin ARM64 produces wrong actions vs x86_64 (pyav vs
        # torchcodec decoder discrepancy). Will re-enable when upstream fixes.
        policies = [
            'tdmpc',
            'diffusion',
            'act',
            'vqbet',
            'pi0',
            'pi0_fast',
            'pi05',
        ]
        if platform.machine() != 'aarch64':
            policies.append('smolvla')
        return policies

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
