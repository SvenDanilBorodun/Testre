# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The robot's public token key, served behind the robot login (VB-03, 2026-11-15).

The student's Hugging Face token travels to the robot over rosbridge,
encrypted for an ephemeral P-256 key the ROS node makes at every start
(``physical_ai_server/data_processing/hf_token_crypto.py``). The node
advertises that key on ``/edubotics/hf_token_state`` — but on an Orange Pi
every account that may use the robot holds a rosbridge session, and rosbridge
lets any client publish any topic and advertise any service: a classmate could
publish a forged state naming their own key, advertise their own
``/register_hf_user`` and read the next student's token.

So the node ALSO writes its public key to a file on a host directory the Pi's
compose bind-mounts (``ROBOT_KEY_DIR``), and the agent serves it on
``GET /api/system/robot-key`` behind the robot login. The Pi's page encrypts
only for the key served here and sends nothing while the topic advertises
another key id; a forged topic is then ignored, and a forged service gets only
a ciphertext for the node's real key.

``ensure_dir`` creates the directory before every robot-tier ``up``
(``docker_manager._compose_up``): the host's ``/run`` is a tmpfs, so a reboot
empties it, and a fielded Pi that self-updated has no such directory until the
new agent makes it. ``read_key`` never trusts the file blindly: a regular file
(no symlink), at most ``MAX_FILE_BYTES``, the exact schema, and a key id that is
the SHA-256 of the point it names. Stdlib only; nothing here logs a key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from typing import Optional

from .constants import ROBOT_KEY_DIR, ROBOT_KEY_FILE_NAME

MAX_FILE_BYTES = 4096
KEY_SCHEMA = 1
ENC_NAME = "enc1"
_KID_RE = re.compile(r"[0-9a-f]{16}")
# A 65-byte uncompressed P-256 point is 87 base64url characters.
_PUB_RE = re.compile(r"[A-Za-z0-9_-]{87}")


def key_dir() -> str:
    """The directory, resolved at call time (``EDUBOTICS_ROBOT_KEY_DIR`` is the
    test seam, like ``pi_auth``'s ``EDUBOTICS_PI_AUTH_FILE``)."""
    return os.environ.get("EDUBOTICS_ROBOT_KEY_DIR") or ROBOT_KEY_DIR


def key_file() -> str:
    return os.path.join(key_dir(), ROBOT_KEY_FILE_NAME)


def ensure_dir(log=None) -> bool:
    """Create the bind-mount source (0755, root) if it is missing. True when it
    exists as a real directory afterwards. Never raises: the robot tier starts
    either way (Docker would create a missing source itself, root-owned, which
    this root agent reads too); only the page then cannot send a token."""
    log = log or (lambda _m: None)
    path = key_dir()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            os.makedirs(path, mode=0o755, exist_ok=True)
            return True
        except OSError as e:
            log(f"Der Schlüsselordner für das Hugging-Face-Token konnte nicht angelegt werden ({type(e).__name__}).")
            return False
    except OSError as e:
        log(f"Der Schlüsselordner für das Hugging-Face-Token ist nicht lesbar ({type(e).__name__}).")
        return False
    if stat.S_ISDIR(info.st_mode):
        return True
    log("An der Stelle des Schlüsselordners für das Hugging-Face-Token liegt kein Ordner; "
        "das Token kann auf diesem Pi nicht übertragen werden.")
    return False


def _kid_of_point(pub: str) -> Optional[str]:
    try:
        point = base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))
    except (ValueError, TypeError):
        return None
    if len(point) != 65 or point[0] != 0x04:
        return None
    return hashlib.sha256(point).hexdigest()[:16]


def parse_key(raw: bytes) -> Optional[dict]:
    """``{"enc", "kid", "pub"}`` from the file's bytes, or None for anything
    that is not exactly the node's schema."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_FILE_BYTES:
        return None
    try:
        data = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != {"v", "enc", "kid", "pub"}:
        return None
    if data["v"] != KEY_SCHEMA or data["enc"] != ENC_NAME:
        return None
    kid, pub = data["kid"], data["pub"]
    if type(kid) is not str or type(pub) is not str:
        return None
    if not _KID_RE.fullmatch(kid) or not _PUB_RE.fullmatch(pub):
        return None
    if _kid_of_point(pub) != kid:
        return None
    return {"enc": ENC_NAME, "kid": kid, "pub": pub}


def read_key() -> Optional[dict]:
    """The node's current public key, or None (no file yet: the robot tier is
    down or the node is still starting; or a file that fails ``parse_key``)."""
    path = key_file()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return parse_key(raw)
