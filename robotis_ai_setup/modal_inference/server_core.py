"""Cloud-Inferenz server core: the sessioned server's HTTPS routes, Modal-free (spec §3.3).

``build_app(backend, cache, server_sha)`` returns the FastAPI app the Modal container serves with
uvicorn (inference_app.py) and the tests drive with ``TestClient``; it runs on CPU locally.
HTTPS only (O7'): one request per observation, all of a session's requests reach this container.

* Every route but health needs the CONNECT token (``Authorization: Bearer <token>``); infer and bye
  also ``X-Conn-Id``. The EDGE token is Modal's business: its proxy refused every request without
  a live one before this container saw it, and stamped ``x-modal-server-session-id``, which every
  backend call carries (never a body value).
* The session check (the server heartbeat) is a background task, single-flight per session, never
  awaited by an observation (D42); 30 s of failed checks fail closed, counted from the first
  failure of the current run of failures (J-5).
* An observation's JPEGs are checked by SOF size and scan count BEFORE any decode (D25, D40); one
  GPU thread runs every chunk; one ``EngineCache`` (count-based LRU, D27) holds the models.
* Logs carry session ids, conn ids and exception TYPES only: never a token, a header or a frame.
"""
import asyncio
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time

_HERE = Path(__file__).resolve().parent
# NEVER `_HERE.parents[1]` unguarded: in the container this file is /root/server_core.py and
# Path('/root').parents has ONE entry -> IndexError at import [proved] (the main.py `_safe_parent` trap).
_ROOT = _HERE.parents[1] if len(_HERE.parents) > 1 else None
_SHARED = _ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'inference' if _ROOT else None
if _SHARED is not None and _SHARED.is_dir() and str(_SHARED) not in sys.path:   # a checkout; container: /root
    sys.path.insert(0, str(_SHARED))
import cloud_protocol as P       # noqa: E402
import chunk_engine              # noqa: E402  (module top imports numpy only, §2.7)
from session_backend import SessionDenied   # noqa: E402

from fastapi import FastAPI, Request          # noqa: E402
from fastapi.exceptions import RequestValidationError   # noqa: E402
from fastapi.responses import JSONResponse, Response    # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException   # noqa: E402

log = logging.getLogger('edubotics.inference.server')

LIMITS = {'cadence_steps': 5, 'actions_per_chunk': 50, 'aggregate_new_weight': 0.7,
          'max_obs_per_s': 8.0, 'stall_end_s': 15.0, 'resend_after_s': 2.0}
HEARTBEAT_FAIL_CLOSED_S = 30.0; HEARTBEAT_RETRY_S = 2.0      # D42 (G-1), J-5
HTTPS_CACHE_TTL_S = 10.0; HTTPS_ENTRY_EVICT_S = 120.0
RATE_PER_S = 8.0; RATE_BURST = 8
LRU_MAX_MODELS = 8               # D27: max_concurrency + 2; an entry in use is never evicted
MODEL_MAX_BYTES = 1 << 30
PREFETCH_TIMEOUT_S = 110.0       # < the cloud API's 120 s wait (§4.3)
ALLOW_PATTERNS = ['config.json', 'model.safetensors', 'policy_preprocessor*.json', 'policy_preprocessor*.safetensors',
                  'policy_postprocessor*.json', 'policy_postprocessor*.safetensors', 'edubotics_model_meta.json']
MODEL_REPO_RE = re.compile(r'EduBotics-Solutions/[A-Za-z0-9][A-Za-z0-9._-]{0,95}')
REVISION_RE = re.compile(r'[0-9a-f]{40}')

STATUS = {'bad_frame': 400, 'version': 400, 'auth': 401, 'replaced': 409, 'ended': 410, 'model': 422,
          'model_type': 422, 'cameras': 422, 'shape': 422, 'state': 422, 'rate': 429, 'internal': 500,
          'unavailable': 503}
VERDICT_CODES = frozenset({'auth', 'ended', 'replaced'})      # a session check answering these ends the run
GPU_THREADS = 1
CPU_THREADS = 4
STATS_WINDOW = 64
_STATS_MAX_BYTES = 2048


# ---------------------------------------------------------------- models

def _allowed(name):
    return any(fnmatch.fnmatch(name, p) for p in ALLOW_PATTERNS)


