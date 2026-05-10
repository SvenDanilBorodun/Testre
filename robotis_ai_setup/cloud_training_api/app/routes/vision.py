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

MAX_PROMPT_CHARS = 200
MAX_PROMPTS = 8
MAX_IMAGE_BYTES = 1_500_000  # ~1.5 MB JPEG/PNG
DEFAULT_SCORE_THRESHOLD = 0.10

VISION_APP_NAME = os.getenv("MODAL_VISION_APP_NAME", "edubotics-vision")
VISION_FUNCTION_NAME = os.getenv("MODAL_VISION_FUNCTION_NAME", "OWLv2Detector.detect")
# Wait this many seconds for a Modal cold start before bailing with a
# 504 so the React UI can show a "noch nicht bereit" toast and let
# the student retry.
MODAL_INVOKE_TIMEOUT_S = float(os.getenv("VISION_MODAL_TIMEOUT_S", "10"))


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
    bbox: list[float]  # [x1, y1, x2, y2]


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
        logger.exception("Modal lookup failed")
        raise HTTPException(
            status_code=503,
            detail=(
                "Cloud-Erkennung ist gerade nicht erreichbar. "
                "Bitte den Lehrer fragen."
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
        logger.exception("Modal invocation failed")
        raise HTTPException(
            status_code=502,
            detail=f"Cloud-Erkennung ist fehlgeschlagen: {e}",
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
    def _atomic_quota_consume() -> tuple[bool, int | None]:
        try:
            res = (
                sb.rpc(
                    "consume_vision_quota",
                    {"p_user_id": user_id},
                )
                .execute()
            )
            row = res.data
            if isinstance(row, list) and row:
                row = row[0]
            if not isinstance(row, dict):
                return True, None
            allowed = bool(row.get("allowed", True))
            remaining = row.get("remaining")
            return allowed, remaining if isinstance(remaining, int) else None
        except Exception:
            # RPC not present (migration 017 pending) — fall through to
            # the per-row read-then-write path so the endpoint still
            # works in older deployments.
            logger.info("consume_vision_quota RPC unavailable; falling back")
        try:
            row = (
                sb.table("users")
                .select("vision_quota_per_term, vision_used_per_term")
                .eq("id", user_id)
                .single()
                .execute()
            )
            data = row.data or {}
        except Exception:
            logger.warning("vision quota lookup failed for user=%s", user_id, exc_info=True)
            return True, None
        quota_v = data.get("vision_quota_per_term")
        used_v = data.get("vision_used_per_term") or 0
        if quota_v is not None and used_v >= quota_v:
            return False, 0
        if quota_v is not None:
            try:
                sb.table("users").update(
                    {"vision_used_per_term": used_v + 1}
                ).eq("id", user_id).execute()
            except Exception:
                logger.warning("vision quota increment failed", exc_info=True)
        remaining = (quota_v - used_v - 1) if quota_v is not None else None
        return True, remaining

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
    response = await _invoke_modal(image_bytes, body.prompts, body.score_threshold)
    elapsed_ms = response["elapsed_ms"]
    raw = response["result"] or {}
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
