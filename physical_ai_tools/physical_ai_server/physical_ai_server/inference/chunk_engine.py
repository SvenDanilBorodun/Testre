#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
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

"""Cloud-Inferenz chunk engine: one ACT model, one observation -> one action chunk (spec §2.7).

Shared, Modal-free, shipped twice like ``cloud_protocol.py``: inside physical_ai_server and as
/root/chunk_engine.py in the Modal image. The module top imports only json/os/time/numpy;
torch, LeRobot and cv2 are imported inside the functions that need them, so the pure validators
run in the deps-free CI suite.

* ``validate_model_dir`` reads the model's JSON files only and refuses, BEFORE LeRobot ever sees
  them, everything the cloud does not run (non-ACT, temporal ensembling, PEFT, more than one
  observation step) and every processor step LeRobot would import by class name or that is not
  on the allowlist (D12: the GPU container is shared, LeRobot imports any ``class`` a processor
  JSON names).
* ``decode_jpeg_rgb`` checks the JPEG's SOF size BEFORE decoding it (D25 / R-3).
* ``ChunkEngine.chunk`` mirrors ``lerobot.utils.control_utils.predict_action`` but asks the policy
  for the whole chunk and postprocesses it in ONE call (D26: bit-exact with per-row and with
  ``InferenceManager.predict`` called once per row).
"""
import json
import os
import time

import numpy as np

PRE_STEPS_ALLOWED = ('rename_observations_processor', 'to_batch_processor', 'device_processor',
                     'normalizer_processor')
POST_STEPS_ALLOWED = ('unnormalizer_processor', 'device_processor')
MODEL_REFUSED_KINDS = ('model', 'model_type')

_IMAGE_PREFIX = 'observation.images.'
_STATE_KEY = 'observation.state'
_ACTION_KEY = 'action'
_MAX_CHUNK_SIZE = 1000          # ready.policy bounds (cloud_protocol.parse_ready)
_MAX_IMAGE_SIDE = 8192
_FPS_MIN, _FPS_MAX = 1, 255     # ready.policy.trained_fps bounds (cloud_protocol.parse_ready)
_MISSING = object()


class ModelRefused(Exception):
    """A model the cloud does not run. ``kind`` is the wire error code: 'model' or 'model_type'.

    ``str(e)`` is an English detail for logs, never shown to a student.
    """

    def __init__(self, kind, detail=''):
        if kind not in MODEL_REFUSED_KINDS:
            raise ValueError(f'unknown ModelRefused kind: {kind!r}')
        super().__init__(f'{kind}: {detail}' if detail else kind)
        self.kind = kind
        self.detail = detail


def _protocol():
    """``cloud_protocol``: the package sibling on the robot, the top-level module in the Modal image."""
    if __package__:
        from . import cloud_protocol
    else:
        import cloud_protocol
    return cloud_protocol


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return _MISSING


def _is_int(v, lo, hi):
    return type(v) is int and lo <= v <= hi


def _shape(feature):
    if not isinstance(feature, dict):
        return None
    shape = feature.get('shape')
    if type(shape) is not list or not all(type(x) is int for x in shape):
        return None
    return shape


def _check_steps(pipeline, allowed, label):
    if not isinstance(pipeline, dict) or type(pipeline.get('steps')) is not list:
        raise ModelRefused('model', f'{label}: no steps list')
    for step in pipeline['steps']:
        if not isinstance(step, dict):
            raise ModelRefused('model', f'{label}: a step is not an object')
        if 'class' in step:
            raise ModelRefused('model', f'{label}: a step names a class')
        name = step.get('registry_name')
        if type(name) is not str or name not in allowed:
            raise ModelRefused('model', f'{label}: step not on the allowlist')


