"""Cloudflare R2 (S3 API) for managed-training checkpoints and logs (spec §3.4, E1).

The API holds the only R2 key (Railway). A worker never sees it: it gets one
short-lived presigned PUT per file, whose signature binds Content-Type,
Content-Length and x-amz-checksum-sha256 — the manifest's SHA-256 (spec 052
§9.8, OQ-1; proven: X-Amz-SignedHeaders =
content-length;content-type;host;x-amz-checksum-sha256) — and one presigned GET
per file for a resume. R2 refuses a body whose SHA-256 is not the signed one
(400 BadDigest) and a PUT that omits or changes the signed header (403), and
returns the stored value on a HEAD with ChecksumMode=ENABLED (``head_meta``):
gate G0b, runpod_training/e2e/r2_checksum_gate.py, variant V1, on the test
bucket 2026-10-29. Content-MD5 is NOT signed any more: R2 answers 400
InvalidRequest „You can only specify one non-default checksum at a time." to a
PUT that carries both (G0, the same day). Every object of a training lives under
``{R2_KEY_PREFIX}t{training_id}/``; the janitor deletes them once the row is
terminal.

boto3 is imported inside ``_client()`` (and only there), so ``from app import
main`` never needs it at import time. Every function here is BLOCKING: async
callers run them in ``asyncio.to_thread`` or the publish executor (§4.13).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading

logger = logging.getLogger(__name__)

PUT_TTL_S = 900
GET_TTL_S = 3600
DEFAULT_KEY_PREFIX = "train/"
DEFAULT_KEEP_PREFIX = "keep/"
STREAM_CHUNK = 1024 * 1024
FALLBACK_PUT_TRIES = 3
FALLBACK_PUT_DELAYS_S = (2, 4)

_LOCK = threading.Lock()
_CLIENT = None


class R2NotConfigured(RuntimeError):
    """A required R2_* variable is missing (the boot already logged which)."""


class R2Missing(Exception):
    """The object does not exist (HEAD/GET answered 404 / NoSuchKey)."""


class R2CopyUnsupported(Exception):
    """CopyObject is not implemented by this endpoint (the caller streams instead)."""


def key_prefix() -> str:
    return os.environ.get("R2_KEY_PREFIX") or DEFAULT_KEY_PREFIX


def keep_prefix() -> str:
    return os.environ.get("R2_KEEP_PREFIX") or DEFAULT_KEEP_PREFIX


def keep_key(training_id: int, step: int, relpath: str) -> str:
    """The kept continuable copy of one checkpoint file (owner E36)."""
    return f"{keep_prefix()}t{int(training_id)}/s{int(step):07d}/{relpath}"


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


def sha256_b64(sha256_hex: str) -> str:
    """The base64 of a SHA-256 given as hex, the form S3's x-amz-checksum-sha256 carries."""
    raw = bytes.fromhex(str(sha256_hex))
    if len(raw) != 32:
        raise ValueError("a SHA-256 has 32 bytes")
    return base64.b64encode(raw).decode("ascii")


def presign_put(key: str, size: int, sha256_hex: str) -> tuple[str, dict]:
    """A presigned PUT for exactly this length and SHA-256, and the headers the worker must send.

    ``ChecksumSHA256`` becomes a SIGNED header with the pinned botocore (not a query
    parameter): a client that omits or changes it breaks the signature, and R2 refuses
    a body whose SHA-256 is not the signed one (G0b V1). ``ChecksumAlgorithm`` is not
    passed (it would add a signed x-amz-sdk-checksum-algorithm header, which G0b V2/V4b
    proved unnecessary) and Content-MD5 is not signed (R2 refuses both together)."""
    checksum = sha256_b64(sha256_hex)
    url = _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket(),
            "Key": key,
            "ContentType": "application/octet-stream",
            "ContentLength": int(size),
            "ChecksumSHA256": checksum,
        },
        ExpiresIn=PUT_TTL_S,
    )
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(int(size)),
        "x-amz-checksum-sha256": checksum,
    }
    return url, headers


def presign_get(key: str) -> str:
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=GET_TTL_S,
    )


def copy_object(src_key: str, dst_key: str) -> None:
    """Server-side S3 CopyObject src → dst within the bucket (the kept copy, owner E36).

    A ``NotImplemented`` / HTTP 400 / 501 answer raises ``R2CopyUnsupported`` (the
    caller falls back to a presigned PUT, §3 item 21); a missing source raises
    ``R2Missing``; anything else propagates."""
    try:
        _client().copy_object(Bucket=bucket(), Key=dst_key,
                              CopySource={"Bucket": bucket(), "Key": src_key})
    except Exception as exc:  # botocore ClientError
        if _is_missing(exc):
            raise R2Missing(src_key) from None
        resp = getattr(exc, "response", None)
        if isinstance(resp, dict):
            code = str((resp.get("Error") or {}).get("Code") or "")
            status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            if code == "NotImplemented" or status in (400, 501):
                raise R2CopyUnsupported(f"{src_key} -> {dst_key}") from None
        raise


def put_file_presigned(key: str, path: str) -> None:
    """Upload a local file through a presigned PUT — exactly the protocol the workers
    use (the signed SHA-256, no aws-chunked trailer; §3 item 21). Raises RuntimeError on a
    403 or after FALLBACK_PUT_TRIES failures (the publish maps it to Transient)."""
    size = os.path.getsize(path)
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(STREAM_CHUNK), b""):
            sha.update(chunk)
    url, headers = presign_put(key, size, sha.hexdigest())

    import httpx

    def _chunks():
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(STREAM_CHUNK), b""):
                yield chunk

    timeout = httpx.Timeout(connect=10, read=600, write=600, pool=10)
    with httpx.Client(follow_redirects=False) as client:
        for attempt in range(FALLBACK_PUT_TRIES):
            try:
                resp = client.put(url, content=_chunks(), headers=dict(headers), timeout=timeout)
                status = resp.status_code
            except (httpx.HTTPError, OSError) as exc:
                status = None
                logger.warning("fallback PUT %s: %s", key, type(exc).__name__)
            if status is not None and 200 <= status < 300:
                return
            if status == 403:
                raise RuntimeError(f"fallback PUT {key} refused (403)")
            if attempt < FALLBACK_PUT_TRIES - 1:
                import time
                time.sleep(FALLBACK_PUT_DELAYS_S[attempt])
    raise RuntimeError(f"fallback PUT {key} failed after {FALLBACK_PUT_TRIES} tries")


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


def head_meta(key: str) -> tuple[int, str | None]:
    """(size, sha256 hex or None) of an object, from one HEAD with ChecksumMode=ENABLED.

    botocore sends ``x-amz-checksum-mode: ENABLED`` and maps the answer's
    ``x-amz-checksum-sha256`` to ``ChecksumSHA256``. A value that is not the base64
    of exactly 32 bytes (absent — an object uploaded without the signed checksum, or a
    CopyObject copy, which G0 measured to carry no SHA-256 — or a multipart
    composite ``…-N``) reads None: the caller then stream-hashes the object (spec §9.8,
    review 3 m8). R2Missing when absent."""
    try:
        resp = _client().head_object(Bucket=bucket(), Key=key, ChecksumMode="ENABLED")
    except Exception as exc:  # botocore ClientError
        if _is_missing(exc):
            raise R2Missing(key) from None
        raise
    return int(resp["ContentLength"]), _decode_sha256(resp.get("ChecksumSHA256"))


def _decode_sha256(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != 32:
        return None
    return raw.hex()


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
