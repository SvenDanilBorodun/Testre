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
  in ``AUTH_FAILURE_WINDOW_S`` every further request of that address whose
  bearer does NOT verify against the cached keys is a German 429 before
  anything else runs. A bearer that verifies (``jwt_auth.verified_subject``,
  no fetch) still passes, so one device sending junk from behind a school's
  NAT cannot lock out the school's signed-in users (VA-02, 2026-11-12); they
  still meet their per-user rate limits. Requests without the header
  (``/health``, ``/version``, preflights) pass. Under ``AUTH_VERIFY_MODE=remote``
  nothing verifies locally, so an address over its budget is refused whole.
* ``PublicReadCorsMiddleware`` — ``Access-Control-Allow-Origin: *`` on GET/HEAD
  of ``/version`` (2026-11-11): its legal links are read by every SPA, a Pi's
  at a LAN address ``ALLOWED_ORIGINS`` cannot list. An allowed origin still
  gets its own echo from ``CORSMiddleware``.
* ``AnyOriginBearerCorsMiddleware`` — any origin may call the student routes
  of ``main.py::_ANY_ORIGIN_BEARER_ROUTES`` without credentials (SA-06,
  2026-11-13: ``POST /pi/tickets``; 2026-11-14: every bearer route a Pi's SPA
  calls from its LAN origin): an exact (method, path template) list, never a
  prefix, teacher, admin and worker routes refused at construction; and no
  answer to an origin ``ALLOWED_ORIGINS`` does not list carries
  ``Access-Control-Allow-Credentials``. Mounted OUTSIDE ``CORSMiddleware``.
* ``client_ip`` — the address every IP-keyed bucket and the failed-
  authentication cap count against, chosen by ``CLIENT_IP_SOURCE``
  (2026-11-11, after the merge-day measurement on Railway):

  - ``x-real-ip`` (the default): the ``X-Real-IP`` header, the one Railway
    documents for the client's address and writes at its edge; a missing or
    malformed one falls back to the ``forwarded-for`` rule.
  - ``forwarded-for``: the ``TRUSTED_PROXY_HOPS``-th entry from the RIGHT of
    ``X-Forwarded-For`` (``client_ip_from``). On Railway that is the address of
    the edge server that took the connection, which changes from connection to
    connection, so every per-address limit counted per edge server (measured:
    a 429 only on one kept-alive connection, never across separate ones).
  - ``forwarded-for-first``: the LEFT-most ``X-Forwarded-For`` entry; correct
    only behind an edge that overwrites the header, as Railway says its edge
    does. Kept as a switch, so a correction is a variable, not a release.

  Fewer entries than hops, or nothing parseable → the socket peer.
  docs/deploy/DEPLOY.md §3c is the curl check that proves the choice.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware

from app.services import jwt_auth
from app.services.rate_limiter import FailureCounter, RateLimiter

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TRUSTED_PROXY_HOPS_DEFAULT = 1
TRUSTED_PROXY_HOPS_MAX = 5
CLIENT_IP_SOURCES = ("x-real-ip", "forwarded-for", "forwarded-for-first")
CLIENT_IP_SOURCE_DEFAULT = "x-real-ip"
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


def client_ip_source() -> str:
    """``CLIENT_IP_SOURCE``: where the client address is read (module
    docstring). One of ``CLIENT_IP_SOURCES``, case and blanks ignored, default
    ``x-real-ip``; anything else stops the boot (the message names the
    variable, never the value)."""
    raw = (os.environ.get("CLIENT_IP_SOURCE") or "").strip().lower()
    if not raw:
        return CLIENT_IP_SOURCE_DEFAULT
    if raw not in CLIENT_IP_SOURCES:
        raise RuntimeError("CLIENT_IP_SOURCE must be one of: " + ", ".join(CLIENT_IP_SOURCES) + ".")
    return raw


def _normalise_ip(text: str | None) -> str | None:
    if not text:
        return None
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


