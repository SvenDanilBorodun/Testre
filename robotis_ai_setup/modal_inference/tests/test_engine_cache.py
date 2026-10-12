"""The container's model cache: one load per model, a count-based LRU that never evicts a model in
use (spec §3.3, D27), and the default loader's refusals before any download."""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from conftest import REPO_ID, REVISION, FakeEngine
import chunk_engine
import server_core as S


class CountingLoader:
    def __init__(self, delay=0.0, fail=None):
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, repo, revision, device):
        with self.lock:
            self.calls.append((repo, revision, device))
        time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return FakeEngine()


def _key(i):
    return (f'EduBotics-Solutions/m{i}', f'{i:040x}')


def test_constants():
    assert S.LRU_MAX_MODELS == 7 and S.MODEL_MAX_BYTES == 1 << 30     # D27: max_concurrency (5) + 2 (V1-2)
    assert S.ALLOW_PATTERNS == ['config.json', 'model.safetensors', 'policy_preprocessor*.json',
                                'policy_preprocessor*.safetensors', 'policy_postprocessor*.json',
                                'policy_postprocessor*.safetensors', 'edubotics_model_meta.json']


def test_single_load_per_key_under_8_threads():
    loader = CountingLoader(delay=0.2)
    cache = S.EngineCache('cpu', loader=loader)
    barrier = threading.Barrier(8)
    leases = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        lease = cache.acquire(REPO_ID, REVISION)
        with lock:
            leases.append(lease)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert loader.calls == [(REPO_ID, REVISION, 'cpu')]
    assert len({id(lease.engine) for lease in leases}) == 1
    assert cache.refcount(REPO_ID, REVISION) == 8
    for lease in leases:
        lease.release()
        lease.release()                     # idempotent
    assert cache.refcount(REPO_ID, REVISION) == 0
    assert cache.loaded() == [(REPO_ID, REVISION)]       # released, still cached
    with cache.acquire(REPO_ID, REVISION) as lease:
        assert lease.engine is leases[0].engine
    assert len(loader.calls) == 1


def test_different_keys_load_in_parallel():
    loader = CountingLoader(delay=0.3)
    cache = S.EngineCache('cpu', loader=loader)
    t0 = time.monotonic()
    threads = [threading.Thread(target=lambda i=i: cache.acquire(*_key(i)).release()) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(loader.calls) == 4 and time.monotonic() - t0 < 1.0


def test_lru_evicts_only_refcount_0_entries_lru_first():
    loader = CountingLoader()
    cache = S.EngineCache('cpu', max_models=3, loader=loader)
    for i in (1, 2, 3):
        cache.acquire(*_key(i)).release()
    assert cache.loaded() == [_key(1), _key(2), _key(3)]
    cache.acquire(*_key(1)).release()                      # 1 is now the most recently used
    cache.acquire(*_key(4)).release()
    assert cache.loaded() == [_key(3), _key(1), _key(4)]  # 2 was the least recently used
    held = cache.acquire(*_key(3))                         # 3 is LRU but in use
    cache.acquire(*_key(5)).release()
    assert _key(3) in cache.loaded() and _key(1) not in cache.loaded()
    held.release()
    assert len(cache.loaded()) == 3


def test_in_use_entry_is_never_evicted():
    cache = S.EngineCache('cpu', max_models=2, loader=CountingLoader())
    a = cache.acquire(*_key(1))
    b = cache.acquire(*_key(2))
    c = cache.acquire(*_key(3))
    assert len(cache.loaded()) == 3                        # above the count while all three are in use
    assert len({id(a.engine), id(b.engine), id(c.engine)}) == 3
    a.release()
    assert cache.loaded() == [_key(2), _key(3)]            # evicted at its release
    b.release()
    c.release()
    assert len(cache.loaded()) == 2


def test_eviction_empties_the_cuda_cache(monkeypatch):
    calls = []
    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(empty_cache=lambda: calls.append(1)))
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    cache = S.EngineCache('cuda', max_models=1, loader=CountingLoader())
    cache.acquire(*_key(1)).release()
    assert calls == []
    cache.acquire(*_key(2)).release()
    assert calls == [1]


def test_init_device_is_a_noop_on_cpu(monkeypatch):
    monkeypatch.setitem(sys.modules, 'torch', None)        # any import of torch would raise
    S.EngineCache('cpu').init_device()


def test_failed_load_is_not_cached():
    loader = CountingLoader(fail=chunk_engine.ModelRefused('model', 'nope'))
    cache = S.EngineCache('cpu', loader=loader)
    for _ in range(2):
        with pytest.raises(chunk_engine.ModelRefused):
            cache.acquire(REPO_ID, REVISION)
    assert len(loader.calls) == 2 and cache.loaded() == []


def test_acquire_cached_never_loads():
    loader = CountingLoader()
    cache = S.EngineCache('cpu', loader=loader)
    assert cache.acquire_cached(REPO_ID, REVISION) is None
    cache.acquire(REPO_ID, REVISION).release()
    lease = cache.acquire_cached(REPO_ID, REVISION)
    assert lease is not None and cache.refcount(REPO_ID, REVISION) == 1
    lease.release()
    assert len(loader.calls) == 1


# ---------------------------------------------------------------- default_loader

