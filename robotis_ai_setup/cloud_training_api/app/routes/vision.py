"""Phase-3 cloud-burst perception endpoint.

Routes a single open-vocabulary detection call to the ``edubotics-vision``
Modal app (OWLv2 on T4, Apache-2.0 model with German-friendly CLIP text
encoder). The Modal app uses ``min_containers=0`` + memory snapshots
for sub-3-second cold starts, so the typical cost is under $0.0001 per
call.

Auth: any logged-in user.
Rate limit: 5/60s/user (in-process limiter mounted in main.py).
Quota: per-user term cap (default 200 calls), enforced via the
       ``users.vision_quota_per_term`` column once present.

The endpoint accepts a base64-encoded JPEG/PNG and a list of German
prompts; returns the model's detections. The actual Modal invocation
goes through ``modal.Function.from_name(..., "OWLv2Detector.detect")``
which preserves the cold-start caching behavior.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import get_current_user
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vision", tags=["vision"])

# Audit R3 / F33: tightened from 200 to 80 so the cloud cap matches the
# frontend slicer (perception.js:OPEN_VOCAB_PROMPT_MAX) and CLAUDE.md
# §6.7. The 200-vs-80 drift let non-React clients (curl, alt SDK)
# submit 200-char prompts that burned Modal time + student quota for
# what the OWLv2 text encoder treats as near-noise anyway.
MAX_PROMPT_CHARS = 80
MAX_PROMPTS = 8
MAX_IMAGE_BYTES = 1_500_000  # ~1.5 MB JPEG/PNG
DEFAULT_SCORE_THRESHOLD = 0.25

VISION_APP_NAME = os.getenv("MODAL_VISION_APP_NAME", "edubotics-vision")
VISION_FUNCTION_NAME = os.getenv("MODAL_VISION_FUNCTION_NAME", "OWLv2Detector.detect")
# Wait this many seconds for a Modal cold start before bailing with a
# 504 so the React UI can show a "noch nicht bereit" toast and let
# the student retry.
MODAL_INVOKE_TIMEOUT_S = float(os.getenv("VISION_MODAL_TIMEOUT_S", "30"))


class DetectRequest(BaseModel):
    image_b64: str = Field(..., description="Base64 image bytes (JPEG/PNG).")
    prompts: list[str] = Field(..., min_length=1, max_length=MAX_PROMPTS)
    score_threshold: float = Field(
        default=DEFAULT_SCORE_THRESHOLD, ge=0.0, le=1.0,
    )

    @field_validator("prompts")
    @classmethod
    def _validate_prompts(cls, value: list[str]) -> list[str]:
        # Strip + dedupe so a caller passing the same prompt 8 times
        # doesn't 8x the OWLv2 text-encoder cost (audit §B8). Also
        # filter control characters that could confuse the Modal
        # serializer.
        seen: list[str] = []
        for p in value:
            if not isinstance(p, str):
                raise ValueError("Prompts müssen Zeichenketten sein.")
            stripped = p.strip().replace("\r", " ").replace("\n", " ")
            if not stripped:
                raise ValueError("Leere Prompts sind nicht erlaubt.")
            if len(stripped) > MAX_PROMPT_CHARS:
                raise ValueError(
                    f"Prompt zu lang (max {MAX_PROMPT_CHARS} Zeichen).",
                )
            if stripped not in seen:
                seen.append(stripped)
        if not seen:
            raise ValueError("Keine gültigen Prompts.")
        if len(seen) > MAX_PROMPTS:
            seen = seen[:MAX_PROMPTS]
        return seen

    @field_validator("image_b64")
    @classmethod
    def _validate_image(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("Bild fehlt.")
        # Strip whitespace before any size accounting — base64 from
        # browser canvas sometimes has line wraps that inflate the
        # length without representing extra bytes.
        cleaned = "".join(value.split())
        if len(cleaned) > MAX_IMAGE_BYTES * 4 // 3 + 32:
            raise ValueError("Bild zu groß.")
        # validate=True rejects non-base64 chars cheaply; combined with
        # the encoded-length cap above, this short-circuits before any
        # large allocation. The endpoint decodes a SECOND time from the
        # cleaned string; we accept the duplicate decode in exchange
        # for clean separation between validation and use (audit §B1/B2).
        try:
            decoded = base64.b64decode(cleaned, validate=True)
        except binascii.Error as e:
            raise ValueError(f"Ungültiges Base64-Bild: {e}")
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError("Bild zu groß.")
        return cleaned


class Detection(BaseModel):
    label: str
    score: float
    # Audit F51: enforce exactly 4 floats so a worker bug shipping
    # [x1, y1, x2] (length 3) doesn't break the React renderer.
    bbox: list[float] = Field(..., min_length=4, max_length=4)


class DetectResponse(BaseModel):
    detections: list[Detection]
    elapsed_ms: int
    cold_start: bool


async def _invoke_modal(image_bytes: bytes, prompts: list[str], score_threshold: float) -> dict:
    """Call the Modal vision app asynchronously so the FastAPI worker
    isn't blocked for the duration of inference. With ``uvicorn
    --workers 1`` (our deploy shape) a synchronous .remote() call
    would stall every other request for the 200ms-3s+ of OWLv2
    inference. Audit §5: ``fn.remote.aio()`` is the documented async
    variant.
    """
    try:
        import modal  # type: ignore
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Cloud-Erkennung ist auf diesem Server nicht verfügbar.",
        )
    try:
        fn = modal.Function.from_name(VISION_APP_NAME, VISION_FUNCTION_NAME)
    except Exception as e:
        # Audit F50: distinguish "Modal app never deployed" (NotFoundError)
        # from "Modal API is currently unreachable" (transient) so the
        # operator sees the actionable message instead of a generic
        # "vorübergehend nicht erreichbar".
        logger.exception("Modal lookup failed")
        not_found_exc = getattr(getattr(modal, "exception", None), "NotFoundError", None)
        if not_found_exc is not None and isinstance(e, not_found_exc):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Cloud-Erkennung ist auf dieser Installation noch nicht "
                    "installiert. Bitte den Lehrer fragen."
                ),
            ) from e
        raise HTTPException(
            status_code=503,
            detail=(
                "Cloud-Erkennung ist vorübergehend nicht erreichbar. "
                "Bitte gleich erneut versuchen."
            ),
        ) from e
    started = time.monotonic()
    try:
        if hasattr(fn, "remote") and hasattr(fn.remote, "aio"):
            coro = fn.remote.aio(image_bytes, prompts, score_threshold)
            result = await asyncio.wait_for(coro, timeout=MODAL_INVOKE_TIMEOUT_S)
        else:
            # Older Modal SDK fallback — drop into a worker thread so
            # the event loop isn't blocked.
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    fn.remote, image_bytes, prompts, score_threshold,
                ),
                timeout=MODAL_INVOKE_TIMEOUT_S,
            )
    except asyncio.TimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=(
                "Cloud-Erkennung antwortet nicht — der Worker wird gestartet. "
                "Bitte gleich erneut versuchen."
            ),
        ) from e
    except Exception as e:
        # Audit F46: full exception detail (incl. Python class name +
        # Modal internal strings) used to leak to the student. Log
        # with exc_info and return a fixed German message.
        logger.exception("Modal invocation failed")
        raise HTTPException(
            status_code=502,
            detail="Cloud-Erkennung ist fehlgeschlagen. Bitte erneut versuchen.",
        ) from e
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {"result": result, "elapsed_ms": elapsed_ms}


@router.post("/detect", response_model=DetectResponse)
async def detect(
    body: DetectRequest,
    user=Depends(get_current_user),
):
    image_bytes = base64.b64decode(body.image_b64, validate=True)
    # `get_current_user` returns the gotrue ``User`` object, not a
    # dict; use attribute access (audit §A1).
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Nicht angemeldet.")
    sb = get_supabase()

    # Quota check is an atomic UPDATE…RETURNING — race-safe versus
    # two concurrent requests both seeing `used=199` against quota=200
    # (audit §D2). The UPDATE only fires when there's room left; the
    # caller short-circuits on zero rows returned.
    #
    # Audit round-3 §C: the previous silent-fallback path (read-modify-
    # write on the users row) was non-atomic and let concurrent calls
    # exceed the quota. We now hard-fail with 503 if the RPC is missing
    # so the operator knows migration 017 needs to land — silent
    # downgrade to a broken path was worse than a clear deploy error.
    def _atomic_quota_consume() -> tuple[bool, int | None]:
        try:
            res = (
                sb.rpc(
                    "consume_vision_quota",
                    {"p_user_id": user_id},
                )
                .execute()
            )
        except Exception as e:
            # Audit F47: previously a bare `except Exception` mapped
            # every postgrest error to 503 "RPC missing". A transient
            # pool exhaustion / row-lock timeout was indistinguishable
            # from a genuine "migration 017 not deployed" outage.
            # Inspect the underlying error code: PGRST202 / 42883
            # specifically signal "function not found".
            code = getattr(e, "code", "") or ""
            message = str(e)
            # Audit H4: parenthesise explicitly so a transient error
            # whose message happens to contain "function" (e.g. "internal
            # function timeout") doesn't false-positive into the
            # "RPC absent" branch and surface a misleading 503.
            rpc_missing = code in ("PGRST202", "42883") or (
                "function" in message.lower()
                and "does not exist" in message.lower()
            )
            if rpc_missing:
                logger.error("consume_vision_quota RPC missing: %s", e)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Cloud-Erkennung ist auf diesem Server noch nicht "
                        "fertig konfiguriert. Bitte den Lehrer fragen."
                    ),
                ) from e
            logger.exception("consume_vision_quota RPC error")
            raise HTTPException(
                status_code=500,
                detail="Datenbankfehler. Bitte erneut versuchen.",
            ) from e
        row = res.data
        if isinstance(row, list) and row:
            row = row[0]
        if not isinstance(row, dict):
            # Audit F52: previously fail-OPEN here meant a schema
            # change at Supabase would let unbounded calls through
            # uncounted. Fail-CLOSED instead — the operator sees the
            # 429 and notices the deploy gap.
            logger.error("consume_vision_quota returned unexpected shape: %r", row)
            return False, None
        allowed = bool(row.get("allowed", True))
        remaining = row.get("remaining")
        return allowed, remaining if isinstance(remaining, int) else None

    def _refund_quota() -> None:
        """Best-effort decrement of vision_used_per_term after a Modal
        failure so a flaky cold-start storm doesn't burn a student's
        whole term budget. Audit round-3 §A. Race-safe via the
        ``refund_vision_quota`` RPC; falls back to a quiet no-op if
        the RPC isn't deployed yet (the cap is still atomic so the
        worst case is a small unrefunded over-charge).
        """
        try:
            sb.rpc("refund_vision_quota", {"p_user_id": user_id}).execute()
        except Exception:
            # Audit F53: bumped from info → warning so a metric scraper
            # picks up the refund failures and a deploy-gap regression
            # (017 not applied yet) is visible without grepping logs.
            logger.warning("refund_vision_quota RPC unavailable; quota not refunded", exc_info=True)

    # Offload the supabase call to a worker thread so the event loop
    # doesn't block during the round trip (uvicorn --workers 1 makes
    # this critical — audit §E1).
    allowed, _remaining = await asyncio.to_thread(_atomic_quota_consume)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Cloud-Erkennungs-Kontingent für dieses Halbjahr erreicht.",
        )

    started = time.monotonic()
    try:
        response = await _invoke_modal(image_bytes, body.prompts, body.score_threshold)
    except HTTPException:
        # Modal call failed (502/504/etc) — give the student their
        # quota back so a flaky network doesn't cost them a call.
        # Audit round-3 §A.
        await asyncio.to_thread(_refund_quota)
        raise
    elapsed_ms = response["elapsed_ms"]
    raw = response["result"] or {}
    # Audit F37: worker can include {"error": ...} on a per-call
    # failure (Bild fehlerhaft, processor crash). The proxy used to
    # drop it silently, leaving React with "no detections" and no
    # signal. Log server-side so the operator has a trail.
    worker_error = raw.get("error") if isinstance(raw, dict) else None
    if worker_error:
        logger.warning("OWLv2 worker error: %s", worker_error)
    raw_dets = raw.get("detections") if isinstance(raw, dict) else raw
    if not isinstance(raw_dets, list):
        raw_dets = []
    cold_start = bool(raw.get("cold_start")) if isinstance(raw, dict) else False

    detections: list[Detection] = []
    for d in raw_dets:
        if not isinstance(d, dict):
            continue
        try:
            detections.append(
                Detection(
                    label=str(d.get("label", "")),
                    score=float(d.get("score", 0.0)),
                    bbox=[float(x) for x in d.get("bbox") or [0, 0, 0, 0]],
                ),
            )
        except (TypeError, ValueError):
            continue

    total_elapsed = int((time.monotonic() - started) * 1000)
    return DetectResponse(
        detections=detections,
        elapsed_ms=total_elapsed if total_elapsed > 0 else elapsed_ms,
        cold_start=cold_start,
    )
