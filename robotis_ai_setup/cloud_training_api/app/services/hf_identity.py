"""Shared HuggingFace-identity helpers for the dataset discovery layer.

A student's HF username ("Benutzer-ID") is the SOLE anchor the dataset_sweep
and POST /datasets/sync use to enumerate that student's HF datasets (migration
030 persists it on public.users.hf_username). Three call-sites must agree on
how a self-asserted username is normalised and which authors are off-limits —
routes/me.py (PATCH /me), routes/datasets.py (POST /datasets/sync), and
services/dataset_sweep.py — so the logic lives here once.
"""

from __future__ import annotations

import re

# HF usernames/orgs: 1-96 chars, ASCII alphanumeric + hyphens, no leading or
# trailing hyphen. HF is stricter in places (e.g. no consecutive hyphens at
# signup), but we only need to reject anything that couldn't be a real owner
# namespace or could smuggle a path / wildcard into HfApi.list_datasets.
_HF_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,94}[A-Za-z0-9])?$")

# Lowercased authors the sweep + sync must NEVER enumerate. These are upstream
# example/sample namespaces, not student accounts. The per-user hf_username
# anchor already prevents fan-out, but a student whose hf_username is mistakenly
# set to one of these would otherwise pull hundreds of upstream datasets into
# their registry — the exact incident migration 030 cleans up (RobotisSW had
# ~200 example repos fan-registered across every account).
UPSTREAM_DENY_AUTHORS = frozenset({
    "robotissw",
    "hotteok",
    "lerobot",
    "huggingface",
    "hf-internal-testing",
})


def normalize_hf_username(raw: str | None) -> str | None:
    """Trim + format-validate a self-asserted HF username.

    Returns the cleaned value, or None if it is empty or malformed. Does NOT
    apply the deny-list — callers check is_denied_author() separately so they
    can give a distinct error message for a denied (vs. malformed) name.
    """
    if not raw or not isinstance(raw, str):
        return None
    name = raw.strip()
    if not _HF_USERNAME_RE.match(name):
        return None
    return name


def is_denied_author(name: str | None) -> bool:
    """True if `name` is an upstream / non-student author the sweep + sync must
    skip (case-insensitive)."""
    return bool(name) and name.strip().lower() in UPSTREAM_DENY_AUTHORS
