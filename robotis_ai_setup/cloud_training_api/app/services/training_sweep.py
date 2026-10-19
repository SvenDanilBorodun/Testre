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

# The student-facing give-up text of a LEGACY row (a managed row gets S21,
# training_texts_de, which names no provider: FR-22). Shared with
# routes/training.py's synchronous give-up so the student reads one sentence.
LEGACY_CANCEL_GIVEUP_DE = (
    "Abbruch konnte nicht an Cloud-Worker zugestellt werden "
    f"(Modal-Cancel nach {MAX_CANCEL_ATTEMPTS} Versuchen "
    "weiterhin fehlgeschlagen). Credit wurde freigegeben — "
    "bitte Training neu starten."
)

# The legacy active sweep (§4.4): today's reconciler on every legacy row, so a
# legacy training whose worker died is noticed without a student opening the
# training page.
ACTIVE_SWEEP_INTERVAL_S = 60


def _pg_code(exc) -> str | None:
    import re

    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    m = re.search(r"\b(P0\d{3})\b", str(exc))
    return m.group(1) if m else None


def _complete(supabase, training_id, final: str, message) -> str:
    """complete_cancel_training (049): the ONE terminal write of a cancel.
    P0049 = the row is no longer cancel_requested (someone else finished it)."""
    try:
        supabase.rpc(
            "complete_cancel_training",
            {"p_training_id": training_id, "p_final": final, "p_message": message},
        ).execute()
    except Exception as exc:
        if _pg_code(exc) == "P0049":
            logger.info("training_sweep: row %s was already resolved", training_id)
            return "resolved"
        raise
    return final


async def _retry_one(supabase, row: dict) -> str:
    """Retry the cancel of one row. Returns the new status string.

    Returns one of: 'canceled' (sweep success), 'failed' (max attempts
    exhausted), 'cancel_requested' (transient failure, will retry),
    'resolved' (someone else finished the row meanwhile).
    """
    if row.get("managed"):
        return await _retry_one_managed(supabase, row)

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
        status = _complete(supabase, training_id, "canceled", None)
        logger.info(
            "training_sweep: row %s had no cloud_job_id, marked canceled",
            training_id,
        )
        return status

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
            status = _complete(supabase, training_id, "failed", LEGACY_CANCEL_GIVEUP_DE)
            logger.error(
                "training_sweep: training %s exhausted %d cancel attempts, "
                "marked failed (last error: %s)",
                training_id, MAX_CANCEL_ATTEMPTS, exc,
            )
            return status
        logger.warning(
            "training_sweep: cancel retry %d/%d for training %s failed: %s",
            attempts + 1, MAX_CANCEL_ATTEMPTS, training_id, exc,
        )
        return "cancel_requested"

    # Modal cancel succeeded — flip to canceled (credit auto-frees).
    status = _complete(supabase, training_id, "canceled", None)
    logger.info(
        "training_sweep: training %s canceled on Modal retry %d",
        training_id, attempts + 1,
    )
    return status


async def _retry_one_managed(supabase, row: dict) -> str:
    """A managed row (049): cancel_request_training again (it counts the attempt
    and hands back the CURRENT attempt's state, backend and cloud_ref), stop
    that attempt, then complete_cancel_training. Every state change goes
    through 049's functions (§3.1 rule of state)."""
    from app.services import training_dispatch
    from app.services.training_texts_de import S21

    training_id = row.get("id")
    try:
        data = supabase.rpc(
            "cancel_request_training", {"p_training_id": training_id, "p_user_id": row.get("user_id")}
        ).execute().data
    except Exception as exc:
        if _pg_code(exc) in ("P0050", "P0002", "P0051"):
            return "resolved"
        raise
    info = (data or [{}])[0] if isinstance(data, list) else (data or {})
    attempts = int(info.get("o_cancel_attempts") or 0)
    stopped = await training_dispatch.stop_for_cancel(
        info.get("o_backend"), info.get("o_attempt_state"), info.get("o_cloud_ref")
    )
    if stopped:
        return _complete(supabase, training_id, "canceled", None)
    if attempts >= MAX_CANCEL_ATTEMPTS:
        logger.error("training_sweep: managed training %s exhausted %d cancel attempts, marked failed",
                     training_id, MAX_CANCEL_ATTEMPTS)
        return _complete(supabase, training_id, "failed", S21)
    return "cancel_requested"


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
        .select("id, user_id, managed, cloud_job_id, cancel_attempts, cancel_attempted_at, status")
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


async def active_sweep_tick() -> int:
    """§4.4: today's reconciler (routes/training.py::_sync_modal_status, with
    its last_progress_at stall rule) on every LEGACY row that is queued or
    running. Managed rows are the managed sweep's (training_dispatch)."""
    from app.routes.training import _sync_modal_status
    from app.services.supabase_client import get_supabase

    def _read():
        return (
            get_supabase().table("trainings")
            .select("id, status, managed, cloud_job_id, requested_at, last_progress_at, terminated_at, error_message")
            .eq("managed", False)
            .in_("status", ["queued", "running"])
            .limit(200)
            .execute()
        ).data or []

    rows = await asyncio.to_thread(_read)
    for row in rows:
        try:
            await _sync_modal_status(row)
        except Exception as exc:  # noqa: BLE001 - one row never stops the round
            logger.warning("active_sweep: training %s: %s", row.get("id"), type(exc).__name__)
    return len(rows)


async def active_sweep_loop(interval_s: int = ACTIVE_SWEEP_INTERVAL_S) -> None:
    """Forever loop (60 s), spawned at FastAPI startup unless
    TRAINING_ACTIVE_SWEEP_DISABLED=1."""
    logger.info("active_sweep: starting legacy reconcile loop, interval=%ds", interval_s)
    await asyncio.sleep(min(20, interval_s))
    while True:
        try:
            await active_sweep_tick()
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.error("active_sweep: tick raised %s", type(exc).__name__)
        await asyncio.sleep(interval_s)
