"""Policy types the cloud trains.

Mirrors the gate enforced by `POST /trainings/start`. The React frontend uses
this when the ROS service `/training/get_available_policy` is unreachable —
namely cloud-only mode, where `physical_ai_server` is not started so rosbridge
has nothing behind it and the dropdown would otherwise be permanently empty.

Authoritative source: `app.routes.training.TRAINABLE_POLICIES` — since the
training-hour ledger (migration 052, spec §8.2) exactly `act`, refused again in
`start_training_safe` (P0061). The `ALLOWED_POLICIES` Railway variable is no
longer read.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user
from app.routes.training import POLICY_MAX_TIMEOUT_HOURS, TRAINABLE_POLICIES

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
        for name in sorted(TRAINABLE_POLICIES)
    ]
    return PolicyListResponse(policies=items)