class FakeHub:
    def __init__(self, sizes):
        self.sizes = sizes
        self.info_calls = []
        self.download_calls = []
        self.loaded = []

    def install(self, monkeypatch):
        hub = self
        import huggingface_hub

        class Api:
            def __init__(self, token=None):
                hub.token = token

            def model_info(self, repo, revision=None, files_metadata=False):
                hub.info_calls.append((repo, revision, files_metadata))
                return types.SimpleNamespace(siblings=[types.SimpleNamespace(rfilename=n, size=s)
                                                       for n, s in hub.sizes.items()])

        def download(repo, revision=None, allow_patterns=None, token=None, **kw):
            hub.download_calls.append((repo, revision, list(allow_patterns), token))
            return '/tmp/model-dir'

        def load(path, device):
            hub.loaded.append((path, device))
            return FakeEngine()
        monkeypatch.setattr(huggingface_hub, 'HfApi', Api)
        monkeypatch.setattr(huggingface_hub, 'snapshot_download', download)
        monkeypatch.setattr(chunk_engine.ChunkEngine, 'load', staticmethod(load))
        return self


REAL_SIBLINGS = {'.gitattributes': 1519, 'config.json': 1700, 'model.safetensors': 206_699_736,
                 'policy_preprocessor.json': 1500, 'policy_preprocessor_step_3_normalizer_processor.safetensors': 4000,
                 'policy_postprocessor.json': 700, 'policy_postprocessor_step_0_unnormalizer_processor.safetensors': 300,
                 'edubotics_model_meta.json': 90, 'train_config.json': 9_000_000_000, 'training_log.txt': 5_000_000}


def test_default_loader_refuses_bad_refs_without_any_hub_call(monkeypatch):
    hub = FakeHub(REAL_SIBLINGS).install(monkeypatch)
    bad = [('SvenBorodun/act', REVISION), ('EduBotics-Solutions/../x', REVISION), ('EduBotics-Solutions/', REVISION),
           ('EduBotics-Solutions/a/b', REVISION), (REPO_ID, 'main'), (REPO_ID, 'A' * 40), (REPO_ID, 'a' * 39),
           (None, REVISION), (REPO_ID, None), ('EduBotics-Solutions/' + 'a' * 97, REVISION)]
    for repo, rev in bad:
        with pytest.raises(chunk_engine.ModelRefused) as e:
            S.default_loader(repo, rev, 'cpu')
        assert e.value.kind == 'model'
    assert hub.info_calls == [] and hub.download_calls == []


def test_default_loader_size_cap_and_allow_patterns(monkeypatch):
    monkeypatch.setenv('HF_TOKEN', 'hf_test_token_value')
    hub = FakeHub(REAL_SIBLINGS).install(monkeypatch)
    engine = S.default_loader(REPO_ID, REVISION, 'cuda')
    assert isinstance(engine, FakeEngine)
    assert hub.info_calls == [(REPO_ID, REVISION, True)] and hub.token == 'hf_test_token_value'
    # train_config.json (9 GB here) is outside the allow patterns: it neither counts nor downloads.
    assert hub.download_calls == [(REPO_ID, REVISION, S.ALLOW_PATTERNS, 'hf_test_token_value')]
    assert hub.loaded == [('/tmp/model-dir', 'cuda')]

    hub = FakeHub(dict(REAL_SIBLINGS, **{'model.safetensors': S.MODEL_MAX_BYTES})).install(monkeypatch)
    with pytest.raises(chunk_engine.ModelRefused) as e:
        S.default_loader(REPO_ID, REVISION, 'cpu')
    assert e.value.kind == 'model' and hub.download_calls == []


def test_default_loader_turns_hub_and_load_failures_into_model_refusals(monkeypatch):
    import huggingface_hub
    hub = FakeHub(REAL_SIBLINGS).install(monkeypatch)

    class Down:
        def __init__(self, token=None):
            pass

        def model_info(self, *a, **k):
            raise OSError('hub down')
    monkeypatch.setattr(huggingface_hub, 'HfApi', Down)
    with pytest.raises(chunk_engine.ModelRefused) as e:
        S.default_loader(REPO_ID, REVISION, 'cpu')
    assert e.value.kind == 'model'

    hub = FakeHub(REAL_SIBLINGS).install(monkeypatch)

    def boom(path, device):
        raise RuntimeError('torch says no')
    monkeypatch.setattr(chunk_engine.ChunkEngine, 'load', staticmethod(boom))
    with pytest.raises(chunk_engine.ModelRefused) as e:
        S.default_loader(REPO_ID, REVISION, 'cpu')
    assert e.value.kind == 'model'

    def refuse(path, device):
        raise chunk_engine.ModelRefused('model_type', 'diffusion')
    monkeypatch.setattr(chunk_engine.ChunkEngine, 'load', staticmethod(refuse))
    with pytest.raises(chunk_engine.ModelRefused) as e:
        S.default_loader(REPO_ID, REVISION, 'cpu')
    assert e.value.kind == 'model_type'
    assert hub.download_calls


def test_token_bucket():
    clock = [0.0]
    b = S.TokenBucket(8.0, 8, clock=lambda: clock[0])
    assert all(b.take() for _ in range(8)) and not b.take()
    clock[0] += 0.125
    assert b.take() and not b.take()
    clock[0] += 100
    assert sum(b.take() for _ in range(20)) == 8
    assert (S.RATE_PER_S, S.RATE_BURST) == (8.0, 8)
