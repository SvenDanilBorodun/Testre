#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The student's Hugging Face token, encrypted on its way to the robot (2026-11-13).

The SPA relays the student's token to `/register_hf_user` over rosbridge. On a
Windows PC that is loopback, but on an Orange Pi it is a plain websocket on the
school network, readable by any passive listener. So the robot now holds an
EPHEMERAL P-256 key pair, in memory only, made fresh at every node start, and
advertises its public half on `/edubotics/hf_token_state` (the additive keys
`enc`, `kid`, `pub`; an older SPA ignores them). The SPA encrypts the token for
that key with WebCrypto and sends, in the EXISTING string field of the EXISTING
srv (no interface change):

    enc1:<kid>.<client public key>.<iv>.<ciphertext>

    kid               16 hex characters: sha256(robot public key)[:16]
    client public key the SPA's own ephemeral P-256 key, an uncompressed point
                      (65 bytes), base64url without padding
    iv                12 random bytes, base64url
    ciphertext        AES-256-GCM of the token's UTF-8 bytes, its 16-byte tag
                      appended (WebCrypto's layout), base64url

    shared secret   ECDH(robot private key, client public key): the 32-byte x
                    coordinate (WebCrypto's deriveBits(256))
    key             HKDF-SHA256(shared, salt=CONTEXT, info="aes-256-gcm|" + kid), 32 bytes
    AAD             CONTEXT + "|" + kid

An older robot reads such a string as a malformed token and writes nothing
(`hf_token_store.valid_shape` wants `hf_…`). A ciphertext for another key id
(the robot restarted since the page read the key) is refused in German, and
the SPA sends again with the key the next state message carries. Clearing the
slot stays the empty string, in the clear.

What this does NOT stop (accepted, docs/KNOWN-ISSUES.md): an ACTIVE attacker
on the LAN who rewrites the page the Pi serves can make the browser encrypt for
the attacker's key. It stops the passive listener.

`cryptography` is pinned in the server image (docker/physical_ai_server/
Dockerfile) and imported lazily: a node without it advertises no key and keeps
accepting a plain token (a Windows PC's loopback path, where an SPA sends
plaintext only when no key is advertised).
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Optional

WIRE_PREFIX = 'enc1:'
ENC_NAME = 'enc1'
CONTEXT = b'edubotics-hf-token:v1'
# A token is at most 3 + 256 characters (hf_token_store.TOKEN_SHAPE); a wire
# value far beyond that is not ours and is refused before any crypto.
MAX_WIRE_LEN = 1024
KID_RE = re.compile(r'[0-9a-f]{16}')
_B64URL_RE = re.compile(r'[A-Za-z0-9_-]+')
POINT_LEN = 65
IV_LEN = 12
TAG_LEN = 16


class WireError(Exception):
    """A wire value that cannot be decrypted. ``code`` is ``malformed``,
    ``stale`` (another key id) or ``bad`` (the AEAD check failed). Never
    carries the value itself."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def is_wire(value) -> bool:
    return isinstance(value, str) and value.startswith(WIRE_PREFIX)


def _b64url_decode(text: str) -> bytes:
    if not _B64URL_RE.fullmatch(text or ''):
        raise WireError('malformed')
    try:
        return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    except (ValueError, TypeError):
        raise WireError('malformed') from None


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def parse_wire(value: str):
    """``(kid, client_point, iv, ciphertext)`` of a wire value, or WireError."""
    if not is_wire(value) or len(value) > MAX_WIRE_LEN:
        raise WireError('malformed')
    parts = value[len(WIRE_PREFIX):].split('.')
    if len(parts) != 4 or not KID_RE.fullmatch(parts[0]):
        raise WireError('malformed')
    point = _b64url_decode(parts[1])
    iv = _b64url_decode(parts[2])
    ciphertext = _b64url_decode(parts[3])
    if len(point) != POINT_LEN or point[0] != 0x04 or len(iv) != IV_LEN or len(ciphertext) <= TAG_LEN:
        raise WireError('malformed')
    return parts[0], point, iv, ciphertext


def kid_of(public_point: bytes) -> str:
    return hashlib.sha256(public_point).hexdigest()[:16]


class RobotKey:
    """The robot's ephemeral P-256 key pair. Memory only; never logged."""

    def __init__(self, private_key):
        from cryptography.hazmat.primitives import serialization

        self._private = private_key
        self.public_point = private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.kid = kid_of(self.public_point)
        self.public_b64 = b64url(self.public_point)

    @classmethod
    def generate(cls) -> 'RobotKey':
        from cryptography.hazmat.primitives.asymmetric import ec

        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_value(cls, value: int) -> 'RobotKey':
        """Tests only: a key from a known private scalar (the interop fixture)."""
        from cryptography.hazmat.primitives.asymmetric import ec

        return cls(ec.derive_private_key(int(value), ec.SECP256R1()))

    def advertisement(self) -> dict:
        """The additive keys of /edubotics/hf_token_state."""
        return {'enc': ENC_NAME, 'kid': self.kid, 'pub': self.public_b64}

    def decrypt(self, value: str) -> str:
        """The token inside ``value``, or WireError. The result is NOT
        shape-checked here; the caller runs its own validation."""
        kid, point, iv, ciphertext = parse_wire(value)
        if kid != self.kid:
            raise WireError('stale')
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        try:
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        except ValueError:
            raise WireError('malformed') from None
        shared = self._private.exchange(ec.ECDH(), peer)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=CONTEXT,
                   info=b'aes-256-gcm|' + kid.encode('ascii')).derive(shared)
        try:
            plain = AESGCM(key).decrypt(iv, ciphertext, CONTEXT + b'|' + kid.encode('ascii'))
        except InvalidTag:
            raise WireError('bad') from None
        try:
            return plain.decode('utf-8')
        except UnicodeDecodeError:
            raise WireError('bad') from None


def generate_or_none() -> Optional[RobotKey]:
    """A fresh key, or None when `cryptography` is missing (the node then
    advertises no key and keeps accepting a plain token)."""
    try:
        return RobotKey.generate()
    except ImportError:
        return None


def encrypt_for_tests(public_b64: str, kid: str, token: str) -> str:
    """Tests only: what the SPA's WebCrypto code sends, done in Python."""
    import os

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    robot = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64url_decode(public_b64))
    mine = ec.generate_private_key(ec.SECP256R1())
    shared = mine.exchange(ec.ECDH(), robot)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=CONTEXT,
               info=b'aes-256-gcm|' + kid.encode('ascii')).derive(shared)
    iv = os.urandom(IV_LEN)
    ciphertext = AESGCM(key).encrypt(iv, token.encode('utf-8'), CONTEXT + b'|' + kid.encode('ascii'))
    point = mine.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return f'{WIRE_PREFIX}{kid}.{b64url(point)}.{b64url(iv)}.{b64url(ciphertext)}'
