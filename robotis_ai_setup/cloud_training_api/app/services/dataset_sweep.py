"""Periodic HuggingFace → Supabase dataset reconciliation.

Why this service exists: the React app POSTs /datasets right after an
HF upload reports Success so group siblings can see the new dataset
within seconds. If the WSL distro has no internet at exactly that
moment (classroom Wi-Fi blip, brief Cloud API outage), the upload
succeeds on HF but registration never runs. Without this service the
dataset would never be visible to siblings until the student manually
re-triggered something — and there is no UI for that.

How it works:
  1. Every SWEEP_INTERVAL_S, derive the set of HF authors used by our
     students by parsing the `dataset_name` column of the trainings
     table (always stored as "<author>/<repo>"). Map author -> a list
     of (user_id, workgroup_id) candidates so we know who to credit a
     newly-discovered dataset to.
  2. For each author, list every HF dataset they own via
     HfApi.list_datasets(author=...). Compare against the existing
     datasets registry rows.
  3. Insert any missing rows with discovered_via_sweep = TRUE and
     workgroup_id derived from the candidate user's *current*
     users.workgroup_id. Group attribution at sweep time matches the
     behaviour of late manual registrations.
  4. We never DELETE registry rows. If a student deletes the HF repo
     from their HF account, that is their call (they used to own it);
     the orphan registry row simply stops resolving on the React side.

The sweep is single-tenant by design: Cloud API runs uvicorn --workers 1
on Railway, so spawning the task once at startup is correct. If the
worker count is ever raised, switch to a Postgres advisory lock so only
one instance sweeps at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Default cadence: 10 min is enough that "I uploaded 5 min ago" complaints
# resolve themselves without a refresh, and slow enough to keep the HF
# rate-limit budget intact (one list_datasets call per known author).
DEFAULT_SWEEP_INTERVAL_S = 600


def _parse_author(repo_id: str | None) -> str | None:
    """Extract the author from an "author/name" repo id; None if invalid."""
    if not repo_id or "/" not in repo_id:
        return None
    author = repo_id.split("/", 1)[0].strip()
    return author or None


def _gather_author_candidates(supabase) -> dict[str, list[dict[str, Any]]]:
    """Build {hf_author: [{user_id, workgroup_id, full_name}, ...]}.

    We learn an author's identity in two places:
      - trainings.dataset_name and trainings.model_name (any HF repo
        ever used) — gives us the author + the user_id who used it
      - datasets.hf_repo_id (any already-registered repo) — gives us
        the author + the canonical owner_user_id

    A single author can map to multiple users (e.g. a class shares an
    "EduBotics-Solutions" account). We therefore track candidates as a
    list and pick the matching candidate per discovered dataset using
    a follow-up users-table lookup.
    """
    candidates: dict[str, list[dict[str, Any]]] = {}

    def _add(author: str, user_id: str) -> None:
        candidates.setdefault(author, [])
        # Deduplicate by user_id so one user mentioned in 50 trainings
        # doesn't show up 50 times.
        if not any(c.get("user_id") == user_id for c in candidates[author]):
            candidates[author].append({"user_id": user_id})

    try:
        # Page through trainings: author info lives in dataset_name +
        # model_name. We pull a generous slice; on a 1k-training
        # classroom this is well under 1 MB.
        trainings = (
            supabase.table("trainings")
            .select("user_id, dataset_name, model_name")
            .limit(5000)
            .execute()
        ).data or []
        for row in trainings:
            uid = row.get("user_id")
            if not uid:
                continue
            for col in ("dataset_name", "model_name"):
                a = _parse_author(row.get(col))
                if a:
                    _add(a, uid)
    except Exception as exc:
        logger.warning("dataset_sweep: trainings author scan failed: %s", exc)

    try:
        ds_rows = (
            supabase.table("datasets")
            .select("owner_user_id, hf_repo_id")
            .limit(5000)
            .execute()
        ).data or []
        for row in ds_rows:
            uid = row.get("owner_user_id")
            a = _parse_author(row.get("hf_repo_id"))
            if uid and a:
                _add(a, uid)
    except Exception as exc:
        logger.warning("dataset_sweep: datasets author scan failed: %s", exc)

    return candidates


def _enrich_with_workgroup(
    supabase, candidates: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Attach the *current* workgroup_id for each candidate user.

    A late registration inherits the author's current group, matching
    the behaviour of a late manual POST /datasets — documented in the
    plan. If the student moved groups between upload and sweep, the
    new row attributes to the new group; siblings of the old group
    keep visibility on already-registered rows via the audit table.
    """
    # Flatten unique user ids so we can do one IN() lookup.
    uids: set[str] = set()
    for cs in candidates.values():
        for c in cs:
            if c.get("user_id"):
                uids.add(c["user_id"])
    if not uids:
        return candidates

    try:
        users = (
            supabase.table("users")
            .select("id, workgroup_id, full_name, username")
            .in_("id", list(uids))
            .execute()
        ).data or []
        by_id = {u["id"]: u for u in users}
        for cs in candidates.values():
            for c in cs:
                u = by_id.get(c.get("user_id")) or {}
                c["workgroup_id"] = u.get("workgroup_id")
                c["username"] = u.get("username")
                c["full_name"] = u.get("full_name")
    except Exception as exc:
        logger.warning("dataset_sweep: workgroup lookup failed: %s", exc)
    return candidates


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
    """Wrap HfApi.list_datasets so a single bad author doesn't blow up
    the whole tick."""
    try:
        return list(api.list_datasets(author=author, limit=200))
    except Exception as exc:
        logger.warning(
            "dataset_sweep: HfApi.list_datasets(author=%s) failed: %s",
            author, exc,
        )
        return []


