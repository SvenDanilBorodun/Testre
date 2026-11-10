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

THE KEY FILE (verification round B, VB-03, 2026-11-15). On an Orange Pi every
account that may use the robot holds a rosbridge session, and rosbridge lets
any client PUBLISH on any topic and advertise any service: a classmate could
publish a forged /edubotics/hf_token_state naming their own key, and advertise
their own /register_hf_user, and so read the next student's token. So the node
also writes its public key to ``KEY_FILE_DIR/KEY_FILE_NAME``
(``write_key_file``), a directory the Orange Pi compose bind-mounts from the
host, where the pi-agent serves it behind the robot login
(``GET /api/system/robot-key``). The Pi's SPA encrypts ONLY for the key the
agent serves, and sends nothing while the topic advertises another key id. A
forged topic is then ignored, and a forged /register_hf_user receives only a
ciphertext for this node's key, which it cannot open. On Windows (loopback)
the directory does not exist and nothing is written.

CLOUD-INFERENZ'S SESSION TOKENS ride the same envelope (verification round C,
VC-06, 2026-11-15): on a remote page the SPA encrypts the connect token and the
Modal edge token of `/edubotics/cloud_inference/start` for this key too, each
under its OWN context (``CLOUD_SESSION_CONTEXT``, ``CLOUD_EDGE_CONTEXT``: the
HKDF salt and the AAD), so an envelope made for one kind never opens as
another. The edge token is up to 4 096 characters, so its wire value has its
own bound (``CLOUD_EDGE_MAX_WIRE_LEN``).

NO VALUE OPENS TWICE (verification round C, VC-05): a session holder who
advertised one of the services receives every push's ciphertext; it could not
read it, but it could replay it to the node later (a classmate's token back in
the slot after their sign-out). ``RobotKey.decrypt`` records the SHA-256 of
every value it opened (of the DECODED parts, and only canonical base64 is
accepted, so a re-encoded value is the same value) and refuses it the second
time (``replayed``); of the client point only the x-coordinate counts, since
ECDH uses only the shared x and the negated point opens the same value
(verification round D, VD-01). Each genuine push carries a fresh ephemeral client key and
iv, so a real re-push is never a duplicate. The record lives with the key, so a
new key (every node start) starts empty; it holds at most ``MAX_SEEN_WIRES``
digests, and a key whose record is full refuses everything (``stale``) until
the node rotates to a fresh one, so flooding the record can never evict the
digest of a value worth replaying.

What this does NOT stop (accepted, docs/KNOWN-ISSUES.md): an ACTIVE attacker
on the LAN who rewrites the page the Pi serves (no HTTPS) can make the browser
encrypt for the attacker's key. It stops the passive listener and a session
holder's forged topic or service.

`cryptography` is pinned in the server image (docker/physical_ai_server/
Dockerfile) and imported lazily: a node without it advertises no key and keeps
accepting a plain token (a Windows PC's loopback path, where an SPA sends
plaintext only when no key is advertised).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
from typing import Optional

WIRE_PREFIX = 'enc1:'
ENC_NAME = 'enc1'
# The envelope's context: the HKDF salt and the head of the AAD. One per kind
# of secret (VC-06); the SPA's hfTokenCrypto.js spells the same three.
CONTEXT = b'edubotics-hf-token:v1'
CLOUD_SESSION_CONTEXT = b'edubotics-cloud-session-token:v1'
CLOUD_EDGE_CONTEXT = b'edubotics-cloud-edge-token:v1'
# A token is at most 3 + 256 characters (hf_token_store.TOKEN_SHAPE); a wire
# value far beyond that is not ours and is refused before any crypto. The
# connect token is 43 characters; the Modal edge token up to 4 096
# (cloud_protocol.EDGE_TOKEN_RE), i.e. a wire value of up to 5 610.
MAX_WIRE_LEN = 1024
CLOUD_EDGE_MAX_WIRE_LEN = 6144
_MAX_WIRE_LEN_BY_CONTEXT = {
    CONTEXT: MAX_WIRE_LEN,
    CLOUD_SESSION_CONTEXT: MAX_WIRE_LEN,
    CLOUD_EDGE_CONTEXT: CLOUD_EDGE_MAX_WIRE_LEN,
}
# VC-05: how many opened values one key remembers (32 bytes each). A full
# record refuses everything until the node rotates the key.
MAX_SEEN_WIRES = 1024
KID_RE = re.compile(r'[0-9a-f]{16}')
_B64URL_RE = re.compile(r'[A-Za-z0-9_-]+')
POINT_LEN = 65
IV_LEN = 12
TAG_LEN = 16

# The key file the pi-agent serves (VB-03, module docstring). The SAME path is
# the Orange Pi compose's bind mount (host and container alike) and
# pi_agent/constants.py's ROBOT_KEY_DIR / ROBOT_KEY_FILE_NAME;
# robotis_ai_setup/tests/test_robot_key_file_lockstep.py holds them equal.
KEY_FILE_DIR = '/run/edubotics-robot-key'
KEY_FILE_NAME = 'hf_token_key.json'
KEY_FILE_SCHEMA = 1


