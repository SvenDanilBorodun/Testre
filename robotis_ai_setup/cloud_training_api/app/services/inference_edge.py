"""Cloud-Inferenz: the ONLY Modal caller of the inference path (spec §4.3, D24/D33/D35).

The Modal app `edubotics-inference` is a SESSIONED server: the cloud API, after
its own ownership, cap and model checks, mints a per-session edge token with
`Server.sessions.start()` and later ends the session with
`sessions.terminate(token)` (it takes the TOKEN, not the id — hence the
encrypted copy in `inference_sessions.edge_token_enc`).

Two thread pools, never the default executor that the supabase calls share
(D35, F-2):
  * START_POOL (64 ≥ the global session cap 60): `sessions.start` may wait up to
    25 min for capacity inside the SDK and cannot be cancelled once running. It is
    awaited ≤ START_TIMEOUT_S; still QUEUED then -> cancelled (it never runs);
    RUNNING -> left alone and its late success handed to `on_late`, which attaches
    it to the (by then ended) row so the terminate loop ends it.
  * CONTROL_POOL (8): `terminate` and `get_url`, so a start can never starve them.
  G-7: every waiting `sessions.start` holds one connection of the SDK's shared
  aiohttp connector (default limit 100): keep START_POOL + CONTROL_POOL ≤ 90.

ONE terminate path (D33): `terminate_loop`, a startup task of its own that the
session sweep never awaits (F-2), every TERMINATE_PERIOD_S and on `kick()`.

`modal` is imported inside functions only, so the route tests stub it like
test_modal_client.py does. Nothing here logs a token, its hash, an envelope or a
credential object: ids, HTTP statuses and exception TYPES only.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time

from app.services import inference_sessions

logger = logging.getLogger(__name__)

APP = os.environ.get("MODAL_INFERENCE_APP_NAME", "edubotics-inference")
CLS = os.environ.get("MODAL_INFERENCE_CLASS_NAME", "CloudInference")
START_TIMEOUT_S = 120.0
PREFETCH_TIMEOUT_S = 120.0
URL_TTL_S = 600.0
# D33 (H-7): > the container's 10 s session-check TTL + two check RPCs + two request
# gaps while observations flow, so the robot normally hears WHY before its session dies.
TERMINATE_AFTER_S = 15
TERMINATE_PERIOD_S = 15.0
TERMINATE_BATCH = 32
TERMINATE_CONCURRENCY = 8
TERMINATE_CALL_WAIT_S = 20.0
EDGE_AUTH_HEADER = "Modal-Authorization"   # == cloud_protocol.EDGE_AUTH_HEADER (lockstep, D37)
CONNECT_AUTH_HEADER = "Authorization"      # == cloud_protocol.CONNECT_AUTH_HEADER
PREFETCH_PATH = "/v1/prefetch"             # == cloud_protocol.PREFETCH_PATH

START_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="modal-start")
CONTROL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="modal-control")


class EdgeTimeout(Exception):
    """sessions.start did not finish within START_TIMEOUT_S; args[0] is 'queued' or 'running'."""


_server_lock = threading.Lock()
_server_obj = None
_url_cache: tuple[float, str] | None = None
# Tasks the event loop would otherwise hold only weakly (G-7): the late attach
# of a start that outlived its bound, and every prefetch.
_TASKS: set = set()
_in_flight_lock = threading.Lock()
_IN_FLIGHT: set[str] = set()
_kick_event: asyncio.Event | None = None
_kick_loop: asyncio.AbstractEventLoop | None = None


def _reset_for_tests() -> None:
    global _server_obj, _url_cache, _kick_event, _kick_loop
    with _server_lock:
        _server_obj = None
    _url_cache = None
    _kick_event = None
    _kick_loop = None
    with _in_flight_lock:
        _IN_FLIGHT.clear()
    _TASKS.clear()


def _server():
    """modal.Server.from_name(APP, CLS), built once (lazy, process-wide)."""
    global _server_obj
    with _server_lock:
        if _server_obj is None:
            import modal

            _server_obj = modal.Server.from_name(APP, CLS)
        return _server_obj


def spawn(coro) -> asyncio.Task:
    """create_task, held strongly in _TASKS until it is done."""
    task = asyncio.get_running_loop().create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _swallow(fut) -> None:
    """Retrieve an outcome nobody awaits, so asyncio never logs it."""
    if not fut.cancelled():
        fut.exception()


async def _await_without_cancel(cf: concurrent.futures.Future, timeout: float) -> bool:
    """Wait up to `timeout` for a pool future; True when it is done. Never cancels it."""
    af = asyncio.wrap_future(cf)
    af.add_done_callback(_swallow)
    done, _ = await asyncio.wait({af}, timeout=timeout)
    return bool(done)


# ------------------------------------------------------------------- url


async def server_url() -> str | None:
    """The server's https URL from `get_url()` (in CONTROL_POOL), cached URL_TTL_S."""
    global _url_cache
    now = time.monotonic()
    cached = _url_cache
    if cached is not None and now - cached[0] < URL_TTL_S:
        return cached[1]
    cf = CONTROL_POOL.submit(lambda: _server().get_url())
    if not await _await_without_cancel(cf, PREFETCH_TIMEOUT_S):
        cf.cancel()
        logger.warning("inference get_url timed out")
        return None
    exc = cf.exception()
    if exc is not None:
        logger.warning("inference get_url failed: %s", type(exc).__name__)
        return None
    url = cf.result()
    if not isinstance(url, str) or not url.startswith("https://"):
        logger.warning("inference get_url: unexpected answer %s", type(url).__name__)
        return None
    url = url.rstrip("/")
    _url_cache = (now, url)
    return url


