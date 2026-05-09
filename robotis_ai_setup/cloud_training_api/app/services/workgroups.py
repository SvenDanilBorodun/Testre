"""Workgroup helpers shared by routes/training.py, routes/me.py, and the
periodic dataset sweep. Lives in services/ so the helper is unit-testable
without importing the route module (which pulls in FastAPI deps).

The single responsibility here is "what workgroups can this user see?"
The audit table (workgroup_memberships) is the source of truth — it keeps
historical visibility for ex-members, which is what the user asked for
in the Arbeitsgruppen spec ("dataset uploads stay visible after a student
leaves the group"). users.workgroup_id alone would lose that visibility.
"""

from __future__ import annotations

import logging

from app.auth import get_user_profile
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def resolve_visible_workgroup_ids(user_id: str) -> list[str]:
    """Every workgroup the user is or was a member of (per audit table).

    Falls back to the user's currently-set workgroup_id when the audit
    table has no rows yet — covers the corner case where a row was
    inserted before migration 011 ran in an integration environment.
    """
    supabase = get_supabase()
    rows = (
        supabase.table("workgroup_memberships")
        .select("workgroup_id")
        .eq("user_id", user_id)
        .execute()
    ).data or []
    if rows:
        return [r["workgroup_id"] for r in rows if r.get("workgroup_id")]
    profile = get_user_profile(user_id)
    wg = profile.get("workgroup_id")
    return [wg] if wg else []