_real_ip_missing_logged = False


def client_ip_from_headers(real_ip: str | None, xff: str | None, peer: str | None,
                           hops: int, source: str = CLIENT_IP_SOURCE_DEFAULT) -> str:
    """The client address under ``source`` (module docstring), from the raw
    ``X-Real-IP`` and ``X-Forwarded-For`` values and the socket peer."""
    global _real_ip_missing_logged
    if source == "x-real-ip":
        ip = _normalise_ip(real_ip) if real_ip and "," not in real_ip else None
        if ip:
            return ip
        if not _real_ip_missing_logged:
            _real_ip_missing_logged = True
            logger.warning("CLIENT_IP_SOURCE=x-real-ip but a request carried no usable X-Real-IP; "
                           "falling back to X-Forwarded-For (logged once per process).")
        return client_ip_from(xff, peer, hops)
    if source == "forwarded-for-first":
        if xff:
            ip = _normalise_ip(xff.split(",", 1)[0])
            if ip:
                return ip
        return peer or "unknown"
    return client_ip_from(xff, peer, hops)


def client_ip(request: Request, hops: int, source: str = CLIENT_IP_SOURCE_DEFAULT) -> str:
    peer = request.client.host if request.client else None
    return client_ip_from_headers(request.headers.get("x-real-ip"), request.headers.get("x-forwarded-for"),
                                  peer, hops, source)


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    return token or None


def _verifies_locally(request: Request) -> bool:
    """True when the request's bearer is an access token that verifies against
    the CACHED keys (``jwt_auth.verified_subject``: pure CPU, never a fetch).
    The failed-authentication cap lets such a request through an address that
    is over its budget, so one device behind a school's NAT cannot lock out
    every signed-in user of the school (VA-02, 2026-11-12). Under
    ``AUTH_VERIFY_MODE=remote`` no key is cached and nothing verifies here:
    the whole address stays refused, as before."""
    if jwt_auth.verify_mode() == "remote":
        return False
    token = _bearer(request)
    return bool(token) and jwt_auth.verified_subject(token) is not None


def _hashed(prefix: str, token: str) -> str:
    return prefix + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def rate_limit_key(request: Request, per_user_prefixes, hops: int,
                   source: str = CLIENT_IP_SOURCE_DEFAULT) -> str:
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
    return client_ip(request, hops, source)


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
    def __init__(self, app, *, rules, per_user_prefixes, limiter: RateLimiter, hops: int,
                 source: str = CLIENT_IP_SOURCE_DEFAULT):
        super().__init__(app)
        self.rules = list(rules)
        self.per_user_prefixes = tuple(per_user_prefixes)
        self.limiter = limiter
        self.hops = hops
        self.source = source

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
                key = rate_limit_key(request, self.per_user_prefixes, self.hops, self.source)
                if not self.limiter.check(f"{rule_method}:{prefix}", key, limit, window):
                    logger.warning("Rate limit hit on %s %s from %s", rule_method, prefix, key)
                    # A Response, never an HTTPException: Starlette does not route a
                    # middleware's exception through FastAPI's handlers (→ a 500).
                    return JSONResponse(status_code=429, content={"detail": RATE_LIMITED_DE})
                break
        return await call_next(request)


class AuthFailureLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, counter: FailureCounter, hops: int,
                 limit: int = AUTH_FAILURE_LIMIT, window_s: float = AUTH_FAILURE_WINDOW_S,
                 source: str = CLIENT_IP_SOURCE_DEFAULT):
        super().__init__(app)
        self.counter = counter
        self.hops = hops
        self.source = source
        self.limit = int(limit)
        self.window_s = float(window_s)

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("authorization") is None:
            return await call_next(request)
        ip = client_ip(request, self.hops, self.source)
        if self.counter.count(ip, self.window_s) >= self.limit and not _verifies_locally(request):
            return JSONResponse(status_code=429, content={"detail": AUTH_FAILURES_DE})
        response = await call_next(request)
        if response.status_code == 401:
            if self.counter.record(ip, self.window_s) == self.limit:
                logger.warning(
                    "failed-authentication cap reached for %s: %d in %.0f s, answering 429",
                    ip, self.limit, self.window_s,
                )
        return response