def default_loader(repo, revision, device):
    """Validate the ref, check the size, download the allowed root files, load (``ChunkEngine.load``).

    Every failure is ``ModelRefused`` ('model', or 'model_type' from the engine's own validation).
    """
    if (type(repo) is not str or not MODEL_REPO_RE.fullmatch(repo)
            or type(revision) is not str or not REVISION_RE.fullmatch(revision)):
        raise chunk_engine.ModelRefused('model', 'bad model reference')
    from huggingface_hub import HfApi, snapshot_download
    token = os.environ.get('HF_TOKEN') or None
    try:
        info = HfApi(token=token).model_info(repo, revision=revision, files_metadata=True)
        total = sum(int(s.size or 0) for s in (info.siblings or []) if _allowed(s.rfilename))
    except Exception as e:  # noqa: BLE001 - any hub failure refuses the model
        raise chunk_engine.ModelRefused('model', f'model info: {type(e).__name__}') from None
    if total > MODEL_MAX_BYTES:
        raise chunk_engine.ModelRefused('model', 'model too large')
    try:
        path = snapshot_download(repo, revision=revision, allow_patterns=ALLOW_PATTERNS, token=token)
    except Exception as e:  # noqa: BLE001
        raise chunk_engine.ModelRefused('model', f'download: {type(e).__name__}') from None
    try:
        return chunk_engine.ChunkEngine.load(path, device)
    except chunk_engine.ModelRefused:
        raise
    except Exception as e:  # noqa: BLE001
        raise chunk_engine.ModelRefused('model', f'load: {type(e).__name__}') from None


class _Slot:
    __slots__ = ('engine', 'refs')

    def __init__(self, engine):
        self.engine = engine
        self.refs = 0


class Lease:
    """One use of a cached engine. ``release()`` is idempotent; also a context manager."""

    def __init__(self, cache, key, engine):
        self._cache = cache
        self._key = key
        self.engine = engine
        self._released = False
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
        self._cache._release(self._key)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class EngineCache:
    """Thread-safe count-based model LRU (D27): loads once per key (a per-key lock), keeps at most
    ``max_models`` entries, evicts least-recently-used entries with refcount 0 only."""

    def __init__(self, device, max_models=LRU_MAX_MODELS, loader=None):
        self.device = device
        self.max_models = max_models
        self._loader = loader or default_loader
        self._lock = threading.Lock()
        self._slots = OrderedDict()            # (repo, revision) -> _Slot, least recently used first
        self._key_locks = {}

    def init_device(self):
        if str(self.device).startswith('cuda'):
            import torch
            torch.cuda.init()
            torch.zeros(1, device='cuda')

    def _lease_locked(self, key):
        slot = self._slots[key]
        slot.refs += 1
        self._slots.move_to_end(key)
        return Lease(self, key, slot.engine)

    def acquire_cached(self, repo, revision):
        """A lease on an already loaded engine, or None. Never loads, never waits for a load."""
        key = (repo, revision)
        with self._lock:
            return self._lease_locked(key) if key in self._slots else None

    def acquire(self, repo, revision):
        """A lease on the engine, loading it on a miss (``ModelRefused`` propagates, nothing cached)."""
        key = (repo, revision)
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                if key in self._slots:
                    return self._lease_locked(key)
            engine = self._loader(repo, revision, self.device)
            with self._lock:
                self._slots[key] = _Slot(engine)
                lease = self._lease_locked(key)
                evicted = self._evict_locked()
        if evicted:
            self._empty_device_cache()
        return lease

    def _release(self, key):
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None and slot.refs > 0:
                slot.refs -= 1
            evicted = self._evict_locked()
        if evicted:
            self._empty_device_cache()

    def _evict_locked(self):
        evicted = 0
        while len(self._slots) > self.max_models:
            victim = next((k for k, s in self._slots.items() if s.refs == 0), None)
            if victim is None:                 # only in-use entries: stay above the count until a release
                break
            del self._slots[victim]
            evicted += 1
        return evicted

    def _empty_device_cache(self):
        if str(self.device).startswith('cuda'):
            import torch
            torch.cuda.empty_cache()

    def loaded(self):
        with self._lock:
            return list(self._slots)

    def refcount(self, repo, revision):
        with self._lock:
            slot = self._slots.get((repo, revision))
            return slot.refs if slot is not None else 0


