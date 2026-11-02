"""Stopping a training: the student's „Abbrechen" and the teacher's „Training stoppen" (spec §8.4).

ONE path for both callers (decision 13). ``stop_training`` (migration 052,
compare-and-set under the row lock, §4.9) decides between the two ends BEFORE
either happens:

* ``publish`` / ``publish_final`` — a GPU trained and a ready checkpoint exists:
  the attempt is ended ``stopped`` (or ``succeeded`` for a ready final), the token
  NULLed (the fence) and the row stays ``running``; this module hands it to the
  unchanged publish machinery (``hf_publish.schedule_publish``) and stops the GPU
  (a failed DELETE goes to the reaper's retry set; the ended attempt is no longer
  the attached current attempt of an active row, so the reaper deletes it).
* ``cancel`` — everything else: the row is ``cancel_requested`` and today's managed
  cancel continuation runs (stop the GPU → ``complete_cancel_training(canceled)``,
  or the ``MAX_CANCEL_ATTEMPTS`` give-up with S21). A ``derived`` (pre-052) row keeps
  the legacy Modal ``cancel_training_job`` path.

Hours are never moved here: the 052 settlement trigger charges the started hours
inside the terminal transaction (a publish settles at its ``finish_training``).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from app.services import training_dispatch
from app.services import training_texts_de as texts

logger = logging.getLogger(__name__)


def _sb():
    from app.services.supabase_client import get_supabase

    return get_supabase()


def _read_back(training_id: int) -> dict:
    rows = (
        _sb().table("trainings").select("status, charged_hours").eq("id", int(training_id)).execute()
    ).data or []
    return rows[0] if rows else {}


async def complete_cancel(training_id: int, final: str, message) -> dict:
    """complete_cancel_training; P0049 (another instance, e.g. the cancel sweep, completed
    it first) → read the row back and answer its state (review 3 m11)."""
    try:
        _sb().rpc(
            "complete_cancel_training",
            {"p_training_id": int(training_id), "p_final": final, "p_message": message},
        ).execute()
    except Exception as exc:
        if training_dispatch.pg_code(exc) != "P0049":
            raise
        logger.info("cancel of training %s lost a race; reading the row back", training_id)
    row = _read_back(training_id)
    return {"status": row.get("status") or final, "charged_hours": row.get("charged_hours")}


def _answer(training_id: int, status: str, outcome: str, checkpoint_step=None, charged_hours=None) -> dict:
    return {"status": status, "training_id": int(training_id), "outcome": outcome,
            "checkpoint_step": checkpoint_step, "charged_hours": charged_hours}


async def stop(training_id: int, *, owner: str, actor: str, role: str, note: str) -> dict:
    """§8.4. ``owner`` = the student whose row it is (stop_training selects by id AND user_id,
    so another student's training answers P0002 → the German 404); ``actor`` only reaches
    the log. Answers ``{status, training_id, outcome, checkpoint_step, charged_hours}``."""
    from app.services import hf_publish
    from app.services.training_sweep import LEGACY_CANCEL_GIVEUP_DE, MAX_CANCEL_ATTEMPTS

    logger.info("stop of training %s by %s %s", training_id, role, actor)
    try:
        data = _sb().rpc("stop_training", {
            "p_training_id": int(training_id), "p_user_id": str(owner), "p_actor_id": str(actor),
            "p_actor_role": role, "p_note": note,
        }).execute().data
    except Exception as exc:
        code = training_dispatch.pg_code(exc)
        if code == "P0002":
            raise HTTPException(status_code=404, detail=texts.TRAINING_NOT_FOUND_DE)
        if code == "P0050":
            raise HTTPException(status_code=400, detail=texts.TRAINING_NOT_ACTIVE_DE)
        if code == "P0051":
            raise HTTPException(status_code=409, detail=texts.S9)
        raise
    info = (data or [{}])[0] if isinstance(data, list) else (data or {})
    outcome = info.get("o_outcome")
    if outcome in ("publish", "publish_final"):
        hf_publish.schedule_publish(int(training_id), int(info.get("o_attempt") or 0))
        await training_dispatch.stop_for_cancel(info.get("o_backend"), info.get("o_attempt_state"),
                                                info.get("o_cloud_ref"))
        logger.info("training %s stopped by %s: publishing step %s", training_id, role, info.get("o_step"))
        return _answer(training_id, "running", "publishing", checkpoint_step=info.get("o_step"))

    attempts = int(info.get("o_cancel_attempts") or 0)
    managed = bool(info.get("o_managed"))
    if managed:
        stopped = await training_dispatch.stop_for_cancel(
            info.get("o_backend"), info.get("o_attempt_state"), info.get("o_cloud_ref"))
    elif info.get("o_cloud_job_id"):
        from app.services.modal_client import cancel_training_job

        try:
            await cancel_training_job(info["o_cloud_job_id"])
            stopped = True
        except Exception as exc:  # noqa: BLE001 - stays cancel_requested; the sweep retries
            logger.warning("Modal cancel failed for training %s (attempt %d, sweep will retry): %s",
                           training_id, attempts, type(exc).__name__)
            stopped = False
    else:
        stopped = True  # nothing ever ran on a GPU
    if stopped:
        done = await complete_cancel(training_id, "canceled", None)
        return _answer(training_id, done["status"], done["status"], charged_hours=done["charged_hours"])
    if attempts >= MAX_CANCEL_ATTEMPTS:
        message = texts.S21 if managed else LEGACY_CANCEL_GIVEUP_DE
        logger.error("cancel of training %s exhausted %d attempts, marked failed", training_id, MAX_CANCEL_ATTEMPTS)
        done = await complete_cancel(training_id, "failed", message)
        return _answer(training_id, done["status"], done["status"], charged_hours=done["charged_hours"])
    logger.info("cancel queued for training %s (the sweep retries)", training_id)
    return _answer(training_id, "cancel_requested", "cancel_requested")
