"""chunk_engine's pure half: model validation and the JPEG decode (spec §2.7, D12, D25).

No torch, no LeRobot: ``validate_model_dir`` reads JSON only and ``decode_jpeg_rgb``
needs only cv2, so this file runs in the deps-free CI suite. The LeRobot half
(``ChunkEngine.load``/``chunk``) is ``test_chunk_engine_parity.py``, image only.

The "real" files below are a real EduBotics ACT's config.json (the public
``EduBotics-Solutions/vfbdfveg-act-omx_f_kjkkj-bfs6egcrlv`` @ 6477f6ca) and a real
LeRobot 0.5.1 processor pair (``SvenBorodun/act_omx_pick_redb_c50``), copied verbatim.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import re

import numpy as np
import pytest

from physical_ai_server.inference import chunk_engine as CE
from physical_ai_server.inference import cloud_protocol as P

_SRC = Path(CE.__file__).read_text(encoding='utf-8')

REAL_CONFIG = {
    'type': 'act', 'n_obs_steps': 1,
    'input_features': {
        'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 480, 640]},
        'observation.images.scene': {'type': 'VISUAL', 'shape': [3, 480, 640]},
        'observation.state': {'type': 'STATE', 'shape': [6]},
    },
    'output_features': {'action': {'type': 'ACTION', 'shape': [6]}},
    'device': 'cuda', 'use_amp': False, 'use_peft': False, 'push_to_hub': False, 'repo_id': None,
    'private': None, 'tags': None, 'license': None, 'pretrained_path': None,
    'chunk_size': 100, 'n_action_steps': 15,
    'normalization_mapping': {'VISUAL': 'MEAN_STD', 'STATE': 'MEAN_STD', 'ACTION': 'MEAN_STD'},
    'vision_backbone': 'resnet18', 'pretrained_backbone_weights': 'ResNet18_Weights.IMAGENET1K_V1',
    'replace_final_stride_with_dilation': False, 'pre_norm': False, 'dim_model': 512, 'n_heads': 8,
    'dim_feedforward': 3200, 'feedforward_activation': 'relu', 'n_encoder_layers': 4,
    'n_decoder_layers': 1, 'use_vae': True, 'latent_dim': 32, 'n_vae_encoder_layers': 4,
    'temporal_ensemble_coeff': None, 'dropout': 0.1, 'kl_weight': 10.0, 'optimizer_lr': 1e-05,
    'optimizer_weight_decay': 0.0001, 'optimizer_lr_backbone': 1e-05,
}
_FEATURES = {
    'observation.state': {'type': 'STATE', 'shape': [6]},
    'observation.images.scene': {'type': 'VISUAL', 'shape': [3, 480, 640]},
    'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 480, 640]},
    'action': {'type': 'ACTION', 'shape': [6]},
}
_NORM = {'VISUAL': 'MEAN_STD', 'STATE': 'MEAN_STD', 'ACTION': 'MEAN_STD'}
REAL_PRE = {'name': 'policy_preprocessor', 'steps': [
    {'registry_name': 'rename_observations_processor', 'config': {'rename_map': {}}},
    {'registry_name': 'to_batch_processor', 'config': {}},
    {'registry_name': 'device_processor', 'config': {'device': 'cuda', 'float_dtype': None}},
    {'registry_name': 'normalizer_processor',
     'config': {'eps': 1e-08, 'features': _FEATURES, 'norm_map': _NORM},
     'state_file': 'policy_preprocessor_step_3_normalizer_processor.safetensors'},
]}
REAL_POST = {'name': 'policy_postprocessor', 'steps': [
    {'registry_name': 'unnormalizer_processor',
     'config': {'eps': 1e-08, 'features': {'action': {'type': 'ACTION', 'shape': [6]}}, 'norm_map': _NORM},
     'state_file': 'policy_postprocessor_step_0_unnormalizer_processor.safetensors'},
    {'registry_name': 'device_processor', 'config': {'device': 'cpu', 'float_dtype': None}},
]}
REAL_META = {'dataset_repo_id': 'SvenBorodun/omx_f_kjkkj', 'dataset_fps': 30, 'policy_type': 'act'}

EXPECTED_SUMMARY = {
    'type': 'act', 'expected_cameras': ['gripper', 'scene'],
    'image_shapes': {'gripper': [3, 480, 640], 'scene': [3, 480, 640]},
    'trained_fps': 30, 'chunk_size': 100, 'n_action_steps': 15, 'state_dim': 6, 'action_dim': 6,
    'requires_task': False,
}


def _model(tmp_path, config=None, pre=None, post=None, meta=REAL_META, drop=()):
    files = {'config.json': REAL_CONFIG if config is None else config,
             'policy_preprocessor.json': REAL_PRE if pre is None else pre,
             'policy_postprocessor.json': REAL_POST if post is None else post}
    if meta is not None:
        files['edubotics_model_meta.json'] = meta
    for name, content in files.items():
        if name in drop:
            continue
        (tmp_path / name).write_text(json.dumps(content) if not isinstance(content, str) else content)
    return str(tmp_path)


def _refused(tmp_path, kind, **kw):
    with pytest.raises(CE.ModelRefused) as e:
        CE.validate_model_dir(_model(tmp_path, **kw))
    assert e.value.kind == kind
    return e.value


def _cfg(**over):
    c = copy.deepcopy(REAL_CONFIG)
    c.update(over)
    return c


def test_accepts_real_act_files(tmp_path):
    summary = CE.validate_model_dir(_model(tmp_path))
    assert summary == EXPECTED_SUMMARY
    assert CE.PRE_STEPS_ALLOWED == ('rename_observations_processor', 'to_batch_processor',
                                    'device_processor', 'normalizer_processor')
    assert CE.POST_STEPS_ALLOWED == ('unnormalizer_processor', 'device_processor')


def test_refuses_non_act(tmp_path):
    for t in ('diffusion', 'pi0', 'smolvla', None, 7):
        _refused(tmp_path, 'model_type', config=_cfg(type=t))


def test_refuses_temporal_ensemble(tmp_path):
    _refused(tmp_path, 'model_type', config=_cfg(temporal_ensemble_coeff=0.01))
    _refused(tmp_path, 'model_type', config=_cfg(temporal_ensemble_coeff=0))


def test_refuses_peft_and_multi_obs(tmp_path):
    _refused(tmp_path, 'model_type', config=_cfg(use_peft=True))
    _refused(tmp_path, 'model_type', config=_cfg(n_obs_steps=2))
    _refused(tmp_path, 'model_type', config=_cfg(n_obs_steps='1'))
    c = _cfg()
    del c['n_obs_steps']                       # LeRobot's default is 1
    assert CE.validate_model_dir(_model(tmp_path, config=c)) == EXPECTED_SUMMARY


def test_refuses_class_step_and_unknown_registry_name(tmp_path):
    def with_pre_step(step, index=0):
        pre = copy.deepcopy(REAL_PRE)
        pre['steps'][index] = step
        return pre

    # A 'class' key makes LeRobot import whatever class it names (S9): refused whatever else it says.
    _refused(tmp_path, 'model', pre=with_pre_step({'class': 'os.system', 'config': {}}))
    _refused(tmp_path, 'model', pre=with_pre_step(
        {'registry_name': 'to_batch_processor', 'class': 'evil.Step', 'config': {}}, 1))
    _refused(tmp_path, 'model', pre=with_pre_step({'registry_name': 'evil_processor', 'config': {}}))
    _refused(tmp_path, 'model', pre=with_pre_step({'registry_name': ['to_batch_processor']}))
    _refused(tmp_path, 'model', pre=with_pre_step('to_batch_processor'))
    # Each pipeline has its OWN allowlist.
    post = copy.deepcopy(REAL_POST)
    post['steps'].append({'registry_name': 'normalizer_processor', 'config': {}})
    _refused(tmp_path, 'model', post=post)
    post = copy.deepcopy(REAL_POST)
    post['steps'][0] = {'class': 'lerobot.processor.UnnormalizerProcessorStep'}
    _refused(tmp_path, 'model', post=post)
    _refused(tmp_path, 'model', pre={'name': 'x', 'steps': 'none'})
    _refused(tmp_path, 'model', pre=['not', 'a', 'dict'])
    _refused(tmp_path, 'model', pre='{not json')
    # Missing files are a refusal, never a crash.
    for name in ('config.json', 'policy_preprocessor.json', 'policy_postprocessor.json'):
        d = tmp_path / ('missing_' + name.replace('.', '_'))
        d.mkdir()
        with pytest.raises(CE.ModelRefused) as e:
            CE.validate_model_dir(_model(d, drop=(name,)))
        assert e.value.kind == 'model', name
    with pytest.raises(CE.ModelRefused):
        CE.validate_model_dir(str(tmp_path / 'nope'))


def test_summary_shape_and_trained_fps_rule(tmp_path):
    summary = CE.validate_model_dir(_model(tmp_path))
    # The summary IS ready.policy: it must pass the robot's parser as built.
    h, pl = P.decode(P.build_ready(server_sha='t', policy=summary, limits={}))
    assert P.parse_ready(h, pl)[0] == summary
    # Config order of the cameras, not alphabetical.
    c = _cfg()
    c['input_features'] = {'observation.images.scene': {'type': 'VISUAL', 'shape': [3, 240, 320]},
                           'observation.state': {'type': 'STATE', 'shape': [5]},
                           'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 480, 640]}}
    c['output_features'] = {'action': {'type': 'ACTION', 'shape': [7]}}
    s = CE.validate_model_dir(_model(tmp_path, config=c))
    assert s['expected_cameras'] == ['scene', 'gripper']
    assert s['image_shapes'] == {'scene': [3, 240, 320], 'gripper': [3, 480, 640]}
    assert (s['state_dim'], s['action_dim']) == (5, 7)

    # trained_fps: the read_policy_summary rule (bool / non-number / <= 0 -> None), absent meta -> None.
    for value, want in ((30, 30), (15.0, 15.0), (True, None), ('30', None), (0, None), (-5, None),
                        (None, None), ([30], None)):
        meta = dict(REAL_META, dataset_fps=value)
        assert CE.validate_model_dir(_model(tmp_path, meta=meta))['trained_fps'] == want, value
    (tmp_path / 'edubotics_model_meta.json').unlink()
    assert CE.validate_model_dir(_model(tmp_path, meta=None))['trained_fps'] is None
    (tmp_path / 'edubotics_model_meta.json').write_text('{broken')
    assert CE.validate_model_dir(str(tmp_path))['trained_fps'] is None
    # A value the robot's parser could not take (outside 1..255) is not sent: None, never a broken ready.
    for value in (0.5, 300, 1e9):
        meta = dict(REAL_META, dataset_fps=value)
        s = CE.validate_model_dir(_model(tmp_path, meta=meta))
        assert s['trained_fps'] is None
        h, pl = P.decode(P.build_ready(server_sha='t', policy=s, limits={}))
        P.parse_ready(h, pl)

    # Shapes and features the robot or the policy cannot work with are a 'model' refusal.
    def feats(**over):
        f = copy.deepcopy(REAL_CONFIG['input_features'])
        f.update(over)
        return {k: v for k, v in f.items() if v is not None}

    bad_inputs = [
        feats(**{'observation.images.gripper': {'type': 'VISUAL', 'shape': [1, 480, 640]}}),
        feats(**{'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 480]}}),
        feats(**{'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 0, 640]}}),
        feats(**{'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 480, 9000]}}),
        feats(**{'observation.images.gripper': {'type': 'VISUAL', 'shape': 'x'}}),
        feats(**{'observation.images.bad name': {'type': 'VISUAL', 'shape': [3, 480, 640]}}),
        feats(**{'observation.state': None}),
        feats(**{'observation.state': {'type': 'STATE', 'shape': [33]}}),
        feats(**{'observation.images.gripper': None, 'observation.images.scene': None}),
        feats(**{f'observation.images.c{i}': {'type': 'VISUAL', 'shape': [3, 4, 4]} for i in range(3)}),
        'not a dict',
    ]
    for f in bad_inputs:
        _refused(tmp_path, 'model', config=_cfg(input_features=f))
    for out in ({}, {'action': {'type': 'ACTION', 'shape': [33]}}, {'action': {'type': 'ACTION', 'shape': []}},
                None):
        _refused(tmp_path, 'model', config=_cfg(output_features=out))
    for over in ({'chunk_size': 0}, {'chunk_size': 1001}, {'chunk_size': True}, {'n_action_steps': 0},
                 {'n_action_steps': '15'}):
        _refused(tmp_path, 'model', config=_cfg(**over))
    _refused(tmp_path, 'model', config='{broken')
    _refused(tmp_path, 'model', config=[1, 2])


def test_module_top_imports_numpy_only():
    tree = ast.parse(_SRC)
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or '').split('.')[0])
    assert top <= {'json', 'os', 'time', 'numpy'}, top
    # torch / lerobot / cv2 only inside functions; never anything from physical_ai_server by name.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert 'physical_ai_server' not in (node.module or '')
        if isinstance(node, ast.Import):
            assert all('physical_ai_server' not in a.name for a in node.names)
    assert not re.search(r'EDUBOTICS_[A-Z0-9_]+', _SRC)
    assert 'websocket' not in _SRC.lower()


# ---------------------------------------------------------------- decode (D25 / R-3)

class _Spy:
    def __init__(self, cv2):
        self.real = cv2.imdecode
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.real(*args, **kwargs)


def test_decode_checks_sof_before_imdecode(monkeypatch):
    cv2 = pytest.importorskip('cv2')
    bomb = cv2.imencode('.jpg', np.full((8192, 8192, 3), 127, np.uint8),
                        [cv2.IMWRITE_JPEG_QUALITY, 1, cv2.IMWRITE_JPEG_OPTIMIZE, 1,
                         cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420])[1].tobytes()
    assert len(bomb) <= P.MAX_JPEG_BYTES and P.jpeg_size(bomb) == (8192, 8192)
    rng = np.random.default_rng(1)
    img = (rng.random((480, 640, 3)) * 255).astype(np.uint8)
    good = cv2.imencode('.jpg', img)[1].tobytes()

    spy = _Spy(cv2)
    monkeypatch.setattr(cv2, 'imdecode', spy)
    assert CE.decode_jpeg_rgb(bomb, 640, 480) is None
    assert CE.decode_jpeg_rgb(good, 320, 240) is None                 # the right JPEG, the wrong size
    assert CE.decode_jpeg_rgb(good, 480, 640) is None                 # w/h swapped
    assert CE.decode_jpeg_rgb(b'not a jpeg', 640, 480) is None
    assert CE.decode_jpeg_rgb(cv2.imencode('.png', img)[1].tobytes(), 640, 480) is None
    assert spy.calls == 0

    rgb = CE.decode_jpeg_rgb(good, 640, 480)
    assert spy.calls == 1
    assert rgb.shape == (480, 640, 3) and rgb.dtype == np.uint8
    want = cv2.cvtColor(spy.real(np.frombuffer(good, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    assert np.array_equal(rgb, want)


def test_decode_catches_cv2_error(monkeypatch):
    cv2 = pytest.importorskip('cv2')
    good = cv2.imencode('.jpg', np.full((48, 64, 3), 128, np.uint8))[1].tobytes()

    def boom(*_a, **_k):
        raise cv2.error('OPENCV_IO_MAX_IMAGE_PIXELS exceeded')

    monkeypatch.setattr(cv2, 'imdecode', boom)
    assert CE.decode_jpeg_rgb(good, 64, 48) is None
    monkeypatch.setattr(cv2, 'imdecode', lambda *_a, **_k: None)
    assert CE.decode_jpeg_rgb(good, 64, 48) is None
    # A decode whose result has another shape than the SOF promised is refused too.
    monkeypatch.setattr(cv2, 'imdecode', lambda *_a, **_k: np.zeros((48, 64), np.uint8))
    assert CE.decode_jpeg_rgb(good, 64, 48) is None
    # A truncated JPEG with an intact SOF decodes to something or None, never raises.
    monkeypatch.undo()
    out = CE.decode_jpeg_rgb(good[:len(good) // 2], 64, 48)
    assert out is None or out.shape == (48, 64, 3)


def test_model_refused_kinds():
    assert CE.ModelRefused('model').kind == 'model'
    assert CE.ModelRefused('model_type', 'x').kind == 'model_type'
    with pytest.raises(ValueError):
        CE.ModelRefused('other')
