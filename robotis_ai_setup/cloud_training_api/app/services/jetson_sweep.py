"""Periodic Jetson-lock sweeper.

When a student's React app stops heartbeating (browser crash, machine
reboot, tab kept open after walking away with the laptop closed) the
lock would otherwise stay held forever. Migration 019 ships an RPC
``sweep_jetson_locks()`` that clears any lock whose
``current_owner_heartbeat_at`` is older than 5 minutes; this service
calls that RPC every 60s.

The Jetson agent observes the resulting NULL ``current_owner_user_id``
in its own 10s ``agent-heartbeat`` response and triggers the wipe
lifecycle (docker compose down + volume rm + recreate). So this loop
plus the agent's transition watcher together form the abandonment
recovery story.

Single-tenant on purpose: Cloud API runs uvicorn --workers 1 on
Railway. If we ever scale out, swap to a Postgres advisory lock so
only one instance sweeps at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_S = 60


def _run_sweep_once() -> int:
    """One iteration. Returns the count of released locks."""
    # Local import keeps the module-load graph clean for unit tests that
    # stub supabase via sys.modules.
    from app.services.supabase_client import get_supabase

    supabase = get_supabase()
    if supabase is None:
        # Test-stub bleed-through: a previous test left
        # `app.services.supabase_client.get_supabase = lambda: None` in
        # place and forgot the context manager that restores it. Without
        # this guard the `.rpc()` call would AttributeError and the
        # blanket except below would silently return 0 — masquerading
        # as a successful empty sweep. Fail loud instead.
        logger.error("jetson_sweep: get_supabase() returned None — refusing to sweep")
        return 0
    try:
        result = supabase.rpc("sweep_jetson_locks", {}).execute()
    except Exception as exc:
        logger.warning("jetson_sweep: sweep_jetson_locks RPC failed: %s", exc)
        return 0
    # The RPC returns a scalar INTEGER; supabase-py wraps it as .data.
    if result.data is None:
        return 0
    if not isinstance(result.data, int):
        logger.warning(
            "jetson_sweep: unexpected RPC return shape %r — treating as 0",
            type(result.data).__name__,
        )
        return 0
    return result.data


async def sweep_loop(interval_s: int | None = None) -> None:
    """Async wrapper that invokes the sync sweep on a thread."""
    s = interval_s or int(
        os.environ.get("JETSON_SWEEP_INTERVAL_S", DEFAULT_SWEEP_INTERVAL_S)
    )
    logger.info("jetson_sweep: starting loop, interval=%ds", s)
    # Small initial delay so we don't race import-time schema probes
    # on cold deploys.
    await asyncio.sleep(min(15, s))
    while True:
        try:
            released = await asyncio.to_thread(_run_sweep_once)
            if released:
                logger.info("jetson_sweep: released %d expired lock(s)", released)
        except Exception as exc:
            # Loop must never die — a failed tick gets logged and we
            # try again next interval.
            logger.error("jetson_sweep: tick raised %s", exc)
        await asyncio.sleep(s)
