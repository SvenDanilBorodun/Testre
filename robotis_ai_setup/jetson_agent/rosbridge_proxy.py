"""WebSocket bridge that JWT-gates the Jetson's rosbridge.

The Jetson exposes rosbridge on 127.0.0.1:9090 (loopback only — never
reachable from the classroom LAN). This proxy listens on 0.0.0.0:9091,
accepts a WebSocket connection from the student's React app, expects
the first frame to be ``{"op": "auth", "token": "<Supabase JWT>"}``,
verifies the JWT, and only then transparently bridges bytes to/from
the upstream rosbridge.

Verification:
  * Algorithm picked at first run via the JWT header's ``alg`` field.
    Supabase uses HS256 (legacy) or RS256 (modern); the proxy supports
    both. RS256 needs the JWKS URL; HS256 needs the shared secret. Both
    are baked into /etc/edubotics/jetson.env by setup.sh.
  * ``aud`` must equal ``authenticated``.
  * ``exp`` must be in the future.
  * ``sub`` must equal the current owner UUID, queried from the agent's
    loopback /owner endpoint (refreshed every 1s with caching).

Close codes:
  * 4400 — no/invalid auth frame within 5 s
  * 4401 — JWT verification failed
  * 4403 — JWT valid but caller is not the current owner

Logging via the standard ``logging`` module → systemd journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Optional

# Third-party deps installed by setup.sh: websockets, python-jose.
import websockets
from jose import jwt as jose_jwt
from jose import jwk as jose_jwk
from jose.exceptions import JWTError, ExpiredSignatureError

logger = logging.getLogger("rosbridge-proxy")

UPSTREAM_URL = os.environ.get("EDUBOTICS_UPSTREAM_WS", "ws://127.0.0.1:9090")
FRONT_PORT = int(os.environ.get("EDUBOTICS_PROXY_FRONT_PORT", "9091"))
OWNER_URL = os.environ.get("EDUBOTICS_OWNER_URL", "http://127.0.0.1:5180/owner")
AUTH_FRAME_TIMEOUT_S = 5.0
OWNER_CACHE_TTL_S = 1.0

ENV_PATH = os.environ.get("EDUBOTICS_JETSON_ENV", "/etc/edubotics/jetson.env")


def _load_env() -> dict:
    out: dict[str, str] = {}
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip('"').strip("'")
    except OSError as exc:
        logger.error("could not read %s: %s", ENV_PATH, exc)
    return out


_ENV = _load_env()
SUPABASE_URL = _ENV.get("EDUBOTICS_SUPABASE_URL", "")
SUPABASE_JWT_ALGORITHM = _ENV.get("EDUBOTICS_SUPABASE_JWT_ALGORITHM", "RS256")
SUPABASE_JWT_SECRET = _ENV.get("EDUBOTICS_SUPABASE_JWT_SECRET", "")


# ---------------------------------------------------------------------------
# JWKS cache (RS256 path).
# ---------------------------------------------------------------------------


_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL_S = 3600  # 1 hour


def _fetch_jwks() -> dict:
    """Fetch Supabase's JWKS endpoint. Caches for 1 hour."""
    global _jwks_cache, _jwks_fetched_at
    if _jwks_cache and (time.monotonic() - _jwks_fetched_at) < _JWKS_TTL_S:
        return _jwks_cache
    if not SUPABASE_URL:
        return {}
    url = SUPABASE_URL.rstrip("/") + "/auth/v1/keys"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            _jwks_cache = json.loads(resp.read().decode("utf-8"))
            _jwks_fetched_at = time.monotonic()
            return _jwks_cache
    except Exception as exc:
        logger.warning("JWKS fetch failed: %s", exc)
        return {}


def _find_jwks_key(kid: str) -> Optional[dict]:
    jwks = _fetch_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


# ---------------------------------------------------------------------------
# Owner cache (loopback HTTP polled once per second).
# ---------------------------------------------------------------------------


_owner_cache: Optional[str] = None
_owner_fetched_at: float = 0.0


