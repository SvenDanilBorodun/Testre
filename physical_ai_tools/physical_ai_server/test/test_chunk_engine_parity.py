"""The cloud's chunk == the robot's local inference, bit for bit (spec §2.7, D26, R-23).

Image only: needs LeRobot 0.5.1 + torch (``docker-publish.yml::smoke-test`` runs it in
the amd64 and opi server images, where a skip fails the step). Every ACTConfig is
built with ``pretrained_backbone_weights=None``, so nothing is downloaded (R-23).

For a random-init ACT of the trained shape (2 cameras 3x480x640, state 6, action 6,
chunk 100, n_action_steps 15), saved with its processors and loaded both ways:

* ``ChunkEngine.chunk`` (the whole chunk, ONE postprocessor call) ==
  ``InferenceManager.predict`` called 15 times (the local path: ``select_action``'s queue,
  one postprocessor call per row) on the first 15 rows;
* == the same chunk postprocessed row by row, for MEAN_STD, MIN_MAX and IDENTITY.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('lerobot')

from lerobot.configs.types import FeatureType, NormalizationMode, PolicyFeature  # noqa: E402
from lerobot.policies.act.configuration_act import ACTConfig  # noqa: E402
from lerobot.policies.act.modeling_act import ACTPolicy  # noqa: E402
from lerobot.policies.factory import make_pre_post_processors  # noqa: E402

from physical_ai_server.inference.inference_manager import InferenceManager  # noqa: E402

try:   # the shipped package; an image older than the cloud files gets them on PYTHONPATH (spec §9.2)
    from physical_ai_server.inference import chunk_engine as CE
except ImportError:  # pragma: no cover - only against a pre-Cloud-Inferenz image
    import chunk_engine as CE

CAMS = ('gripper', 'scene')
MODES = (NormalizationMode.MEAN_STD, NormalizationMode.MIN_MAX, NormalizationMode.IDENTITY)


def _save_model(path, mode=NormalizationMode.MEAN_STD, seed=0):
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    inp = {f'observation.images.{c}': PolicyFeature(type=FeatureType.VISUAL, shape=(3, 480, 640)) for c in CAMS}
    inp['observation.state'] = PolicyFeature(type=FeatureType.STATE, shape=(6,))
    out = {'action': PolicyFeature(type=FeatureType.ACTION, shape=(6,))}
    cfg = ACTConfig(input_features=inp, output_features=out, chunk_size=100, n_action_steps=15, device='cpu',
                    pretrained_backbone_weights=None,
                    normalization_mapping={'VISUAL': NormalizationMode.MEAN_STD, 'STATE': mode, 'ACTION': mode})
    policy = ACTPolicy(cfg).eval()
    stats = {}
    for key, feature in {**inp, **out}.items():
        lo = rng.random(feature.shape).astype(np.float32) - 1.0
        stats[key] = {'mean': torch.tensor(rng.random(feature.shape).astype(np.float32)),
                      'std': torch.tensor((rng.random(feature.shape) + 0.5).astype(np.float32)),
                      'min': torch.tensor(lo), 'max': torch.tensor(lo + 2.0)}
    pre, post = make_pre_post_processors(policy_cfg=cfg, dataset_stats=stats)
    path = str(path)
    policy.save_pretrained(path)
    pre.save_pretrained(path)
    post.save_pretrained(path)
    with open(os.path.join(path, 'edubotics_model_meta.json'), 'w') as f:
        json.dump({'dataset_fps': 30, 'policy_type': 'act'}, f)
    return path


def _observation(seed=1):
    rng = np.random.default_rng(seed)
    images = {c: (rng.random((480, 640, 3)) * 255).astype(np.uint8) for c in CAMS}
    state = rng.standard_normal(6).astype(np.float32)
    return images, state


def _local_rows(path, images, state, rows=15):
    im = InferenceManager(device='cpu')
    im.policy_path = path
    im.policy_type = 'act'
    assert im.load_policy()
    return np.stack([im.predict(images, state, task_instruction=None) for _ in range(rows)])


@pytest.fixture(scope='module')
def mean_std_model(tmp_path_factory):
    return _save_model(tmp_path_factory.mktemp('act_mean_std'))


def test_whole_chunk_equals_15_predict_calls(mean_std_model):
    images, state = _observation()
    local = _local_rows(mean_std_model, images, state)
    engine = CE.ChunkEngine.load(mean_std_model, 'cpu')
    actions, gpu_ms = engine.chunk(images, state, 50)
    assert actions.dtype == np.dtype('<f4') and actions.shape == (50, 6)
    assert gpu_ms > 0
    assert np.array_equal(actions[:15], local.astype('<f4'))
    assert engine.summary['expected_cameras'] == list(CAMS) and engine.summary['trained_fps'] == 30


@pytest.mark.parametrize('mode', MODES, ids=[m.value for m in MODES])
def test_whole_chunk_equals_per_row(tmp_path, mode):
    from lerobot.policies.utils import prepare_observation_for_inference

    path = _save_model(tmp_path, mode)
    images, state = _observation()
    engine = CE.ChunkEngine.load(path, 'cpu')
    whole, _ = engine.chunk(images, state, 50)
    with torch.inference_mode():
        obs = {f'observation.images.{k}': v for k, v in images.items()}
        obs['observation.state'] = state
        obs = engine.preprocessor(prepare_observation_for_inference(obs, torch.device('cpu'), None, None))
        ch = engine.policy.predict_action_chunk(obs)[:, :50, :]
        rows = torch.stack([engine.postprocessor(ch[:, j, :]) for j in range(50)], dim=1)[0].numpy()
    assert np.array_equal(whole, rows.astype('<f4'))
    assert np.array_equal(whole[:15], _local_rows(path, images, state).astype('<f4'))


def test_chunk_respects_n(mean_std_model):
    images, state = _observation(2)
    engine = CE.ChunkEngine.load(mean_std_model, 'cpu')
    full, _ = engine.chunk(images, state, 100)
    assert full.shape == (100, 6)
    for n in (1, 15, 50, 99):
        part, _ = engine.chunk(images, state, n)
        assert part.shape == (n, 6)
        assert np.array_equal(part, full[:n])
    for bad in (0, 101, -1, 1.0, True, None):
        with pytest.raises(ValueError):
            engine.chunk(images, state, bad)


def test_cpu_device_override(tmp_path):
    """A pipeline saved for cuda (every trained EduBotics model) loads and runs on CPU."""
    path = _save_model(tmp_path / 'cuda_saved')
    reference = _save_model(tmp_path / 'cpu_saved')
    with open(os.path.join(path, 'config.json')) as f:
        cfg = json.load(f)
    cfg['device'] = 'cuda'
    with open(os.path.join(path, 'config.json'), 'w') as f:
        json.dump(cfg, f)
    with open(os.path.join(path, 'policy_preprocessor.json')) as f:
        pre = json.load(f)
    devices = [s for s in pre['steps'] if s.get('registry_name') == 'device_processor']
    assert devices, 'the saved preprocessor has a device step'
    for step in devices:
        step['config']['device'] = 'cuda'
    with open(os.path.join(path, 'policy_preprocessor.json'), 'w') as f:
        json.dump(pre, f)

    engine = CE.ChunkEngine.load(path, 'cpu')
    pre_devices = [s for s in engine.preprocessor.steps if type(s).__name__ == 'DeviceProcessorStep']
    assert pre_devices and all(str(s.device) == 'cpu' for s in pre_devices)
    assert next(engine.policy.parameters()).device.type == 'cpu'
    images, state = _observation(3)
    got, _ = engine.chunk(images, state, 50)
    want, _ = CE.ChunkEngine.load(reference, 'cpu').chunk(images, state, 50)
    assert np.array_equal(got, want)
