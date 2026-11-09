"""Local verification of Supabase access tokens (owner decision 2026-11-10).

Every authenticated request used to call ``supabase.auth.get_user(token)``, a
network round trip to Supabase Auth, synchronously on the event loop. With one
uvicorn worker a flood of fake ``x.y.z`` tokens therefore stalled the whole API
(the managed-training loops and ``/version`` included). The access token is now
verified HERE, against the project's public signing keys:

* Keys come from ``<SUPABASE_URL>/auth/v1/.well-known/jwks.json`` and are cached
  by ``kid``. They are fetched off the event loop at boot and every
  ``JWKS_REFRESH_S`` (a rotated key is published as "standby" before it signs,
  so the periodic refresh has it in time), and when a token names an unknown
  ``kid`` — at most once per ``JWKS_REFETCH_MIN_S``, single-flight, never once
  per request. A failed fetch keeps the cached keys.
* Only the asymmetric algorithms in ``ALLOWED_ALGORITHMS`` are accepted, and only
  the one the JWKS names for that ``kid`` (which must equal the token header's
  ``alg``). Never ``none``, never HS256: a shared secret has no public key and is
  never in the JWKS.
* Claims: the signature, ``exp`` (``LEEWAY_S``), ``aud == "authenticated"``,
  ``iss == <SUPABASE_URL>/auth/v1``, ``sub`` a UUID, ``role == "authenticated"``
  and not an anonymous user.
* With no key cached and the JWKS unreachable, ``KeysUnavailable`` (the caller
  answers a German 503 the SPA retries; it never signs anybody out).

``AUTH_VERIFY_MODE=remote`` is the one-variable rollback to Supabase's own check
(``auth.get_user``, in a thread). Accepted residual: a token whose owner signed
out stays valid until it expires (at most one hour, owner decision).

Library: PyJWT (``PyJWT[crypto]``, pinned in requirements.txt). Its only
dependency is the ``cryptography`` the API already pins; the claim and
algorithm handling is where hand-rolled JWT code goes wrong.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time

import httpx
import jwt

logger = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = frozenset({"ES256", "RS256"})
# A JWK without an `alg` member is judged by its key type.
_DEFAULT_ALG = {("EC", "P-256"): "ES256", ("RSA", None): "RS256"}
AUDIENCE = "authenticated"
LEEWAY_S = 30
JWKS_TIMEOUT_S = 5.0
JWKS_MAX_BYTES = 64 * 1024
JWKS_REFRESH_S = 600.0
JWKS_REFETCH_MIN_S = 30.0
# A Supabase access token is about 1 KB; anything far larger is refused before
# any parsing.
MAX_TOKEN_CHARS = 8192
KID_MAX_CHARS = 128
VERIFY_MODES = ("local", "remote")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TokenInvalid(Exception):
    """The token is not a valid access token of this project (→ 401)."""


class KeysUnavailable(Exception):
    """No signing key is cached and none could be fetched (→ 503)."""


class _UnknownKey(Exception):
    pass


def verify_mode() -> str:
    """``AUTH_VERIFY_MODE``: ``local`` (default) or ``remote`` (the rollback)."""
    return (os.environ.get("AUTH_VERIFY_MODE") or "local").strip().lower()


def validate_boot() -> None:
    """Stop the boot on a mode that is neither ``local`` nor ``remote`` (the
    message names the variable, never a value)."""
    if verify_mode() not in VERIFY_MODES:
        raise RuntimeError("AUTH_VERIFY_MODE must be 'local' or 'remote'.")


def _supabase_base() -> str:
    return (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")


def issuer() -> str:
    return _supabase_base() + "/auth/v1"


def jwks_url() -> str:
    return _supabase_base() + "/auth/v1/.well-known/jwks.json"


# ------------------------------------------------------------------ the key cache


class _KeyCache:
    """``{kid: (alg, PyJWK)}`` plus the fetch clock. Thread-safe: the fetch runs
    in a worker thread, the reads on the event loop and in sync handlers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[str, jwt.PyJWK]] = {}
        self._last_attempt: float | None = None  # time.monotonic() of the last fetch start
        self._last_success: float | None = None

    def get(self, kid: str):
        with self._lock:
            return self._keys.get(kid)

    def size(self) -> int:
        with self._lock:
            return len(self._keys)

    def replace(self, keys: dict) -> None:
        with self._lock:
            self._keys = dict(keys)
            self._last_success = time.monotonic()

    def mark_attempt(self) -> None:
        with self._lock:
            self._last_attempt = time.monotonic()

    def may_refetch(self) -> bool:
        with self._lock:
            last = self._last_attempt
        return last is None or time.monotonic() - last >= JWKS_REFETCH_MIN_S

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._keys = {}
            self._last_attempt = None
            self._last_success = None


