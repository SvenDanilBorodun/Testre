"""Cloud-Inferenz control plane: /inference/* (migration 048; spec §4.3).

A student runs a trained ACT model on a Modal GPU while the robot stays at
home. Reserve, then arm (D31):

  POST /inference/sessions                 model chosen -> checks, a DB row
                                           `reserved`, a Modal session
                                           (sessions.start), the model prefetched.
                                           The answer carries NO secret.
  POST /inference/sessions/{id}/arm        Start pressed -> a FRESH connect token
                                           (the reservation's dies) + the edge
                                           token: the ONLY answer with secrets,
                                           handed by the page to the robot once.
  POST /inference/sessions/{id}/heartbeat  every 15 s from the page (45 s rule)
  POST /inference/sessions/{id}/end        Stop / pagehide keepalive (no body)
  GET  /inference/usage                    today's Cloud-Zeit

Conventions that are easy to undo by accident:
  * STUDENTS ONLY (owner A-Q4): every route refuses any other role in German.
    Teachers read their students' cloud runs through routes/teacher.py.
  * Rule §4: the service role bypasses RLS. Every session RPC is keyed by the
    JWT's user id (P0002 -> German 404, never 403); a training is visible by
    exactly routes/training.py::get_training's ladder. No route takes a user id.
  * Every handler is `async def` and runs blocking work off the loop
    (asyncio.to_thread, or inference_edge's own pools). Every answer — refusals
    included — carries `Cache-Control: no-store` (`_no_store`).
  * No Modal call before the reservation row exists: a refused caller never
    costs a GPU. Every attach-time end is 503, `superseded` included (G-4), so a
    409 from reserve always means the model itself was refused.
  * Neither token, its hash, the envelope nor a credential object is logged or
    put into an exception text.
  * Every German `detail=` literal is bound at its raise site on purpose:
    german_detail_lint.py reads keyword arguments.
"""

from __future__ import annotations

import asyncio
import functools
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import get_current_profile
from app.services import inference_edge
from app.services import inference_sessions as svc
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inference", tags=["inference"])

_NO_STORE = {"Cache-Control": "no-store"}


class ReserveBody(BaseModel):
    training_id: int = Field(..., ge=1)


def _ok(content: dict) -> JSONResponse:
    return JSONResponse(content=content, headers=dict(_NO_STORE))


def _no_store(handler):
    """Answer every HTTPException as a no-store JSON body, and an unexpected
    exception as a German 500 that names nothing (its TYPE is logged)."""

    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                                headers=dict(_NO_STORE))
        except Exception as exc:  # noqa: BLE001 - never str(exc)
            logger.error("inference route %s failed: %s", handler.__name__, type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={"detail": "Die Cloud-Inferenz ist gerade nicht verfügbar. Bitte versuche es gleich noch einmal."},
                headers=dict(_NO_STORE),
            )

    return wrapper


def _require_student(profile) -> str:
    if not isinstance(profile, dict) or profile.get("role") != "student":
        raise HTTPException(
            status_code=403,
            detail="Die Cloud-Inferenz ist nur für Schülerinnen und Schüler verfügbar.",
        )
    return str(profile["id"])


def _session_id(session_id) -> str:
    if not isinstance(session_id, str) or not svc.SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Cloud-Sitzung nicht gefunden.")
    return session_id


def _session_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Cloud-Sitzung nicht gefunden.")


def _session_unreadable() -> HTTPException:
    return HTTPException(status_code=500, detail="Die Cloud-Sitzung konnte nicht geladen werden.")


def _gpu_not_started() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Die Cloud-GPU konnte gerade nicht gestartet werden. Bitte versuche es gleich noch einmal.",
    )


def _not_created() -> HTTPException:
    return HTTPException(status_code=500, detail="Die Cloud-Sitzung konnte nicht angelegt werden.")


def _model_not_act() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "Dieses Modell kann noch nicht in der Cloud ausgeführt werden – in der Cloud laufen "
            "bisher nur ACT-Modelle."
        ),
    )


def _valid_creds(creds) -> bool:
    msid = getattr(creds, "session_id", None)
    token = getattr(creds, "token", None)
    return (isinstance(msid, str) and svc.MODAL_SESSION_ID_RE.fullmatch(msid) is not None
            and isinstance(token, str) and svc.EDGE_TOKEN_RE.fullmatch(token) is not None)