class TokenBucket:
    """``rate`` tokens per second, at most ``burst``; ``take()`` -> bool."""

    def __init__(self, rate=RATE_PER_S, burst=RATE_BURST, clock=time.monotonic):
        self.rate = float(rate)
        self.burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._t = clock()

    def take(self):
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + max(0.0, now - self._t) * self.rate)
        self._t = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


# ---------------------------------------------------------------- the app

class _Answer(Exception):
    """An error frame answer raised inside a handler."""

    def __init__(self, code, *, seq=None, reason=None, server_v=None, status=None):
        super().__init__(code)
        self.code = code
        self.seq = seq
        self.reason = reason
        self.server_v = server_v
        self.status = status


def _error_response(code, *, seq=None, reason=None, server_v=None, status=None):
    if code not in P.ERROR_CODES:
        code = 'internal'
    if not (type(reason) is str and 1 <= len(reason) <= 64):
        reason = None
    if not (type(seq) is int and 0 <= seq <= P.MAX_SEQ):
        seq = None
    return Response(content=P.build_error(code, seq=seq, reason=reason, server_v=server_v),
                    status_code=status or STATUS[code], media_type=P.CONTENT_TYPE)


def _p50(values):
    if not values:
        return None
    ordered = sorted(values)
    return round(float(ordered[len(ordered) // 2]), 2)


class _Entry:
    """One session's state in this container."""

    def __init__(self, grant, modal_session_id, conn_id, now, clock):
        self.grant = grant
        self.modal_session_id = modal_session_id
        self.conn_id = conn_id
        self.checked_at = now
        self.last_ok = now
        self.failing_since = None          # J-5: the first failure of the current run of failed checks
        self.fails = 0
        self.hb_task = None
        self.denied = None                 # (code, reason) of a check that ended the session
        self.bucket = TokenBucket(clock=clock)
        self.last_used = now
        self.summary = None
        self.chunks = 0
        self.rate_drops = 0
        self.internal_errors = 0
        self.gpu_ms = deque(maxlen=STATS_WINDOW)
        self.decode_ms = deque(maxlen=STATS_WINDOW)

    def stats(self):
        return {'chunks': self.chunks, 'gpu_ms_p50': _p50(self.gpu_ms), 'decode_ms_p50': _p50(self.decode_ms),
                'rate_drops': self.rate_drops, 'internal_errors': self.internal_errors}


def _bearer_token(request):
    value = request.headers.get(P.CONNECT_AUTH_HEADER)
    if type(value) is not str:
        return None
    scheme, _, token = value.partition(' ')
    if scheme.lower() != 'bearer' or not P.TOKEN_RE.fullmatch(token):
        return None
    return token


def _conn_id(request):
    value = request.headers.get(P.CONN_HEADER)
    if type(value) is not str or not P.CONN_ID_RE.fullmatch(value):
        return None
    return value


async def _read_capped(request):
    """The body, at most MAX_FRAME_BYTES (Content-Length beyond it -> 413 without reading)."""
    declared = request.headers.get('content-length')
    if declared is not None:
        try:
            n = int(declared)
        except ValueError:
            raise _Answer('bad_frame') from None
        if n < 0:
            raise _Answer('bad_frame')
        if n > P.MAX_FRAME_BYTES:
            raise _Answer('bad_frame', status=413)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > P.MAX_FRAME_BYTES:
            raise _Answer('bad_frame', status=413)
    return bytes(buf)


def _merge_stats(server_stats, robot_stats):
    out = dict(server_stats or {})
    if isinstance(robot_stats, dict):
        merged = dict(out, robot=robot_stats)
        try:
            if len(json.dumps(merged, allow_nan=False)) <= _STATS_MAX_BYTES:
                return merged
        except (TypeError, ValueError):
            pass
    return out


def build_app(backend, cache, server_sha, *, limits=LIMITS, clock=time.monotonic, cache_ttl_s=HTTPS_CACHE_TTL_S):
    """The FastAPI app of one container: health, hello, infer, bye, prefetch (§2.2)."""
    gpu_pool = ThreadPoolExecutor(max_workers=GPU_THREADS, thread_name_prefix='gpu')
    cpu_pool = ThreadPoolExecutor(max_workers=CPU_THREADS, thread_name_prefix='cpu')
    entries = {}                    # sha256(connect token) -> _Entry
    background = set()              # strong references to running background tasks

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            gpu_pool.shutdown(wait=False, cancel_futures=True)
            cpu_pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(_Answer)
    async def _on_answer(_request, exc):
        return _error_response(exc.code, seq=exc.seq, reason=exc.reason, server_v=exc.server_v, status=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _on_http(_request, exc):          # an unknown route or method still answers with a frame
        return _error_response('bad_frame', status=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _on_validation(_request, _exc):
        return _error_response('bad_frame')

    @app.exception_handler(Exception)
    async def _on_crash(_request, exc):
        log.error('unhandled %s', type(exc).__name__)
        return _error_response('internal')

    def _key(token):
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def _sweep(now):
        stale = [k for k, e in entries.items() if now - e.last_used >= HTTPS_ENTRY_EVICT_S]
        for k in stale:
            del entries[k]

    def _denied(exc):
        code = exc.code if exc.code in ('auth', 'ended', 'replaced', 'unavailable') else 'unavailable'
        return _Answer(code, reason=exc.reason if code == 'ended' else None)

    async def _connect(token, modal_session_id, conn_id):
        try:
            return await backend.connect(token, modal_session_id, conn_id)
        except asyncio.CancelledError:
            raise
        except SessionDenied as e:
            raise _denied(e) from None
        except Exception as e:  # noqa: BLE001
            log.warning('session connect failed: %s', type(e).__name__)
            raise _Answer('unavailable') from None

    async def _lease(entry):
        grant = entry.grant
        lease = cache.acquire_cached(grant.model_repo, grant.model_revision)
        if lease is not None:
            return lease
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(cpu_pool, cache.acquire, grant.model_repo, grant.model_revision)

    async def _end_server_error(token, modal_session_id, conn_id, stats):
        try:
            await backend.end(token, modal_session_id, conn_id, 'server_error', stats)
        except Exception as e:  # noqa: BLE001 - end never raises; belt and braces
            log.warning('session end failed: %s', type(e).__name__)

    def _check_failed(entry):
        now = clock()
        entry.fails += 1
        if entry.failing_since is None:
            entry.failing_since = now
        entry.checked_at = now - cache_ttl_s + HEARTBEAT_RETRY_S     # the next attempt >= 2 s later

    async def _check(entry, token):
        """The background session check (doubles as the server heartbeat). Never raises."""
        try:
            await backend.heartbeat(token, entry.modal_session_id, entry.conn_id, entry.stats())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - H-3: anything but a verdict is a transport failure
            if isinstance(e, SessionDenied) and e.code in VERDICT_CODES:
                entry.denied = (e.code, e.reason)
                log.info('session %s ended by check: %s', entry.grant.session_id, e.code)
                return
            log.warning('session check failed: %s', type(e).__name__)
            _check_failed(entry)
            return
        now = clock()
        entry.checked_at = entry.last_ok = now
        entry.fails = 0
        entry.failing_since = None

    def _start_check(entry, token):
        task = asyncio.get_running_loop().create_task(_check(entry, token))
        entry.hb_task = task
        background.add(task)
        task.add_done_callback(background.discard)

    def _auth(request, *, need_conn):
        token = _bearer_token(request)
        conn_id = _conn_id(request) if need_conn else None
        if token is None or (need_conn and conn_id is None):
            raise _Answer('auth')
        return token, conn_id

    @app.get(P.HEALTH_PATH)
    async def health():
        return JSONResponse({'ok': True, 'v': list(P.SERVER_ACCEPTS), 'server_sha': server_sha})

    @app.post(P.HELLO_PATH)
    async def hello(request: Request):
        token, _ = _auth(request, need_conn=False)
        modal_session_id = request.headers.get(P.MODAL_SESSION_HEADER)
        body = await _read_capped(request)
        try:
            info = P.parse_hello(*P.decode(body))
        except P.ProtocolError:
            raise _Answer('bad_frame') from None
        if info['v'] not in P.SERVER_ACCEPTS:
            raise _Answer('version', server_v=list(P.SERVER_ACCEPTS))
        conn_id = info['conn_id']
        now = clock()
        _sweep(now)
        grant = await _connect(token, modal_session_id, conn_id)
        entry = _Entry(grant, modal_session_id, conn_id, now, clock)
        try:
            lease = await _lease(entry)
        except chunk_engine.ModelRefused as e:
            log.warning('session %s model refused: %s', grant.session_id, e.kind)
            await _end_server_error(token, modal_session_id, conn_id, None)
            raise _Answer(e.kind) from None
        except Exception as e:  # noqa: BLE001 - a load that crashed is a model failure
            log.warning('session %s model load failed: %s', grant.session_id, type(e).__name__)
            await _end_server_error(token, modal_session_id, conn_id, None)
            raise _Answer('model') from None
        try:
            entry.summary = lease.engine.summary
        finally:
            lease.release()
        entries[_key(token)] = entry
        n = min(int(limits['actions_per_chunk']), int(entry.summary['chunk_size']))
        served = dict(limits, actions_per_chunk=n, max_end_in_s=grant.max_end_in_s)
        log.info('session %s hello conn %s', grant.session_id, conn_id)
        return Response(content=P.build_ready(server_sha=server_sha, policy=entry.summary, limits=served),
                        media_type=P.CONTENT_TYPE)

    @app.post(P.INFER_PATH)
    async def infer(request: Request):
        token, conn_id = _auth(request, need_conn=True)
        modal_session_id = request.headers.get(P.MODAL_SESSION_HEADER)
        body = await _read_capped(request)
        key = _key(token)
        _sweep(clock())
        entry = entries.get(key)
        if entry is None or entry.modal_session_id != modal_session_id or entry.conn_id != conn_id:   # F-15
            try:
                grant = await _connect(token, modal_session_id, conn_id)
            except _Answer:
                entries.pop(key, None)
                raise
            entry = _Entry(grant, modal_session_id, conn_id, clock(), clock)
            entries[key] = entry
        now = clock()
        entry.last_used = now
        if entry.denied is not None:
            entries.pop(key, None)
            code, reason = entry.denied
            raise _Answer(code, reason=reason if code == 'ended' else None)
        if (entry.fails >= 1 and entry.failing_since is not None
                and now - entry.failing_since >= HEARTBEAT_FAIL_CLOSED_S):          # J-5 fail closed
            entries.pop(key, None)
            log.warning('session %s failed closed: no successful check', entry.grant.session_id)
            raise _Answer('unavailable')
        if now - entry.checked_at >= cache_ttl_s and (entry.hb_task is None or entry.hb_task.done()):
            _start_check(entry, token)                                          # NOT awaited (D42)
        return await _observation(entry, token, body)

    async def _observation(entry, token, body):
        # 1. the frame
        try:
            header, payload = P.decode(body)
            if header['t'] != 'obs':
                raise P.ProtocolError('not_obs')
            seq, obs_e, cams, state, _task = P.parse_obs(header, payload)
        except P.ProtocolError:
            raise _Answer('bad_frame') from None
        # 2. the session's rate
        if not entry.bucket.take():
            entry.rate_drops += 1
            raise _Answer('rate', seq=seq)
        try:
            lease = await _lease(entry)
        except chunk_engine.ModelRefused as e:
            await _end_server_error(token, entry.modal_session_id, entry.conn_id, entry.stats())
            raise _Answer(e.kind, seq=seq) from None
        except Exception as e:  # noqa: BLE001
            log.warning('session %s model load failed: %s', entry.grant.session_id, type(e).__name__)
            raise _Answer('internal', seq=seq) from None
        try:
            summary = lease.engine.summary
            entry.summary = summary
            # 3. cameras, 4. state
            if set(cams) != set(summary['expected_cameras']):
                raise _Answer('cameras', seq=seq)
            if state.shape[0] != summary['state_dim']:
                raise _Answer('state', seq=seq)
            # 5. size and scans BEFORE any decode (D25, D40), then the decode in the CPU pool
            sizes = {}
            for name, data in cams.items():
                _, h, w = summary['image_shapes'][name]
                if P.jpeg_size(data) != (w, h) or P.jpeg_scans(data) > P.MAX_JPEG_SCANS:
                    raise _Answer('shape', seq=seq)
                sizes[name] = (w, h)
            loop = asyncio.get_running_loop()

            def decode_all():
                t0 = time.perf_counter()
                out = {name: chunk_engine.decode_jpeg_rgb(cams[name], w, h) for name, (w, h) in sizes.items()}
                return out, (time.perf_counter() - t0) * 1000.0
            try:
                images, decode_ms = await loop.run_in_executor(cpu_pool, decode_all)
            except Exception as e:  # noqa: BLE001 - a JPEG the decoder chokes on will not get better
                log.warning('session %s decode failed: %s', entry.grant.session_id, type(e).__name__)
                raise _Answer('shape', seq=seq) from None
            if any(img is None for img in images.values()):
                raise _Answer('shape', seq=seq)
            # 6. the chunk on the ONE GPU thread
            n = min(int(limits['actions_per_chunk']), int(summary['chunk_size']))
            submitted = time.perf_counter()

            def run_chunk():
                started = time.perf_counter()
                actions, gpu_ms = lease.engine.chunk(images, state, n)
                return actions, gpu_ms, (started - submitted) * 1000.0
            try:
                actions, gpu_ms, queue_ms = await loop.run_in_executor(gpu_pool, run_chunk)
                frame = P.build_chunk(seq, obs_e, actions, gpu_ms=gpu_ms, queue_ms=queue_ms, decode_ms=decode_ms)
            except Exception as e:  # noqa: BLE001
                entry.internal_errors += 1
                log.warning('session %s chunk failed: %s', entry.grant.session_id, type(e).__name__)
                raise _Answer('internal', seq=seq) from None
        finally:
            lease.release()
        # 7. the answer
        entry.chunks += 1
        entry.gpu_ms.append(float(gpu_ms))
        entry.decode_ms.append(float(decode_ms))
        return Response(content=frame, media_type=P.CONTENT_TYPE)

    @app.post(P.BYE_PATH)
    async def bye(request: Request):
        token, conn_id = _auth(request, need_conn=True)
        modal_session_id = request.headers.get(P.MODAL_SESSION_HEADER)
        body = await _read_capped(request)
        try:
            reason, robot_stats = P.parse_bye(*P.decode(body))
        except P.ProtocolError:
            raise _Answer('bad_frame') from None
        key = _key(token)
        entry = entries.get(key)
        if entry is not None and entry.conn_id == conn_id:     # a stale connection's bye leaves the live one
            entries.pop(key, None)
        else:
            entry = None
        stats = _merge_stats(entry.stats() if entry is not None else None, robot_stats)
        try:
            await backend.end(token, modal_session_id, conn_id, reason, stats or None)
        except Exception as e:  # noqa: BLE001 - end never raises; belt and braces
            log.warning('session end failed: %s', type(e).__name__)
        log.info('session bye conn %s: %s', conn_id, reason)
        return Response(status_code=204)

    @app.post(P.PREFETCH_PATH)
    async def prefetch(request: Request):
        token, _ = _auth(request, need_conn=False)
        modal_session_id = request.headers.get(P.MODAL_SESSION_HEADER)
        await _read_capped(request)            # an empty request; read (capped) and ignored
        try:
            grant = await backend.peek(token, modal_session_id)
        except asyncio.CancelledError:
            raise
        except SessionDenied as e:
            raise _denied(e) from None
        except Exception as e:  # noqa: BLE001
            log.warning('session peek failed: %s', type(e).__name__)
            raise _Answer('unavailable') from None

        def load():
            cache.acquire(grant.model_repo, grant.model_revision).release()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(cpu_pool, load)

        def consume(f):                          # a load that ends after the answer must not go unretrieved
            if not f.cancelled() and f.exception() is not None:
                log.warning('session %s prefetch load failed: %s', grant.session_id, type(f.exception()).__name__)
        try:
            await asyncio.wait_for(asyncio.shield(fut), PREFETCH_TIMEOUT_S)
        except asyncio.TimeoutError:
            fut.add_done_callback(consume)
            log.info('session %s prefetch still loading', grant.session_id)
            return Response(status_code=204)
        except chunk_engine.ModelRefused as e:
            log.warning('session %s prefetch model refused: %s', grant.session_id, e.kind)
            raise _Answer(e.kind) from None
        except Exception as e:  # noqa: BLE001
            log.warning('session %s prefetch load failed: %s', grant.session_id, type(e).__name__)
            raise _Answer('model') from None
        return Response(status_code=204)

    return app
