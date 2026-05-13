"""Policy types allowed for the current user.

Mirrors the env-driven gate enforced by `POST /trainings/start`. The React
frontend uses this when the ROS service `/training/get_available_policy` is
unreachable — namely cloud-only mode, where `physical_ai_server` is not
started so rosbridge has nothing behind it and the dropdown would otherwise
be permanently empty.

Authoritative source: `app.routes.training.ALLOWED_POLICIES` (driven by the
`ALLOWED_POLICIES` env var). Keeping the cloud response in sync with the
validator means a client that uses this endpoint to populate its dropdown
can never select a policy that `/trainings/start` will then reject.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user
from app.routes.training import ALLOWED_POLICIES, POLICY_MAX_TIMEOUT_HOURS

router = APIRouter(prefix="/policies", tags=["policies"])


class PolicyInfo(BaseModel):
    name: str
    max_timeout_hours: float | None = None


class PolicyListResponse(BaseModel):
    policies: list[PolicyInfo]


@router.get("", response_model=PolicyListResponse)
def list_policies(user=Depends(get_current_user)) -> PolicyListResponse:
    items = [
        PolicyInfo(
            name=name,
            max_timeout_hours=POLICY_MAX_TIMEOUT_HOURS.get(name),
        )
        for name in sorted(ALLOWED_POLICIES)
    ]
    return PolicyListResponse(policies=items)