def _extract_meta(ds_obj: Any) -> dict[str, Any]:
    """HF SDK returns DatasetInfo objects; the field set varies across
    versions. Pull the basics defensively, never raise.
    """
    out: dict[str, Any] = {}
    repo_id = getattr(ds_obj, "id", None) or getattr(ds_obj, "repo_id", None)
    if repo_id:
        out["hf_repo_id"] = repo_id
        out["name"] = repo_id.split("/", 1)[-1] if "/" in repo_id else repo_id
    return out


def _run_sweep_once() -> int:
    """One iteration. Returns the number of newly-registered rows.

    Bounded work — a missing HF token makes this a no-op so a deploy
    that intentionally turned the sweep off (no HF_TOKEN) doesn't spam
    Railway logs.
    """
    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        logger.info("dataset_sweep: HF_TOKEN not set, skipping tick")
        return 0

    # Imports are deferred so a no-op tick (no HF_TOKEN) doesn't pay
    # the import cost or fail in environments where heavy deps are
    # intentionally absent (e.g. unit tests).
    from huggingface_hub import HfApi  # local import keeps cold start fast

    from app.services.supabase_client import get_supabase

    supabase = get_supabase()
    candidates = _enrich_with_workgroup(supabase, _gather_author_candidates(supabase))
    if not candidates:
        return 0

    api = HfApi(token=hf_token)
    discovered_total = 0

    for author, cands in candidates.items():
        hf_datasets = _list_hf_datasets(api, author)
        if not hf_datasets:
            continue

        # For each candidate user that maps to this author, find any
        # HF datasets not yet in their registry and back-fill them.
        for cand in cands:
            uid = cand.get("user_id")
            if not uid:
                continue
            already = _registered_repo_ids(supabase, uid)
            for ds in hf_datasets:
                meta = _extract_meta(ds)
                repo_id = meta.get("hf_repo_id")
                if not repo_id or repo_id in already:
                    continue
                # Defensive: only attribute repos to a candidate when
                # the author of the repo matches the candidate's known
                # author. (We iterate per-author already, but a future
                # change could share authors across users.)
                if _parse_author(repo_id) != author:
                    continue
                payload = {
                    "owner_user_id": uid,
                    "hf_repo_id": repo_id,
                    "name": meta.get("name", repo_id),
                    "description": "",
                    "workgroup_id": cand.get("workgroup_id"),
                    "discovered_via_sweep": True,
                }
                try:
                    supabase.table("datasets").insert(payload).execute()
                    discovered_total += 1
                    logger.info(
                        "dataset_sweep: Datensatz %s fuer Schueler %s ergaenzt",
                        repo_id, cand.get("username") or uid,
                    )
                except Exception as exc:
                    # UNIQUE (owner_user_id, hf_repo_id) means a
                    # racing live POST got there first — ignore.
                    msg = str(exc).lower()
                    if "duplicate" in msg or "unique" in msg or "23505" in msg:
                        continue
                    logger.warning(
                        "dataset_sweep: insert for %s/%s failed: %s",
                        uid, repo_id, exc,
                    )

    return discovered_total


async def sweep_loop(interval_s: int | None = None) -> None:
    """Async wrapper that invokes the sync sweep on a thread so we
    don't block the FastAPI event loop with HF list_datasets calls."""
    s = interval_s or int(
        os.environ.get("DATASET_SWEEP_INTERVAL_S", DEFAULT_SWEEP_INTERVAL_S)
    )
    logger.info("dataset_sweep: starting loop, interval=%ds", s)
    # First tick: small initial delay so we don't race the import-time
    # health check on a cold deploy.
    await asyncio.sleep(min(60, s))
    while True:
        try:
            new_count = await asyncio.to_thread(_run_sweep_once)
            if new_count:
                logger.info("dataset_sweep: tick added %d row(s)", new_count)
        except Exception as exc:
            # Loop must never die — a failed tick gets logged and we
            # try again next interval.
            logger.error("dataset_sweep: tick raised %s", exc)
        await asyncio.sleep(s)
