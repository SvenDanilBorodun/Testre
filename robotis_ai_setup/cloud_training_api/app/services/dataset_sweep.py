"""Periodic HuggingFace → Supabase dataset reconciliation.

Why this service exists: the React app registers a dataset right after an HF
upload reports Success (POST /datasets) and on demand (POST /datasets/sync). If
neither runs — a classroom Wi-Fi blip at the wrong instant, a rosbridge
reconnect race, or a student who uploaded outside the app — the dataset would be
invisible in the training tab until something re-triggered registration. This
sweep is the safety net.

How it works (migration 030 rewrite). The OLD design GUESSED an author by
parsing trainings.dataset_name + existing datasets rows and then back-filled
EVERY candidate user that ever touched that author. That is exactly how ~200
upstream `RobotisSW`/`Hotteok` example datasets got fan-registered across every
account (600+ junk rows) and poisoned each student's register_dataset_safe
author anchor so real uploads 403'd. The rewrite removes all author guessing:

  1. Every SWEEP_INTERVAL_S, read the SOLE per-student anchor —
     public.users.hf_username (migration 030). No guessing, no fan-out.
  2. For each student that has an hf_username (and isn't on the upstream
     deny-list), list THAT student's HF datasets via
     HfApi.list_datasets(author=hf_username) and insert any missing registry
     rows (discovered_via_sweep = TRUE), attributed to that student + their
     current workgroup.
  3. We never DELETE registry rows.

Kill-switch: DATASET_SWEEP_ENABLED=0 makes every tick a no-op (an operational
lever — e.g. during a data cleanup — without redeploying different code).

Single-tenant by design: Cloud API runs uvicorn --workers 1 on Railway, so
spawning the task once at startup is correct. If the worker count is ever
raised, switch to a Postgres advisory lock so only one instance sweeps.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.services.hf_identity import is_denied_author

logger = logging.getLogger(__name__)

# Default cadence: 10 min is enough that "I uploaded 5 min ago" resolves
# without a refresh, and slow enough to keep the HF rate-limit budget intact
# (one list_datasets call per student who has linked an hf_username).
DEFAULT_SWEEP_INTERVAL_S = 600


def _sweep_enabled() -> bool:
    """DATASET_SWEEP_ENABLED kill-switch (default ON). Only an explicit falsey
    value turns the sweep off; anything else keeps it running."""
    return os.environ.get("DATASET_SWEEP_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _parse_author(repo_id: str | None) -> str | None:
    """Extract the author from an "author/name" repo id; None if invalid."""
    if not repo_id or "/" not in repo_id:
        return None
    author = repo_id.split("/", 1)[0].strip()
    return author or None


def _students_with_hf_username(supabase) -> list[dict[str, Any]]:
    """Every user that has linked an HF username (migration 030).

    Returns [{id, hf_username, workgroup_id}, ...]. The hf_username is the only
    anchor we trust — no author guessing from trainings/datasets rows. A missing
    column (pre-030 deploy) is caught and treated as "no students" so the sweep
    goes dormant rather than erroring.
    """
    try:
        rows = (
            supabase.table("users")
            .select("id, hf_username, workgroup_id")
            .not_.is_("hf_username", "null")
            .limit(5000)
            .execute()
        ).data or []
    except Exception as exc:
        logger.warning("dataset_sweep: users hf_username scan failed: %s", exc)
        return []
    return [r for r in rows if r.get("id") and r.get("hf_username")]


def _registered_repo_ids(supabase, owner_user_id: str) -> set[str]:
    try:
        rows = (
            supabase.table("datasets")
            .select("hf_repo_id")
            .eq("owner_user_id", owner_user_id)
            .execute()
        ).data or []
        return {r["hf_repo_id"] for r in rows if r.get("hf_repo_id")}
    except Exception as exc:
        logger.warning(
            "dataset_sweep: registered-repo lookup for %s failed: %s",
            owner_user_id, exc,
        )
        return set()


def _list_hf_datasets(api, author: str) -> list[Any]:
    """Wrap HfApi.list_datasets so a single bad author doesn't blow up the
    whole tick."""
    try:
        return list(api.list_datasets(author=author, limit=500))
    except Exception as exc:
        logger.warning(
            "dataset_sweep: HfApi.list_datasets(author=%s) failed: %s",
            author, exc,
        )
        return []


def _extract_repo_id(ds_obj: Any) -> str | None:
    """HF SDK returns DatasetInfo objects; the id field name varies across
    versions. Pull it defensively, never raise."""
    return getattr(ds_obj, "id", None) or getattr(ds_obj, "repo_id", None)


def _run_sweep_once() -> int:
    """One iteration. Returns the number of newly-registered rows.

    No-ops (returning 0) when the kill-switch is off or HF_TOKEN is unset, so a
    deploy that intentionally paused the sweep doesn't spam Railway logs.
    """
    if not _sweep_enabled():
        logger.info(
            "dataset_sweep: disabled via DATASET_SWEEP_ENABLED, skipping tick"
        )
        return 0

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        logger.info("dataset_sweep: HF_TOKEN not set, skipping tick")
        return 0

    # Imports deferred so a no-op tick doesn't pay the import cost or fail in
    # environments where heavy deps are intentionally absent (unit tests).
    from huggingface_hub import HfApi  # local import keeps cold start fast

    from app.services.supabase_client import get_supabase

    supabase = get_supabase()
    students = _students_with_hf_username(supabase)
    if not students:
        return 0

    api = HfApi(token=hf_token)
    discovered_total = 0

    for student in students:
        uid = student["id"]
        author = str(student["hf_username"]).strip()
        if is_denied_author(author):
            # A student's hf_username should never be an upstream/system
            # account; never enumerate one (the deny-list is the belt to the
            # per-user-anchor's suspenders — see migration 030).
            logger.info(
                "dataset_sweep: skipping denied author %s for user %s",
                author, uid,
            )
            continue

        hf_datasets = _list_hf_datasets(api, author)
        if not hf_datasets:
            continue

        already = _registered_repo_ids(supabase, uid)
        for ds in hf_datasets:
            repo_id = _extract_repo_id(ds)
            if not repo_id or repo_id in already:
                continue
            # Only this student's own namespace. list_datasets(author=) should
            # already guarantee it; belt-and-suspenders against an SDK that
            # ever returns cross-namespace results.
            if _parse_author(repo_id) != author:
                continue
            payload = {
                "owner_user_id": uid,
                "hf_repo_id": repo_id,
                "name": repo_id.split("/", 1)[-1] if "/" in repo_id else repo_id,
                "description": "",
                "workgroup_id": student.get("workgroup_id"),
                "discovered_via_sweep": True,
            }
            try:
                supabase.table("datasets").insert(payload).execute()
                discovered_total += 1
                logger.info(
                    "dataset_sweep: registered %s for user %s", repo_id, uid
                )
            except Exception as exc:
                # UNIQUE (owner_user_id, hf_repo_id) means a racing live POST
                # got there first — ignore.
                msg = str(exc).lower()
                if "duplicate" in msg or "unique" in msg or "23505" in msg:
                    continue
                logger.warning(
                    "dataset_sweep: insert for %s/%s failed: %s",
                    uid, repo_id, exc,
                )

    return discovered_total


async def sweep_loop(interval_s: int | None = None) -> None:
    """Async wrapper that invokes the sync sweep on a thread so we don't block
    the FastAPI event loop with HF list_datasets calls."""
    s = interval_s or int(
        os.environ.get("DATASET_SWEEP_INTERVAL_S", DEFAULT_SWEEP_INTERVAL_S)
    )
    logger.info("dataset_sweep: starting loop, interval=%ds", s)
    # First tick: small initial delay so we don't race the import-time health
    # check on a cold deploy.
    await asyncio.sleep(min(60, s))
    while True:
        try:
            new_count = await asyncio.to_thread(_run_sweep_once)
            if new_count:
                logger.info("dataset_sweep: tick added %d row(s)", new_count)
        except Exception as exc:
            # Loop must never die — a failed tick gets logged and we try again
            # next interval.
            logger.error("dataset_sweep: tick raised %s", exc)
        await asyncio.sleep(s)