# ----------------------------------------------------------------- start


def _start_blocking():
    return _server().sessions.start(idle_timeout=inference_sessions.EDGE_IDLE_S)


async def start_session(on_late):
    """A fresh Modal session's credentials (`.session_id`, `.token`).

    Raises the SDK's exception, or EdgeTimeout after START_TIMEOUT_S: 'queued'
    (cancelled, it never ran, nothing can arrive late) or 'running' (left alone;
    a late success is handed to the coroutine function `on_late(creds)` in a task
    kept in _TASKS).
    """
    cf = START_POOL.submit(_start_blocking)
    af = asyncio.wrap_future(cf)
    done, _ = await asyncio.wait({af}, timeout=START_TIMEOUT_S)
    if done:
        return af.result()
    if cf.cancel():
        raise EdgeTimeout("queued")
    loop = asyncio.get_running_loop()

    def _late(fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.warning("inference late start failed: %s", type(exc).__name__)
            return
        task = loop.create_task(on_late(fut.result()))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)

    af.add_done_callback(_late)
    raise EdgeTimeout("running")


# ------------------------------------------------------------- terminate


def kick() -> None:
    """Wake the terminate loop now (reserve and end call it). Never blocks; a no-op
    before the loop runs (its first pass sees every due row anyway)."""
    ev, loop = _kick_event, _kick_loop
    if ev is None or loop is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        ev.set()
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass  # the loop is closed


def _terminate_blocking(token: str) -> None:
    # The SDK retries 3 times within 60 s and treats 404 (already gone) as success.
    _server().sessions.terminate(token)


async def terminate_now(token) -> bool:
    """ONE terminate call in CONTROL_POOL for a session no row holds (missing,
    conflict, a credential of the wrong shape). Waits ≤ TERMINATE_CALL_WAIT_S
    without cancelling a running call; True when it finished without error."""
    if not isinstance(token, str) or not token:
        return False
    cf = CONTROL_POOL.submit(_terminate_blocking, token)
    if not await _await_without_cancel(cf, TERMINATE_CALL_WAIT_S):
        cf.cancel()
        logger.warning("inference terminate_now still running after %.0f s", TERMINATE_CALL_WAIT_S)
        return False
    exc = cf.exception()
    if exc is not None:
        logger.warning("inference terminate_now failed: %s", type(exc).__name__)
        return False
    return True


def _in_flight_ids() -> set[str]:
    with _in_flight_lock:
        return set(_IN_FLIGHT)


def _release(rid: str) -> None:
    with _in_flight_lock:
        _IN_FLIGHT.discard(rid)


def _edges_due(sb) -> list:
    data = sb.rpc("inference_edges_due", {"p_min_age_s": TERMINATE_AFTER_S, "p_limit": TERMINATE_BATCH}).execute().data
    return data if isinstance(data, list) else []


def _mark(sb, rid: str) -> None:
    sb.rpc("mark_inference_edge_terminated", {"p_session_id": rid}).execute()


