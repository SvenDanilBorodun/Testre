"""Cloud-Inferenz session sweep (migration 048; spec §4.3).

Every SWEEP_PERIOD_S: the `sweep_inference_sessions` RPC (ends reservations the
robot never used, expired runs, runs whose page is gone and runs whose Modal
container stopped reporting — all on the DB clock), then
`inference_sessions.log_ended_sessions` (the teacher's run rows, only for
sessions the robot connected to). Every exception is logged by type and the loop
continues, like the dataset and Jetson sweeps.

It never waits for Modal: terminating the ended sessions' Modal sessions is
`inference_edge.terminate_loop`'s, a task of its own (F-2), so a Modal control
plane that hangs can never freeze this loop.

Single-tenant on purpose (uvicorn --workers 1 on Railway). Disabled with
INFERENCE_SWEEPER_DISABLED=1 (main.py), together with the terminate loop.
"""

from __future__ import annotations

import asyncio
import logging

from app.services import inference_sessions

logger = logging.getLogger(__name__)

SWEEP_PERIOD_S = 15.0


def _sweep_rpc(sb) -> int:
    data = sb.rpc("sweep_inference_sessions", {}).execute().data
    return data if isinstance(data, int) and not isinstance(data, bool) else 0


async def sweep_loop(period_s: float = SWEEP_PERIOD_S, sb=None, initial_delay_s: float | None = None) -> None:
    """Forever: the sweep RPC, then the run rows, then sleep `period_s`."""
    delay = min(15.0, period_s) if initial_delay_s is None else initial_delay_s
    logger.info("inference_sweep: starting loop, period=%.0f s", period_s)
    if delay > 0:
        # Do not race the boot-time schema probe on a cold deploy.
        await asyncio.sleep(delay)
    while True:
        client = sb
        try:
            if client is None:
                from app.services.supabase_client import get_supabase

                client = get_supabase()
            ended = await asyncio.to_thread(_sweep_rpc, client)
            if ended:
                logger.info("inference_sweep: ended %d session(s)", ended)
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.warning("inference_sweep: sweep RPC failed: %s", type(exc).__name__)
        try:
            if client is not None:
                logged = await asyncio.to_thread(inference_sessions.log_ended_sessions, client)
                if logged:
                    logger.info("inference_sweep: %d cloud run row(s) written", logged)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inference_sweep: run rows failed: %s", type(exc).__name__)
        await asyncio.sleep(period_s)
