"""Cloudflare R2 (S3 API) for managed-training checkpoints and logs (spec §3.4, E1).

The API holds the only R2 key (Railway). A worker never sees it: it gets one
short-lived presigned PUT per file, whose signature binds Content-Type,
Content-Length and Content-MD5 (proven: X-Amz-SignedHeaders =
content-length;content-md5;content-type;host), and one presigned GET per file
for a resume. Every object of a training lives under
``{R2_KEY_PREFIX}t{training_id}/``; the janitor deletes them once the row is
terminal.

boto3 is imported inside ``_client()`` (and only there), so ``from app import
main`` never needs it at import time. Every function here is BLOCKING: async
callers run them in ``asyncio.to_thread`` or the publish executor (§4.13).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

logger = logging.getLogger(__name__)

PUT_TTL_S = 900
GET_TTL_S = 3600
DEFAULT_KEY_PREFIX = "train/"
STREAM_CHUNK = 1024 * 1024

_LOCK = threading.Lock()
_CLIENT = None


class R2NotConfigured(RuntimeError):
    """A required R2_* variable is missing (the boot already logged which)."""


class R2Missing(Exception):
    """The object does not exist (HEAD/GET answered 404 / NoSuchKey)."""


def key_prefix() -> str:
    return os.environ.get("R2_KEY_PREFIX") or DEFAULT_KEY_PREFIX


def training_prefix(training_id: int) -> str:
    return f"{key_prefix()}t{int(training_id)}/"


def ckpt_key(training_id: int, attempt: int, step: int, relpath: str) -> str:
    return f"{key_prefix()}t{int(training_id)}/a{int(attempt)}/s{int(step):07d}/{relpath}"


def log_key(training_id: int, attempt: int) -> str:
    return f"{key_prefix()}t{int(training_id)}/a{int(attempt)}/log/training_log.txt"


def bucket() -> str:
    name = os.environ.get("R2_BUCKET")
    if not name:
        raise R2NotConfigured("R2_BUCKET")
    return name


def configured() -> bool:
    return all(os.environ.get(k) for k in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def _client():
    """The one boto3 S3 client, built lazily with exactly §3.4's settings."""
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            missing = [k for k in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
                       if not os.environ.get(k)]
            if missing:
                raise R2NotConfigured(", ".join(missing))
            import boto3
            from botocore.config import Config

            _CLIENT = boto3.client(
                "s3",
                endpoint_url=os.environ["R2_ENDPOINT_URL"],
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=60,
                ),
            )
        return _CLIENT


def _reset_for_tests() -> None:
    global _CLIENT
    with _LOCK:
        _CLIENT = None


def presign_put(key: str, size: int, md5_b64: str) -> tuple[str, dict]:
    """A presigned PUT for exactly this length and MD5, and the headers the worker must send."""
    url = _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket(),
            "Key": key,
            "ContentType": "application/octet-stream",
            "ContentLength": int(size),
            "ContentMD5": md5_b64,
        },
        ExpiresIn=PUT_TTL_S,
    )
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(int(size)),
        "Content-MD5": md5_b64,
    }
    return url, headers


def presign_get(key: str) -> str:
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=GET_TTL_S,
    )


def _is_missing(exc) -> bool:
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    code = str((resp.get("Error") or {}).get("Code") or "")
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


def head_size(key: str) -> int:
    """The object's size, or R2Missing."""
    try:
        resp = _client().head_object(Bucket=bucket(), Key=key)
    except Exception as exc:  # botocore ClientError
        if _is_missing(exc):
            raise R2Missing(key) from None
        raise
    return int(resp["ContentLength"])


def exists(key: str) -> bool:
    try:
        head_size(key)
    except R2Missing:
        return False
    return True


def get_bytes(key: str, max_bytes: int) -> bytes:
    """Download a small object completely (at most ``max_bytes`` + 1 are read)."""
    try:
        resp = _client().get_object(Bucket=bucket(), Key=key)
    except Exception as exc:
        if _is_missing(exc):
            raise R2Missing(key) from None
        raise
    body = resp["Body"]
    try:
        return body.read(max_bytes + 1)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def stream_to_file(key: str, path: str, should_continue=None) -> tuple[int, str]:
    """Stream an object to ``path``; return (size, sha256 hex). ``should_continue``
    (optional) is asked between chunks; False stops with RuntimeError."""
    try:
        resp = _client().get_object(Bucket=bucket(), Key=key)
    except Exception as exc:
        if _is_missing(exc):
            raise R2Missing(key) from None
        raise
    body = resp["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "wb") as fh:
            while True:
                if should_continue is not None and not should_continue():
                    raise RuntimeError("stopped")
                chunk = body.read(STREAM_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return size, digest.hexdigest()


def stream_sha256(key: str) -> tuple[int, str]:
    """(size, sha256 hex) of an object, streamed, nothing kept."""
    try:
        resp = _client().get_object(Bucket=bucket(), Key=key)
    except Exception as exc:
        if _is_missing(exc):
            raise R2Missing(key) from None
        raise
    body = resp["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(STREAM_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return size, digest.hexdigest()


def delete_keys(keys: list[str]) -> None:
    """Delete objects (a missing one is not an error); 1000 per request."""
    keys = list(keys)
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        if not chunk:
            continue
        resp = _client().delete_objects(
            Bucket=bucket(), Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
        )
        errors = (resp or {}).get("Errors") or []
        if errors:
            raise RuntimeError(f"R2 delete_objects reported {len(errors)} error(s)")
