# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The cloud API's edge: body limits, rate limits, the failed-authentication cap
and the client address (2026-11-10, API hardening).

The middleware classes live here, not in main.py, so the tests can mount them on
a small app without importing every router (and supabase-py, which CI's test
venv does not install). main.py keeps the rule tables as literals — several
tests read them from its source — and passes them in.

* ``BodySizeLimitMiddleware`` — a ``Content-Length`` above ``MAX_BODY_BYTES``
  (1 MB) is a 413 on every route, above the tighter per-prefix cap (384 KB) on
  the listed prefixes; a body method (POST/PUT/PATCH/DELETE) that sends
  ``Transfer-Encoding`` (a chunked body, whose size no header bounds) is a 411.
  No route needs a chunked body: browsers, the worker's httpx and the
  keepalive fetch all send a length.
* ``RateLimitMiddleware`` — the per-rule sliding windows. The per-user prefixes
  are keyed by the VERIFIED ``sub`` of the token (``jwt_auth.verified_subject``:
  the cached keys only, never a fetch): a forged token cannot claim another
  user's bucket, and a rotating fake token falls into its IP's bucket instead of
  getting a fresh one each time. A UUID worker token on ``/trainings/worker``
  keeps its hashed key; under ``AUTH_VERIFY_MODE=remote`` the old hashed-token
  key comes back (no keys are cached then).
* ``AuthFailureLimitMiddleware`` — every 401 answered to a request that carried
  ``Authorization`` counts against its client address; at ``AUTH_FAILURE_LIMIT``
  in ``AUTH_FAILURE_WINDOW_S`` every further request of that address that
  carries ``Authorization`` is a German 429 before anything else runs.
  Requests without the header (``/health``, ``/version``, preflights) pass. A
  school behind one NAT address shares the budget: 60 failures a minute is far
  above what signed-in students produce (supabase-js refreshes before expiry).