async def _mark_async(sb, rid: str) -> bool:
    try:
        await asyncio.to_thread(_mark, sb, rid)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("inference edge mark failed: %s %s", rid, type(exc).__name__)
        return False


async def _terminate_row(sb, row) -> str:
    rid = row.get("id") if isinstance(row, dict) else None
    if not isinstance(rid, str):
        return "skipped"
    with _in_flight_lock:
        if rid in _IN_FLIGHT:
            return "skipped"
        _IN_FLIGHT.add(rid)
    keep = False
    try:
        age = row.get("ended_age_s")
        if isinstance(age, (int, float)) and not isinstance(age, bool) \
                and age > inference_sessions.EDGE_IDLE_S + 60:
            # Modal has idled the session out by now; give up and erase the ciphertext.
            return "given_up" if await _mark_async(sb, rid) else "failed"
        token = inference_sessions.edge_open(rid, row.get("edge_token_enc"))
        if token is None:
            logger.warning("inference edge undecryptable: %s", rid)
            return "undecryptable" if await _mark_async(sb, rid) else "failed"
        cf = CONTROL_POOL.submit(_terminate_blocking, token)
        del token
        if not await _await_without_cancel(cf, TERMINATE_CALL_WAIT_S):
            if cf.cancel():
                return "queued"  # never ran; retried next pass
            keep = True
            cf.add_done_callback(lambda _f, rid=rid: _release(rid))
            return "running"  # left running; the id stays in flight until it ends
        exc = cf.exception()
        if exc is not None:
            logger.warning("inference edge terminate failed: %s %s", rid, type(exc).__name__)
            return "failed"
        return "terminated" if await _mark_async(sb, rid) else "failed"
    finally:
        if not keep:
            _release(rid)


async def terminate_pass(sb) -> dict:
    """One pass over the due rows, at most TERMINATE_CONCURRENCY at once.
    Returns a count per outcome (tests and logs)."""
    rows = await asyncio.to_thread(_edges_due, sb)
    stats = {k: 0 for k in ("terminated", "given_up", "undecryptable", "failed", "queued", "running", "skipped")}
    if not rows:
        return stats
    sem = asyncio.Semaphore(TERMINATE_CONCURRENCY)

    async def one(row):
        async with sem:
            return await _terminate_row(sb, row)

    for outcome in await asyncio.gather(*(one(r) for r in rows)):
        stats[outcome] += 1
    done = {k: v for k, v in stats.items() if v}
    if done:
        logger.info("inference edge pass: %s", done)
    return stats


async def terminate_loop(sb=None, period_s: float | None = None) -> None:
    """Forever: wait for kick() or the period, then one pass; exceptions are logged
    by type and the loop goes on. Its own startup task (F-2)."""
    global _kick_event, _kick_loop
    _kick_loop = asyncio.get_running_loop()
    _kick_event = asyncio.Event()
    period = TERMINATE_PERIOD_S if period_s is None else period_s
    logger.info("inference terminate loop: period %.0f s", period)
    while True:
        try:
            await asyncio.wait_for(_kick_event.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass
        _kick_event.clear()
        try:
            client = sb
            if client is None:
                from app.services.supabase_client import get_supabase

                client = get_supabase()
            await terminate_pass(client)
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.warning("inference terminate pass failed: %s", type(exc).__name__)


# --------------------------------------------------------------- prefetch


async def prefetch(edge_token: str, connect_token: str) -> None:
    """POST /v1/prefetch to the session's own container, so the model is in its LRU
    while the student reads the summary (D31). Logs the status code or the
    exception TYPE only; never raises."""
    try:
        url = await server_url()
        if not url:
            logger.warning("inference prefetch skipped: no server URL")
            return
        import httpx

        async with httpx.AsyncClient(timeout=PREFETCH_TIMEOUT_S) as client:
            response = await client.post(
                url + PREFETCH_PATH,
                headers={EDGE_AUTH_HEADER: f"Bearer {edge_token}", CONNECT_AUTH_HEADER: f"Bearer {connect_token}"},
                follow_redirects=False,
            )
        logger.info("inference prefetch status=%s", response.status_code)
    except Exception as exc:  # noqa: BLE001 - never str(exc): it may carry the request
        logger.warning("inference prefetch failed: %s", type(exc).__name__)