KEYS = _KeyCache()


def parse_jwks(doc) -> dict:
    """The usable signing keys of a JWKS document: asymmetric, an allowed
    algorithm, a sane ``kid``, a signature key. Anything else is skipped."""
    out: dict[str, tuple[str, jwt.PyJWK]] = {}
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list):
        return out
    for jwk in keys:
        if not isinstance(jwk, dict):
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > KID_MAX_CHARS:
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        ops = jwk.get("key_ops")
        if ops is not None and (not isinstance(ops, list) or "verify" not in ops):
            continue
        alg = jwk.get("alg")
        if alg is None:
            kty = jwk.get("kty")
            alg = _DEFAULT_ALG.get((kty, jwk.get("crv") if kty == "EC" else None))
        if alg not in ALLOWED_ALGORITHMS:
            continue
        try:
            key = jwt.PyJWK(jwk, algorithm=alg)
        except Exception:  # noqa: BLE001 - a malformed or mismatched JWK is skipped
            continue
        out[kid] = (alg, key)
    return out


def _http_get_json(url: str, headers: dict):
    """BLOCKING. The one network call of this module (a seam for the tests)."""
    with httpx.Client(timeout=JWKS_TIMEOUT_S, follow_redirects=False) as client:
        resp = client.get(url, headers=headers)
    resp.raise_for_status()
    if len(resp.content) > JWKS_MAX_BYTES:
        raise ValueError("JWKS document too large")
    return resp.json()


def fetch_jwks_blocking() -> dict:
    """BLOCKING: fetch and parse the JWKS. The public anon key rides as
    ``apikey`` when set (the gateway may ask for one); the service-role key is
    never sent here."""
    headers = {"Accept": "application/json"}
    anon = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    if anon:
        headers["apikey"] = anon
    return parse_jwks(_http_get_json(jwks_url(), headers))


# Single-flight: the task of the fetch in flight and the loop it runs on.
_inflight: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None = None


async def _fetch_and_store() -> bool:
    try:
        keys = await asyncio.to_thread(fetch_jwks_blocking)
    except Exception as exc:  # noqa: BLE001 - never the text: it may carry a URL
        logger.warning("JWKS fetch failed: %s; keeping %d cached key(s)", type(exc).__name__, KEYS.size())
        return False
    if not keys:
        logger.error("JWKS lists no usable ES256/RS256 key; keeping %d cached key(s)", KEYS.size())
        return False
    KEYS.replace(keys)
    return True


async def refresh(force: bool = False) -> bool:
    """Fetch the JWKS off the event loop, at most once per ``JWKS_REFETCH_MIN_S``
    unless ``force``. A caller arriving while a fetch is in flight awaits that
    fetch instead of starting another. True when the keys were replaced."""
    global _inflight
    loop = asyncio.get_running_loop()
    if _inflight is not None and _inflight[0] is loop and not _inflight[1].done():
        return await asyncio.shield(_inflight[1])
    if not force and not KEYS.may_refetch():
        return False
    KEYS.mark_attempt()
    task = loop.create_task(_fetch_and_store())
    _inflight = (loop, task)
    return await asyncio.shield(task)


