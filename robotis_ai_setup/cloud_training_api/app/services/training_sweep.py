"""Background cancel-retry sweep (migration 023).

Companion to /trainings/cancel. When the synchronous Modal cancel raises
(transient Modal API failure, network blip, throttling), /trainings/cancel
leaves the row in status='cancel_requested' rather than pretending the
cancel succeeded. This loop retries the Modal cancel every
SWEEP_INTERVAL_S until either:

  * Modal cancel succeeds → flip status to 'canceled' (credit auto-frees
    because get_remaining_credits counts only failed/canceled as freed).
  * 5 attempts have been made → flip status to 'failed' so the credit
    IS released. The GPU container most likely already ran to its
    timeout_hours cap or self-terminated; a stuck row is worse than
    a one-credit loss.

Single-tenant by design: Cloud API runs uvicorn --workers 1, so spawning
this loop once at startup is correct. Scale-out would require either
Redis-backed locking or a Postgres advisory lock so only one worker
sweeps at a time.

Does NOT modify credit accounting directly. The credit pool is derived
from `trainings.status NOT IN ('failed', 'canceled')` by the
get_remaining_credits and the workgroup credit RPCs. Flipping a row's
status from cancel_requested → canceled (or failed) is sufficient to
free the slot — start_training_safe / get_remaining_credits /
adjust_workgroup_credits are untouched.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 30 s default: tight enough that a student waiting for their cancel
# doesn't sit on the spinner for minutes, loose enough that we don't
# hammer Modal's cancel API. Override via env for ops.
DEFAULT_SWEEP_INTERVAL_S = 30

# After this many failed Modal cancel attempts, give up and flip the row
# to 'failed' so the credit is released. The GPU container has almost
# certainly exited by now (either by hitting its own timeout_hours or
# because Modal eventually reaped it).
MAX_CANCEL_ATTEMPTS = 5

# Process at most this many cancel_requested rows per tick. Stops one
# slow Modal cancel from blocking the entire sweep loop on a busy
# classroom. The partial index idx_trainings_cancel_requested keeps the
# query O(actively-pending) so even a 100-row backlog finishes inside
# a few ticks.
BATCH_LIMIT = 20


async def _retry_one(supabase, row: dict) -> str:
    """Retry Modal cancel for one row. Returns the new status string.

    Returns one of: 'canceled' (sweep success), 'failed' (max attempts
    exhausted), 'cancel_requested' (transient failure, will retry).
    """
    # Local import keeps the module-load graph clean and lets unit tests
    # stub modal_client without touching this module.
    from app.services.modal_client import cancel_training_job

    training_id = row.get("id")
    cloud_job_id = row.get("cloud_job_id")
    attempts = int(row.get("cancel_attempts") or 0)

    if not cloud_job_id:
        # Row was cancel_requested but never got a cloud_job_id — the
        # /start dispatch must have failed before recording it. There
        # is nothing on Modal to cancel; close out as canceled directly.
        supabase.table("trainings").update(
            {
                "status": "canceled",
                "terminated_at": datetime.now(timezone.utc).isoformat(),
                "worker_token": None,
            }
        ).eq("id", training_id).execute()
        logger.info(
            "training_sweep: row %s had no cloud_job_id, marked canceled",
            training_id,
        )
        return "canceled"

    now_iso = datetime.now(timezone.utc).isoformat()
    # Optimistically record the attempt BEFORE talking to Modal — if
    # the sweep crashes mid-call, the next tick can still see the bump.
    supabase.table("trainings").update(
        {
            "cancel_attempts": attempts + 1,
            "cancel_attempted_at": now_iso,
        }
    ).eq("id", training_id).execute()

    try:
        await cancel_training_job(cloud_job_id)
    except Exception as exc:
        if attempts + 1 >= MAX_CANCEL_ATTEMPTS:
            # Give up: flip to failed so the credit is released. The
            # GPU has almost certainly already exited; a stuck
            # cancel_requested row blocks the student forever.
            supabase.table("trainings").update(
                {
                    "status": "failed",
                    "terminated_at": now_iso,
                    "worker_token": None,
                    "error_message": (
                        "Abbruch konnte nicht an Cloud-Worker zugestellt werden "
                        f"(Modal-Cancel nach {MAX_CANCEL_ATTEMPTS} Versuchen "
                        "weiterhin fehlgeschlagen). Credit wurde freigegeben — "
                        "bitte Training neu starten."
                    ),
                }
            ).eq("id", training_id).execute()
            logger.error(
                "training_sweep: training %s exhausted %d cancel attempts, "
                "marked failed (last error: %s)",
                training_id, MAX_CANCEL_ATTEMPTS, exc,
            )
            return "failed"
        logger.warning(
            "training_sweep: cancel retry %d/%d for training %s failed: %s",
            attempts + 1, MAX_CANCEL_ATTEMPTS, training_id, exc,
        )
        return "cancel_requested"

    # Modal cancel succeeded — flip to canceled (credit auto-frees).
    supabase.table("trainings").update(
        {
            "status": "canceled",
            "terminated_at": datetime.now(timezone.utc).isoformat(),
            "worker_token": None,
        }
    ).eq("id", training_id).execute()
    logger.info(
        "training_sweep: training %s canceled on Modal retry %d",
        training_id, attempts + 1,
    )
    return "canceled"


async def _tick() -> int:
    """One iteration of the sweep. Returns the number of rows processed."""
    # Local imports keep the module importable without the supabase
    # client at unit-test time.
    from app.services.supabase_client import get_supabase

    supabase = get_supabase()
    # No upper bound on cancel_attempts. The cap is enforced by _retry_one,
    # which flips any row whose attempt has reached MAX_CANCEL_ATTEMPTS to
    # 'failed' (terminal, credit freed). Bounding the query by
    # `cancel_attempts < MAX` instead used to STRAND any row that reached
    # the cap WITHOUT being flipped terminal — e.g. a student spam-clicking
    # Abbrechen drove cancel_attempts past the cap via the route (which has
    # no cap), or this sweep crashed right after its optimistic pre-write
    # bumped the count to the cap. Such a row sat in cancel_requested
    # forever and never freed its credit. Selecting every cancel_requested
    # row lets _retry_one resolve any of them on the next tick.
    rows = (
        supabase.table("trainings")
        .select("id, cloud_job_id, cancel_attempts, cancel_attempted_at, status")
        .eq("status", "cancel_requested")
        .order("cancel_attempted_at", desc=False)
        .limit(BATCH_LIMIT)
        .execute()
    ).data or []

    if not rows:
        return 0

    # Process sequentially. Modal's cancel API is fast (<1 s) and we
    # don't want to fan out 20 concurrent cancels against a rate-limited
    # Modal account when the failure mode that triggered the sweep was
    # most likely a Modal rate-limit in the first place.
    for row in rows:
        try:
            await _retry_one(supabase, row)
        except Exception as exc:
            # _retry_one is supposed to swallow its own exceptions, but
            # if anything propagates we log and continue — the loop
            # must never die.
            logger.error(
                "training_sweep: _retry_one raised for row %s: %s",
                row.get("id"), exc,
            )
    return len(rows)


async def sweep_loop(interval_s: int | None = None) -> None:
    """Forever loop. Spawned as a task at FastAPI startup."""
    s = interval_s or int(
        os.environ.get("TRAINING_CANCEL_SWEEP_INTERVAL_S", DEFAULT_SWEEP_INTERVAL_S)
    )
    logger.info("training_sweep: starting cancel-retry loop, interval=%ds", s)
    # Small initial delay so we don't race the boot-time schema probe.
    await asyncio.sleep(min(15, s))
    while True:
        try:
            processed = await _tick()
            if processed:
                logger.info("training_sweep: tick processed %d row(s)", processed)
        except Exception as exc:
            # Loop must never die — any failure gets logged and we try
            # again next interval.
            logger.error("training_sweep: tick raised %s", exc)
        await asyncio.sleep(s)