def _trained_fps(meta):
    """``meta.dataset_fps`` by ``InferenceManager.read_policy_summary``'s rule (bool / non-number /
    <= 0 -> None); additionally None outside the wire's 1..255, so the ready frame always parses."""
    if not isinstance(meta, dict):
        return None
    fps = meta.get('dataset_fps')
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not fps > 0:
        return None
    if not _FPS_MIN <= fps <= _FPS_MAX:
        return None
    return fps


def validate_model_dir(model_dir):
    """The ``ready.policy`` summary (§2.2) of an ACT model directory, or ``ModelRefused``.

    Reads config.json, policy_preprocessor.json, policy_postprocessor.json and (optional)
    edubotics_model_meta.json; imports nothing.
    """
    config = _read_json(os.path.join(model_dir, 'config.json'))
    if not isinstance(config, dict):
        raise ModelRefused('model', 'config.json missing or unreadable')

    policy_type = config.get('type') or config.get('model_type')
    if policy_type != 'act':
        raise ModelRefused('model_type', 'not act')
    if config.get('temporal_ensemble_coeff') is not None:
        raise ModelRefused('model_type', 'temporal ensembling')
    n_obs = config.get('n_obs_steps', 1)
    if type(n_obs) is not int or n_obs != 1:
        raise ModelRefused('model_type', 'n_obs_steps != 1')
    if config.get('use_peft') not in (None, False):
        raise ModelRefused('model_type', 'peft')

    P = _protocol()
    inputs = config.get('input_features')
    if not isinstance(inputs, dict):
        raise ModelRefused('model', 'input_features')
    cameras, shapes, state_dim = [], {}, None
    for key, feature in inputs.items():
        shape = _shape(feature)
        if key == _STATE_KEY:
            if shape is None or len(shape) != 1 or not _is_int(shape[0], 1, P.MAX_STATE_DIM):
                raise ModelRefused('model', 'state shape')
            state_dim = shape[0]
        elif type(key) is str and key.startswith(_IMAGE_PREFIX):
            name = key[len(_IMAGE_PREFIX):]
            if not P.NAME_RE.fullmatch(name) or not isinstance(feature, dict) or feature.get('type') != 'VISUAL':
                raise ModelRefused('model', 'camera feature')
            if (shape is None or len(shape) != 3 or shape[0] != 3
                    or not all(_is_int(x, 1, _MAX_IMAGE_SIDE) for x in shape[1:])):
                raise ModelRefused('model', 'visual shape')
            cameras.append(name)
            shapes[name] = list(shape)
        else:
            # Anything else (another VISUAL key, an environment state, ...) is an input the robot cannot send.
            raise ModelRefused('model', 'unsupported input feature')
    if state_dim is None:
        raise ModelRefused('model', 'no observation.state')
    if not 1 <= len(cameras) <= P.MAX_CAMERAS:
        raise ModelRefused('model', 'camera count')

    outputs = config.get('output_features')
    if not isinstance(outputs, dict) or set(outputs) != {_ACTION_KEY}:
        raise ModelRefused('model', 'output_features')
    action_shape = _shape(outputs[_ACTION_KEY])
    if action_shape is None or len(action_shape) != 1 or not _is_int(action_shape[0], 1, P.MAX_ACTION_DIM):
        raise ModelRefused('model', 'action shape')

    chunk_size = config.get('chunk_size')
    n_action_steps = config.get('n_action_steps')
    if not _is_int(chunk_size, 1, _MAX_CHUNK_SIZE) or not _is_int(n_action_steps, 1, _MAX_CHUNK_SIZE):
        raise ModelRefused('model', 'chunk_size / n_action_steps')

    pre = _read_json(os.path.join(model_dir, 'policy_preprocessor.json'))
    post = _read_json(os.path.join(model_dir, 'policy_postprocessor.json'))
    if pre is _MISSING or post is _MISSING:
        raise ModelRefused('model', 'processor json missing or unreadable')
    _check_steps(pre, PRE_STEPS_ALLOWED, 'preprocessor')
    _check_steps(post, POST_STEPS_ALLOWED, 'postprocessor')

    meta = _read_json(os.path.join(model_dir, 'edubotics_model_meta.json'))
    return {
        'type': 'act',
        'expected_cameras': cameras,
        'image_shapes': shapes,
        'trained_fps': _trained_fps(meta),
        'chunk_size': chunk_size,
        'n_action_steps': n_action_steps,
        'state_dim': state_dim,
        'action_dim': action_shape[0],
        'requires_task': False,
    }