async def refresh_loop() -> None:
    """Boot warm-up, then a refresh every ``JWKS_REFRESH_S`` (a startup task)."""
    while True:
        try:
            ok = await refresh(force=True)
            if ok:
                logger.info("JWKS: %d signing key(s) cached", KEYS.size())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.warning("JWKS refresh loop error: %s", type(exc).__name__)
        await asyncio.sleep(JWKS_REFRESH_S)


# ------------------------------------------------------------------ verification

_last_alg_log = 0.0


def _note_refused_alg(alg) -> None:
    """A token in another algorithm is either forged or a sign that the project
    still signs with its legacy shared secret — say so, at most once a minute."""
    global _last_alg_log
    now = time.monotonic()
    if now - _last_alg_log >= 60.0:
        _last_alg_log = now
        logger.warning(
            "access token refused: algorithm %s is not one of %s (is the project's CURRENT "
            "signing key asymmetric? rollback: AUTH_VERIFY_MODE=remote)",
            str(alg)[:16], sorted(ALLOWED_ALGORITHMS),
        )


def _header(token) -> tuple[str, str]:
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise TokenInvalid("shape")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise TokenInvalid("header") from None
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        _note_refused_alg(alg)
        raise TokenInvalid("alg")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > KID_MAX_CHARS:
        raise TokenInvalid("kid")
    return alg, kid


def check_claims(claims) -> dict:
    """The claims every access token of a signed-in EduBotics user carries."""
    if not isinstance(claims, dict):
        raise TokenInvalid("claims")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not _UUID_RE.fullmatch(sub.lower()):
        raise TokenInvalid("sub")
    if claims.get("role") != "authenticated":
        raise TokenInvalid("role")
    if claims.get("is_anonymous") is True:
        raise TokenInvalid("anonymous")
    return claims


def verify_cached(token) -> dict:
    """Verify against the cached keys only (pure CPU, no I/O). Raises
    ``TokenInvalid``, or ``_UnknownKey`` when the ``kid`` is not cached."""
    alg, kid = _header(token)
    entry = KEYS.get(kid)
    if entry is None:
        raise _UnknownKey(kid)
    key_alg, key = entry
    if key_alg != alg:
        raise TokenInvalid("alg mismatch")
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=[key_alg],
            audience=AUDIENCE,
            issuer=issuer(),
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        raise TokenInvalid("signature or claims") from None
    return check_claims(claims)


def verified_subject(token) -> str | None:
    """The ``sub`` of a token that verifies against the cached keys, else None.
    Never fetches (the rate-limit middleware's per-user key)."""
    try:
        return verify_cached(token)["sub"]
    except (TokenInvalid, _UnknownKey):
        return None


async def verify(token) -> dict:
    """The claims of a valid access token. ``TokenInvalid`` (→ 401) or
    ``KeysUnavailable`` (→ 503). An unknown ``kid`` triggers one bounded,
    single-flight refetch off the loop."""
    try:
        return verify_cached(token)
    except _UnknownKey:
        pass
    await refresh()
    try:
        return verify_cached(token)
    except _UnknownKey:
        if KEYS.size() == 0:
            raise KeysUnavailable() from None
        raise TokenInvalid("unknown kid") from None


def verify_remote_blocking(token) -> dict:
    """BLOCKING — the ``AUTH_VERIFY_MODE=remote`` rollback: Supabase Auth checks
    the token (``auth.get_user``); the claims (``sub``, ``aal``, ``session_id``)
    are then read from the token it accepted. Any failure is ``TokenInvalid``,
    as before."""
    from app.services.supabase_client import get_supabase

    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise TokenInvalid("shape")
    try:
        resp = get_supabase().auth.get_user(token)
    except Exception as exc:  # noqa: BLE001 - network, outage, refusal: a generic 401 as before
        logger.warning("Supabase auth.get_user failed: %s", type(exc).__name__)
        raise TokenInvalid("remote") from None
    user = getattr(resp, "user", None)
    if user is None:
        raise TokenInvalid("remote")
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise TokenInvalid("claims") from None
    if str(claims.get("sub")) != str(getattr(user, "id", "")):
        raise TokenInvalid("sub mismatch")
    return check_claims(claims)
