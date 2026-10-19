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
* ``validate_processor_steps`` is the ROBOT's allowlist (RunPod training spec §4.10), run by
  ``InferenceManager.load_policy`` before LeRobot sees a model: the robot runs every policy the
  student may train (ACT and the VLAs), so its step lists are the union over LeRobot's eight
  processor factories, and the two tokenizer steps that load a hub repo with
  ``trust_remote_code=True`` are pinned to the names LeRobot's own factories write. It is
  additive: ``validate_model_dir`` (the cloud's ACT-only check) is unchanged.
"""
import json
import os
import time

import numpy as np

PRE_STEPS_ALLOWED = ('rename_observations_processor', 'to_batch_processor', 'device_processor',
                     'normalizer_processor')
POST_STEPS_ALLOWED = ('unnormalizer_processor', 'device_processor')
MODEL_REFUSED_KINDS = ('model', 'model_type')

# The robot's allowlist (RunPod training spec §4.10): the union of the steps LeRobot 0.5.1's eight
# policy processor factories write (act, diffusion, vqbet, tdmpc, pi0, pi0_fast, pi05, smolvla).
ROBOT_PRE_STEPS_ALLOWED = ('rename_observations_processor', 'to_batch_processor', 'device_processor',
                           'normalizer_processor', 'smolvla_new_line_processor', 'pi0_new_line_processor',
                           'tokenizer_processor', 'delta_actions_processor',
                           'pi05_prepare_state_tokenizer_processor_step',
                           'pi0_fast_prepare_state_tokenizer_processor_step', 'action_tokenizer_processor')
ROBOT_POST_STEPS_ALLOWED = ('unnormalizer_processor', 'device_processor', 'absolute_actions_processor')
# The only repos the two hub-loading steps may name: tokenizer_processor (pi0, pi05, pi0_fast use
# paligemma, smolvla its VLM) and action_tokenizer_processor (pi0_fast's FAST tokenizer, loaded with
# trust_remote_code=True by default, plus its paligemma tokenizer).
ROBOT_TOKENIZER_NAMES = ('google/paligemma-3b-pt-224', 'HuggingFaceTB/SmolVLM2-500M-Video-Instruct')
ROBOT_ACTION_TOKENIZER_NAMES = ('lerobot/fast-action-tokenizer',)
# S17 of the training spec §3.7, duplicated from cloud_training_api's training_texts_de (lockstep-tested).
PROCESSOR_STEPS_REFUSED_DE = ('Das Modell enthält nicht erlaubte Verarbeitungsschritte und wird aus '
                              'Sicherheitsgründen nicht geladen.')
_PALIGEMMA_TOKENIZER = 'google/paligemma-3b-pt-224'
_FAST_ACTION_TOKENIZER = 'lerobot/fast-action-tokenizer'
_PROCESSOR_JSON_MAX_BYTES = 1024 * 1024
_STATE_FILE_RE = r'[A-Za-z0-9_.-]+\.safetensors'
_PROCESSOR_FILES = (('policy_preprocessor.json', ROBOT_PRE_STEPS_ALLOWED),
                    ('policy_postprocessor.json', ROBOT_POST_STEPS_ALLOWED))

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


class ProcessorStepsRefused(Exception):
    """A model the robot refuses to load. ``str(e)`` is an English detail for logs (it never names
    the model's path); the student sees ``PROCESSOR_STEPS_REFUSED_DE``."""


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


def _repo_root(model_dir):
    """The directory every state file must resolve inside (SR-17).

    The nearest LEXICAL ancestor of ``model_dir`` named ``models--*`` when there is one: a Hugging
    Face cache snapshot (``models--<org>--<name>/snapshots/<sha>/pretrained_model``), whose files are
    symlinks into that repo's ``blobs/``. Else ``model_dir`` itself.
    """
    from pathlib import Path

    here = Path(os.path.abspath(model_dir))
    for candidate in (here, *here.parents):
        if candidate.name.startswith('models--'):
            return candidate
    return here


def _read_processor_json(path, label):
    try:
        size = os.path.getsize(path)
    except OSError:
        raise ProcessorStepsRefused(f'{label}: missing') from None
    if size > _PROCESSOR_JSON_MAX_BYTES:
        raise ProcessorStepsRefused(f'{label}: larger than 1 MiB')
    data = _read_json(path)
    if data is _MISSING:
        raise ProcessorStepsRefused(f'{label}: unreadable')
    return data


def _check_state_file(model_dir, root, value, label):
    import re
    from pathlib import Path

    if type(value) is not str or not re.fullmatch(_STATE_FILE_RE, value):
        raise ProcessorStepsRefused(f'{label}: state_file is not a .safetensors basename')
    try:
        target = (Path(model_dir) / value).resolve(strict=True)
    except (OSError, RuntimeError):         # missing, dangling, a symlink loop
        raise ProcessorStepsRefused(f'{label}: state_file not on disk') from None
    # A file not on disk would make LeRobot fall back to a hub download; one outside the repo root
    # is another model's (or anybody's) bytes.
    if not target.is_file() or not target.is_relative_to(root):
        raise ProcessorStepsRefused(f'{label}: state_file is not a regular file inside the repo root')


def validate_processor_steps(model_dir):
    """``None``, or ``ProcessorStepsRefused`` for a model LeRobot would turn into foreign code.

    Run by ``InferenceManager.load_policy`` before ``from_pretrained`` and
    ``make_pre_post_processors``. Reads config.json and the two processor JSONs (each <= 1 MiB)
    and stats the state files; imports nothing from LeRobot. Refused:

    * a processor JSON that is missing, larger than 1 MiB or not ``{"steps": [<object>, ...]}``;
    * a step with a ``"class"`` key (LeRobot imports it and calls it with the step's config), a
      ``registry_name`` not in its pipeline's ``ROBOT_*_STEPS_ALLOWED``, or a non-object config;
    * ``tokenizer_processor`` naming a tokenizer outside ``ROBOT_TOKENIZER_NAMES``;
      ``action_tokenizer_processor`` naming one outside ``ROBOT_ACTION_TOKENIZER_NAMES`` or a
      paligemma tokenizer other than LeRobot's default (both load with ``trust_remote_code``);
    * a ``pi0_fast`` config.json whose ``action_tokenizer_name`` / ``text_tokenizer_name`` are not
      LeRobot's (``modeling_pi0_fast`` loads both with ``trust_remote_code=True``);
    * a ``state_file`` that is not a ``*.safetensors`` basename resolving to a regular file inside
      the repo root (``_repo_root``).

    Scope (stated, SR-28): this closes the processor-JSON and the pi0_fast tokenizer paths;
    draccus resolves a config's ``type`` through its registry only.
    """
    config = _read_json(os.path.join(model_dir, 'config.json'))
    if not isinstance(config, dict):
        raise ProcessorStepsRefused('config.json missing or not an object')
    if 'pi0_fast' in (config.get('type'), config.get('model_type')):
        if (config.get('action_tokenizer_name') != _FAST_ACTION_TOKENIZER
                or config.get('text_tokenizer_name') != _PALIGEMMA_TOKENIZER):
            raise ProcessorStepsRefused('pi0_fast config.json: a tokenizer name is not pinned')

    root = _repo_root(model_dir).resolve()
    for filename, allowed in _PROCESSOR_FILES:
        label = filename[:-len('.json')]
        pipeline = _read_processor_json(os.path.join(model_dir, filename), label)
        if not isinstance(pipeline, dict) or type(pipeline.get('steps')) is not list:
            raise ProcessorStepsRefused(f'{label}: no steps list')
        for step in pipeline['steps']:
            if not isinstance(step, dict):
                raise ProcessorStepsRefused(f'{label}: a step is not an object')
            if 'class' in step:
                raise ProcessorStepsRefused(f'{label}: a step names a class')
            name = step.get('registry_name')
            if type(name) is not str or name not in allowed:
                raise ProcessorStepsRefused(f'{label}: a step is not on the allowlist')
            step_config = step.get('config', {})
            if not isinstance(step_config, dict):
                raise ProcessorStepsRefused(f'{label}: a step config is not an object')
            if name == 'tokenizer_processor':
                tokenizer = step_config.get('tokenizer_name')
                if type(tokenizer) is not str or tokenizer not in ROBOT_TOKENIZER_NAMES:
                    raise ProcessorStepsRefused(f'{label}: tokenizer_name not pinned')
            elif name == 'action_tokenizer_processor':
                tokenizer = step_config.get('action_tokenizer_name')
                if type(tokenizer) is not str or tokenizer not in ROBOT_ACTION_TOKENIZER_NAMES:
                    raise ProcessorStepsRefused(f'{label}: action_tokenizer_name not pinned')
                if step_config.get('paligemma_tokenizer_name', _PALIGEMMA_TOKENIZER) != _PALIGEMMA_TOKENIZER:
                    raise ProcessorStepsRefused(f'{label}: paligemma_tokenizer_name not pinned')
            if 'state_file' in step:
                _check_state_file(model_dir, root, step['state_file'], label)


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
