"""V1-3: the warm-up chunk. A fresh container's first chunk costs 1.7-2.2 s of GPU time (CUDA / cuDNN
first use); that cost is paid ONCE PER MODEL LOAD by one chunk on zero inputs of the policy's own shapes,
on the GPU executor thread and through ``engine.chunk`` (the observation path: inference_mode, the AMP
rule, the processors), BEFORE the engine is cached, i.e. before any lease on it exists. Both loading
paths (prefetch and hello) go through ``EngineCache.acquire``. A failing warm-up is logged with its
exception type only and never fails the load; it touches no session (rate bucket, stats, budget).
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest

from conftest import (CONN, CONN_B, REPO_ID, REVISION, SUMMARY, TOKEN, FakeClock, FakeEngine, headers,
                      hello_frame, obs_frame)
import cloud_protocol as P
import server_core as S


class RecordingEngine(FakeEngine):
    """A FakeEngine that keeps the inputs themselves (dtype, values), not only their shapes."""

    def __init__(self, summary=None, fail=None, fail_first=0):
        super().__init__(summary, fail)
        self.inputs = []
        self.fail_first = fail_first

    def chunk(self, images, state, n):
        with self.lock:
            self.inputs.append(({k: v.copy() for k, v in images.items()}, np.array(state, copy=True), n,
                                threading.current_thread().name))
            failing = len(self.inputs) <= self.fail_first
        if failing:
            raise RuntimeError('cuda says no: secret-detail-never-logged')
        return super().chunk(images, state, n)


def _is_warm_up(record):
    images, state, n, _ = record
    return (all(img.dtype == np.uint8 and not img.any() for img in images.values())
            and state.dtype == np.float32 and not state.any())


def _assert_warm_up_call(record, summary=SUMMARY):
    images, state, n, thread = record
    assert _is_warm_up(record)
    assert sorted(images) == sorted(summary['expected_cameras'])
    for name, img in images.items():
        _, h, w = summary['image_shapes'][name]
        assert img.shape == (h, w, 3)                               # what decode_jpeg_rgb hands the engine
    assert state.shape == (summary['state_dim'],)
    assert n == min(S.LIMITS['actions_per_chunk'], summary['chunk_size'])
    assert thread.startswith('gpu')                                 # the ONE GPU executor thread


def _prefetch(client):
    return client.post(P.PREFETCH_PATH, content=b'', headers=headers(conn=None))


def _hello(client, conn=CONN):
    return client.post(P.HELLO_PATH, content=hello_frame(P, conn=conn), headers=headers(conn=conn))


def _infer(client, seq=1, conn=CONN):
    return client.post(P.INFER_PATH, content=obs_frame(P, seq), headers=headers(conn=conn))


# ---------------------------------------------------------------- through the routes

def test_prefetch_warms_the_model_once_and_hello_finds_it_warm(server):
    engine = RecordingEngine()
    client, *_ = server(engine=engine, warm_up=True)
    with client:
        assert _prefetch(client).status_code == 204
        assert len(engine.inputs) == 1
        _assert_warm_up_call(engine.inputs[0])
        assert _hello(client).status_code == 200
        assert _hello(client, conn=CONN_B).status_code == 200
        assert len(engine.inputs) == 1                              # cached: no second warm-up
        assert _infer(client, conn=CONN_B).status_code == 200
    assert len(engine.inputs) == 2 and not _is_warm_up(engine.inputs[1])


def test_hello_that_loads_warms_before_it_answers(server):
    engine = RecordingEngine()
    client, *_ = server(engine=engine, warm_up=True)
    with client:
        assert _hello(client).status_code == 200
        assert len(engine.inputs) == 1                              # before the ready frame went out
        _assert_warm_up_call(engine.inputs[0])
        assert _infer(client).status_code == 200
        assert _infer(client, seq=2).status_code == 200
    assert [_is_warm_up(r) for r in engine.inputs] == [True, False, False]


def test_the_warm_up_uses_the_policys_own_shapes(server):
    summary = dict(SUMMARY, expected_cameras=['scene'], image_shapes={'scene': [3, 30, 40]}, state_dim=5,
                   action_dim=5, chunk_size=20, n_action_steps=10)
    engine = RecordingEngine(summary)
    client, *_ = server(engine=engine, warm_up=True)
    with client:
        assert _prefetch(client).status_code == 204
    _assert_warm_up_call(engine.inputs[0], summary)
    assert engine.inputs[0][2] == 20                                 # n capped by the policy's chunk_size


def test_the_warm_up_touches_no_session(server):
    """No rate token, no chunk count, no gpu_ms sample: 8 observations at one instant all pass (burst 8)
    and the 9th is the rate's, exactly as without a warm-up; bye reports 8 chunks."""
    engine = RecordingEngine()
    client, backend, *_ = server(engine=engine, clock=FakeClock(), warm_up=True)
    with client:
        assert _hello(client).status_code == 200
        assert [_infer(client, seq=i).status_code for i in range(1, 10)] == [200] * 8 + [429]
        assert client.post(P.BYE_PATH, content=P.build_bye('robot_stop', None),
                           headers=headers()).status_code == 204
    stats = backend.calls[-1][5]
    assert stats['chunks'] == 8 and stats['rate_drops'] == 1 and stats['internal_errors'] == 0
    assert backend.names() == ['connect', 'end']                     # the warm-up made no backend call
    assert sum(_is_warm_up(r) for r in engine.inputs) == 1 and len(engine.inputs) == 9


