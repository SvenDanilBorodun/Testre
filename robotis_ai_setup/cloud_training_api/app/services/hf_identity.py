"""Shared HuggingFace-identity helpers for the dataset discovery layer.

A student's HF username ("Benutzer-ID") is the anchor POST /datasets/sync and
the dataset_sweep use to enumerate that student's HF datasets (migration 030
persists it on public.users.hf_username). Since 2026-11-13 (security audit
SA-08) it is written ONLY by proof — the token save (``store_user_hf_credential``
after the Hub's whoami) — and every discovery and deletion path acts only on the
PROVEN name (``user_hf_credentials.hf_username``); PATCH /me no longer writes it.

The RESERVED namespaces are never a student's: the platform's organisation
(``HF_ORG``, the one constant; ``training_dispatch.HF_ORG`` is this object) and
the platform token's own account, resolved by whoami at boot
(``services/platform_identity.py``) and never from client input. Every gate —
PATCH /me, the token save, dataset registration, sync, the sweep and the
deletion of a student's repos — refuses them; migration 059's triggers are the
database floor. Hugging Face names are case-insensitive ASCII: every comparison
is ``lower()``.
"""

from __future__ import annotations

import re
import threading

# The platform's Hugging Face organisation: every model the managed training
# publishes lives here (routes/training.py's model names, hf_publish, the
# inference allowlist). No student namespace may ever be it.
HF_ORG = "EduBotics-Solutions"

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

# The platform token's own account(s), lowercased: filled at boot by
# services/platform_identity.py (whoami of HF_TOKEN, and the names migration
# 059's table already holds). A set because a rotated token may belong to
# another account; a name once reserved stays reserved for the process.
_platform_accounts: set[str] = set()
_platform_lock = threading.Lock()


def note_platform_account(name: str | None) -> bool:
    """Reserve the platform token's account name. True if it was new."""
    if not isinstance(name, str) or not _HF_USERNAME_RE.match(name.strip()):
        return False
    key = name.strip().lower()
    with _platform_lock:
        if key in _platform_accounts:
            return False
        _platform_accounts.add(key)
        return True


def reserved_namespaces() -> frozenset[str]:
    """The lowercased namespaces no student may claim: the organisation and the
    platform token's account(s) known to this process."""
    with _platform_lock:
        return frozenset({HF_ORG.lower(), *_platform_accounts})


def is_reserved_namespace(name: str | None) -> bool:
    """True for the organisation or the platform token's account (case-insensitive)."""
    return isinstance(name, str) and name.strip().lower() in reserved_namespaces()


def namespace_of(repo_id: str | None) -> str | None:
    """The owner part of an ``owner/name`` repo id, or None."""
    if not isinstance(repo_id, str) or "/" not in repo_id:
        return None
    owner = repo_id.split("/", 1)[0].strip()
    return owner or None


def is_org_repo(repo_id: str | None) -> bool:
    """True when the repo lives in the platform's organisation (case-insensitive)."""
    owner = namespace_of(repo_id)
    return owner is not None and owner.lower() == HF_ORG.lower()


def proven_hf_username(supabase, user_id: str) -> str | None:
    """BLOCKING: the account name Hugging Face reported for the user's own stored
    token (``user_hf_credentials.hf_username``, migration 042), or None when no
    token is stored. The ONE name discovery and deletion act on (SA-08). Raises
    when the table cannot be read: a caller fails closed."""
    rows = (
        supabase.table("user_hf_credentials")
        .select("hf_username")
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
    ).data or []
    name = rows[0].get("hf_username") if rows else None
    return name.strip() if isinstance(name, str) and name.strip() else None


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
    """True if `name` is an author no student may hold, enumerate or register:
    an upstream example namespace, the platform's organisation or the platform
    token's own account (case-insensitive)."""
    if not name or not isinstance(name, str):
        return False
    key = name.strip().lower()
    return key in UPSTREAM_DENY_AUTHORS or key in reserved_namespaces()