class PublicReadCorsMiddleware:
    """``Access-Control-Allow-Origin: *`` on GET/HEAD of a few PUBLIC paths
    (launch plan step 4, 2026-11-11; today exactly ``/version``).

    ``/version`` carries the legal links every SPA shows, and a Pi's SPA runs at
    whatever LAN address the school gave it, which ``ALLOWED_ORIGINS`` (exact
    origins, credentials allowed) cannot list. The answer holds nothing private
    and no route under it reads a cookie or a token, so any origin may read it
    WITHOUT credentials. Added INSIDE ``CORSMiddleware``: for an allowed origin
    that middleware still replaces the star with the origin itself (and its
    credentials header); for any other origin the star stays. A plain ASGI
    middleware (no task per request); only a response that carries no
    ``Access-Control-Allow-Origin`` yet is touched.
    """

    def __init__(self, app, *, paths):
        self.app = app
        self.paths = frozenset(paths)

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") not in ("GET", "HEAD")
            or scope.get("path") not in self.paths
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_star(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "access-control-allow-origin" not in headers:
                    headers["Access-Control-Allow-Origin"] = "*"
            await send(message)

        await self.app(scope, receive, send_with_star)


# ------------------------------------------------------------------ any-origin bearer CORS

# The methods a route of the any-origin list may carry, and the only request
# headers its preflight admits (the SPA sends exactly these two).
ANY_ORIGIN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ANY_ORIGIN_REQUEST_HEADERS = frozenset({"authorization", "content-type"})
# Never on the any-origin list, whatever main.py says: the teacher web and the
# admin dashboard run at an origin ALLOWED_ORIGINS lists, and a managed
# worker's routes are called by a GPU host, never by a browser.
ANY_ORIGIN_FORBIDDEN_PREFIXES = ("/teacher", "/admin", "/trainings/worker")

_ROUTE_PARAM_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def compile_route_template(template: str) -> re.Pattern:
    """A FastAPI path template (``/workflows/{workflow_id}/clone``) as a regex
    that matches exactly that route's paths: every literal segment verbatim,
    every ``{param}`` exactly ONE non-empty segment (Starlette's default
    ``str`` convertor, which is what every route here declares)."""
    if not isinstance(template, str) or not template.startswith("/") or "//" in template:
        raise ValueError(f"not a path template: {template!r}")
    if template != "/" and template.endswith("/"):
        raise ValueError(f"a trailing slash never matches a route here: {template!r}")
    pieces = _ROUTE_PARAM_RE.split(template)
    if any("{" in p or "}" in p for p in pieces):
        raise ValueError(f"malformed path parameter in {template!r}")
    params = _ROUTE_PARAM_RE.findall(template)
    pattern = "".join(re.escape(lit) + ("[^/]+" if i < len(params) else "") for i, lit in enumerate(pieces))
    return re.compile(pattern)


def _is_forbidden_any_origin(template: str) -> bool:
    return any(template == p or template.startswith(p + "/") for p in ANY_ORIGIN_FORBIDDEN_PREFIXES)


class AnyOriginBearerCorsMiddleware:
    """CORS for the BEARER-authenticated routes a Pi's SPA calls (SA-06,
    2026-11-13: ``POST /pi/tickets``; 2026-11-14: every student route of
    ``main.py::_ANY_ORIGIN_BEARER_ROUTES``), and no credentials for any origin
    ``ALLOWED_ORIGINS`` does not list.

    A Pi's SPA runs at whatever LAN address the school gave the Pi
    (``http://192.168.x.y``, ``http://edubotics.local``), which
    ``ALLOWED_ORIGINS`` cannot list, and calls the cloud API from there. The
    listed routes are authenticated by an ``Authorization`` header — which a
    browser never sends on its own, so a foreign page holds nothing to send —
    and the API reads no cookie anywhere, so any origin may call them WITHOUT
    credentials: ``Access-Control-Allow-Origin: *`` and no
    ``Access-Control-Allow-Credentials``.

    The list is EXACT: ``(method, path template)`` pairs, each template matched
    whole (``compile_route_template``), never a prefix that could swallow a
    teacher route; a template under ``ANY_ORIGIN_FORBIDDEN_PREFIXES`` refuses to
    construct.

    * A request WITHOUT ``Origin`` (not a browser's CORS request) and a request
      from a LISTED origin pass through untouched: ``CORSMiddleware``'s rules
      (the echo and its credentials header) apply as before.
    * For any other origin, a preflight of a listed (method, path) whose
      requested headers are at most ``Authorization`` and ``Content-Type`` is
      answered HERE with the star (``CORSMiddleware`` would refuse it with a
      400 before any inner layer ran); every other preflight falls through to
      ``CORSMiddleware``, which refuses an unlisted origin.
    * A real request passes through every layer (the failed-authentication cap,
      the body and rate limits, the route); a listed (method, path) gains the
      star on its response when nothing set an origin yet, and NO response to
      an unlisted origin carries ``Access-Control-Allow-Credentials``
      (``CORSMiddleware`` adds it to every answer, its 400 included).

    Mounted OUTSIDE ``CORSMiddleware`` (added after it). A plain ASGI
    middleware (no task per request)."""

    MAX_AGE_S = "600"
    ALLOW_HEADERS = "Authorization, Content-Type"

    def __init__(self, app, *, routes, listed_origins=()):
        self.app = app
        compiled = []
        for method, template in routes:
            verb = str(method).upper()
            if verb not in ANY_ORIGIN_METHODS:
                raise ValueError(f"method {method!r} cannot be on the any-origin list")
            if _is_forbidden_any_origin(template):
                raise ValueError(f"{template!r} must keep the strict ALLOWED_ORIGINS rule")
            compiled.append((verb, compile_route_template(template)))
        if not compiled:
            raise ValueError("the any-origin list is empty")
        self.routes = tuple(compiled)
        self.listed_origins = frozenset(listed_origins)

    def methods_for(self, path: str) -> tuple[str, ...]:
        """The listed methods of ``path``, in a stable order."""
        found = {verb for verb, rx in self.routes if rx.fullmatch(path)}
        return tuple(sorted(found))

    def allows(self, method: str, path: str) -> bool:
        return str(method).upper() in self.methods_for(path)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []
        }
        origin = request_headers.get("origin")
        if origin is None or origin in self.listed_origins:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        methods = self.methods_for(path)
        if scope.get("method") == "OPTIONS" and "access-control-request-method" in request_headers:
            requested = request_headers["access-control-request-method"].strip().upper()
            asked = {
                h.strip().lower()
                for h in request_headers.get("access-control-request-headers", "").split(",")
                if h.strip()
            }
            if requested in methods and asked <= ANY_ORIGIN_REQUEST_HEADERS:
                preflight = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": ", ".join(methods + ("OPTIONS",)),
                        "Access-Control-Allow-Headers": self.ALLOW_HEADERS,
                        "Access-Control-Max-Age": self.MAX_AGE_S,
                        "Vary": "Origin",
                    },
                )
                await preflight(scope, receive, send)
                return
            listed = False  # a refused preflight: CORSMiddleware answers it
        else:
            listed = scope.get("method") in methods

        async def send_for_unlisted_origin(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "access-control-allow-credentials" in headers:
                    del headers["Access-Control-Allow-Credentials"]
                if listed and "access-control-allow-origin" not in headers:
                    headers["Access-Control-Allow-Origin"] = "*"
                    headers.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, send_for_unlisted_origin)
