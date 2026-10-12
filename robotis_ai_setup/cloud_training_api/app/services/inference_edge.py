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

ONE start per student at a time (owner, round 3; R1 MINOR): the page retries a
`gpu_not_started` reserve up to twice and a timed-out start keeps running, so a
retry used to hold a second (and third) START_POOL thread and GPU start, and a
class retrying in a shortage filled the pool. A reservation that arrives while a
start launched for one of the SAME student's earlier reservations still runs now
waits on THAT start (`_STARTS`, keyed by user id; process-local like every loop
here, a single uvicorn worker) and submits none. The start's outcome goes to the
newest reservation still waiting; an older one waiting is released at once with
StartSuperseded (its row was superseded when the newer one was created). With
nobody waiting, a success goes to the NEWEST reservation's `on_late`, exactly
once. So one success = one Modal session = one attach. A start that finishes
while nobody waits (the page's pause between two attempts) is attached to the
ended row and terminated as before; the next attempt then starts its own.

ONE terminate path (D33): `terminate_loop`, a startup task of its own that the
session sweep never awaits (F-2), every TERMINATE_PERIOD_S and on `kick()`.

`modal` is imported inside functions only, so the route tests stub it like
test_modal_client.py does. Nothing here logs a token, its hash, an envelope or a
credential object: ids, HTTP statuses and exception TYPES only.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import threading
import time

from app.services import inference_sessions

logger = logging.getLogger(__name__)

APP = os.environ.get("MODAL_INFERENCE_APP_NAME", "edubotics-inference")
CLS = os.environ.get("MODAL_INFERENCE_CLASS_NAME", "CloudInference")
START_TIMEOUT_S = 120.0
# A start older than this is presumed hung (the SDK itself gives up on capacity after
# ~25 min) and is never joined, so one hung start cannot lock its student out.
START_JOIN_MAX_AGE_S = 1800.0
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


class StartSuperseded(Exception):
    """A newer reservation of the same student joined this start and takes its outcome."""


class _Waiter:
    """One reserve request waiting on a start: its outcome future and its late path."""

    __slots__ = ("fut", "on_late")

    def __init__(self, fut: asyncio.Future, on_late) -> None:
        self.fut = fut
        self.on_late = on_late


class _UserStart:
    """The one `sessions.start` of one student, shared by every reservation of theirs
    that arrives while it runs. Touched on its event loop only (`start_session` and
    `_settle` run there), so it needs no lock."""

    __slots__ = ("uid", "loop", "cf", "t0", "waiter", "on_late", "settled")

    def __init__(self, uid: str, loop: asyncio.AbstractEventLoop, cf: concurrent.futures.Future) -> None:
        self.uid = uid
        self.loop = loop
        self.cf = cf
        self.t0 = time.monotonic()
        self.waiter: _Waiter | None = None  # the newest reservation's request, while it waits
        self.on_late = None                 # the newest reservation's late path, even after it gave up
        self.settled = False


_server_lock = threading.Lock()
_server_obj = None
_url_cache: tuple[float, str] | None = None
# The get_url call in flight, shared by every caller that arrives while it runs
# (the startup warm-up and the first prefetches, or a class's prefetches after
# the TTL), so they never queue several get_url calls in CONTROL_POOL.
_url_future: concurrent.futures.Future | None = None
# Tasks the event loop would otherwise hold only weakly (G-7): the late attach
# of a start that outlived its bound, and every prefetch.
_TASKS: set = set()
_in_flight_lock = threading.Lock()
_IN_FLIGHT: set[str] = set()
_kick_event: asyncio.Event | None = None
_kick_loop: asyncio.AbstractEventLoop | None = None
# user id -> the start a new reservation of that student joins (see the module docstring)
_STARTS: dict[str, _UserStart] = {}


def _reset_for_tests() -> None:
    global _server_obj, _url_cache, _url_future, _kick_event, _kick_loop
    with _server_lock:
        _server_obj = None
    _url_cache = None
    _url_future = None
    _kick_event = None
    _kick_loop = None
    with _in_flight_lock:
        _IN_FLIGHT.clear()
    _TASKS.clear()
    _STARTS.clear()


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


def _get_url_blocking():
    return _server().get_url()


async def server_url() -> str | None:
    """The server's https URL from `get_url()` (in CONTROL_POOL), cached URL_TTL_S.

    Callers that arrive while a get_url runs wait for that same call. One that
    outlives PREFETCH_TIMEOUT_S is left behind (never cancelled once running) and
    the next caller starts a fresh one, exactly as without the sharing."""
    global _url_cache, _url_future
    now = time.monotonic()
    cached = _url_cache
    if cached is not None and now - cached[0] < URL_TTL_S:
        return cached[1]
    cf = _url_future
    if cf is None or cf.done():
        cf = CONTROL_POOL.submit(_get_url_blocking)
        _url_future = cf
    if not await _await_without_cancel(cf, PREFETCH_TIMEOUT_S):
        cf.cancel()
        if _url_future is cf:
            _url_future = None
        logger.warning("inference get_url timed out")
        return None
    if cf.cancelled():  # another caller's timeout cancelled it while still queued
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


async def warm_url() -> None:
    """V1-9: resolve the server URL once at API startup (main.py schedules this as a
    task of its own and never awaits it), so the first prefetch of the process finds
    it cached instead of losing the race with a quick Start. get_url runs in
    CONTROL_POOL as always; the cache keeps its URL_TTL_S. A Modal that does not
    answer leaves the cache empty (server_url logged the exception TYPE) and the
    prefetch resolves lazily as before. Never raises."""
    try:
        url = await server_url()
    except Exception as exc:  # noqa: BLE001 - a startup task must never die loudly
        logger.warning("inference url warm-up failed: %s", type(exc).__name__)
        return
    if url:
        logger.info("inference url warm-up: resolved")
    else:
        logger.info("inference url warm-up: not resolved, the first prefetch resolves it")


# ----------------------------------------------------------------- start


def _start_blocking():
    return _server().sessions.start(idle_timeout=inference_sessions.EDGE_IDLE_S)


def _spawn_on(loop: asyncio.AbstractEventLoop, coro) -> None:
    """create_task on `loop` (the caller runs on it), held strongly in _TASKS."""
    task = loop.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _forget(entry: _UserStart) -> None:
    entry.settled = True
    if _STARTS.get(entry.uid) is entry:
        del _STARTS[entry.uid]


def _post_settle(entry: _UserStart, _cf) -> None:
    """The pool future's done callback (any thread): settle on the entry's loop."""
    try:
        entry.loop.call_soon_threadsafe(_settle, entry)
    except RuntimeError:
        pass  # the loop is closed (process shutdown); nothing can be delivered any more


def _settle(entry: _UserStart) -> None:
    """On the loop, once per start: hand its outcome to the newest reservation still
    waiting, else a success to the newest reservation's late path."""
    if entry.settled:
        return
    _forget(entry)
    cf = entry.cf
    if cf.cancelled():
        return  # cancelled only while still queued and with nobody waiting
    waiter, entry.waiter = entry.waiter, None
    exc = cf.exception()
    if waiter is not None and not waiter.fut.done():
        if exc is not None:
            waiter.fut.set_exception(exc)
        else:
            waiter.fut.set_result(cf.result())
        return
    if exc is not None:
        logger.warning("inference late start failed: %s user=%s", type(exc).__name__, entry.uid)
        return
    if entry.on_late is not None:
        _spawn_on(entry.loop, entry.on_late(cf.result()))


def _leave(entry: _UserStart, waiter: _Waiter) -> None:
    """A request stops waiting (its bound, or it was cancelled). An outcome already
    handed to it is not lost: a success goes to its own late path."""
    if entry.waiter is waiter:
        entry.waiter = None
    fut = waiter.fut
    if fut.done() and not fut.cancelled():
        exc = fut.exception()
        if exc is None:
            _spawn_on(entry.loop, waiter.on_late(fut.result()))
        return
    if entry.waiter is None and not entry.settled and entry.cf.cancel():
        _forget(entry)  # still queued and nobody waits: it never runs


async def start_session(user_id: str, on_late):
    """A Modal session's credentials (`.session_id`, `.token`) for the reservation
    calling this, of student `user_id`.

    Joins the start still running for an earlier reservation of the same student
    (never another student's) instead of submitting another. Raises the SDK's
    exception (permanent), StartSuperseded (a newer reservation of the student
    joined and takes the outcome), or EdgeTimeout after START_TIMEOUT_S: 'queued'
    (cancelled, it never ran, nothing can arrive late) or 'running' (left alone; a
    late success is handed to the coroutine function `on_late(creds)` of the newest
    reservation, in a task kept in _TASKS, unless a newer reservation waits for it).
    """
    loop = asyncio.get_running_loop()
    entry = _STARTS.get(user_id)
    if entry is not None and entry.loop is loop and not entry.settled:
        # The reservation calling this superseded the one still waiting here (one live
        # reservation per student): release that request now, the newer one takes over.
        older, entry.waiter = entry.waiter, None
        if older is not None and not older.fut.done():
            older.fut.set_exception(StartSuperseded())
        if time.monotonic() - entry.t0 <= START_JOIN_MAX_AGE_S:
            logger.info("inference start joined: user=%s", user_id)
        else:
            logger.warning("inference start presumed hung, not joined: user=%s", user_id)
            entry = None
    else:
        entry = None
    if entry is None:
        cf = START_POOL.submit(_start_blocking)
        entry = _UserStart(user_id, loop, cf)
        _STARTS[user_id] = entry
        cf.add_done_callback(functools.partial(_post_settle, entry))
    waiter = _Waiter(loop.create_future(), on_late)
    entry.waiter = waiter
    entry.on_late = on_late
    try:
        await asyncio.wait({waiter.fut}, timeout=START_TIMEOUT_S)
    except asyncio.CancelledError:
        _leave(entry, waiter)
        raise
    if waiter.fut.done():
        return waiter.fut.result()  # the credentials, or StartSuperseded / the SDK's exception
    _leave(entry, waiter)
    if entry.settled and entry.cf.cancelled():
        raise EdgeTimeout("queued")
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