async def _end_edge_failed(sb, uid: str, session_id: str) -> None:
    """Best effort: end the row `edge_failed` and wake the terminate loop."""
    try:
        await asyncio.to_thread(svc.end_session, sb, uid, session_id, "edge_failed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("inference edge_failed end not stored: %s %s", session_id, type(exc).__name__)
    inference_edge.kick()


# -------------------------------------------------------------------- reserve


@router.post("/sessions")
@_no_store
async def reserve_session(body: ReserveBody, profile=Depends(get_current_profile)):
    uid = _require_student(profile)
    training_id = getattr(body, "training_id", None)
    if not isinstance(training_id, int) or isinstance(training_id, bool) or training_id < 1:
        raise HTTPException(status_code=404, detail="Modell nicht gefunden.")
    row = await asyncio.to_thread(svc.visible_training, profile, training_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Modell nicht gefunden.")
    if row.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="Dieses Training ist noch nicht fertig.")
    if row.get("model_type") != "act":
        raise _model_not_act()
    repo = row.get("model_name")
    if not isinstance(repo, str) or not svc.MODEL_REPO_RE.fullmatch(repo):
        raise HTTPException(status_code=409, detail="Dieses Modell liegt nicht im EduBotics-Modellarchiv.")
    try:
        summary = await asyncio.to_thread(svc.resolve_model, repo)
    except svc.ModelResolveError as exc:
        if exc.code == "unreachable":
            raise HTTPException(
                status_code=502,
                detail="Hugging Face ist gerade nicht erreichbar. Bitte in einer Minute erneut versuchen.",
            ) from None
        if exc.code == "not_act":
            raise _model_not_act() from None
        if exc.code == "invalid":
            raise HTTPException(
                status_code=409, detail="Die Modelldateien sind unvollständig oder beschädigt."
            ) from None
        raise HTTPException(status_code=409, detail="Das Modell wurde bei Hugging Face nicht gefunden.") from None

    sb = get_supabase()
    t0, h0 = svc.new_token()
    try:
        reservation = await asyncio.to_thread(svc.create_reservation, sb, profile, row, summary, h0)
    except svc.DailyCapReached:
        raise HTTPException(
            status_code=429, detail="Deine Cloud-Zeit für heute ist aufgebraucht. Morgen geht es weiter."
        ) from None
    except svc.CloudBusy:
        raise HTTPException(
            status_code=503,
            detail="Die Cloud ist gerade voll ausgelastet. Bitte in ein paar Minuten erneut versuchen.",
        ) from None
    except svc.NotFound:
        raise HTTPException(status_code=404, detail="Modell nicht gefunden.") from None
    except svc.SessionStoreError:
        raise _not_created() from None
    session_id = reservation["session_id"]
    # A superseded reservation the robot never used frees its GPU slot now (D33).
    inference_edge.kick()

    async def on_late(creds) -> None:
        """A start that outlived its bound succeeded after all: attach it to the
        (by then ended) row so the terminate loop ends it, or terminate it now."""
        token = getattr(creds, "token", None)
        if not _valid_creds(creds):
            await inference_edge.terminate_now(token)
            return
        try:
            result = await asyncio.to_thread(
                svc.attach_edge, sb, session_id, creds.session_id, svc.edge_seal(session_id, token))
        except Exception as exc:  # noqa: BLE001
            logger.warning("inference late attach failed: %s %s", session_id, type(exc).__name__)
            await inference_edge.terminate_now(token)
            return
        if result.get("status") in ("missing", "conflict"):
            await inference_edge.terminate_now(token)
            return
        inference_edge.kick()

    try:
        creds = await inference_edge.start_session(on_late)
    except Exception as exc:  # noqa: BLE001 - EdgeTimeout or the SDK's own
        logger.warning("inference start failed: %s session=%s user=%s", type(exc).__name__, session_id, uid)
        await _end_edge_failed(sb, uid, session_id)
        raise _gpu_not_started() from None
    if not _valid_creds(creds):
        logger.warning("inference start: credentials of an unexpected shape, session=%s", session_id)
        await inference_edge.terminate_now(getattr(creds, "token", None))
        await _end_edge_failed(sb, uid, session_id)
        raise _gpu_not_started()
    try:
        envelope = svc.edge_seal(session_id, creds.token)
        attached = await asyncio.to_thread(svc.attach_edge, sb, session_id, creds.session_id, envelope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inference attach failed: %s session=%s", type(exc).__name__, session_id)
        await inference_edge.terminate_now(creds.token)
        await _end_edge_failed(sb, uid, session_id)
        raise _not_created() from None
    status = attached.get("status")
    if status == "reserved":
        inference_edge.spawn(inference_edge.prefetch(creds.token, t0))
        return _ok({
            "session_id": session_id,
            "summary": summary,
            "session_s": reservation.get("session_s"),
            "remaining_today_s": reservation.get("remaining_today_s"),
            "reserve_s": svc.RESERVE_S,
        })
    if status == "ended":
        # Stored on the ended row: the terminate loop ends the Modal session (D39, G-4).
        inference_edge.kick()
        raise _gpu_not_started()
    await inference_edge.terminate_now(creds.token)
    raise _not_created()


# ------------------------------------------------------------------------ arm


@router.post("/sessions/{session_id}/arm")
@_no_store
async def arm_session(session_id: str, profile=Depends(get_current_profile)):
    uid = _require_student(profile)
    sid = _session_id(session_id)
    sb = get_supabase()
    t1, h1 = svc.new_token()
    try:
        armed = await asyncio.to_thread(svc.arm, sb, uid, sid, h1)
    except svc.NotFound:
        raise _session_not_found() from None
    except svc.SessionStoreError:
        raise _session_unreadable() from None
    status = armed.get("status")
    if status == "armed":
        edge_token = svc.edge_open(sid, armed.get("edge_token_enc"))
        if edge_token is None:
            logger.warning("inference arm: edge envelope does not open, session=%s", sid)
            await _end_edge_failed(sb, uid, sid)
            raise HTTPException(
                status_code=503, detail="Die Cloud-Sitzung ist nicht mehr nutzbar. Bitte erneut starten."
            )
        return _ok({"token": t1, "edge_token": edge_token, "session_s": armed.get("session_s")})
    if status == "active":
        raise HTTPException(status_code=409, detail="Dieser Cloud-Lauf läuft bereits.")
    if status == "not_ready":
        raise HTTPException(
            status_code=409, detail="Die Cloud-GPU wird noch vorbereitet. Bitte einen Moment warten."
        )
    if status == "ended":
        raise HTTPException(
            status_code=410,
            detail=svc.END_REASON_DE.get(armed.get("end_reason")) or "Die Reservierung ist abgelaufen.",
        )
    raise _session_unreadable()


# ------------------------------------------------------------------ heartbeat


@router.post("/sessions/{session_id}/heartbeat")
@_no_store
async def heartbeat_session(session_id: str, profile=Depends(get_current_profile)):
    uid = _require_student(profile)
    sid = _session_id(session_id)
    try:
        beat = await asyncio.to_thread(svc.client_heartbeat, get_supabase(), uid, sid)
    except svc.NotFound:
        raise _session_not_found() from None
    except svc.SessionStoreError:
        raise _session_unreadable() from None
    reason = beat.get("end_reason")
    return _ok({
        "status": beat.get("status"),
        "end_reason": reason,
        "end_reason_de": svc.END_REASON_DE.get(reason, "") if reason else "",
        "max_end_in_s": beat.get("max_end_in_s"),
    })


# ------------------------------------------------------------------------ end


@router.post("/sessions/{session_id}/end")
@_no_store
async def end_session(session_id: str, profile=Depends(get_current_profile)):
    uid = _require_student(profile)
    sid = _session_id(session_id)
    try:
        ended = await asyncio.to_thread(svc.end_session, get_supabase(), uid, sid, "user_stop")
    except svc.NotFound:
        raise _session_not_found() from None
    except svc.SessionStoreError:
        raise _session_unreadable() from None
    # A reservation the robot never used is terminated at once; a connected run
    # TERMINATE_AFTER_S later, so the robot hears why first (D33).
    inference_edge.kick()
    return _ok({
        "status": ended.get("status"),
        "end_reason": ended.get("end_reason"),
        "active_seconds": ended.get("active_seconds"),
    })


# ---------------------------------------------------------------------- usage


@router.get("/usage")
@_no_store
async def read_usage(profile=Depends(get_current_profile)):
    uid = _require_student(profile)
    try:
        used = await asyncio.to_thread(svc.usage, get_supabase(), uid)
    except svc.SessionStoreError:
        raise HTTPException(status_code=500, detail="Die Cloud-Zeit konnte nicht geladen werden.") from None
    return _ok({
        "daily_cap_s": svc.DAILY_CAP_S,
        "session_cap_s": svc.SESSION_MAX_S,
        "used_today_s": used.get("used_today_s"),
        "remaining_today_s": used.get("remaining_today_s"),
        "live_session_id": used.get("live_session_id"),
    })
