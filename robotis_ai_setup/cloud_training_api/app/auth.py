"""Authentication dependencies of the cloud API.

The access token is verified LOCALLY against the project's signing keys
(``services/jwt_auth.py``, owner decision 2026-11-10), never by a network call
per request; ``AUTH_VERIFY_MODE=remote`` is the one-variable rollback. Every
authenticated request then reads the caller's ``public.users`` row OFF the
event loop: ``auth.users`` → ``public.users`` is ``ON DELETE CASCADE``
(baseline), so a deleted account is refused at once (401) although its token
would verify for up to an hour. The row rides on the returned ``CurrentUser``,
so ``get_current_profile`` / ``_teacher`` / ``_admin`` read it once.

Admin routes additionally require a two-factor session (``aal2``, owner
decision 2026-11-10): an AAL1 admin token is a 403 whose body carries
``"code": "mfa_required"``, which the teacher web answers with its TOTP screen.
"""

import asyncio
import logging
import threading
from collections import OrderedDict

from fastapi import Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from app.services import jwt_auth
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

SESSION_INVALID_DE = "Ungültige oder abgelaufene Sitzung."
ACCOUNT_GONE_DE = "Dieses Konto gibt es nicht mehr. Bitte wende dich an deine Lehrkraft oder den Admin."
AUTH_UNAVAILABLE_DE = (
    "Die Anmeldung kann gerade nicht geprüft werden. Bitte versuche es gleich noch einmal."
)
MFA_REQUIRED_DE = (
    "Für den Admin-Bereich ist die Zwei-Faktor-Anmeldung erforderlich. Bitte bestätige "
    "deine Anmeldung mit dem Code aus deiner Authenticator-App."
)
# A log alert matches this marker (owner decision 2026-11-10): one WARNING per
# admin session, naming the user id only.
ADMIN_SESSION_LOG_MARKER = "EDUBOTICS_ADMIN_SESSION"
_ADMIN_SESSIONS_MAX = 4096

_PROFILE_COLUMNS = (
    "id, email, role, username, full_name, classroom_id, "
    "workgroup_id, training_credits, created_by, "
    # Migration 030: the student's linked HuggingFace username. The
    # boot-time schema probe (main.py::_validate_required_schema)
    # guarantees the column exists before this select runs, so a
    # missing column is a deploy-ordering bug, not a runtime NULL.
    "hf_username"
)


class CodedHTTPException(HTTPException):
    """An HTTPException whose JSON body also carries a machine-readable ``code``
    (main.py registers ``coded_http_exception_handler``)."""

    def __init__(self, status_code: int, detail: str, code: str):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