class WireError(Exception):
    """A wire value that cannot be decrypted. ``code`` is ``malformed``,
    ``stale`` (another key id, or a key whose replay record is full), ``bad``
    (the AEAD check failed) or ``replayed`` (this key opened the value before,
    VC-05). Never carries the value itself."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def is_wire(value) -> bool:
    return isinstance(value, str) and value.startswith(WIRE_PREFIX)


def _b64url_decode(text: str) -> bytes:
    """The bytes of a CANONICAL base64url text, else WireError. The decoder
    ignores the unused low bits of a last character, so two texts can decode
    to the same bytes; only the one the encoder writes is accepted (VC-05)."""
    if not _B64URL_RE.fullmatch(text or ''):
        raise WireError('malformed')
    try:
        raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    except (ValueError, TypeError):
        raise WireError('malformed') from None
    if b64url(raw) != text:
        raise WireError('malformed')
    return raw


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def parse_wire(value: str, max_len: int = MAX_WIRE_LEN):
    """``(kid, client_point, iv, ciphertext)`` of a wire value, or WireError."""
    if not is_wire(value) or len(value) > max_len:
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
        # VC-05: the SHA-256 of every value this key opened (never a value).
        self._seen = set()
        self._seen_lock = threading.Lock()

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

    @property
    def exhausted(self) -> bool:
        """True once the replay record is full: the key then refuses every
        value, and the node must rotate to a fresh key (VC-05)."""
        with self._seen_lock:
            return len(self._seen) >= MAX_SEEN_WIRES

    def decrypt(self, value: str, context: bytes = CONTEXT) -> str:
        """The secret inside ``value``, opened under ``context`` (the HF
        token by default), or WireError. Each value opens at most ONCE per key
        (VC-05). The result is NOT shape-checked here; the caller runs its own
        validation."""
        if context not in _MAX_WIRE_LEN_BY_CONTEXT:
            raise ValueError('unknown envelope context')
        kid, point, iv, ciphertext = parse_wire(value, _MAX_WIRE_LEN_BY_CONTEXT[context])
        if kid != self.kid:
            raise WireError('stale')
        # The digest of the decoded parts (fixed lengths, so the concatenation
        # is unambiguous), keyed by the context. Of the client point only the
        # x-coordinate counts (VD-01): ECDH uses only the shared point's x, so
        # (x, y) and its negation (x, p - y) open the same value, and on P-256
        # (cofactor 1) no other point gives that shared x. A digest over the
        # point as sent let a captured value open once more, negated.
        digest = hashlib.sha256(context + b'|' + point[1:33] + iv + ciphertext).digest()
        with self._seen_lock:
            if digest in self._seen:
                raise WireError('replayed')
            if len(self._seen) >= MAX_SEEN_WIRES:
                raise WireError('stale')
        plain = self._open(kid, point, iv, ciphertext, context)
        with self._seen_lock:
            # Re-checked: two threads may have opened the same value at once.
            if digest in self._seen:
                raise WireError('replayed')
            self._seen.add(digest)
        return plain

    def _open(self, kid: str, point: bytes, iv: bytes, ciphertext: bytes, context: bytes) -> str:
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
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=context,
                   info=b'aes-256-gcm|' + kid.encode('ascii')).derive(shared)
        try:
            plain = AESGCM(key).decrypt(iv, ciphertext, context + b'|' + kid.encode('ascii'))
        except InvalidTag:
            raise WireError('bad') from None
        try:
            return plain.decode('utf-8')
        except UnicodeDecodeError:
            raise WireError('bad') from None


def key_file_payload(key: RobotKey) -> dict:
    """What the key file holds: the schema version and the advertisement."""
    return {'v': KEY_FILE_SCHEMA, **key.advertisement()}


def write_key_file(key: Optional[RobotKey], directory: Optional[str] = None) -> bool:
    """Publish ``key``'s PUBLIC half for the pi-agent (VB-03). True when the
    file was written.

    Nothing happens where ``directory`` does not exist (every compose but the
    Orange Pi's: Windows keeps the topic's key). With no key (no
    `cryptography`), a file a previous node left behind is removed, so the
    agent serves no key rather than a stale one. The write is atomic (temp,
    fsync, 0644, rename); an OSError is the caller's to log, never a token.
    """
    directory = KEY_FILE_DIR if directory is None else directory  # read at call time
    if not os.path.isdir(directory):
        return False
    path = os.path.join(directory, KEY_FILE_NAME)
    if key is None:
        try:
            os.unlink(path)
        except OSError:
            pass
        return False
    data = (json.dumps(key_file_payload(key), separators=(',', ':')) + '\n').encode('ascii')
    fd, tmp = tempfile.mkstemp(prefix='.' + KEY_FILE_NAME + '.', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return True
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
    return True


def generate_or_none() -> Optional[RobotKey]:
    """A fresh key, or None when `cryptography` is missing (the node then
    advertises no key and keeps accepting a plain token)."""
    try:
        return RobotKey.generate()
    except ImportError:
        return None


def encrypt_for_tests(public_b64: str, kid: str, token: str, context: bytes = CONTEXT) -> str:
    """Tests only: what the SPA's seal sends, done in Python."""
    import os

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    robot = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64url_decode(public_b64))
    mine = ec.generate_private_key(ec.SECP256R1())
    shared = mine.exchange(ec.ECDH(), robot)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=context,
               info=b'aes-256-gcm|' + kid.encode('ascii')).derive(shared)
    iv = os.urandom(IV_LEN)
    ciphertext = AESGCM(key).encrypt(iv, token.encode('utf-8'), context + b'|' + kid.encode('ascii'))
    point = mine.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return f'{WIRE_PREFIX}{kid}.{b64url(point)}.{b64url(iv)}.{b64url(ciphertext)}'
