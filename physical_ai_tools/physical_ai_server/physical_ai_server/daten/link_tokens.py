#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Short-lived link tokens for the read-only Daten routes (spec §B5).

A ``<video src>`` / ``<img src>`` GET is a no-cors request without an Origin
header, so nginx's Origin allowlist cannot gate media; without a token any page
the student visits could embed the student's recordings. The node mints the
tokens (``/daten/command`` action ``link``) — never the sidecar — so rosbridge
stays the only root of trust; the sidecar only verifies.

Format: ``v1.<b64url(json{"s": scope, "e": exp_unix})>.<b64url(hmac)[:32]>``
with scopes ``lib`` and ``ds:<ns>/<name>``. The MAC is HMAC-SHA256 over the
payload segment (its ASCII bytes) with a 32-byte secret both processes share
through ``/run/edubotics-daten/secret`` (dir 0700, file 0600), published
atomically by ``load_or_create_secret``. Verification is constant-time
(``hmac.compare_digest``), checks expiry and the route's scope.

Stdlib only; never imports ROS or LeRobot (the sidecar's import fence).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional, Tuple

SECRET_DIR = '/run/edubotics-daten'      # = contract.SECRET_DIR (lockstep-tested)
TOKEN_TTL_S = 1800                       # = contract.TOKEN_TTL_S (lockstep-tested)
TOKEN_VERSION = 'v1'
MAC_CHARS = 32
SECRET_BYTES = 32
LIB_SCOPE = 'lib'
DS_SCOPE_PREFIX = 'ds:'
# A token is a few hundred characters at most; anything far longer is garbage
# and is refused before it is decoded.
_MAX_TOKEN_CHARS = 1024

# The codes the sidecar answers with (contract.HTTP_ERRORS).
TOKEN_INVALID = 'token_invalid'
TOKEN_EXPIRED = 'token_expired'
SCOPE = 'scope'


def load_or_create_secret(d=SECRET_DIR):
    """The 32 secret bytes, created on first use (R-16, P22).

    Only COMPLETE files are ever published: a temp file is written and fsynced,
    then ``os.link``ed to the final name (atomic; ``FileExistsError`` when the
    other process won), so a racing reader never sees a short secret. Both the
    node and the sidecar call this; whichever links first wins and both read
    the same bytes."""
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, 'secret')
    for _ in range(5):
        try:
            with open(path, 'rb') as f:
                b = f.read()
            if len(b) == SECRET_BYTES:
                return b
            os.unlink(path)              # never a half-written file: only complete ones are published
        except FileNotFoundError:
            pass
        tmp = os.path.join(d, f'.secret.{os.getpid()}.{secrets.token_hex(4)}')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(SECRET_BYTES))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)           # atomic publish; FileExistsError when the other process won
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
    raise RuntimeError('link-token secret')


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _mac(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, payload.encode('ascii'), hashlib.sha256).digest())[:MAC_CHARS]


def dataset_scope(dataset_id: str) -> str:
    """The scope of one dataset's routes: ``ds:<ns>/<name>``."""
    return DS_SCOPE_PREFIX + dataset_id


def mint(secret: bytes, scope: str, ttl_s: int = TOKEN_TTL_S, now: Optional[float] = None) -> str:
    """A token for ``scope`` that expires ``ttl_s`` seconds from ``now``."""
    exp = int((time.time() if now is None else now) + ttl_s)
    payload = _b64(json.dumps({'s': scope, 'e': exp}, separators=(',', ':'),
                              ensure_ascii=True).encode('ascii'))
    return f'{TOKEN_VERSION}.{payload}.{_mac(secret, payload)}'


def read_scope(secret: bytes, token, now: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(scope, None)`` for a genuine, unexpired token; else ``(None, code)``
    with ``token_invalid`` (malformed or a wrong MAC) or ``token_expired``. The
    payload is decoded only once its MAC is proven ours. Never raises."""
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS:
        return None, TOKEN_INVALID
    parts = token.split('.')
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return None, TOKEN_INVALID
    payload, mac = parts[1], parts[2]
    try:
        expected = _mac(secret, payload)
    except (UnicodeEncodeError, TypeError, ValueError):
        return None, TOKEN_INVALID
    # Constant time on the MAC.
    if not hmac.compare_digest(expected.encode('ascii'), mac.encode('ascii', 'replace')):
        return None, TOKEN_INVALID
    try:
        claims = json.loads(_unb64(payload).decode('ascii'))
        token_scope, exp = claims['s'], claims['e']
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None, TOKEN_INVALID
    if not isinstance(token_scope, str) or type(exp) is not int:
        return None, TOKEN_INVALID
    if (time.time() if now is None else now) >= exp:
        return None, TOKEN_EXPIRED
    return token_scope, None


def verify(secret: bytes, token, scope: str, now: Optional[float] = None) -> Tuple[bool, Optional[str]]:
    """``(True, None)`` when ``token`` is genuine, unexpired and for ``scope``;
    else ``(False, code)`` with ``token_invalid`` (malformed or a wrong MAC),
    ``token_expired`` or ``scope``. Never raises."""
    token_scope, error = read_scope(secret, token, now)
    if error:
        return False, error
    if not hmac.compare_digest(token_scope.encode('utf-8'), str(scope).encode('utf-8')):
        return False, SCOPE
    return True, None