def test_a_failing_warm_up_does_not_fail_the_load(server, caplog):
    caplog.set_level(logging.INFO, logger='edubotics.inference.server')
    engine = RecordingEngine(fail_first=1)                           # the warm-up raises, the model works
    client, *_ = server(engine=engine, warm_up=True)
    with client:
        assert _prefetch(client).status_code == 204
        assert _hello(client).status_code == 200
        assert _infer(client).status_code == 200
    assert len(engine.inputs) == 2                                   # no retry of the warm-up
    warn = [r for r in caplog.records if 'warm-up' in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING
    assert warn[0].getMessage() == 'model warm-up failed: RuntimeError'
    assert warn[0].exc_info is None and 'secret-detail' not in caplog.text and TOKEN not in caplog.text


def test_a_broken_engine_reports_through_the_observation_path(server):
    """The load still succeeds; the first real observation answers 'internal' as before (V1-3)."""
    engine = RecordingEngine(fail=RuntimeError('boom'))
    client, *_ = server(engine=engine, warm_up=True)
    with client:
        assert _hello(client).status_code == 200
        r = _infer(client)
        assert r.status_code == 500 and P.parse_error(*P.decode(r.content))[0] == 'internal'


# ---------------------------------------------------------------- the cache's contract

def test_the_warm_up_runs_before_the_entry_is_cached_or_leased():
    started, release = threading.Event(), threading.Event()
    calls = []

    def warm_up(engine):
        calls.append(threading.current_thread().name)
        started.set()
        assert release.wait(5)

    loads = []
    engine = FakeEngine()
    cache = S.EngineCache('cpu', loader=lambda r, v, d: loads.append(r) or engine, warm_up=warm_up)
    leases = []
    first = threading.Thread(target=lambda: leases.append(cache.acquire(REPO_ID, REVISION)), name='loader-a')
    second = threading.Thread(target=lambda: leases.append(cache.acquire(REPO_ID, REVISION)), name='loader-b')
    first.start()
    assert started.wait(5)
    second.start()
    time.sleep(0.1)
    assert cache.loaded() == [] and cache.acquire_cached(REPO_ID, REVISION) is None   # nothing handed out yet
    assert leases == []                                              # the second acquirer waits for it
    release.set()
    first.join(5)
    second.join(5)
    assert len(leases) == 2 and all(lease.engine is engine for lease in leases)
    assert calls == ['loader-a'] and loads == [REPO_ID]              # one load, one warm-up
    assert cache.refcount(REPO_ID, REVISION) == 2


def test_once_per_load_a_reload_after_eviction_warms_again():
    warmed = []
    cache = S.EngineCache('cpu', max_models=1, loader=lambda r, v, d: FakeEngine(), warm_up=warmed.append)
    for key in [('EduBotics-Solutions/a', 'a' * 40), ('EduBotics-Solutions/a', 'a' * 40),
                ('EduBotics-Solutions/b', 'b' * 40), ('EduBotics-Solutions/a', 'a' * 40)]:
        cache.acquire(*key).release()
    assert len(warmed) == 3                                          # a, b, a again after b evicted it


def test_a_failed_load_is_never_warmed():
    warmed = []
    import chunk_engine

    def refuse(r, v, d):
        raise chunk_engine.ModelRefused('model', 'x')
    cache = S.EngineCache('cpu', loader=refuse, warm_up=warmed.append)
    with pytest.raises(chunk_engine.ModelRefused):
        cache.acquire(REPO_ID, REVISION)
    assert warmed == [] and cache.loaded() == []


def test_a_cache_without_a_warm_up_loads_as_before():
    cache = S.EngineCache('cpu', loader=lambda r, v, d: FakeEngine())
    cache.acquire(REPO_ID, REVISION).release()
    assert cache.loaded() == [(REPO_ID, REVISION)]


# ---------------------------------------------------------------- the real engine (server image only)

def test_the_real_engine_takes_the_zero_inputs(tmp_path):
    """Skips without torch + LeRobot (CI's python-tests); run in the server image. A random-init ACT of a
    small trained shape: the zero inputs pass prepare_observation_for_inference + the processors."""
    pytest.importorskip('torch')
    pytest.importorskip('lerobot')
    import json

    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.act.modeling_act import ACTPolicy
    from lerobot.policies.factory import make_pre_post_processors

    import chunk_engine

    inputs = {f'observation.images.{c}': PolicyFeature(type=FeatureType.VISUAL, shape=(3, 48, 64))
              for c in ('gripper', 'scene')}
    inputs['observation.state'] = PolicyFeature(type=FeatureType.STATE, shape=(6,))
    outputs = {'action': PolicyFeature(type=FeatureType.ACTION, shape=(6,))}
    config = ACTConfig(input_features=inputs, output_features=outputs, chunk_size=100, n_action_steps=15,
                       device='cpu', pretrained_backbone_weights=None)
    rng = np.random.default_rng(0)
    stats = {k: {'mean': torch.tensor(rng.random(f.shape).astype(np.float32)),
                 'std': torch.tensor((rng.random(f.shape) + 0.5).astype(np.float32))}
             for k, f in {**inputs, **outputs}.items()}
    pre, post = make_pre_post_processors(policy_cfg=config, dataset_stats=stats)
    ACTPolicy(config).save_pretrained(str(tmp_path))
    pre.save_pretrained(str(tmp_path))
    post.save_pretrained(str(tmp_path))
    (tmp_path / 'edubotics_model_meta.json').write_text(json.dumps({'dataset_fps': 30, 'policy_type': 'act'}))
    engine = chunk_engine.ChunkEngine.load(str(tmp_path), 'cpu')
    S.warm_up_engine(engine, S.LIMITS['actions_per_chunk'])
    images = {c: (rng.random((48, 64, 3)) * 255).astype(np.uint8) for c in ('gripper', 'scene')}
    actions, _ = engine.chunk(images, rng.standard_normal(6).astype(np.float32), 50)
    assert actions.shape == (50, 6) and np.isfinite(actions).all()
