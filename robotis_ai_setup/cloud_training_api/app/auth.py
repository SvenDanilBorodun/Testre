from fastapi import Header, HTTPException

from app.services.supabase_client import get_supabase


async def get_current_user(authorization: str = Header(alias="Authorization")):
    """Validate Supabase JWT and return user info."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.removeprefix("Bearer ")
    supabase = get_supabase()

    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {e}")
