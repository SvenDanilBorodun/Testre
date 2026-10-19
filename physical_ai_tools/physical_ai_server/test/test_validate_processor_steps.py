"""The robot's processor allowlist: ``chunk_engine.validate_processor_steps`` (RunPod spec §4.10).

A trained model reaches a robot from a GPU host we do not trust (a RunPod Community pod can
alter the files it uploads, D6). LeRobot turns a processor JSON into code on load: a ``"class"``
key is ``importlib.import_module`` + ``step_class(**config)`` before any type check
(``lerobot/processor/pipeline.py``), ``action_tokenizer_processor`` loads whatever repo its JSON
names with ``trust_remote_code=True`` by default, ``pi0_fast``'s model loads ``config.json``'s
tokenizer names with ``trust_remote_code=True``, and a ``state_file`` that is not on disk falls
back to a hub download. ``InferenceManager.load_policy`` therefore runs this check FIRST, before
``from_pretrained`` and ``make_pre_post_processors``, and refuses in German (S17).

No torch, no LeRobot: the validator reads JSON and stats files only, so this file runs in the
deps-free suite. The measured ACT files are LeRobot 0.5.1's own (spec Appendix A.1).
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import types

import pytest

from physical_ai_server.inference import chunk_engine as CE

PKG = Path(CE.__file__).resolve().parents[1]
INFERENCE_MANAGER_PY = PKG / 'inference' / 'inference_manager.py'
NODE_PY = PKG / 'physical_ai_server.py'

PRE_STATE = 'policy_preprocessor_step_3_normalizer_processor.safetensors'
POST_STATE = 'policy_postprocessor_step_0_unnormalizer_processor.safetensors'
S17 = ('Das Modell enthält nicht erlaubte Verarbeitungsschritte und wird aus '
       'Sicherheitsgründen nicht geladen.')

_FEATURES = {
    'observation.state': {'type': 'STATE', 'shape': [6]},
    'observation.images.gripper': {'type': 'VISUAL', 'shape': [3, 48, 64]},
    'observation.images.scene': {'type': 'VISUAL', 'shape': [3, 48, 64]},
    'action': {'type': 'ACTION', 'shape': [6]},
}
_NORM = {'VISUAL': 'MEAN_STD', 'STATE': 'MEAN_STD', 'ACTION': 'MEAN_STD'}
# Spec Appendix A.1, byte for byte what LeRobot 0.5.1 writes for ACT.
ACT_PRE = {'name': 'policy_preprocessor', 'steps': [
    {'registry_name': 'rename_observations_processor', 'config': {'rename_map': {}}},
    {'registry_name': 'to_batch_processor', 'config': {}},
    {'registry_name': 'device_processor', 'config': {'device': 'cpu', 'float_dtype': None}},
    {'registry_name': 'normalizer_processor',
     'config': {'eps': 1e-08, 'features': _FEATURES, 'norm_map': _NORM},
     'state_file': PRE_STATE},
]}
ACT_POST = {'name': 'policy_postprocessor', 'steps': [
    {'registry_name': 'unnormalizer_processor',
     'config': {'eps': 1e-08, 'features': {'action': {'type': 'ACTION', 'shape': [6]}}, 'norm_map': _NORM},
     'state_file': POST_STATE},
    {'registry_name': 'device_processor', 'config': {'device': 'cpu', 'float_dtype': None}},
]}
ACT_CONFIG = {'type': 'act', 'n_obs_steps': 1, 'chunk_size': 100, 'n_action_steps': 15,
              'input_features': {k: v for k, v in _FEATURES.items() if k != 'action'},
              'output_features': {'action': _FEATURES['action']}}

PALIGEMMA = 'google/paligemma-3b-pt-224'
SMOLVLM = 'HuggingFaceTB/SmolVLM2-500M-Video-Instruct'
FAST = 'lerobot/fast-action-tokenizer'


def _write_model(d, config=None, pre=None, post=None, state_files=(PRE_STATE, POST_STATE)):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    for name, content in (('config.json', ACT_CONFIG if config is None else config),
                          ('policy_preprocessor.json', ACT_PRE if pre is None else pre),
                          ('policy_postprocessor.json', ACT_POST if post is None else post)):
        if content is not _ABSENT:
            (d / name).write_text(content if isinstance(content, str) else json.dumps(content),
                                  encoding='utf-8')
    for name in state_files:
        (d / name).write_bytes(b'\x08\x00\x00\x00\x00\x00\x00\x00{}      ')
    return str(d)


_ABSENT = object()


def _refused(model_dir):
    with pytest.raises(CE.ProcessorStepsRefused) as e:
        CE.validate_processor_steps(model_dir)
    return e.value


def _with_step(pipeline, index, step):
    p = copy.deepcopy(pipeline)
    if index is None:
        p['steps'].append(step)
    else:
        p['steps'][index] = step
    return p


# ---------------------------------------------------------------- the contract's constants

def test_the_allowlists_are_the_union_over_the_eight_factories():
    assert CE.ROBOT_PRE_STEPS_ALLOWED == (
        'rename_observations_processor', 'to_batch_processor', 'device_processor', 'normalizer_processor',
        'smolvla_new_line_processor', 'pi0_new_line_processor', 'tokenizer_processor',
        'delta_actions_processor', 'pi05_prepare_state_tokenizer_processor_step',
        'pi0_fast_prepare_state_tokenizer_processor_step', 'action_tokenizer_processor')
    assert CE.ROBOT_POST_STEPS_ALLOWED == (
        'unnormalizer_processor', 'device_processor', 'absolute_actions_processor')
    assert CE.ROBOT_TOKENIZER_NAMES == (PALIGEMMA, SMOLVLM)
    assert CE.ROBOT_ACTION_TOKENIZER_NAMES == (FAST,)
    # The cloud's ACT-only lists are a subset of the robot's (the robot also runs VLAs).
    assert set(CE.PRE_STEPS_ALLOWED) <= set(CE.ROBOT_PRE_STEPS_ALLOWED)
    assert set(CE.POST_STEPS_ALLOWED) <= set(CE.ROBOT_POST_STEPS_ALLOWED)


def test_the_refusal_sentence_is_s17():
    assert CE.PROCESSOR_STEPS_REFUSED_DE == S17
    assert issubclass(CE.ProcessorStepsRefused, Exception)
    assert not issubclass(CE.ProcessorStepsRefused, CE.ModelRefused)


# ---------------------------------------------------------------- accepted

def test_the_measured_act_files_pass(tmp_path):
    assert CE.validate_processor_steps(_write_model(tmp_path / 'm')) is None


def test_the_vla_pipelines_lerobot_writes_pass(tmp_path):
    # pi0 / pi05: paligemma tokenizer; smolvla: the SmolVLM tokenizer.
    for i, tok in enumerate((PALIGEMMA, SMOLVLM)):
        pre = _with_step(ACT_PRE, None, {'registry_name': 'tokenizer_processor',
                                         'config': {'tokenizer_name': tok, 'max_length': 48}})
        CE.validate_processor_steps(_write_model(tmp_path / f'tok{i}', pre=pre))
    # pi0_fast: the action tokenizer as get_config() saves it (no paligemma_tokenizer_name key),
    # and with the default spelled out; the model's config.json carries the two pinned names.
    for i, extra in enumerate(({}, {'paligemma_tokenizer_name': PALIGEMMA})):
        pre = _with_step(ACT_PRE, None, {'registry_name': 'action_tokenizer_processor',
                                         'config': {'trust_remote_code': True, 'max_action_tokens': 256,
                                                    'action_tokenizer_name': FAST, **extra}})
        config = dict(ACT_CONFIG, type='pi0_fast', action_tokenizer_name=FAST, text_tokenizer_name=PALIGEMMA)
        CE.validate_processor_steps(_write_model(tmp_path / f'fast{i}', pre=pre, config=config))
    # Every robot step name, in its own pipeline, is accepted; absolute_actions only post.
    pre = copy.deepcopy(ACT_PRE)
    pre['steps'] += [{'registry_name': n, 'config': {}} for n in (
        'smolvla_new_line_processor', 'pi0_new_line_processor', 'delta_actions_processor',
        'pi05_prepare_state_tokenizer_processor_step', 'pi0_fast_prepare_state_tokenizer_processor_step')]
    post = _with_step(ACT_POST, None, {'registry_name': 'absolute_actions_processor', 'config': {}})
    CE.validate_processor_steps(_write_model(tmp_path / 'all', pre=pre, post=post))
    # A step without a config key is LeRobot's `step_entry.get("config", {})`.
    CE.validate_processor_steps(_write_model(
        tmp_path / 'noconf', pre=_with_step(ACT_PRE, 1, {'registry_name': 'to_batch_processor'})))


def _hf_cache_snapshot(root, files):
    """``models--o--n/snapshots/<sha>/pretrained_model/<file>`` -> ``../../../blobs/<hash>``,
    exactly as huggingface_hub lays out a cache (files are relative symlinks into blobs/)."""
    repo = Path(root) / 'hub' / 'models--o--n'
    snap = repo / 'snapshots' / ('a' * 40) / 'pretrained_model'
    blobs = repo / 'blobs'
    snap.mkdir(parents=True)
    blobs.mkdir(parents=True)
    for i, (name, data) in enumerate(files.items()):
        blob = blobs / f'{i:064x}'
        blob.write_bytes(data)
        os.symlink(os.path.join('..', '..', '..', 'blobs', blob.name), snap / name)
    return snap


def test_an_hf_cache_snapshot_passes(tmp_path):
    files = {'config.json': json.dumps(ACT_CONFIG).encode(),
             'policy_preprocessor.json': json.dumps(ACT_PRE).encode(),
             'policy_postprocessor.json': json.dumps(ACT_POST).encode(),
             PRE_STATE: b'state-pre', POST_STATE: b'state-post'}
    snap = _hf_cache_snapshot(tmp_path, files)
    assert (snap / PRE_STATE).is_symlink()
    assert CE.validate_processor_steps(str(snap)) is None


# ---------------------------------------------------------------- refused

def test_a_class_step_and_an_unknown_name_are_refused(tmp_path):
    cases = {
        'class_only': _with_step(ACT_PRE, 0, {'class': 'os.system', 'config': {}}),
        'class_beside_registry': _with_step(ACT_PRE, 1, {'registry_name': 'to_batch_processor',
                                                         'class': 'evil.Step', 'config': {}}),
        'unknown': _with_step(ACT_PRE, 1, {'registry_name': 'evil_processor', 'config': {}}),
        'groot': _with_step(ACT_PRE, None, {'registry_name': 'groot_eagle_encode_v3', 'config': {}}),
        'name_not_str': _with_step(ACT_PRE, 1, {'registry_name': ['to_batch_processor']}),
        'step_not_dict': _with_step(ACT_PRE, 1, 'to_batch_processor'),
        'config_not_dict': _with_step(ACT_PRE, 1, {'registry_name': 'to_batch_processor', 'config': [1]}),
    }
    for name, pre in cases.items():
        _refused(_write_model(tmp_path / name, pre=pre))
    # Each pipeline has its OWN list: a post-only step in pre, a pre-only step in post.
    _refused(_write_model(tmp_path / 'abs_in_pre', pre=_with_step(
        ACT_PRE, None, {'registry_name': 'absolute_actions_processor', 'config': {}})))
    _refused(_write_model(tmp_path / 'norm_in_post', post=_with_step(
        ACT_POST, None, {'registry_name': 'normalizer_processor', 'config': {}})))
    _refused(_write_model(tmp_path / 'class_in_post', post=_with_step(
        ACT_POST, 0, {'class': 'lerobot.processor.UnnormalizerProcessorStep'})))


def test_malformed_and_missing_pipelines_are_refused(tmp_path):
    for name, pre in (('steps_str', {'name': 'x', 'steps': 'none'}), ('not_dict', ['a']),
                      ('broken', '{not json'), ('no_steps', {'name': 'x'})):
        _refused(_write_model(tmp_path / name, pre=pre))
    _refused(_write_model(tmp_path / 'no_pre', pre=_ABSENT))
    _refused(_write_model(tmp_path / 'no_post', post=_ABSENT))
    _refused(_write_model(tmp_path / 'no_config', config=_ABSENT))
    _refused(str(tmp_path / 'nope'))
    # Over 1 MiB: refused before it is parsed.
    big = copy.deepcopy(ACT_PRE)
    big['steps'][0]['config']['rename_map'] = {f'k{i}': 'v' * 64 for i in range(20000)}
    assert len(json.dumps(big)) > 1024 * 1024
    _refused(_write_model(tmp_path / 'big', pre=big))


def test_each_tokenizer_pin_is_enforced(tmp_path):
    def tok(cfg):
        return _with_step(ACT_PRE, None, {'registry_name': 'tokenizer_processor', 'config': cfg})

    def act_tok(cfg):
        return _with_step(ACT_PRE, None, {'registry_name': 'action_tokenizer_processor', 'config': cfg})

    cases = {
        'tok_other': tok({'tokenizer_name': 'evil/remote-code-tokenizer'}),
        'tok_absent': tok({}),
        'tok_not_str': tok({'tokenizer_name': [PALIGEMMA]}),
        'tok_case': tok({'tokenizer_name': PALIGEMMA.upper()}),
        'act_other': act_tok({'action_tokenizer_name': 'evil/fast'}),
        'act_absent': act_tok({'trust_remote_code': True}),
        'act_paligemma_other': act_tok({'action_tokenizer_name': FAST, 'paligemma_tokenizer_name': 'evil/pg'}),
        'act_paligemma_none': act_tok({'action_tokenizer_name': FAST, 'paligemma_tokenizer_name': None}),
        'act_tokenizer_name_is_not_the_key': act_tok({'tokenizer_name': FAST}),
    }
    for name, pre in cases.items():
        _refused(_write_model(tmp_path / name, pre=pre))


def test_pi0_fast_config_tokenizer_names_are_pinned(tmp_path):
    good = dict(ACT_CONFIG, type='pi0_fast', action_tokenizer_name=FAST, text_tokenizer_name=PALIGEMMA)
    CE.validate_processor_steps(_write_model(tmp_path / 'good', config=good))
    for name, over in (('action', {'action_tokenizer_name': 'evil/fast'}),
                       ('text', {'text_tokenizer_name': 'evil/pg'}),
                       ('action_absent', {'action_tokenizer_name': _ABSENT}),
                       ('text_absent', {'text_tokenizer_name': _ABSENT})):
        cfg = dict(good)
        for k, v in over.items():
            if v is _ABSENT:
                del cfg[k]
            else:
                cfg[k] = v
        _refused(_write_model(tmp_path / name, config=cfg))
    # The legacy `model_type` key names the policy too (InferenceManager reads type or model_type).
    legacy = {k: v for k, v in good.items() if k != 'type'}
    legacy.update(model_type='pi0_fast', action_tokenizer_name='evil/fast')
    _refused(_write_model(tmp_path / 'model_type', config=legacy))
    # The pin binds pi0_fast only: another type may carry other names (it does not load them).
    CE.validate_processor_steps(_write_model(
        tmp_path / 'pi0', config=dict(ACT_CONFIG, type='pi0', action_tokenizer_name='x/y')))
    # A config.json that is not a JSON object is refused (LeRobot could not load it either).
    _refused(_write_model(tmp_path / 'cfg_list', config=[1, 2]))
    _refused(_write_model(tmp_path / 'cfg_broken', config='{broken'))


def test_state_file_names_are_basenames_inside_the_repo_root(tmp_path):
    def pre_with_state(value):
        p = copy.deepcopy(ACT_PRE)
        p['steps'][3]['state_file'] = value
        return p

    for name, value in (('slash', 'sub/' + PRE_STATE), ('dotdot', '../' + PRE_STATE),
                        ('abs', '/etc/' + PRE_STATE), ('backslash', 'a\\' + PRE_STATE),
                        ('not_safetensors', 'policy_preprocessor_step_3.pt'),
                        ('pickle', 'state.pkl'), ('empty', ''), ('dot', '.'), ('none', None),
                        ('number', 3), ('space', 'a b.safetensors')):
        _refused(_write_model(tmp_path / name, pre=pre_with_state(value)))
    # A name the regex allows but that is not on disk: refused (LeRobot would fall back to the hub).
    _refused(_write_model(tmp_path / 'missing', state_files=(POST_STATE,)))
    _refused(_write_model(tmp_path / 'missing_post', state_files=(PRE_STATE,)))
    # A directory with the right name is not a regular file.
    d = Path(_write_model(tmp_path / 'is_dir', state_files=(POST_STATE,)))
    (d / PRE_STATE).mkdir()
    _refused(str(d))


def test_a_symlink_leaving_the_repo_root_is_refused(tmp_path):
    outside = tmp_path / 'outside.safetensors'
    outside.write_bytes(b'not ours')
    # A plain model directory: the root is the directory itself.
    d = Path(_write_model(tmp_path / 'plain', state_files=(POST_STATE,)))
    os.symlink(outside, d / PRE_STATE)
    _refused(str(d))
    # A symlink to a sibling INSIDE the plain directory is fine.
    d2 = Path(_write_model(tmp_path / 'plain_inside', state_files=(POST_STATE,)))
    (d2 / 'real.safetensors').write_bytes(b'x')
    os.symlink('real.safetensors', d2 / PRE_STATE)
    CE.validate_processor_steps(str(d2))
    # An HF cache snapshot whose state file points out of models--o--n (into another repo's blobs).
    files = {'config.json': json.dumps(ACT_CONFIG).encode(),
             'policy_preprocessor.json': json.dumps(ACT_PRE).encode(),
             'policy_postprocessor.json': json.dumps(ACT_POST).encode(),
             POST_STATE: b'state-post'}
    snap = _hf_cache_snapshot(tmp_path / 'c1', files)
    other = tmp_path / 'c1' / 'hub' / 'models--evil--x' / 'blobs'
    other.mkdir(parents=True)
    (other / 'f').write_bytes(b'evil')
    os.symlink(os.path.join('..', '..', '..', '..', 'models--evil--x', 'blobs', 'f'), snap / PRE_STATE)
    assert (snap / PRE_STATE).resolve().is_file()
    _refused(str(snap))
    # A dangling symlink is not a regular file.
    snap2 = _hf_cache_snapshot(tmp_path / 'c2', files)
    os.symlink(os.path.join('..', '..', '..', 'blobs', 'gone'), snap2 / PRE_STATE)
    _refused(str(snap2))


def test_the_refusal_names_no_path_and_is_not_the_student_sentence(tmp_path):
    d = _write_model(tmp_path / 'secret-dir', pre=_with_step(ACT_PRE, 0, {'class': 'os.system'}))
    e = _refused(d)
    assert 'secret-dir' not in str(e)
    assert str(e) and str(e) != S17


def test_module_top_still_imports_only_json_os_time_numpy():
    # chunk_engine also ships as /root/chunk_engine.py in the Modal image: the new code
    # imports re / pathlib inside the function (test_chunk_engine_validate's fence).
    tree = ast.parse(Path(CE.__file__).read_text(encoding='utf-8'))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or '').split('.')[0])
    assert top <= {'json', 'os', 'time', 'numpy'}, top


# ---------------------------------------------------------------- the robot calls it first

def _function(path, name, cls=None):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if cls is not None and isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
        if cls is None and isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{path.name}: no function {name}')


def _first_line_of_call(fn, attr):
    lines = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Attribute) and n.func.attr == attr)
                  or (isinstance(n.func, ast.Name) and n.func.id == attr))]
    assert lines, f'{fn.name} calls no {attr}'
    return min(lines)


def test_load_policy_validates_before_from_pretrained_and_the_processors():
    fn = _function(INFERENCE_MANAGER_PY, 'load_policy', cls='InferenceManager')
    tries = [n for n in fn.body if isinstance(n, ast.Try)]
    assert len(tries) == 1, 'load_policy has exactly one try'
    first = tries[0].body[0]
    assert (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and ast.unparse(first.value) == 'validate_processor_steps(self.policy_path)'), \
        'the first statement inside load_policy\'s try is validate_processor_steps(self.policy_path)'
    check = _first_line_of_call(fn, 'validate_processor_steps')
    assert check < _first_line_of_call(fn, 'from_pretrained')
    assert check < _first_line_of_call(fn, 'make_pre_post_processors')
    # last_load_error is reset at the start of load_policy (before the CUDA check).
    assert ast.unparse(fn.body[0]) == 'self.last_load_error = None' or (
        isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant)
        and ast.unparse(fn.body[1]) == 'self.last_load_error = None'), \
        'load_policy starts with self.last_load_error = None'
    init = _function(INFERENCE_MANAGER_PY, '__init__', cls='InferenceManager')
    assert 'self.last_load_error = None' in [ast.unparse(s) for s in init.body]


def test_the_eager_load_reports_the_managers_reason():
    fn = _function(NODE_PY, '_eager_load_policy')
    src = ast.unparse(fn)
    assert 'current_status.error = self.inference_manager.last_load_error or ' in src
    assert 'Das Modell konnte nicht geladen werden.' in src   # today's sentence stays the fallback


# ------------------------------------------- the behaviour, with LeRobot and torch stubbed

class _Spy:
    calls = []

    @classmethod
    def from_pretrained(cls, path):
        _Spy.calls.append(('from_pretrained', path))
        raise AssertionError('from_pretrained must not run for a refused model')


def _load_inference_manager(monkeypatch):
    """inference_manager.py under a PRIVATE module name with lerobot and torch stubbed, so the
    package's real ``physical_ai_server.inference.inference_manager`` (the image's parity test)
    never sees the stubs."""
    torch = types.ModuleType('torch')
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.Tensor = type('Tensor', (), {})        # only read by annotations at def time
    stubs = {'torch': torch}
    for name in ('lerobot', 'lerobot.policies', 'lerobot.utils'):
        stubs[name] = types.ModuleType(name)
        stubs[name].__path__ = []

    def make_pre_post_processors(**kwargs):
        _Spy.calls.append(('make_pre_post_processors', kwargs))
        raise AssertionError('make_pre_post_processors must not run for a refused model')

    factory = types.ModuleType('lerobot.policies.factory')
    factory.make_pre_post_processors = make_pre_post_processors
    pretrained = types.ModuleType('lerobot.policies.pretrained')
    pretrained.PreTrainedPolicy = object
    control = types.ModuleType('lerobot.utils.control_utils')
    control.predict_action = lambda **kw: None
    stubs.update({'lerobot.policies.factory': factory, 'lerobot.policies.pretrained': pretrained,
                  'lerobot.utils.control_utils': control})
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location('_ipm_under_test', INFERENCE_MANAGER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_policy_refuses_in_german_and_never_loads(monkeypatch, tmp_path):
    im_mod = _load_inference_manager(monkeypatch)
    im = im_mod.InferenceManager(device='cpu')
    assert im.last_load_error is None
    im._get_policy_class = lambda name: _Spy
    im.policy_type = 'act'
    _Spy.calls = []

    im.policy_path = _write_model(tmp_path / 'bad', pre=_with_step(ACT_PRE, 0, {'class': 'os.system'}))
    assert im.load_policy() is False
    assert im.last_load_error == S17
    assert _Spy.calls == []

    # The reason is per load: a later load that fails for another reason does not keep S17.
    im.policy_path = _write_model(tmp_path / 'good')
    assert im.load_policy() is False            # the stubbed from_pretrained raises
    assert im.last_load_error is None
    assert [c[0] for c in _Spy.calls] == ['from_pretrained']

    # The CUDA refusal (device='cuda', no GPU) resets it too.
    im.last_load_error = 'stale'
    im.device = 'cuda'
    assert im.load_policy() is False
    assert im.last_load_error is None
