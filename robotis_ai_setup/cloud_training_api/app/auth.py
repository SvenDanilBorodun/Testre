import logging

from fastapi import Depends, Header, HTTPException

from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)


async def get_current_user(authorization: str | None = Header(default=None, alias="Authorization")):
    """Validate Supabase JWT and return the authenticated user.

    The actual signature + exp check is delegated to Supabase's auth service via
    supabase.auth.get_user(token); this function only handles the framing,
    fast-fails on malformed input, and ensures the client never sees Supabase
    internals in error messages.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.removeprefix("Bearer ").strip()

    # Cheap structural check: a JWT is three base64url segments separated by dots.
    # Saves a network round-trip when someone sends "Bearer foo".
    if token.count(".") != 2 or not all(token.split(".")):
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        user_response = get_supabase().auth.get_user(token)
    except Exception as e:
        # Network failure, Supabase outage, malformed response — log internally,
        # return a generic 401 to avoid leaking infrastructure details.
        logger.warning("Supabase auth.get_user failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid token")

    user = getattr(user_response, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    return user


def get_user_profile(user_id: str) -> dict:
    """Return the public.users row for a given auth user id."""
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select("id, email, role, username, full_name, classroom_id, training_credits, created_by")
        .eq("id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="User profile not found")
    return result.data


async def get_current_teacher(user=Depends(get_current_user)) -> dict:
    profile = get_user_profile(str(user.id))
    if profile["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Forbidden: teacher role required")
    return profile


async def get_current_admin(user=Depends(get_current_user)) -> dict:
    profile = get_user_profile(str(user.id))
    if profile["role"] != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: admin role required")
    return profile


async def get_current_profile(user=Depends(get_current_user)) -> dict:
    """Return the public.users row for any authenticated user (any role)."""
    return get_user_profile(str(user.id))
