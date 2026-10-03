"""A student's own Hugging Face token: shape, fingerprint, encryption, validation.

Cloud half of the 042 feature. The robot half (`hf_token_store.py` in the
physical_ai_server package) carries a byte-identical `fingerprint()` and the
same shape rule; `tests/test_hf_token_fp_lockstep.py` holds both to one vector
table. Stdlib at import time: `cryptography` and `huggingface_hub` are imported
INSIDE the functions that need them, so the stub-based route tests and the boot
import probe never need either installed.

Nothing here ever logs, formats or raises a token: errors carry a stable
`code` and the caller picks the German sentence.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

KEY_ENV = "EDUBOTICS_HF_TOKEN_KEY"
PREVIOUS_KEY_ENV = "EDUBOTICS_HF_TOKEN_KEY_PREVIOUS"

ENVELOPE_VERSION = "v1"
FP_DOMAIN = b"edubotics-hf-token-fp:"
KID_DOMAIN = b"edubotics-hf-token-kid:"
AAD_PREFIX = b"edubotics-hf-token:v1:"
NONCE_BYTES = 12
KEY_BYTES = 32
HUB_TIMEOUT_S = 10.0

# fullmatch, NEVER match + `$`: Python's `$` also matches before a trailing
# newline, so "hf_<20 chars>\n" would pass the anchored form.
TOKEN_SHAPE = re.compile(r"hf_[A-Za-z0-9_-]{16,256}")


class HfCredentialError(Exception):
    """A credential problem with a stable machine-readable `code`; never carries
    a token or a library message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def valid_shape(token) -> bool:
    return isinstance(token, str) and TOKEN_SHAPE.fullmatch(token) is not None


def fingerprint(token: str) -> str:
    """First 16 hex chars of sha256("edubotics-hf-token-fp:" + token)."""
    return hashlib.sha256(FP_DOMAIN + token.encode("utf-8")).hexdigest()[:16]


def hint(token: str) -> str:
    """`hf_…` + the last four characters (shown on the Startseite card)."""
    return "hf_…" + token[-4:]


# ---------------------------------------------------------------- key handling

def _decode_key(raw: str, env_name: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError(f"{env_name} is not valid base64") from None
    if len(key) != KEY_BYTES:
        raise RuntimeError(f"{env_name} must decode to exactly {KEY_BYTES} bytes")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(KID_DOMAIN + key).hexdigest()[:8]


_lock = threading.Lock()
_cache: dict | None = None


def load_keys(environ=None) -> dict:
    """{'current': (kid, key) | None, 'previous': (kid, key) | None}.

    Absent variable -> None (the routes answer 503). PRESENT but malformed ->
    RuntimeError: a half-configured key must stop the deploy, never degrade into
    "no key" and silently turn the feature off.
    """
    env = os.environ if environ is None else environ
    out: dict = {"current": None, "previous": None}
    for slot, name in (("current", KEY_ENV), ("previous", PREVIOUS_KEY_ENV)):
        raw = env.get(name)
        if raw is not None and raw.strip():
            key = _decode_key(raw, name)
            out[slot] = (key_id(key), key)
    if out["previous"] is not None and out["current"] is None:
        raise RuntimeError(f"{PREVIOUS_KEY_ENV} is set but {KEY_ENV} is not")
    if out["previous"] and out["current"] and out["previous"][0] == out["current"][0]:
        raise RuntimeError(f"{PREVIOUS_KEY_ENV} equals {KEY_ENV}")
    return out


def _keys() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = load_keys()
        return _cache


def _reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def configured() -> bool:
    return _keys()["current"] is not None


def usable(ciphertext: str) -> bool:
    """True when the envelope's key id is one this process holds (no decryption)."""
    kid = _split(ciphertext)[0] if isinstance(ciphertext, str) else None
    if kid is None:
        return False
    keys = _keys()
    return any(k is not None and k[0] == kid for k in (keys["current"], keys["previous"]))


# ----------------------------------------------------------------- encryption

def _aad(user_id: str) -> bytes:
    return AAD_PREFIX + str(user_id).lower().encode("ascii")


def _split(envelope: str):
    parts = envelope.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_VERSION or not re.fullmatch(r"[0-9a-f]{8}", parts[1]):
        return (None, None)
    return (parts[1], parts[2])


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _seal(key: bytes, kid: str, nonce: bytes, user_id: str, token: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ct = AESGCM(key).encrypt(nonce, token.encode("utf-8"), _aad(user_id))
    return f"{ENVELOPE_VERSION}.{kid}.{_b64u(nonce + ct)}"


def encrypt(user_id: str, token: str) -> str:
    cur = _keys()["current"]
    if cur is None:
        raise HfCredentialError("key_missing")
    kid, key = cur
    return _seal(key, kid, os.urandom(NONCE_BYTES), user_id, token)


def decrypt(user_id: str, envelope: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    keys = _keys()
    if keys["current"] is None:
        raise HfCredentialError("key_missing")
    kid, blob = _split(envelope) if isinstance(envelope, str) else (None, None)
    if kid is None:
        raise HfCredentialError("undecryptable")
    key = next((k[1] for k in (keys["current"], keys["previous"]) if k is not None and k[0] == kid), None)
    if key is None:
        raise HfCredentialError("undecryptable")
    try:
        raw = _unb64u(blob)
        if len(raw) < NONCE_BYTES + 17:
            raise HfCredentialError("undecryptable")
        return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], _aad(user_id)).decode("utf-8")
    except HfCredentialError:
        raise
    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError):
        raise HfCredentialError("undecryptable") from None


# ------------------------------------------------------------- the Hub's answer

def _status_of(exc: Exception):
    return getattr(getattr(exc, "response", None), "status_code", None)


def validate_with_hub(token: str) -> dict:
    """`HfApi(token=token).whoami()` in a thread, bounded by HUB_TIMEOUT_S.

    Returns {"name": str, "role": str}. Raises HfCredentialError with code
    'rejected' (401/403: the Hub refused the token), 'unreachable' (timeout,
    network, 429, 5xx) or 'bad_reply' (no usable name). Logs the exception CLASS
    and HTTP status only, never str(exc).
    """
    result: dict = {}

    def _ask():
        try:
            from huggingface_hub import HfApi

            result["info"] = HfApi(token=token).whoami()
        except Exception as exc:  # noqa: BLE001 - classified below
            result["error"] = exc

    worker = threading.Thread(target=_ask, name="hf-whoami", daemon=True)
    worker.start()
    worker.join(HUB_TIMEOUT_S)
    if worker.is_alive():
        logger.warning("hf whoami timed out after %.0f s", HUB_TIMEOUT_S)
        raise HfCredentialError("unreachable")
    if "error" in result:
        exc = result["error"]
        status = _status_of(exc)
        logger.warning("hf whoami failed: %s status=%s", type(exc).__name__, status)
        if status in (401, 403):
            raise HfCredentialError("rejected")
        raise HfCredentialError("unreachable")
    info = result.get("info")
    name = info.get("name") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name:
        raise HfCredentialError("bad_reply")
    access = ((info.get("auth") or {}).get("accessToken") or {}) if isinstance(info, dict) else {}
    role = access.get("role") if isinstance(access.get("role"), str) and access.get("role") else "unknown"
    return {"name": name, "role": role}


def has_credential(supabase, user_id: str) -> bool:
    """True when `user_id` has a stored credential. Only `.select().eq().execute()`
    so the PATCH /me test doubles (which implement exactly those) keep working."""
    res = supabase.table("user_hf_credentials").select("user_id").eq("user_id", str(user_id)).execute()
    return bool(getattr(res, "data", None))
