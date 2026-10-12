#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Cloud-Inferenz HTTPS transport (robot side, spec §5.6). Own thread + asyncio; never calls rclpy.

O7' (owner): HTTPS ONLY — one ``POST /v1/infer`` per observation to the session's own sticky
container, over one kept-alive connection. No WebSocket, no ping loop, no keepalive, no
transport switch.

Credentials (D24, D37). Both tokens are private attributes of the client and travel ONLY as
request headers on every request: ``Modal-Authorization: Bearer <edge token>`` (Modal's proxy
refuses a request without a live session's token before any container starts) and
``Authorization: Bearer <connect token>`` (the session row's credential). Never in a body, a URL, a
log line, an exception text or the snapshot.

Logging (D30, R-9). The repr of an aiohttp ``ClientResponseError`` carries the request's headers,
i.e. both tokens. This module never formats an exception through its repr (no percent-r or bang-r
conversion, no exception info on a log call, no logger exception method): it logs
``type(e).__name__`` and an HTTP status, nothing else, and the loop's exception handler does the
same.

The schedule never cancels a request (D43, M1): every request ends on its own — with a chunk, an
error frame, a frameless answer, a transport failure or the client's own ``INFER_HTTP_TIMEOUT_S``
(D44) — and every end without a usable chunk is reported as ``(seq, obs_e, None)``, which frees the
schedule's slot. A cancel would close the kept-alive connection, and a fresh connection's first
observation costs 3 RTT on a cold TCP window. The only cancels are in ``_shutdown_tasks`` (close).

Answers without an error frame come from Modal's proxy, a school proxy or the network (D38, F-11):
no redirect is ever followed; a frameless 401 or 404 ends the run (``session_gone``); a 3xx, 403,
429, 5xx, any other status, or a 200 that is not ``application/octet-stream`` (a captive portal) is
``cloud_down``; DNS/TCP/TLS failures and timeouts are ``unreachable``. Before the first ``ready``
these decide the hello (up to ``HELLO_ATTEMPTS``); after it they are transient and the schedule's
stall rule decides.
"""

import asyncio
import collections
import json
import logging
import statistics
import threading
import time
from typing import NamedTuple, Optional

import aiohttp

from physical_ai_server.inference import cloud_protocol

_log = logging.getLogger('physical_ai_server.cloud_client')

_STATS_MAX_BYTES = 1024          # a bye's optional stats object (spec §2.2)
_P50_WINDOW = 64                 # rtt / gpu facts over the last 64 chunks
_READ_CHUNK = 65536
_FAILURE_LOG_EVERY_S = 5.0


class EndInfo(NamedTuple):
    kind: str                    # 'server' (an error frame) | 'robot' (the client's own verdict)
    code: str
    reason: Optional[str]
    server_v: Optional[tuple]


class ClientSnapshot(NamedTuple):
    phase: str                   # 'connecting' | 'ready' | 'ended'
    ready: Optional[tuple]       # (policy, limits) from the ready frame
    end: Optional[EndInfo]
    rtt_ms_p50: Optional[float]
    gpu_ms_p50: Optional[float]
    failures: int


class _Answer(NamedTuple):
    kind: str                    # 'frame' | 'error' | 'session_gone' | 'cloud_down' | 'unreachable' | 'internal'
    header: Optional[dict] = None
    payload: object = None
    error: Optional[tuple] = None    # parse_error's (code, retry, seq, reason, server_v)
    status: Optional[int] = None
    rtt_ms: Optional[float] = None


class CloudClient:
    CONNECT_TIMEOUT_S = 10.0                                  # TCP + TLS
    HELLO_HTTP_TIMEOUT_S = 90.0                               # hello -> ready (a model download + load if prefetch missed)
    INFER_HTTP_TIMEOUT_S = 8.0                                # D44: per request, connect included
    BYE_TIMEOUT_S = 1.0
    HELLO_ATTEMPTS = 3
    BACKOFF_S = (0.5, 1.0, 2.0)

    def __init__(self, *, token, edge_token, conn_id, hello, base_url=cloud_protocol.CLOUD_BASE_URL,
                 clock=time.monotonic):
        self.__token = token
        self.__edge = edge_token
        self._conn_id = conn_id
        self._base_url = base_url
        self._clock = clock
        # Built here so a bad field raises inside the node's start guard, not on the loop thread.
        self._hello_frame = cloud_protocol.build_hello(
            conn_id=conn_id, robot_type=hello['robot_type'], robot_profile=hello['robot_profile'],
            cameras=hello['cameras'], state_dim=hello['state_dim'], fps=hello['fps'])
        self._lock = threading.Lock()
        self._phase = 'connecting'
        self._ready = None
        self._end = None
        self._results = []
        self._offers = []
        self._rtt = collections.deque(maxlen=_P50_WINDOW)
        self._gpu = collections.deque(maxlen=_P50_WINDOW)
        self._failures = 0
        self._closing = False
        self._close_reason = None
        self._close_stats = None
        self._loop = None
        self._wake = None
        self._thread = None
        self._tasks = set()
        self._last_failure_log = -1e9

    def __repr__(self):
        return f'<CloudClient conn_id={self._conn_id} phase={self._phase}>'

    # ── the tick's side (any thread) ─────────────────────────────────────────

    def start(self):
        """Run the transport on a daemon thread ('cloud-inference-client')."""
        self._thread = threading.Thread(target=self._thread_main, name='cloud-inference-client', daemon=True)
        self._thread.start()

    def offer(self, frame, seq, obs_e):
        """Start ONE request for this observation. The schedule never offers while one is on the
        wire; a request is never withdrawn or cancelled (D43)."""
        with self._lock:
            if self._closing or self._end is not None:
                return
            self._offers.append((frame, seq, obs_e))
        self._kick()

    def take_chunks(self):
        """Every request that ENDED since the last call: ``(seq, obs_e, actions | None)`` —
        float32 (n, dim) for a chunk, None for any end without one."""
        with self._lock:
            out, self._results = self._results, []
        return out

    def snapshot(self):
        with self._lock:
            rtt = list(self._rtt)
            gpu = list(self._gpu)
            return ClientSnapshot(
                phase='ended' if self._end is not None else self._phase,
                ready=self._ready, end=self._end,
                rtt_ms_p50=round(statistics.median(rtt), 1) if rtt else None,
                gpu_ms_p50=round(statistics.median(gpu), 1) if gpu else None,
                failures=self._failures)

    def close(self, reason, stats=None):
        """Thread-safe, non-blocking. ``reason`` ∈ cloud_protocol.BYE_REASONS sends a bye (≤ 1 s,
        result ignored); None = the server ended it, no bye."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._close_reason = reason
            self._close_stats = stats
        self._kick()

    def join(self, timeout):
        t = self._thread
        if t is not None:
            t.join(timeout)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _kick(self):
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:          # the loop already closed
            pass

    def _headers(self, with_conn):
        h = {cloud_protocol.EDGE_AUTH_HEADER: 'Bearer ' + self.__edge,
             cloud_protocol.CONNECT_AUTH_HEADER: 'Bearer ' + self.__token,
             'Content-Type': cloud_protocol.CONTENT_TYPE}
        if with_conn:
            h[cloud_protocol.CONN_HEADER] = self._conn_id
        return h

    @staticmethod
    def _timeout(total, connect=None):
        """J-1: the ONE request-timeout helper. aiohttp ceils a timeout at or above ceil_threshold
        to the next whole second (0–1 s late), so the threshold sits above the timeout. A
        per-request timeout REPLACES the session's: a connect bound must be passed again here."""
        return aiohttp.ClientTimeout(total=total, connect=connect, ceil_threshold=total + 1)

    def _hello_timeout(self):
        return self._timeout(self.HELLO_HTTP_TIMEOUT_S, connect=self.CONNECT_TIMEOUT_S)

    def _infer_timeout(self):
        return self._timeout(self.INFER_HTTP_TIMEOUT_S)        # connect included (D44)

    def _bye_timeout(self):
        return self._timeout(self.BYE_TIMEOUT_S)

    def _make_connector(self):
        return aiohttp.TCPConnector(limit=2)

    def _set_end(self, end):
        with self._lock:
            first = self._end is None
            if first:
                self._end = end
                self._offers = []
        if first:
            _log.warning('cloud inference ended: %s/%s', end.kind, end.code)

    def _report(self, seq, obs_e, actions):
        with self._lock:
            if self._end is None:
                self._results.append((seq, obs_e, actions))

    def _note_failure(self, what):
        with self._lock:
            self._failures += 1
            n = self._failures
        now = time.monotonic()
        if now - self._last_failure_log >= _FAILURE_LOG_EVERY_S:
            self._last_failure_log = now
            _log.warning('cloud infer request ended without a chunk: %s (failures so far: %d)', what, n)

    def _loop_exception_handler(self, loop, context):
        exc = context.get('exception')
        _log.error('cloud client loop error: %s', type(exc).__name__ if exc is not None else 'unknown')

    # ── the transport (loop thread) ──────────────────────────────────────────

    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except BaseException as e:  # noqa: BLE001 — the transport must end, never take the node down
            _log.error('cloud client stopped: %s', type(e).__name__)
            self._set_end(EndInfo('robot', 'internal', None, None))

    async def _main(self):
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(self._loop_exception_handler)
        self._wake = asyncio.Event()
        self._loop = loop
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=self.CONNECT_TIMEOUT_S),
            connector=self._make_connector(), headers={'User-Agent': 'edubotics-robot/1'})
        try:
            hello = asyncio.ensure_future(self._hello(session))
            self._tasks.add(hello)
            while not hello.done():
                if self._closing:
                    break
                await asyncio.wait({hello}, timeout=0.1)
            self._tasks.discard(hello)
            while not self._closing:
                with self._lock:
                    offers, self._offers = self._offers, []
                    ended = self._end is not None
                if not ended:
                    for frame, seq, obs_e in offers:
                        task = asyncio.ensure_future(self._infer(session, frame, seq, obs_e))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
            if self._close_reason is not None:
                await self._bye(session, self._close_reason, self._close_stats)
        finally:
            await self._shutdown_tasks()
            await session.close()

    async def _shutdown_tasks(self):
        """close(): the ONLY place a request task is cancelled (the run is over)."""
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _post(self, session, path, body, timeout, with_conn):
        """One request -> _Answer. Never raises (CancelledError aside)."""
        t0 = self._clock()
        try:
            async with session.post(cloud_protocol.url(path, self._base_url), data=body,
                                    headers=self._headers(with_conn), timeout=timeout,
                                    allow_redirects=False) as resp:
                status = resp.status
                # The raw header: aiohttp's content_type property reads a MISSING header as
                # application/octet-stream, and a proxy's bare answer has none.
                ctype = resp.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                octet = ctype == cloud_protocol.CONTENT_TYPE
                data = await self._read_capped(resp)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — DNS/TCP/TLS failure, reset, timeout (D30: name only)
            return _Answer('unreachable', error=(type(e).__name__,))
        rtt_ms = (self._clock() - t0) * 1000.0
        frame = None
        if octet and data is not None:
            try:
                frame = cloud_protocol.decode(data)
            except cloud_protocol.ProtocolError:
                frame = None
        if status == 200:
            if not octet:
                return _Answer('cloud_down', status=status)            # a captive portal page (F-11)
            if frame is None:
                return _Answer('internal', status=status)              # a 200 octet-stream that is no frame
            header, payload = frame
            if header.get('t') == 'error':
                return self._error_answer(header, payload, status)
            return _Answer('frame', header=header, payload=payload, status=status, rtt_ms=rtt_ms)
        if frame is not None and frame[0].get('t') == 'error':
            answer = self._error_answer(frame[0], frame[1], status)
            if answer.kind == 'error':
                return answer
        if status in (401, 404):
            return _Answer('session_gone', status=status)              # D38: Modal: no such live session
        return _Answer('cloud_down', status=status)

    @staticmethod
    def _error_answer(header, payload, status):
        try:
            return _Answer('error', error=cloud_protocol.parse_error(header, payload), status=status)
        except cloud_protocol.ProtocolError:
            return _Answer('internal' if status == 200 else 'cloud_down', status=status)

    @staticmethod
    async def _read_capped(resp):
        """The body, or None when it is larger than one frame may be."""
        cl = resp.content_length
        if cl is not None and cl > cloud_protocol.MAX_FRAME_BYTES:
            return None
        buf = bytearray()
        async for part in resp.content.iter_chunked(_READ_CHUNK):
            buf += part
            if len(buf) > cloud_protocol.MAX_FRAME_BYTES:
                return None
        return bytes(buf)

    async def _hello(self, session):
        unreachable = cloud_down = 0
        for attempt in range(self.HELLO_ATTEMPTS):
            if attempt:
                await asyncio.sleep(self.BACKOFF_S[min(attempt - 1, len(self.BACKOFF_S) - 1)])
            if self._closing:
                return
            a = await self._post(session, cloud_protocol.HELLO_PATH, self._hello_frame,
                                 self._hello_timeout(), with_conn=False)
            if a.kind == 'frame':
                try:
                    policy, limits = cloud_protocol.parse_ready(a.header, a.payload)
                except cloud_protocol.ProtocolError:
                    self._set_end(EndInfo('robot', 'internal', None, None))
                    return
                with self._lock:
                    self._ready = (policy, limits)
                    self._phase = 'ready'
                _log.info('cloud inference ready (attempt %d)', attempt + 1)
                return
            if a.kind == 'error':
                code, _retry, _seq, reason, server_v = a.error
                self._set_end(EndInfo('server', code, reason, server_v))
                return
            if a.kind == 'session_gone':
                self._set_end(EndInfo('robot', 'session_gone', None, None))
                return
            if a.kind == 'internal':
                self._set_end(EndInfo('robot', 'internal', None, None))
                return
            if a.kind == 'unreachable':
                unreachable += 1
                _log.warning('cloud hello attempt %d failed: %s', attempt + 1, a.error[0])
            else:
                cloud_down += 1
                _log.warning('cloud hello attempt %d answered HTTP %s without a frame', attempt + 1, a.status)
        self._set_end(EndInfo('robot', 'unreachable' if unreachable >= cloud_down else 'cloud_unavailable',
                              None, None))

    async def _infer(self, session, frame, seq, obs_e):
        a = await self._post(session, cloud_protocol.INFER_PATH, frame, self._infer_timeout(), with_conn=True)
        if a.kind == 'frame':
            try:
                c_seq, c_obs_e, actions, gpu_ms = cloud_protocol.parse_chunk(a.header, a.payload)
            except cloud_protocol.ProtocolError:
                self._set_end(EndInfo('robot', 'internal', None, None))
                return
            if (c_seq, c_obs_e) != (seq, obs_e):
                # A chunk for another request is no chunk for this one: free the slot.
                self._note_failure('chunk for another observation')
                self._report(seq, obs_e, None)
                return
            with self._lock:
                self._rtt.append(a.rtt_ms)
                if gpu_ms is not None:
                    self._gpu.append(gpu_ms)
            self._report(seq, obs_e, actions)
            return
        if a.kind == 'error':
            code, retry, _seq, reason, server_v = a.error
            if retry:                                   # rate / internal: this observation is lost
                self._note_failure(f'error frame {code}')
                self._report(seq, obs_e, None)
            else:
                self._set_end(EndInfo('server', code, reason, server_v))
            return
        if a.kind == 'session_gone':
            self._set_end(EndInfo('robot', 'session_gone', 'after_ready', None))
            return
        if a.kind == 'internal':
            self._set_end(EndInfo('robot', 'internal', None, None))
            return
        what = a.error[0] if a.kind == 'unreachable' else f'HTTP {a.status}'
        self._note_failure(what)
        self._report(seq, obs_e, None)

    async def _bye(self, session, reason, stats):
        if not (isinstance(reason, str) and reason in cloud_protocol.BYE_REASONS):
            return
        if stats is not None:
            try:
                if not isinstance(stats, dict) or len(json.dumps(stats, allow_nan=False)) > _STATS_MAX_BYTES:
                    stats = None
            except (TypeError, ValueError):
                stats = None
        try:
            body = cloud_protocol.build_bye(reason, stats)
        except cloud_protocol.ProtocolError:
            return
        a = await self._post(session, cloud_protocol.BYE_PATH, body, self._bye_timeout(), with_conn=True)
        if a.kind not in ('frame', 'error') and a.status != 204:
            _log.info('cloud bye not confirmed: %s', a.error[0] if a.error else f'HTTP {a.status}')
