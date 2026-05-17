"""Classroom Jetson Orin Nano endpoints.

Scope: ONLY the Inference tab in EduBotics connects to the Jetson.
Roboter Studio (Workshop), Calibration, and Recording all stay on the
student PC. The Jetson runs a follower-only ROS stack + a Python WS
proxy on the agent that JWT-verifies each connection against the
classroom_id's currently-claimed owner.

Three call surfaces:
  1. Agent-side (no JWT, agent_token in body):
       POST /jetson/register
       POST /jetson/{id}/agent-heartbeat
  2. Student-side (JWT required, classroom member):
       GET  /classrooms/{id}/jetson
       POST /jetson/{id}/claim
       POST /jetson/{id}/heartbeat
       POST /jetson/{id}/release
  3. Teacher-side (JWT required, role=teacher, classroom-owned):
       POST /teacher/classrooms/{id}/jetson/pair
       POST /teacher/classrooms/{id}/jetson/force-release

The classroom-member endpoints are mounted at /jetson/{id}/... but the
classroom lookup endpoint lives at /classrooms/{id}/jetson so React can
discover the paired Jetson before it knows its id.

Postgres → HTTP error code mapping in this module:
  P0002 → 404  Jetson/Klassenzimmer/Pairing-Code nicht gefunden
  P0011 → 403  Klassenzimmer gehört nicht zu diesem Lehrer
  P0030 → 409  Jetson belegt
  P0031 → 410  Lock verloren
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.auth import get_current_profile, get_current_teacher
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# Two routers: one for student/agent paths under /jetson and /classrooms,
# one for teacher paths under /teacher/classrooms/{id}/jetson. Mounting
# both lets the rate-limit middleware key on the correct prefix
# (/jetson/* is per-JWT-sub via _PER_USER_RATE_LIMIT_PREFIXES; the
# teacher path is per-IP via the existing /teacher/classrooms rule).
router = APIRouter(tags=["jetson"])
teacher_router = APIRouter(prefix="/teacher", tags=["jetson"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class JetsonRegisterRequest(BaseModel):
    # Agent-supplied lan_ip and version — both optional at first boot.
    lan_ip: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)


class JetsonRegisterResponse(BaseModel):
    jetson_id: str
    agent_token: str
    pairing_code: str
    hf_token: str | None = None
    supabase_url: str | None = None
    supabase_jwt_algorithm: str = "RS256"  # advertised so the agent's
                                            # rosbridge_proxy picks JWKS vs
                                            # shared-secret verification. Set
                                            # to "HS256" only if the project
                                            # is on the legacy auth path; the
                                            # agent picks it up at first boot.
    # HS256 path only: the shared symmetric secret that signed every
    # Supabase JWT in this project. Sent to the agent so its rosbridge
    # proxy can verify student WebSocket auth frames without needing a
    # JWKS endpoint. Empty string for RS256 projects (proxy uses JWKS).
    # CRITICAL: this is sensitive — written to /etc/edubotics/jetson.env
    # mode 600, root-only. Never exposed to students. Setup.sh handles
    # the write atomically before starting the agent.
    supabase_jwt_secret: str | None = None


class JetsonAgentHeartbeat(BaseModel):
    agent_token: str
    lan_ip: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)


class JetsonAgentHeartbeatResponse(BaseModel):
    current_owner_user_id: str | None
    # The wipe-on-release lifecycle is driven by the agent observing
    # current_owner_user_id transition UUID → NULL. No flag needed here.


class JetsonInfo(BaseModel):
    """Public-safe view returned to classroom members + teachers.

    Never includes agent_token, pairing_code, or other secrets; the
    serialiser builds this from a `jetsons` row.
    """

    jetson_id: str
    classroom_id: str
    mdns_name: str | None
    lan_ip: str | None
    agent_version: str | None
    last_seen_at: str | None
    online: bool  # derived from last_seen_at < 60s ago
    current_owner_user_id: str | None
    current_owner_username: str | None
    current_owner_full_name: str | None
    claimed_at: str | None


class JetsonPairRequest(BaseModel):
    pairing_code: str = Field(..., min_length=4, max_length=32)


class JetsonPairResponse(BaseModel):
    jetson_id: str
    mdns_name: str


class JetsonAgentReleaseRequest(BaseModel):
    # Agent-authenticated release for local claim-transition failures.
    # No JWT — the agent has no student session; it proves identity via
    # its own per-Jetson agent_token (same shape as agent_heartbeat).
    agent_token: str


class JetsonReleaseBeaconRequest(BaseModel):
    # sendBeacon body for the tab-close release path. The browser's
    # navigator.sendBeacon API cannot set the Authorization header, so
    # the student JWT travels in the body instead. This endpoint is the
    # ONLY one that accepts a token-in-body — do NOT generalise the
    # pattern. The token is the same Supabase JWT the student's other
    # requests use; we revalidate it with supabase.auth.get_user before
    # touching the lock so a forged body can't release someone else's
    # session.
    access_token: str


class JetsonRegenerateCodeResponse(BaseModel):
    jetson_id: str
    pairing_code: str
    pairing_code_expires_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_pairing_code() -> str:
    """6-digit numeric pairing code. secrets.randbelow avoids the
    common-uniform-distribution mistake of `% 1000000`."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _generate_mdns_name(jetson_id: str) -> str:
    short = jetson_id.split("-", 1)[0]
    return f"edubotics-jetson-{short}.local"


