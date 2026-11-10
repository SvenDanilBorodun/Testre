# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""One-time Orange Pi login tickets (SA-06, 2026-11-13): the routes.

* ``POST /pi/tickets`` — any signed-in EduBotics account (any role, owner
  decision: any account may use a set-up Pi). Body ``{"pi_id": …}``, read by
  hand (FastAPI's own 422 is English and echoes the input). Answers
  ``{"ticket", "expires_in"}``. The browser calls it straight from the Pi's
  page, whose LAN origin ``ALLOWED_ORIGINS`` cannot list, so this one path
  answers any origin (``edge.AnyOriginBearerCorsMiddleware``): it is
  authenticated by an ``Authorization`` header, which a browser never attaches
  on its own, and reads no cookie, so a foreign page gains nothing.
* ``POST /pi/tickets/redeem`` — the Pi agent, server to server, no
  authentication: the ticket is the secret. Body ``{"ticket", "pi_id"}``.
  Answers ``{"user_id", "role", "session_max_s"}``; anything else, whatever
  went wrong, is ONE generic German 401, so a caller learns nothing about why.

Plain ``def`` handlers (nothing here awaits; app/tests/test_event_loop_not_blocked.py).
Every answer carries ``Cache-Control: no-store``. No handler logs a ticket; the
service holds them only as hashes (app/services/pi_tickets.py, which also says
why a blue-green deploy simply costs the browser one more mint).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.services import pi_tickets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pi", tags=["pi"])


def _no_store(content: dict) -> JSONResponse:
    return JSONResponse(content=content, headers={"Cache-Control": "no-store"})


def _refused() -> HTTPException:
    # One sentence for every refusal (unknown, expired, spent, another Pi's).
    return HTTPException(
        status_code=401,
        detail="Das Anmeldeticket für den Roboter ist ungültig oder abgelaufen.",
    )


@router.post("/tickets")
def mint_pi_ticket(body: Any = Body(default=None), user=Depends(get_current_user)):
    """A one-time ticket for the Pi named in the body, for the JWT-verified
    caller. The access token's ``exp`` travels with it, so the Pi's session
    never outlives the login it came from."""
    pi_id = body.get("pi_id") if isinstance(body, dict) else None
    if not pi_tickets.valid_pi_id(pi_id):
        raise HTTPException(
            status_code=400,
            detail="Die Kennung des Roboters fehlt oder ist ungültig. Bitte die Seite neu laden.",
        )
    profile = user.profile if isinstance(getattr(user, "profile", None), dict) else {}
    role = profile.get("role")
    if role not in pi_tickets.ROLES:
        raise HTTPException(
            status_code=403,
            detail="Mit diesem Konto ist keine Anmeldung am Roboter möglich.",
        )
    claims = user.claims if isinstance(getattr(user, "claims", None), dict) else {}
    ticket = pi_tickets.STORE.mint(
        user_id=str(user.id), role=role, pi_id=pi_id, token_exp=claims.get("exp"),
    )
    return _no_store({"ticket": ticket, "expires_in": pi_tickets.TICKET_TTL_S})


@router.post("/tickets/redeem")
def redeem_pi_ticket(body: Any = Body(default=None)):
    """Spend a ticket at the Pi it was minted for. Single-use: every attempt
    spends it, a right one answers who it belongs to."""
    if not isinstance(body, dict):
        raise _refused()
    result = pi_tickets.STORE.redeem(body.get("ticket"), body.get("pi_id"))
    if result is None:
        raise _refused()
    return _no_store(result)