def decode_jpeg_rgb(data, want_w, want_h):
    """The RGB uint8 (want_h, want_w, 3) image of a JPEG, or None (D25 / R-3).

    In this order: the SOF size must be (want_w, want_h) — checked WITHOUT decoding, so a small
    file announcing 8192 x 8192 never reaches the decoder; then ``cv2.imdecode`` inside ``try``
    (the Modal image sets OPENCV_IO_MAX_IMAGE_PIXELS, under which a bigger image raises cv2.error
    instead of allocating); a None or differently shaped result is refused too.
    """
    if _protocol().jpeg_size(data) != (want_w, want_h):
        return None
    import cv2
    try:
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return None
    if img is None or img.shape != (want_h, want_w, 3):
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


class ChunkEngine:
    """One loaded ACT model. ``chunk`` is called on ONE thread (the server's GPU thread)."""

    def __init__(self, policy, preprocessor, postprocessor, device, summary):
        self.policy = policy
        self.preprocessor = preprocessor
        self.postprocessor = postprocessor
        self.device = device
        self.summary = summary

    @classmethod
    def load(cls, model_dir, device):
        """Validate (``ModelRefused``), then load the policy and its processor pipelines on ``device``."""
        summary = validate_model_dir(model_dir)
        from lerobot.policies.act.modeling_act import ACTPolicy
        from lerobot.policies.factory import make_pre_post_processors
        policy = ACTPolicy.from_pretrained(model_dir)
        policy.to(device)
        policy.eval()
        preprocessor, postprocessor = make_pre_post_processors(
            policy_cfg=policy.config,
            pretrained_path=model_dir,
            preprocessor_overrides={'device_processor': {'device': device}},
        )
        return cls(policy, preprocessor, postprocessor, device, summary)

    def chunk(self, images, state, n):
        """``(actions float32 [n, action_dim], gpu_ms)`` for one observation.

        ``images``: {camera name: uint8 HxWx3 RGB}; ``state``: float32 [state_dim]. Mirrors
        ``predict_action``: ``torch.inference_mode()`` and autocast iff the device is cuda and the
        policy's ``use_amp``; ``prepare_observation_for_inference`` + preprocessor; the policy's
        whole chunk cut to ``n`` rows; ONE postprocessor call. ``gpu_ms`` is the wall time of the
        whole call (prepare + pre + model + post + copy), after a cuda synchronize.
        """
        import contextlib

        import torch
        from lerobot.policies.utils import prepare_observation_for_inference

        if type(n) is not int or not 1 <= n <= self.summary['chunk_size']:
            raise ValueError('n out of range')
        device = torch.device(self.device)
        cuda = device.type == 'cuda'
        t0 = time.perf_counter()
        amp = (torch.autocast(device_type=device.type)
               if cuda and getattr(self.policy.config, 'use_amp', False) else contextlib.nullcontext())
        with torch.inference_mode(), amp:
            observation = {f'{_IMAGE_PREFIX}{name}': img for name, img in images.items()}
            observation[_STATE_KEY] = np.asarray(state, dtype=np.float32)
            observation = self.preprocessor(prepare_observation_for_inference(observation, device, None, None))
            chunk = self.policy.predict_action_chunk(observation)[:, :n, :]
            out = self.postprocessor(chunk)
            actions = out[0].float().cpu().numpy().astype('<f4')
        if cuda:
            torch.cuda.synchronize()
        return actions, (time.perf_counter() - t0) * 1000.0