def _get_current_owner() -> Optional[str]:
    """Read /owner from the agent's loopback HTTP server. Caches for 1s
    so a burst of WS connects doesn't hammer the loopback."""
    global _owner_cache, _owner_fetched_at
    if (time.monotonic() - _owner_fetched_at) < OWNER_CACHE_TTL_S:
        return _owner_cache
    try:
        with urllib.request.urlopen(OWNER_URL, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _owner_cache = data.get("current_owner_user_id")
            _owner_fetched_at = time.monotonic()
            return _owner_cache
    except Exception as exc:
        logger.warning("owner lookup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JWT verification.
# ---------------------------------------------------------------------------


def _verify_jwt(token: str) -> Optional[dict]:
    """Verify the JWT. Returns the decoded claims dict on success, None
    on any verification failure (logged at INFO so an operator can see
    why connections are being refused).

    SECURITY: the verification algorithm is locked to the operator-
    configured ``SUPABASE_JWT_ALGORITHM`` (RS256, ES256, or HS256). We
    do NOT let the JWT header pick the algorithm — that's the textbook
    "alg confusion" attack (CVE-2016-10555, CVE-2018-1000531). An
    attacker who knows the project's public key would otherwise send
    `alg: HS256` with the public key used as the HMAC secret and
    bypass verification entirely.

    Modern Supabase projects (post-2024) default to ES256 with the
    public key exposed via JWKS at /auth/v1/.well-known/jwks.json.
    Legacy projects use HS256 with a shared symmetric secret. Both
    use the same _find_jwks_key path for asymmetric algs (ES* / RS*).
    """
    # Strip 'Bearer ' if the client sent it.
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    # Use the OPERATOR-configured algorithm, never the header's claim.
    # python-jose's algorithms= parameter enforces the algorithm at
    # verification time, but we double-belt by also picking the key
    # path off our config rather than the header's `alg`.
    alg = SUPABASE_JWT_ALGORITHM

    try:
        header = jose_jwt.get_unverified_header(token)
    except JWTError as exc:
        logger.info("JWT header parse failed: %s", exc)
        return None

    try:
        # Both RS* and ES* are asymmetric — public key from JWKS, same
        # construct + decode path. ES256 is what modern Supabase projects
        # default to since the 2024 asymmetric-keys migration; RS256 is
        # the older asymmetric option. python-jose handles both via the
        # same _find_jwks_key lookup; the algorithm parameter passed to
        # construct + decode enforces alg-pinning.
        if alg.startswith("RS") or alg.startswith("ES"):
            key_dict = _find_jwks_key(header.get("kid", ""))
            if not key_dict:
                logger.info("no JWKS key for kid=%s", header.get("kid"))
                return None
            key = jose_jwk.construct(key_dict, alg)
            claims = jose_jwt.decode(
                token, key, algorithms=[alg], audience="authenticated"
            )
        elif alg.startswith("HS"):
            if not SUPABASE_JWT_SECRET:
                logger.info(
                    "HS256 configured but EDUBOTICS_SUPABASE_JWT_SECRET unset — "
                    "rejecting all tokens. Either deploy a JWT secret or "
                    "switch the project to RS256."
                )
                return None
            claims = jose_jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=[alg],
                audience="authenticated",
            )
        else:
            logger.info("unsupported JWT algorithm in config: %s", alg)
            return None
    except ExpiredSignatureError:
        logger.info("JWT expired")
        return None
    except JWTError as exc:
        logger.info("JWT verification failed: %s", exc)
        return None

    return claims


# ---------------------------------------------------------------------------
# WebSocket bridge.
# ---------------------------------------------------------------------------


async def _bridge(local: websockets.WebSocketServerProtocol) -> None:
    """Handle one client connection from start to close."""
    peer = local.remote_address
    logger.info("Client connect from %s", peer)

    # Step 1: receive auth frame with timeout.
    try:
        first = await asyncio.wait_for(local.recv(), timeout=AUTH_FRAME_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.info("Client %s did not send auth frame within %ss", peer, AUTH_FRAME_TIMEOUT_S)
        await local.close(code=4400, reason="no auth")
        return
    except websockets.ConnectionClosed:
        return

    # Step 2: parse auth op.
    try:
        msg = json.loads(first)
        if not isinstance(msg, dict) or msg.get("op") != "auth":
            raise ValueError("not an auth op")
        token = msg.get("token", "")
        if not isinstance(token, str) or not token:
            raise ValueError("missing token")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.info("Client %s sent invalid auth frame: %s", peer, exc)
        await local.close(code=4400, reason="invalid auth frame")
        return

    # Step 3: verify JWT.
    claims = _verify_jwt(token)
    if not claims:
        await local.close(code=4401, reason="invalid token")
        return

    # Step 4: check sub against current owner.
    sub = claims.get("sub")
    owner = _get_current_owner()
    if not sub or sub != owner:
        logger.info(
            "Client %s sub=%s rejected (current owner=%s)", peer, sub, owner
        )
        await local.close(
            code=4403, reason="Du bist nicht der aktuelle Besitzer dieses Jetsons."
        )
        return

    logger.info("Auth OK: %s (sub=%s)", peer, sub)

    # Step 5: open upstream + transparent bridge.
    try:
        async with websockets.connect(UPSTREAM_URL, max_size=None) as upstream:
            await asyncio.gather(
                _pipe(local, upstream, "client→ros"),
                _pipe(upstream, local, "ros→client"),
                return_exceptions=True,
            )
    except (websockets.WebSocketException, OSError) as exc:
        logger.warning("Upstream bridge failed: %s", exc)
        try:
            await local.close(code=1011, reason="upstream unavailable")
        except websockets.WebSocketException:
            pass


async def _pipe(src, dst, label: str) -> None:
    try:
        async for msg in src:
            await dst.send(msg)
    except websockets.ConnectionClosed:
        pass
    except Exception as exc:
        logger.warning("pipe %s failed: %s", label, exc)
    finally:
        # Close the OTHER side too so the other pipe task exits.
        try:
            await dst.close()
        except Exception:
            pass


async def _serve() -> None:
    logger.info(
        "rosbridge JWT proxy listening on :%d (upstream=%s, owner=%s)",
        FRONT_PORT, UPSTREAM_URL, OWNER_URL,
    )
    async with websockets.serve(
        _bridge,
        "0.0.0.0",
        FRONT_PORT,
        max_size=None,
    ):
        await asyncio.Future()  # run forever


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
