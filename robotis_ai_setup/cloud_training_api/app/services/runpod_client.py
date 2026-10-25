"""RunPod REST v2, exactly the calls of spec §3.6 (OpenAPI 2.0.0, 2026-10-18)
(the unfiltered data-centre list went with owner E42).

One ``httpx.AsyncClient`` (20 s timeout) for the process, so no call ever
blocks the event loop. Every call returns a ``RunpodResponse`` (status, parsed
JSON body, Retry-After); a transport failure raises ``RunpodNetworkError``.
What an answer MEANS (400 skip, 402 back-off, 429 wait, …) is decided by the
callers in ``training_dispatch``, where the per-tick state lives.

The API key is sent only as the Authorization header and never logged; an
error body is reduced to its problem+json ``detail`` (≤ 300 characters).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.runpod.io"
TIMEOUT_S = 20.0
MIN_CUDA = "12.6"
# EU/EEA, ISO 3166-1 alpha-2 (spec §3.6): the 27 EU members plus IS, LI, NO.
EEA_COUNTRY_CODES = (
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
)
POD_STATUSES = ("PROVISIONING", "STARTING", "RUNNING", "EXITED", "ERROR", "TERMINATED")
GONE_STATUSES = frozenset({"EXITED", "ERROR", "TERMINATED"})

_client = None
_transport = None  # tests inject an httpx.MockTransport


class RunpodNetworkError(Exception):
    """Connect/read timeout, reset, DNS: no HTTP answer at all."""


@dataclass
class RunpodResponse:
    status: int
    body: object
    retry_after: float | None = None

    @property
    def detail(self) -> str:
        if isinstance(self.body, dict):
            d = self.body.get("detail") or self.body.get("title")
            if isinstance(d, str):
                return d[:300]
        return ""


def api_base() -> str:
    return (os.environ.get("RUNPOD_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def configured() -> bool:
    return bool(os.environ.get("RUNPOD_API_KEY"))


def set_transport_for_tests(transport) -> None:
    """Route every call through ``transport`` (an httpx.MockTransport); None resets."""
    global _transport, _client
    _transport = transport
    _client = None


def _get_client():
    global _client
    if _client is None:
        import httpx

        kwargs = {"timeout": httpx.Timeout(TIMEOUT_S), "follow_redirects": False}
        if _transport is not None:
            kwargs["transport"] = _transport
        _client = httpx.AsyncClient(**kwargs)
    return _client


def _retry_after(headers) -> float | None:
    raw = headers.get("retry-after") if headers is not None else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def request(method: str, path: str, *, params=None, json_body=None) -> RunpodResponse:
    key = os.environ.get("RUNPOD_API_KEY") or ""
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        resp = await _get_client().request(method, api_base() + path, params=params, json=json_body,
                                           headers=headers)
    except Exception as exc:  # httpx.TransportError and friends
        raise RunpodNetworkError(type(exc).__name__) from None
    body = None
    if resp.content:
        try:
            body = resp.json()
        except ValueError:
            body = None
    return RunpodResponse(status=resp.status_code, body=body, retry_after=_retry_after(resp.headers))


async def catalog_gpus(cloud: str, *, worldwide: bool = False) -> RunpodResponse:
    """§3.6 call 1: GPU stock for one cloud and CUDA ≥ 12.6, scoped to the EEA unless ``worldwide`` (owner E42)."""
    params = {
        "include": "AVAILABILITY",
        "product": "POD",
        "cloud": cloud,
        "countryCodes": ",".join(EEA_COUNTRY_CODES),
        "minCudaVersion": MIN_CUDA,
    }
    if worldwide:
        del params["countryCodes"]
    return await request("GET", "/v2/catalog/gpus", params=params)


async def datacenters_gdpr() -> RunpodResponse:
    """§3.6 call 2: Secure's allowed data centres (Europe, GDPR-certified)."""
    return await request("GET", "/v2/catalog/datacenters", params={"regions": "EUROPE", "compliance": "GDPR"})


async def create_pod(body: dict) -> RunpodResponse:
    return await request("POST", "/v2/pods", json_body=body)


async def get_pod(pod_id: str) -> RunpodResponse:
    return await request("GET", f"/v2/pods/{pod_id}")


async def list_pods(limit: int = 1000) -> list[dict]:
    """Every pod of the account (follows pagination.nextCursor). Raises
    RunpodNetworkError on a transport failure and RunpodListError on a non-200."""
    pods: list[dict] = []
    cursor = None
    for _ in range(100):  # 100 000 pods: far beyond any account; a loop guard, not a limit
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = await request("GET", "/v2/pods", params=params)
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise RunpodListError(resp.status)
        page = resp.body.get("pods")
        if not isinstance(page, list):
            raise RunpodListError(resp.status)
        pods.extend(p for p in page if isinstance(p, dict))
        pagination = resp.body.get("pagination") or {}
        cursor = pagination.get("nextCursor") if isinstance(pagination, dict) else None
        if not (isinstance(pagination, dict) and pagination.get("hasNextPage") and cursor):
            return pods
    raise RunpodListError(0)


class RunpodListError(Exception):
    def __init__(self, status: int):
        super().__init__(f"pod list failed: status {status}")
        self.status = status


async def delete_pod(pod_id: str) -> RunpodResponse:
    return await request("DELETE", f"/v2/pods/{pod_id}")


async def patch_pod(pod_id: str, body: dict) -> RunpodResponse:
    return await request("PATCH", f"/v2/pods/{pod_id}", json_body=body)