async def coded_http_exception_handler(request, exc: CodedHTTPException):
    """``{"detail": <German>, "code": <code>}``: a refusal the client must tell
    apart by more than its status (the teacher web's TOTP screen on
    ``mfa_required``, a retry on ``auth_unavailable``)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=getattr(exc, "headers", None),
    )


class CurrentUser:
    """What ``Depends(get_current_user)`` hands a route: ``.id`` (the routes read
    nothing else), the verified token claims and the ``public.users`` row."""

    __slots__ = ("id", "claims", "profile")

    def __init__(self, user_id: str, claims: dict, profile: dict | None):
        self.id = user_id
        self.claims = claims
        self.profile = profile

    @property
    def aal(self) -> str:
        aal = self.claims.get("aal") if isinstance(self.claims, dict) else None
        return aal if isinstance(aal, str) else "aal1"


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich.")
    # RFC 7235 §2.1: auth-scheme is case-insensitive. Some HTTP clients
    # send "bearer <token>" instead of "Bearer <token>"; rejecting those
    # at the gate produced surprising 401s for otherwise-valid tokens.
    # Case-fold the prefix check only, preserving the token's original
    # casing (the JWT body itself is case-sensitive base64url).
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Ungültige Anmeldedaten.")
    token = authorization[len("Bearer "):].strip()
    # Cheap structural check: a JWT is three base64url segments separated by dots.
    if token.count(".") != 2 or not all(token.split(".")):
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DE)
    return token


def _read_profile(user_id: str) -> dict | None:
    """BLOCKING: the ``public.users`` row, or None when there is none."""
    rows = (
        get_supabase().table("users").select(_PROFILE_COLUMNS).eq("id", user_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


async def get_current_user(authorization: str | None = Header(default=None, alias="Authorization")) -> CurrentUser:
    """Verify the access token and confirm the account still exists.

    401 for a missing, malformed, forged, expired or foreign token and for a
    deleted account; 503 (``code: auth_unavailable``) when no signing key can be
    had or the database cannot be asked — a 5xx the SPA retries, never a
    sign-out. Nothing here blocks the event loop."""
    token = _bearer_token(authorization)
    try:
        if jwt_auth.verify_mode() == "remote":
            claims = await asyncio.to_thread(jwt_auth.verify_remote_blocking, token)
        else:
            claims = await jwt_auth.verify(token)
    except jwt_auth.KeysUnavailable:
        logger.error("no signing key cached and the JWKS is unreachable: answering 503")
        raise CodedHTTPException(503, AUTH_UNAVAILABLE_DE, "auth_unavailable") from None
    except jwt_auth.TokenInvalid:
        raise HTTPException(status_code=401, detail=SESSION_INVALID_DE) from None
    user_id = claims["sub"]
    try:
        profile = await asyncio.to_thread(_read_profile, user_id)
    except Exception as exc:  # noqa: BLE001 - a database fault is not a bad token
        logger.warning("profile read for user=%s failed: %s", user_id, type(exc).__name__)
        raise CodedHTTPException(503, AUTH_UNAVAILABLE_DE, "auth_unavailable") from None
    if profile is None:
        logger.info("token of a deleted account refused: user=%s", user_id)
        raise HTTPException(status_code=401, detail=ACCOUNT_GONE_DE)
    return CurrentUser(user_id, claims, profile)


def get_user_profile(user_id: str) -> dict:
    """Return the public.users row for a given auth user id. BLOCKING.

    Includes workgroup_id so callers can route credit/visibility decisions
    without a second roundtrip. Migration 011 adds the column; pre-011
    deployments will simply receive NULL.
    """
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select(_PROFILE_COLUMNS)
        .eq("id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Benutzerprofil nicht gefunden.")
    return result.data


async def _profile_of(user) -> dict:
    profile = getattr(user, "profile", None)
    if isinstance(profile, dict):
        return profile
    return await asyncio.to_thread(get_user_profile, str(user.id))


async def get_current_teacher(user=Depends(get_current_user)) -> dict:
    profile = await _profile_of(user)
    if profile["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Nur für Lehrkräfte erlaubt.")
    return profile


_admin_sessions: "OrderedDict[str, None]" = OrderedDict()
_admin_sessions_lock = threading.Lock()


def _note_admin_session(user) -> None:
    """One WARNING per admin session (the token's ``session_id``) for a log
    alert to match; the user id only, no other personal data."""
    claims = getattr(user, "claims", None) or {}
    session = claims.get("session_id") or f"{user.id}:{claims.get('iat')}"
    with _admin_sessions_lock:
        if session in _admin_sessions:
            _admin_sessions.move_to_end(session)
            return
        _admin_sessions[session] = None
        while len(_admin_sessions) > _ADMIN_SESSIONS_MAX:
            _admin_sessions.popitem(last=False)
    logger.warning("%s user=%s", ADMIN_SESSION_LOG_MARKER, user.id)


async def get_current_admin(user=Depends(get_current_user)) -> dict:
    profile = await _profile_of(user)
    if profile["role"] != "admin":
        raise HTTPException(status_code=403, detail="Nur für Administratoren erlaubt.")
    if getattr(user, "aal", "aal1") != "aal2":
        raise CodedHTTPException(403, MFA_REQUIRED_DE, "mfa_required")
    _note_admin_session(user)
    return profile


async def get_current_profile(user=Depends(get_current_user)) -> dict:
    """Return the public.users row for any authenticated user (any role)."""
    return await _profile_of(user)