* ``client_ip_from`` — the ``TRUSTED_PROXY_HOPS``-th address from the RIGHT of
  ``X-Forwarded-For`` (default 1: the address Railway's edge added), never the
  left-most, which the client writes itself. Fewer entries than hops → the
  socket peer. Unverified on Railway until the owner's curl check
  (docs/deploy/DEPLOY.md).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.services import jwt_auth
from app.services.rate_limiter import FailureCounter, RateLimiter

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TRUSTED_PROXY_HOPS_DEFAULT = 1
TRUSTED_PROXY_HOPS_MAX = 5
AUTH_FAILURE_LIMIT = 60
AUTH_FAILURE_WINDOW_S = 60.0

RATE_LIMITED_DE = "Zu viele Anfragen — bitte einen Moment warten."
AUTH_FAILURES_DE = "Zu viele fehlgeschlagene Anmeldeversuche — bitte einen Moment warten."
CHUNKED_REFUSED_DE = "Chunked-Übertragung wird nicht unterstützt — bitte die Anfrage mit Content-Length senden."
BAD_LENGTH_DE = "Ungültige Content-Length-Angabe."

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_WORKER_PREFIX = "/trainings/worker/"


# ------------------------------------------------------------------ client address


def trusted_proxy_hops() -> int:
    """``TRUSTED_PROXY_HOPS``: how many proxies in front of the API append to
    ``X-Forwarded-For`` (Railway's edge: 1). An integer 1–5; anything else stops
    the boot (the message names the variable, never the value)."""
    raw = (os.environ.get("TRUSTED_PROXY_HOPS") or "").strip()
    if not raw:
        return TRUSTED_PROXY_HOPS_DEFAULT
    if not raw.isdigit() or not 1 <= int(raw) <= TRUSTED_PROXY_HOPS_MAX:
        raise RuntimeError(f"TRUSTED_PROXY_HOPS must be an integer from 1 to {TRUSTED_PROXY_HOPS_MAX}.")
    return int(raw)


def _normalise_ip(text: str) -> str | None:
    value = text.strip()
    if value.startswith("[") and "]" in value:  # "[2001:db8::1]:443"
        value = value[1:value.index("]")]
    elif value.count(":") == 1:  # "203.0.113.7:51234"
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip_from(xff: str | None, peer: str | None, hops: int) -> str:
    """The client address: the ``hops``-th entry from the right of
    ``X-Forwarded-For``, else the socket peer."""
    if xff:
        entries = [e for e in (p.strip() for p in xff.split(",")) if e]
        if len(entries) >= hops:
            ip = _normalise_ip(entries[-hops])
            if ip:
                return ip
    return peer or "unknown"


def client_ip(request: Request, hops: int) -> str:
    peer = request.client.host if request.client else None
    return client_ip_from(request.headers.get("x-forwarded-for"), peer, hops)


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    return token or None


def _hashed(prefix: str, token: str) -> str:
    return prefix + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def rate_limit_key(request: Request, per_user_prefixes, hops: int) -> str:
    """The bucket key of one request: ``user:<verified sub>`` on a per-user
    prefix, ``wtok:<hash>`` for a worker token, else the client address."""
    path = request.url.path
    if any(path.startswith(p) for p in per_user_prefixes):
        token = _bearer(request)
        if token:
            if jwt_auth.verify_mode() == "remote":
                return _hashed("jwt:", token)
            sub = jwt_auth.verified_subject(token)
            if sub:
                return "user:" + sub
            if path.startswith(_WORKER_PREFIX) and _UUID_RE.fullmatch(token):
                return _hashed("wtok:", token)
    return client_ip(request, hops)


# ------------------------------------------------------------------ middleware


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limited_prefixes, limited_max_bytes: int, max_bytes: int = MAX_BODY_BYTES):
        super().__init__(app)
        self.limited_prefixes = tuple(limited_prefixes)
        self.limited_max_bytes = int(limited_max_bytes)
        self.max_bytes = int(max_bytes)

    def _limit_for(self, method: str, path: str) -> int:
        for m, prefix in self.limited_prefixes:
            if m == method and path.startswith(prefix):
                return self.limited_max_bytes
        return self.max_bytes

    async def dispatch(self, request: Request, call_next):
        method = request.method
        if method in BODY_METHODS:
            path = request.url.path
            # 411 Length Required: without a Content-Length the size check below
            # is useless and a client could stream a multi-MB body past it.
            if request.headers.get("transfer-encoding") is not None:
                logger.warning("Rejecting %s %s with Transfer-Encoding — Content-Length required.", method, path)
                return JSONResponse(status_code=411, content={"detail": CHUNKED_REFUSED_DE})
            cl = request.headers.get("content-length")
            if cl is not None:
                cl = cl.strip()
                if not cl.isdigit():
                    return JSONResponse(status_code=400, content={"detail": BAD_LENGTH_DE})
                limit = self._limit_for(method, path)
                if int(cl) > limit:
                    logger.warning("Rejecting oversized %s %s body: %s bytes", method, path, cl)
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Anfrage zu groß (>{limit // 1024} KB)."},
                    )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, rules, per_user_prefixes, limiter: RateLimiter, hops: int):
        super().__init__(app)
        self.rules = list(rules)
        self.per_user_prefixes = tuple(per_user_prefixes)
        self.limiter = limiter
        self.hops = hops

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        for rule_method, prefix, limit, window in self.rules:
            if rule_method != "*" and rule_method != method:
                continue
            # Anchored prefix match: a rule for "/trainings/start" must NOT match
            # "/trainings/started"; a prefix ending in "/" covers every sub-resource.
            if prefix.endswith("/"):
                matched = path.startswith(prefix)
            else:
                matched = path == prefix or path.startswith(prefix + "/")
            if matched:
                key = rate_limit_key(request, self.per_user_prefixes, self.hops)
                if not self.limiter.check(f"{rule_method}:{prefix}", key, limit, window):
                    logger.warning("Rate limit hit on %s %s from %s", rule_method, prefix, key)
                    # A Response, never an HTTPException: Starlette does not route a
                    # middleware's exception through FastAPI's handlers (→ a 500).
                    return JSONResponse(status_code=429, content={"detail": RATE_LIMITED_DE})
                break
        return await call_next(request)


class AuthFailureLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, counter: FailureCounter, hops: int,
                 limit: int = AUTH_FAILURE_LIMIT, window_s: float = AUTH_FAILURE_WINDOW_S):
        super().__init__(app)
        self.counter = counter
        self.hops = hops
        self.limit = int(limit)
        self.window_s = float(window_s)

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("authorization") is None:
            return await call_next(request)
        ip = client_ip(request, self.hops)
        if self.counter.count(ip, self.window_s) >= self.limit:
            return JSONResponse(status_code=429, content={"detail": AUTH_FAILURES_DE})
        response = await call_next(request)
        if response.status_code == 401:
            if self.counter.record(ip, self.window_s) == self.limit:
                logger.warning(
                    "failed-authentication cap reached for %s: %d in %.0f s, answering 429",
                    ip, self.limit, self.window_s,
                )
        return response