def _is_online(last_seen_at: str | None, threshold_s: int = 60) -> bool:
    """Agent heartbeat is every 10s; allow 60s slack before flagging
    offline so a brief reschedule on the Jetson doesn't toggle the UI."""
    if not last_seen_at:
        return False
    from datetime import datetime, timezone

    try:
        # Postgres returns ISO 8601 with TZ — Python's fromisoformat
        # handles it on 3.11+.
        dt = datetime.fromisoformat(last_seen_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc)
    # Reject future timestamps (corrupted clock, manual DB edit). Without
    # this guard a row with `last_seen_at = NOW() + 1 year` reports
    # online=true even though the agent hasn't actually checked in.
    if dt > now:
        return False
    return (now - dt).total_seconds() < threshold_s


def _serialise(row: dict, owner_info: dict | None = None) -> JetsonInfo:
    owner_username = None
    owner_full_name = None
    if row.get("current_owner_user_id") and owner_info:
        owner_username = owner_info.get("username")
        owner_full_name = owner_info.get("full_name")
    return JetsonInfo(
        jetson_id=row["id"],
        classroom_id=row["classroom_id"],
        mdns_name=row.get("mdns_name"),
        lan_ip=row.get("lan_ip"),
        agent_version=row.get("agent_version"),
        last_seen_at=row.get("last_seen_at"),
        online=_is_online(row.get("last_seen_at")),
        current_owner_user_id=row.get("current_owner_user_id"),
        current_owner_username=owner_username,
        current_owner_full_name=owner_full_name,
        claimed_at=row.get("claimed_at"),
    )


def _fetch_classroom_jetson(classroom_id: str) -> dict:
    """Return the single jetsons row paired to this classroom, or None.

    There is one Jetson per classroom in v1 (the schema does not enforce
    this, but the React UX assumes it). If a teacher accidentally pairs
    two devices to the same classroom we return the most-recently-paired
    one (largest updated_at).
    """
    supabase = get_supabase()
    result = (
        supabase.table("jetsons")
        .select("*")
        .eq("classroom_id", classroom_id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]


def _fetch_classroom(classroom_id: str) -> dict:
    supabase = get_supabase()
    result = (
        supabase.table("classrooms")
        .select("id, teacher_id, name")
        .eq("id", classroom_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Klassenzimmer nicht gefunden")
    return result.data[0]


def _assert_classroom_member(user_id: str, classroom_id: str) -> None:
    """Caller is a student/teacher of this classroom OR an admin.

    Note: do NOT use ``.single()`` here. PostgREST returns 406 on a
    zero-row response with `.single()` (which surfaces as a bare 500
    in the route), instead of letting us return the German 403 we
    actually want for the "no profile row" case.
    """
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select("role, classroom_id")
        .eq("id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=403, detail="Profil nicht gefunden")
    me = result.data[0]
    role = me.get("role")
    if role == "admin":
        return
    if me.get("classroom_id") == classroom_id:
        return
    # Teacher of the classroom?
    classroom = _fetch_classroom(classroom_id)
    if role == "teacher" and classroom.get("teacher_id") == user_id:
        return
    raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Klassenzimmer")


def _assert_jetson_owner(user_id: str, jetson_id: str) -> dict:
    """Return the jetsons row IF the caller is the current owner; otherwise raise.

    Used by /jetson/{id}/heartbeat and /jetson/{id}/release. The RPC
    server-side enforces this too, but raising here lets us return 410
    even when the lock was released before the request reached Postgres
    (sweep race).
    """
    supabase = get_supabase()
    result = (
        supabase.table("jetsons")
        .select("*")
        .eq("id", jetson_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Jetson nicht gefunden")
    row = result.data[0]
    if row.get("current_owner_user_id") != user_id:
        raise HTTPException(status_code=410, detail="Lock verloren — bitte erneut verbinden")
    return row


def _map_pg_error(exc: Exception) -> HTTPException:
    """Convert known Postgres error codes from migration 019 to HTTP."""
    msg = str(exc)
    if "P0030" in msg:
        return HTTPException(status_code=409, detail="Jetson ist bereits belegt")
    if "P0031" in msg:
        return HTTPException(status_code=410, detail="Lock verloren — bitte erneut verbinden")
    if "P0011" in msg:
        return HTTPException(status_code=403, detail="Klassenzimmer gehört nicht zu diesem Lehrer")
    if "P0002" in msg:
        return HTTPException(status_code=404, detail="Jetson, Klassenzimmer oder Pairing-Code nicht gefunden")
    if "P0001" in msg:
        return HTTPException(status_code=401, detail="Agent-Token ungültig oder Jetson nicht gefunden")
    return HTTPException(status_code=500, detail="Datenbank-Fehler")


# ---------------------------------------------------------------------------
# Agent-side endpoints
# ---------------------------------------------------------------------------


@router.post("/jetson/register", response_model=JetsonRegisterResponse)
async def jetson_register(req: JetsonRegisterRequest):
    """Agent first-boot bootstrap.

    Creates an unpaired jetsons row with a freshly-minted agent_token
    and a 6-digit pairing_code. The pairing code expires after 30 min
    so a teacher who left the room can't pair a stale code days later.

    Returns the secrets the agent needs to operate: its own token,
    the pairing code (printed to stdout for the teacher to read), and
    the shared read-only HF org token (EDUBOTICS_JETSON_HF_TOKEN env on
    Railway) so the agent can download trained policies on demand.

    Unauthenticated by design — any host on the internet can register,
    but without the pairing code being entered by a teacher into the
    admin UI the registration is never bound to a classroom and the
    jetson is unreachable. Rate-limited via the /jetson prefix rule.
    """
    hf_token = os.environ.get("EDUBOTICS_JETSON_HF_TOKEN", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")
    if not hf_token:
        # No HF token configured on Railway → registration would succeed
        # but the agent could never download a policy. Refuse upfront so
        # the operator notices.
        logger.error("jetson_register: EDUBOTICS_JETSON_HF_TOKEN not set on Railway")
        raise HTTPException(
            status_code=503,
            detail="Jetson-Registrierung nicht verfügbar — Server nicht konfiguriert",
        )

    pairing_code = _generate_pairing_code()
    supabase = get_supabase()
    try:
        # Insert; let Postgres generate id + agent_token via DEFAULT
        # gen_random_uuid(). The 30-min expiry is in Python rather than
        # in the migration so the operator can tune it without a schema
        # change.
        from datetime import datetime, timedelta, timezone

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        payload = {
            "pairing_code": pairing_code,
            "pairing_code_expires_at": expires_at,
            "lan_ip": req.lan_ip,
            "agent_version": req.agent_version,
        }
        result = supabase.table("jetsons").insert(payload).execute()
    except Exception as exc:
        # Pairing-code collision is rare (1 in a million) but possible.
        # The UNIQUE constraint will raise 23505 — retry once.
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            pairing_code = _generate_pairing_code()
            try:
                payload["pairing_code"] = pairing_code
                result = supabase.table("jetsons").insert(payload).execute()
            except Exception as exc2:
                logger.error("jetson_register retry failed: %s", exc2)
                raise HTTPException(status_code=500, detail="Registrierung fehlgeschlagen")
        else:
            logger.error("jetson_register failed: %s", exc)
            raise HTTPException(status_code=500, detail="Registrierung fehlgeschlagen")

    if not result.data:
        # Extremely defensive — supabase-py wraps the PostgREST response,
        # and a happy path always returns the inserted row. If we ever
        # see an empty result.data here it's a server-side bug, not user
        # error; raise a clear 500 so the operator notices in logs.
        logger.error("jetson_register: insert returned no row")
        raise HTTPException(status_code=500, detail="Registrierung fehlgeschlagen")
    row = result.data[0]
    # JWT verification algorithm for the agent's rosbridge proxy.
    # Three supported options:
    #   - ES256: modern Supabase default (asymmetric, public key via JWKS)
    #   - RS256: older asymmetric option (also JWKS-based)
    #   - HS256: legacy symmetric (requires SUPABASE_JWT_SECRET to be set)
    # The proxy uses the same JWKS code path for ES256 and RS256;
    # only HS256 needs the shared-secret plumbing below.
    algorithm = os.environ.get("SUPABASE_JWT_ALGORITHM", "ES256")
    # HS256 path only: forward the symmetric secret so the agent's
    # rosbridge proxy can verify student JWTs. For ES256/RS256 the
    # proxy fetches a JWKS from SUPABASE_URL and this field stays empty.
    # If HS256 is configured but the secret env var is missing, refuse
    # the registration upfront — without the secret the agent would
    # accept the registration but every student WS connection would close
    # 4401 with no clear signal to the operator.
    jwt_secret: str | None = None
    if algorithm == "HS256":
        jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "") or None
        if not jwt_secret:
            logger.error(
                "jetson_register: SUPABASE_JWT_ALGORITHM=HS256 but "
                "SUPABASE_JWT_SECRET unset — refusing to register a Jetson "
                "that would reject every student JWT"
            )
            raise HTTPException(
                status_code=503,
                detail="Jetson-Registrierung nicht verfügbar — Server nicht konfiguriert (JWT-Secret fehlt)",
            )
    return JetsonRegisterResponse(
        jetson_id=row["id"],
        agent_token=row["agent_token"],
        pairing_code=row["pairing_code"],
        hf_token=hf_token,
        supabase_url=supabase_url,
        supabase_jwt_algorithm=algorithm,
        supabase_jwt_secret=jwt_secret,
    )


@router.post(
    "/jetson/{jetson_id}/agent-heartbeat",
    response_model=JetsonAgentHeartbeatResponse,
)
async def jetson_agent_heartbeat(
    jetson_id: str = Path(..., description="UUID returned by /jetson/register"),
    req: JetsonAgentHeartbeat = Body(...),
):
    """Agent 10s heartbeat. Verifies agent_token, updates last_seen_at +
    lan_ip + agent_version, and returns the current owner UUID so the
    agent's rosbridge proxy can refuse non-owner connections.

    Auth: agent_token in body — not Supabase JWT. The agent does not
    have a Supabase session; it has its own per-Jetson UUID token
    provisioned at /jetson/register.
    """
    supabase = get_supabase()
    try:
        result = supabase.rpc(
            "agent_heartbeat_jetson",
            {
                "p_jetson_id": jetson_id,
                "p_agent_token": req.agent_token,
                "p_lan_ip": req.lan_ip or "",
                "p_agent_version": req.agent_version or "",
            },
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    # PostgREST returns the scalar result directly via .data.
    owner_id = result.data if isinstance(result.data, str) else None
    return JetsonAgentHeartbeatResponse(current_owner_user_id=owner_id)


@router.post("/jetson/{jetson_id}/agent-release")
async def jetson_agent_release(
    jetson_id: str = Path(..., description="UUID returned by /jetson/register"),
    req: JetsonAgentReleaseRequest = Body(...),
):
    """Agent-authenticated lock release for local claim-transition failures.

    When the agent's docker-compose up / healthcheck flow fails mid-claim
    (docker daemon hiccup, OOM, image-pull timeout), the agent calls
    this endpoint to release the server-side lock proactively rather
    than waiting 5 min for the sweeper. Without this, a student whose
    claim triggered a local failure would see "Jetson belegt von <ihrer
    Name>" for the full sweeper window with no way to retry — a real
    classroom friction point between consecutive lessons.

    Auth: agent_token in body (same model as agent_heartbeat). A student
    JWT cannot reach this — only the physical Jetson holding the matching
    agent_token.
    """
    supabase = get_supabase()
    try:
        supabase.rpc(
            "agent_release_jetson",
            {"p_jetson_id": jetson_id, "p_agent_token": req.agent_token},
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Student-side endpoints (classroom member auth)
# ---------------------------------------------------------------------------


@router.get(
    "/classrooms/{classroom_id}/jetson",
    response_model=JetsonInfo | None,
)
async def get_classroom_jetson(
    classroom_id: str = Path(...),
    profile=Depends(get_current_profile),
):
    """Return the paired Jetson for this classroom, or 404 if no Jetson
    is paired. Used by React on Inference-tab mount to populate the
    availability chip.
    """
    _assert_classroom_member(profile["id"], classroom_id)
    row = _fetch_classroom_jetson(classroom_id)
    if not row:
        raise HTTPException(status_code=404, detail="Kein Klassen-Jetson in diesem Raum")

    # Enrich with owner display name (single lookup, may be NULL).
    owner_info = None
    owner_id = row.get("current_owner_user_id")
    if owner_id:
        supabase = get_supabase()
        owner_row = (
            supabase.table("users")
            .select("username, full_name")
            .eq("id", owner_id)
            .execute()
        ).data
        if owner_row:
            owner_info = owner_row[0]
    return _serialise(row, owner_info=owner_info)


@router.post("/jetson/{jetson_id}/claim", response_model=JetsonInfo)
async def claim_jetson(
    jetson_id: str = Path(...),
    profile=Depends(get_current_profile),
):
    """Student claims the Jetson. Atomic via claim_jetson RPC; race
    losers get P0030 → 409. First-come, no queue."""
    # Resolve the Jetson's classroom first so we can authorise — the
    # caller's classroom must match (or they must be admin / teacher of
    # the Jetson's classroom).
    supabase = get_supabase()
    row = (
        supabase.table("jetsons")
        .select("classroom_id")
        .eq("id", jetson_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status_code=404, detail="Jetson nicht gefunden")
    classroom_id = row[0].get("classroom_id")
    if not classroom_id:
        raise HTTPException(status_code=409, detail="Jetson ist nicht gepaart")
    _assert_classroom_member(profile["id"], classroom_id)

    try:
        result = supabase.rpc(
            "claim_jetson",
            {"p_jetson_id": jetson_id, "p_user_id": profile["id"]},
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    if not result.data:
        raise HTTPException(status_code=500, detail="Datenbank-Fehler")
    # claim_jetson RPC returns the full row.
    jetson_row = result.data[0] if isinstance(result.data, list) else result.data
    owner_info = {
        "username": profile.get("username"),
        "full_name": profile.get("full_name"),
    }
    return _serialise(jetson_row, owner_info=owner_info)


@router.post("/jetson/{jetson_id}/heartbeat")
async def heartbeat_jetson_endpoint(
    jetson_id: str = Path(...),
    profile=Depends(get_current_profile),
):
    """Student 30s heartbeat. Raises 410 if the lock changed hands
    (e.g. sweeper released after 5 min silence; teacher force-released)."""
    supabase = get_supabase()
    try:
        supabase.rpc(
            "heartbeat_jetson",
            {"p_jetson_id": jetson_id, "p_user_id": profile["id"]},
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    return {"ok": True}


@router.post("/jetson/{jetson_id}/release")
async def release_jetson_endpoint(
    jetson_id: str = Path(...),
    profile=Depends(get_current_profile),
):
    """Explicit disconnect ('Trennen' button). Idempotent — second
    release-when-not-owner is a no-op, not an error, so navigator
    .sendBeacon races against the sweeper don't error out the unload
    handler.
    """
    supabase = get_supabase()
    try:
        supabase.rpc(
            "release_jetson",
            {"p_jetson_id": jetson_id, "p_user_id": profile["id"]},
        ).execute()
    except Exception as exc:
        # release_jetson is intentionally idempotent — should not raise
        # P0030/P0031. Any unexpected error is surfaced as 500 so it
        # gets attention in logs.
        raise _map_pg_error(exc)
    return {"ok": True}


@router.post("/jetson/{jetson_id}/release-beacon")
async def release_jetson_beacon(
    jetson_id: str = Path(...),
    req: JetsonReleaseBeaconRequest = Body(...),
):
    """sendBeacon-friendly release path.

    The browser's navigator.sendBeacon API has two limits that the main
    /jetson/{id}/release endpoint can't satisfy:

      1. It cannot set Authorization headers, so the JWT must travel in
         the body.
      2. The body Content-Type is restricted to a small set
         (text/plain, application/x-www-form-urlencoded, multipart/form-
         data, or application/json via a Blob). The browser silently
         drops the request if the server rejects the content type.

    This endpoint takes the Supabase JWT in the JSON body, revalidates
    it via supabase.auth.get_user (so a forged body can't release
    someone else's lock), and then calls the same release_jetson RPC.
    The endpoint is intentionally narrow: no other route accepts a
    token-in-body. Future maintainers should NOT generalise this
    pattern — Bearer-in-header is the only auth path for everything
    else.

    Idempotent like /release: if the caller no longer owns the lock
    (e.g. sweeper fired first, or teacher force-released), the RPC's
    `WHERE current_owner_user_id = p_user_id` predicate matches zero
    rows and the call quietly succeeds.
    """
    supabase = get_supabase()
    # Validate the token. supabase.auth.get_user does the full signature
    # + exp check; we just frame the result and convert auth failures
    # to a 401 with a German message so the browser's beforeunload
    # logger has a clear cause.
    try:
        user_response = supabase.auth.get_user(req.access_token)
    except Exception as exc:
        logger.warning("release-beacon: supabase.auth.get_user failed: %s", exc)
        raise HTTPException(status_code=401, detail="Token ungültig")
    user = getattr(user_response, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Token ungültig")

    try:
        supabase.rpc(
            "release_jetson",
            {"p_jetson_id": jetson_id, "p_user_id": str(user.id)},
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Teacher-side endpoints
# ---------------------------------------------------------------------------


@teacher_router.post(
    "/classrooms/{classroom_id}/jetson/pair",
    response_model=JetsonPairResponse,
)
async def pair_jetson_endpoint(
    classroom_id: str = Path(...),
    req: JetsonPairRequest = Body(...),
    teacher=Depends(get_current_teacher),
):
    """Teacher enters the 6-digit pairing code from the Jetson's
    stdout. Binds the registered Jetson to this classroom and assigns
    its mDNS name."""
    supabase = get_supabase()
    # We can't generate the mdns_name until we know the jetson_id, but
    # the RPC expects it as an input. Approach: insert a placeholder,
    # call the RPC, then UPDATE the mdns_name once the row is returned.
    # Simpler: pass an empty string and let the SECURITY DEFINER RPC
    # generate it from id — but that requires changing the migration.
    # Cleanest v1: pass a deterministic placeholder, then UPDATE.
    placeholder = "edubotics-jetson-pending.local"
    try:
        result = supabase.rpc(
            "pair_jetson",
            {
                "p_classroom_id": classroom_id,
                "p_pairing_code": req.pairing_code.strip(),
                "p_teacher_id": teacher["id"],
                "p_mdns_name": placeholder,
            },
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    jetson_id = result.data if isinstance(result.data, str) else None
    if not jetson_id:
        raise HTTPException(status_code=500, detail="Pairing fehlgeschlagen")

    # Replace placeholder mdns_name with the real one derived from the
    # generated UUID. The RPC wrote a placeholder ("edubotics-jetson-
    # pending.local") because it doesn't know the new UUID at insert
    # time; we patch it here.
    #
    # CRITICAL: this UPDATE MUST succeed for the response to be
    # honest. If it fails and we return the real mdns_name to the
    # teacher, the row still contains the placeholder, and every
    # subsequent `GET /classrooms/{id}/jetson` returns the placeholder
    # — students try to resolve `edubotics-jetson-pending.local` and
    # get NXDOMAIN. Better to raise 500 here so the teacher retries.
    mdns = _generate_mdns_name(jetson_id)
    try:
        supabase.table("jetsons").update({"mdns_name": mdns}).eq("id", jetson_id).execute()
    except Exception as exc:
        logger.error("pair_jetson: mdns_name update failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Jetson gepaart, aber mDNS-Name konnte nicht gesetzt werden. Bitte Pairing wiederholen.",
        )

    return JetsonPairResponse(jetson_id=jetson_id, mdns_name=mdns)


@teacher_router.post("/classrooms/{classroom_id}/jetson/force-release")
async def force_release_jetson_endpoint(
    classroom_id: str = Path(...),
    teacher=Depends(get_current_teacher),
):
    """Teacher emergency unlock. Used when a student walked out without
    clicking Trennen and 5-min sweeper hasn't fired yet (e.g. between
    consecutive class periods)."""
    row = _fetch_classroom_jetson(classroom_id)
    if not row:
        raise HTTPException(status_code=404, detail="Kein Klassen-Jetson in diesem Raum")
    supabase = get_supabase()
    try:
        supabase.rpc(
            "force_release_jetson",
            {"p_jetson_id": row["id"], "p_teacher_id": teacher["id"]},
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    return {"ok": True}


@teacher_router.post(
    "/classrooms/{classroom_id}/jetson/regenerate-code",
    response_model=JetsonRegenerateCodeResponse,
)
async def regenerate_jetson_pairing_code(
    classroom_id: str = Path(...),
    teacher=Depends(get_current_teacher),
):
    """Generate a fresh 6-digit pairing code for the classroom's Jetson.

    Used when the prior code expired (30-min lifetime) or the teacher
    wants to rotate it. Avoids the SSH-back-to-Jetson workflow that
    would otherwise be required to re-run setup.sh. Cryptographic
    randomness comes from Python's `secrets.randbelow` (same helper
    used at first registration); the migration 020 RPC just refreshes
    the row atomically with a new 30-min expiry.

    Returns the new code so the React UI can display it in a modal /
    toast. The teacher then enters it in their other browser tab (or
    on the Jetson if re-pairing a previously unpaired device).
    """
    row = _fetch_classroom_jetson(classroom_id)
    if not row:
        raise HTTPException(status_code=404, detail="Kein Klassen-Jetson in diesem Raum")

    new_code = _generate_pairing_code()
    from datetime import datetime, timedelta, timezone

    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()

    supabase = get_supabase()
    try:
        supabase.rpc(
            "regenerate_pairing_code",
            {
                "p_jetson_id": row["id"],
                "p_teacher_id": teacher["id"],
                "p_new_code": new_code,
                "p_expires_at": expires_at,
            },
        ).execute()
    except Exception as exc:
        # The UNIQUE constraint on pairing_code could theoretically
        # collide here (1-in-a-million). Retry once with a new code
        # — same retry pattern as /jetson/register.
        msg = str(exc)
        if "duplicate" in msg.lower() or "23505" in msg:
            new_code = _generate_pairing_code()
            try:
                supabase.rpc(
                    "regenerate_pairing_code",
                    {
                        "p_jetson_id": row["id"],
                        "p_teacher_id": teacher["id"],
                        "p_new_code": new_code,
                        "p_expires_at": expires_at,
                    },
                ).execute()
            except Exception as exc2:
                logger.error("regenerate_pairing_code retry failed: %s", exc2)
                raise _map_pg_error(exc2)
        else:
            raise _map_pg_error(exc)

    return JetsonRegenerateCodeResponse(
        jetson_id=row["id"],
        pairing_code=new_code,
        pairing_code_expires_at=expires_at,
    )


@teacher_router.post("/classrooms/{classroom_id}/jetson/unpair")
async def unpair_jetson_endpoint(
    classroom_id: str = Path(...),
    teacher=Depends(get_current_teacher),
):
    """Unbind the Jetson from this classroom.

    Side effects in one transaction (see migration 020):
      * classroom_id              → NULL
      * current_owner_user_id     → NULL  (force-releases any active session)
      * current_owner_heartbeat_at → NULL
      * claimed_at                → NULL
      * mdns_name                 → NULL  (the next pair will set a fresh one)

    The agent_token is preserved so the Jetson hardware can be paired
    to another classroom without re-running setup.sh. After unpair the
    operator can either:
      * pair the same device to a different classroom (the next
        teacher needs the device's pairing_code, which they can
        regenerate via /regenerate-code only AFTER they pair — chicken
        and egg, so in practice the operator re-runs setup.sh and the
        agent generates a fresh code via /jetson/register), OR
      * decommission the device (just leave it unpaired; the orphan
        row will be cleaned up by an admin).
    """
    row = _fetch_classroom_jetson(classroom_id)
    if not row:
        raise HTTPException(status_code=404, detail="Kein Klassen-Jetson in diesem Raum")
    supabase = get_supabase()
    try:
        supabase.rpc(
            "unpair_jetson",
            {"p_jetson_id": row["id"], "p_teacher_id": teacher["id"]},
        ).execute()
    except Exception as exc:
        raise _map_pg_error(exc)
    return {"ok": True}
